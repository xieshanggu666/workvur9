from __future__ import annotations

import copy
import json
import random
import uuid

from . import db
from . import enemies as enemies_mod
from . import mapgen
from . import rewards as rewards_mod
from . import forging as forging_mod
from . import shop as shop_mod
from . import rules
from .cards import all_cards, get_card
from .engine import Battle, _statuses_public
from .forging import FORGE_COST, effective_card

# 初始牌组：卡牌 id 列表；建局时展开为独立实例（同名卡各持一份成长状态）
START_DECK = ["strike", "strike", "strike", "strike", "guard", "guard", "guard"]
INIT_LOCKED = ["heavy_blow", "cleave", "shield_bash", "pommel", "battle_trance",
               "flex", "iron_wave", "flurry", "adrenaline", "swift", "blood_echo",
               "reckless", "demon_form", "sword_dance"]


class DuplicateReward(Exception):
    pass


class InvalidAction(Exception):
    pass


class ShopSoldOut(Exception):
    """商店货架项已售出（重复购买/重复移除同一卡牌实例）。"""
    pass


def _make_instances(ids):
    """把卡牌 id 列表展开为 (uid 列表, 实例表, 下一序号)。"""
    uids, instances = [], {}
    for cid in ids:
        uid = f"c{len(uids) + 1}"
        uids.append(uid)
        instances[uid] = {"id": cid, "forges": []}
    return uids, instances


def _new_run_state(seed):
    deck_uids, instances = _make_instances(START_DECK)
    return {
        "seed": seed,
        "status": "in_progress",
        "position": "start",
        "max_health": 75,
        "health": 75,
        "base_energy": 3,
        "deck": deck_uids,             # 手牌引用（uid）
        "card_instances": instances,  # uid -> {id, forges:[分支id]}
        "next_card_seq": len(deck_uids) + 1,  # uid 单调发号器
        "gold": 0,
        "relics": {},
        "in_battle": False,
        "battle_index": 0,
        "battle": None,
        "reward_options": [],
        "reward_claimed": True,
        "forge_claimed": True,        # 当前节点是否已完成锻造
        "shop": None,                 # 商店节点库存与交易记录（离开节点即清空）
        "events_log": [],
        "truncated": False,
    }


def _migrate_state(run):
    """旧档兼容：把裸 id 牌组升级为卡牌实例（同名卡获得独立 uid 与成长状态）。

    旧档可能在战斗中：牌堆/手牌/弃牌堆里仍是裸 id，此时按牌组顺序发号 uid，
    再把牌堆中的每次出现映射到该 id 的 uid 队列，洗牌布局与确定性保持不变。
    """
    changed = False
    if "card_instances" not in run:
        instances = {}
        deck_uids = []
        free_uids = {}  # card_id -> 尚未分配到牌堆的 uid 队列
        for cid in run.get("deck", []):
            uid = f"c{len(instances) + 1}"
            instances[uid] = {"id": cid, "forges": []}
            deck_uids.append(uid)
            free_uids.setdefault(cid, []).append(uid)

        b = run.get("battle")
        if b:
            for pile in ("draw_pile", "hand", "discard"):
                mapped = []
                for ref in b.get(pile, []):
                    if not isinstance(ref, str) or ref not in free_uids:
                        mapped.append(ref)
                        continue
                    queue = free_uids[ref]
                    mapped.append(queue.pop(0) if queue else ref)
                b[pile] = mapped
            b["card_instances"] = instances
        run["deck"] = deck_uids
        run["card_instances"] = instances
        run["next_card_seq"] = len(instances) + 1
        changed = True
    run.setdefault("forge_claimed", True)
    run.setdefault("shop", None)
    return changed


def get_profile_unlocked():
    prof = db.get_profile()
    if prof is None:
        return {"unlocked": list(START_DECK), "locked": list(INIT_LOCKED)}
    return {"unlocked": list(prof.get("unlocked", START_DECK)),
            "locked": list(prof.get("locked", INIT_LOCKED))}


def create_run(seed=None):
    seed = seed if seed is not None else random.randint(0, 2**31 - 1)
    run_id = uuid.uuid4().hex[:12]
    state = _new_run_state(seed)
    map_data = mapgen.generate_map(seed)
    db.insert_run(run_id, state["seed"], state["status"], state["position"], map_data, state)
    db.append_event(run_id, 1, "create", {"seed": state["seed"], "rule_version": rules.RULE_VERSION})
    return _public_view(state, map_data, run_id)


def load_run(run_id):
    return db.load_run(run_id)


def _require_run(run_id):
    run = db.load_run(run_id)
    if run is None:
        raise InvalidAction("run not found")
    return run


# ---------- 敌人解析 ----------
def _enemy_by_node(node_data):
    eid = node_data["enemy"]
    return enemies_mod.get_enemy(eid)


# ---------- 战斗绑定 ----------
def _build_battle(run_state, node_data):
    enemy_def = _enemy_by_node(node_data)
    relic = run_state["relics"]
    boss_hp_bonus = 0
    if node_data["type"] == mapgen.BOSS and "boss_hp_bonus" in relic:
        boss_hp_bonus = relic["boss_hp_bonus"]
    battle = Battle(
        {"max_health": run_state["max_health"], "health": run_state["health"],
         "deck": run_state["deck"], "relics": relic, "base_energy": run_state["base_energy"]},
        enemy_def, seed=run_state["seed"], battle_index=run_state["battle_index"],
        boss_hp_bonus=boss_hp_bonus,
        card_instances=run_state.get("card_instances", {}),
    )
    battle.start_turn()
    return battle


def _load_battle(run_state):
    bstate = run_state["battle"]
    enemy_def = enemies_mod.get_enemy(bstate["enemy"])
    # 战斗内实例表优先（旧档迁移时写入），否则用 run 级实例表
    instances = bstate.get("card_instances", run_state.get("card_instances", {}))
    battle = Battle.from_state(bstate, enemy_def, run_state["seed"],
                               health=run_state["health"], max_health=run_state["max_health"])
    battle.card_instances = dict(instances)
    return battle


# ---------- 行动 ----------
def act(run_id, action):
    rec = load_run(run_id)
    if rec is None:
        raise InvalidAction("run not found")
    run = rec["state"]
    map_data = rec["map"]
    # 旧档兼容：首次载入即迁移到卡牌实例结构（随本次行动结果一起落库）
    _migrate_state(run)
    if run["status"] != "in_progress":
        raise InvalidAction(f"run already ended ({run['status']})")

    # 纯状态推进：不写库；失败解锁（副作用）在落库之后执行
    log = _apply_action(run, action, map_data=map_data)

    db.save_run(run_id, run["status"], run["position"], run)
    seq = db.next_seq(run_id)
    # 校验点：本动作落库后的状态哈希，供回放逐步比对
    payload = {"node": action.get("node"), "card": action.get("card"),
               "option": action.get("option"), "branch": action.get("branch"),
               "kind": action.get("kind"), "sku": action.get("sku"),
               "rule_version": rules.RULE_VERSION, "checkpoint": rules.state_hash(run)}
    db.append_event(run_id, seq, action.get("action"), payload)
    # 失败解锁只发生在“真实对局”的 act 路径；回放直接调用 _apply_action
    # 纯推进、不会走到这里，因此回放中的战败不会写 profile、不会产生解锁奖励
    if run["status"] == "lost":
        _grant_unlock_on_loss(run)
    return {"seq": seq, "log": log, "run": _public_view(run, map_data, run_id)}


def _apply_action(run, action, map_data=None):
    """把一个合法动作应用到内存 run 状态（纯推进：不做任何持久化）。

    实时对局与确定性回放共用本函数，保证两条路径规则完全一致。
    choose_node 需要地图数据：实时行动由调用方传入；回放由重建器传入。
    """
    a = action.get("action")
    if a == "choose_node":
        if map_data is None:
            raise InvalidAction("map data required for choose_node")
        _choose_node(run, map_data, action["node"])
        return []
    if a == "play":
        return _play(run, action["card"])
    if a == "end_turn":
        return _end_turn(run)
    if a == "claim_reward":
        return _claim_reward(run, action["option"])
    if a == "forge":
        return _forge(run, action.get("card"), action.get("branch"))
    if a == "shop_buy":
        return _shop_buy(run, action.get("kind"), action.get("sku"))
    if a == "shop_remove":
        return _shop_remove(run, action.get("card"))
    raise InvalidAction(f"unknown action {a}")


def _choose_node(run, map_data, node):
    from_current = map_data["routes"].get(run["position"], [])
    if run["position"] != mapgen.BOSS and node not in from_current:
        raise InvalidAction(f"node {node} unreachable from {run['position']}")
    node_data = map_data["nodes"][node]
    run["position"] = node
    # 每进入一个新节点都清掉上个节点的商店库存（商店仅在其节点内有效）
    run["shop"] = None

    t = node_data["type"]
    if t in (mapgen.ENCOUNTER, mapgen.ELITE, mapgen.BOSS):
        run["in_battle"] = True
        run["battle_index"] += 1
        battle = _build_battle(run, node_data)
        run["battle"] = battle.dump()
        run["health"] = battle.entities["player"]["hp"]
        run["reward_options"] = []
        run["reward_claimed"] = True
        run["forge_claimed"] = True
        battle_start = battle.to_snapshot()["hand"]
        run["events_log"].append({"at": f"battle:{node}:{run['battle_index']}", "battle": True})
    elif t == mapgen.REST:
        heal = max(1, int(run["max_health"] * 0.2))
        run["health"] = min(run["max_health"], run["health"] + heal)
        run["reward_options"] = []
        run["reward_claimed"] = True
        run["forge_claimed"] = True
        run["events_log"].append({"at": f"rest:{node}", "heal": heal})
    elif t == mapgen.REWARD:
        run["in_battle"] = False
        run["battle"] = None
        run["reward_options"] = rewards_mod.relic_choice_options(run["seed"] + run["battle_index"] * 7)
        run["reward_claimed"] = False
        run["forge_claimed"] = True
    elif t == mapgen.FORGE:
        # 锻造节点：进入即待锻造，玩家可花金币为一张牌选择强化分支（仅一次）
        run["in_battle"] = False
        run["battle"] = None
        run["reward_options"] = []
        run["reward_claimed"] = True
        run["forge_claimed"] = False
        run["events_log"].append({"at": f"forge:{node}"})
    elif t == mapgen.SHOP:
        # 旅途商店：按 (种子, 节点) 确定性生成库存；交易记录随库存保存，续局/回放一致
        run["in_battle"] = False
        run["battle"] = None
        run["reward_options"] = []
        run["reward_claimed"] = True
        run["forge_claimed"] = True
        stock_seed = (run["seed"] * 10007 + node_data["row"] * 131 + ord(node[0])) & 0xFFFFFFFF
        owned_cards = {inst["id"] for inst in run.get("card_instances", {}).values()}
        run["shop"] = shop_mod.generate_stock(stock_seed, owned_cards, set(run["relics"]))
        run["events_log"].append({"at": f"shop:{node}",
                                  "cards": len(run["shop"]["cards"]),
                                  "relics": len(run["shop"]["relics"])})
    elif t == "start":
        run["reward_claimed"] = True
        run["forge_claimed"] = True


def _battle_or_raise(run):
    if not run["in_battle"] or not run["battle"]:
        raise InvalidAction("not in battle")


def _play(run, card_ref):
    _battle_or_raise(run)
    battle = _load_battle(run)
    if not battle.in_turn:
        raise InvalidAction("not player turn")
    # card 为手牌引用（uid；旧档/裸测兼容卡牌 id）
    if card_ref not in battle.hand:
        raise InvalidAction("hand does not contain that card")
    card = battle._card_def(card_ref)
    if card["cost"] > battle.energy:
        raise InvalidAction("not enough energy")
    battle.energy -= card["cost"]
    try:
        log = battle.play_card(card_ref)
    except ValueError as e:
        raise InvalidAction(str(e))
    return _after_battle_step(run, battle, log)


def _end_turn(run):
    _battle_or_raise(run)
    battle = _load_battle(run)
    run["reward_claimed"] = True
    enemy_log, intent = battle.end_turn()
    log = []
    if intent is not None:
        # 敌方回合标记（含技能名），前端据此播放“敌方行动”横幅
        log.append({"action": "enemy_turn", "target": "player", "value": 0,
                    "source": "enemy", "tags": ["system"],
                    "extra": {"name": intent.get("name", "")}})
    # 敌方结算事件按结算顺序入日志，前端依序播放连锁动画
    log.extend(enemy_log)
    return _after_battle_step(run, battle, log)


def _after_battle_step(run, battle, log):
    snap = battle.to_snapshot()
    result = battle.battle_result()
    run["health"] = battle.entities["player"]["hp"]
    if result == "ongoing":
        run["battle"] = battle.dump()
        log.append({"snapshot": snap})
        return log

    # 战斗结束
    run["in_battle"] = False
    if result == "won":
        run["battle"] = None
        # 用当前节点敌人掉落生成奖励
        enemy = battle.enemy_def
        run["reward_options"] = rewards_mod.battle_reward_options(
            run["seed"] + run["battle_index"], enemy, run)
        run["reward_claimed"] = False
        log.append({"result": "won", "snapshot": snap})
        if enemy.get("boss"):
            run["status"] = "won"
            run["reward_options"] = []
            run["reward_claimed"] = True
            log.append({"result": "run_won"})
    else:
        run["battle"] = None
        run["status"] = "lost"
        log.append({"result": "lost", "snapshot": snap})
        # 注：失败解锁属于存档副作用，不在纯推进路径触发——
        # 由 act() 落库后调用；回放（_apply_action）因此天然不写 profile
    return log


def _claim_reward(run, option_idx):
    if run["reward_claimed"]:
        raise DuplicateReward("reward already claimed")
    if run["status"] != "in_progress":
        raise InvalidAction("not in progress")
    opts = run["reward_options"]
    if option_idx < 0 or option_idx >= len(opts):
        raise InvalidAction("bad reward option")
    opt = opts[option_idx]
    _apply_option(run, opt)
    run["reward_claimed"] = True
    run["reward_options"] = []
    return [{"reward_claimed": opt["name"]}]


def _apply_option(run, opt):
    for eff in opt.get("effects", []):
        _apply_option_effect(run, eff)


def _apply_option_effect(run, eff):
    t = eff["type"]
    if t == "add_card":
        _add_card_instance(run, eff["card"])
    elif t == "gold":
        run["gold"] += eff["value"]
    elif t == "heal_run":
        run["health"] = min(run["max_health"], run["health"] + eff["value"])
    elif t == "relic_set":
        run["relics"][eff["relic"]] = eff.get("value", 1)


def _add_card_instance(run, cid):
    """奖励入牌：发放带独立成长状态的新卡牌实例（同名卡互不共享锻造）。"""
    seq = run.get("next_card_seq", len(run.get("card_instances", {})) + 1)
    uid = f"c{seq}"
    run["next_card_seq"] = seq + 1
    run.setdefault("card_instances", {})[uid] = {"id": cid, "forges": []}
    run["deck"].append(uid)
    return uid


# ---------- 卡牌锻造 ----------
def _forge(run, card_uid, branch):
    # 仅在尚未锻造的锻造节点可操作：forge_claimed 承担幂等键，重复请求不重复扣款（409）
    if run.get("forge_claimed", True):
        raise DuplicateReward("forge already used at this node")
    if not card_uid or card_uid not in run.get("card_instances", {}):
        raise InvalidAction("unknown card instance")
    if branch not in forging_mod.BRANCH_IDS:
        raise InvalidAction("unknown forge branch")
    if run["gold"] < FORGE_COST:
        raise InvalidAction("not enough gold")
    # 先校验全部通过再扣款，避免失败请求产生任何副作用
    run["gold"] -= FORGE_COST
    inst = run["card_instances"][card_uid]
    inst.setdefault("forges", []).append(branch)
    run["forge_claimed"] = True
    run["events_log"].append({
        "at": f"forge:{run['position']}", "card": inst["id"], "uid": card_uid, "branch": branch,
    })
    base = get_card(inst["id"])
    eff = effective_card(base, inst["forges"])
    return [{"forged": {"uid": card_uid, "card": inst["id"], "branch": branch,
                        "cost": eff["cost"], "gold_left": run["gold"]}}]


# ---------- 旅途商店 ----------
def _active_shop(run):
    if run.get("in_battle") or not run.get("shop"):
        raise InvalidAction("not at a shop")
    return run["shop"]


def _find_offer(shop, kind, sku):
    bucket = shop["cards"] if kind == "card" else shop["relics"] if kind == "relic" else None
    if bucket is None:
        raise InvalidAction("unknown shop shelf (kind must be card/relic)")
    item = next((it for it in bucket if it["sku"] == sku), None)
    if item is None:
        raise InvalidAction("unknown shop item")
    if item["sold"]:
        # 售罄（含重复购买）：不扣款
        raise ShopSoldOut("item already sold")
    return item


def _commit_shop_tx(run, mutate, record):
    """统一交易：先对 run 做深拷贝快照，执行扣款与变更；任意异常都整体回退到快照，
    保证失败请求零副作用（不扣款、不改牌组/遗物/库存）。成功才写入交易记录。"""
    snapshot = copy.deepcopy(run)
    try:
        result = mutate(run)
    except Exception:
        run.clear()
        run.update(snapshot)
        raise
    record_obj = record(result)
    run["shop"]["tx"].append(record_obj)
    return [{"shop_tx": record_obj}]


def _shop_buy(run, kind, sku):
    shop = _active_shop(run)
    item = _find_offer(shop, kind, sku)  # 非法货架/售罄在扣款前拦截
    price = item["price"]
    if run["gold"] < price:
        raise InvalidAction("not enough gold")  # 校验先于扣款，无副作用

    if kind == "card":
        cid = item["card"]
        # 货架生成时按未持有过滤；若已通过其它途径获得同名卡，仍允许购买（独立新实例）
        def mutate(r):
            r["gold"] -= price
            uid = _add_card_instance(r, cid)
            next(it for it in r["shop"]["cards"] if it["sku"] == sku)["sold"] = True
            return {"uid": uid}
    else:
        rid = item["relic"]
        if rid in run["relics"]:
            raise ShopSoldOut("relic already owned")
        relic = shop_mod.relic_def(rid)

        def mutate(r):
            r["gold"] -= price
            for eff in relic.get("effects", []):
                _apply_option_effect(r, eff)
            next(it for it in r["shop"]["relics"] if it["sku"] == sku)["sold"] = True
            return {"relic": rid}

    return _commit_shop_tx(
        run, mutate,
        lambda res: {"type": "buy", "kind": kind, "sku": sku, "price": price,
                     "gold_left": run["gold"], **res})


def _shop_remove(run, card_uid):
    shop = _active_shop(run)
    if not card_uid or card_uid not in run.get("card_instances", {}):
        raise InvalidAction("unknown card instance")
    if len(run["deck"]) <= shop_mod.REMOVE_MIN_DECK:
        raise InvalidAction("deck too small to remove")
    cost = shop["remove"]["cost"]
    if run["gold"] < cost:
        raise InvalidAction("not enough gold")  # 校验先于扣款，无副作用
    cid = run["card_instances"][card_uid]["id"]

    def mutate(r):
        r["gold"] -= cost
        r["deck"].remove(card_uid)
        del r["card_instances"][card_uid]
        r["shop"]["remove"]["used"] += 1
        r["shop"]["remove"]["cost"] = shop_mod.next_remove_cost(r["shop"]["remove"]["used"])
        return {"uid": card_uid, "card": cid, "deck_size": len(r["deck"])}

    return _commit_shop_tx(
        run, mutate,
        lambda res: {"type": "remove", **res, "price": cost, "gold_left": run["gold"]})


def _grant_unlock_on_loss(run):
    prof = db.get_profile()
    unlocked = list(prof["unlocked"]) if prof else list(START_DECK)
    locked = list(prof["locked"]) if prof else list(INIT_LOCKED)
    pool = [c for c in locked if all_cards_locked().get(c)]
    if not pool:
        return
    rng = random.Random(run["seed"] + run["battle_index"])
    cid = pool[rng.randrange(len(pool))]
    unlocked.append(cid)
    locked.remove(cid)
    db.upsert_profile({"unlocked": unlocked, "locked": locked})


def all_cards_locked():
    return {c["id"]: c for c in all_cards()}


# ---------- 视口 ----------
def resume(run_id):
    rec = load_run(run_id)
    if rec is None:
        raise InvalidAction("run not found")
    # 旧档兼容：续局时迁移并落库，之后所有战斗/锻造都走卡牌实例
    if _migrate_state(rec["state"]):
        db.save_run(run_id, rec["state"]["status"], rec["state"]["position"], rec["state"])
    return _public_view(rec["state"], rec["map"], rec["id"])


def replay(run_id):
    rec = load_run(run_id)
    if rec is None:
        raise InvalidAction("run not found")
    from .replay import build_replay
    return build_replay(rec, db.load_events(run_id))


def _hand_public(run, bstate):
    """战斗手牌视口：新档给出含生效费用/锻造标记的实例项，旧档回退为裸 id。"""
    instances = bstate.get("card_instances", run.get("card_instances", {}))
    if not instances:
        return list(bstate["hand"])
    out = []
    for ref in bstate["hand"]:
        inst = instances.get(ref)
        if inst is None:
            out.append(ref)
            continue
        eff = effective_card(get_card(inst["id"]), inst.get("forges", []))
        out.append({
            "uid": ref, "id": inst["id"], "cost": eff["cost"],
            "forges": list(inst.get("forges", [])),
        })
    return out


def _public_view(run, map_data, run_id, include_profile=True):
    # include_profile=False：回放松口。不读取 profile 表，且不携带解锁信息，
    # 从数据层面保证回放与“解锁奖励”隔离
    reachable = map_data["routes"].get(run["position"], [])
    snap = None
    if run["in_battle"] and run["battle"]:
        def pub_ent(k):
            e = run["battle"]["entities"][k]
            return {
                "name": e["name"], "hp": e["hp"], "max_hp": e["max_hp"],
                "block": e["block"], "alive": e["alive"],
                "statuses": _statuses_public(e["statuses"]),
            }
        snap = {
            "player": pub_ent("player"),
            "enemy": pub_ent("enemy"),
            "energy": run["battle"]["energy"],
            "max_energy": run["battle"]["max_energy"],
            "turn": run["battle"]["turn"],
            "in_turn": run["battle"]["in_turn"],
            "truncated": run["battle"]["truncated"],
            "hand": _hand_public(run, run["battle"]),
            "enemy_id": run["battle"]["enemy"],
        }
    # 牌组视口：同名卡按实例独立呈现（携带各自锻造分支）
    instances = run.get("card_instances", {})
    deck_view = [{
        "uid": uid, "id": instances[uid]["id"],
        "forges": list(instances[uid].get("forges", [])),
    } for uid in run["deck"] if uid in instances] or list(run["deck"])
    node_data = map_data["nodes"].get(run["position"], {})
    return {
        "run_id": run_id,
        "seed": run["seed"],
        "status": run["status"],
        "position": run["position"],
        "health": run["health"],
        "max_health": run["max_health"],
        "gold": run["gold"],
        "energy": run["battle"]["energy"] if run["in_battle"] else run["base_energy"],
        "deck": deck_view,
        "relics": dict(run["relics"]),
        "reward_options": list(run["reward_options"]),
        "reward_claimed": run["reward_claimed"],
        "forge_available": node_data.get("type") == mapgen.FORGE and not run.get("forge_claimed", True),
        "forge_claimed": bool(run.get("forge_claimed", True)),
        "forge_cost": FORGE_COST,
        "forge_branches": forging_mod.public_branches(),
        "shop_available": node_data.get("type") == mapgen.SHOP and bool(run.get("shop")),
        "shop": shop_mod.public_view(run.get("shop")),
        "in_battle": run["in_battle"],
        "battle": snap,
        "reachable": [map_data["nodes"][n] for n in reachable],
        "map": _map_public(map_data, run["position"]),
        "unlocked_cards": get_profile_unlocked() if include_profile else None,
        "truncated": bool(run["battle"]["truncated"]) if run["in_battle"] and run["battle"] else bool(run.get("truncated", False)),
    }


def _map_public(map_data, position):
    return {
        "nodes": map_data["nodes"],
        "routes": map_data["routes"],
        "start": map_data["start"],
        "boss": map_data["boss"],
        "position": position,
    }
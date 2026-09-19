from __future__ import annotations

"""可交互回放的确定性重建器。

把 battle_events 动作日志重建成“整局时间线”：每个动作一帧，帧帧携带完整视口，
前端可逐步播放 / 暂停 / 任意跳转，路线、战斗、锻造、交易状态全部按帧还原。

隔离性（与实时对局的关键区别）：
- 全程只操作深拷贝的内存状态，不写 runs / battle_events / profile 任何一张表；
- 走 service._apply_action 纯推进路径，战败不会触发失败解锁；
- 视口通过 include_profile=False 构建，连 profile 表都不读取。

校验与兼容：
- 每个动作落库时带 rule_version + checkpoint（状态哈希），回放逐帧比对；
- 旧日志缺这些字段：按 LEGACY_RULE_VERSION 推断并标记 legacy，跳过校验，
  裸 id 的 play 动作按手牌出现序稳定映射到实例 uid；
- 末帧与数据库内最终状态再做一次哈希对账；地图可由种子重新生成做旁证。
所有不一致只产生 warnings，绝不阻断回放。
"""

import copy

from . import mapgen
from . import rules
from . import service
from .cards import get_card
from .forging import branch_name

_NODE_LABELS = {
    mapgen.ENCOUNTER: "遭遇战", mapgen.ELITE: "精英战", mapgen.BOSS: "首领战",
    mapgen.REST: "休息点", mapgen.REWARD: "奖励祭坛", mapgen.FORGE: "锻造台",
    mapgen.SHOP: "旅途商店", "start": "营地",
}


def build_replay(rec, events):
    run_id = rec["id"]
    seed = rec["state"]["seed"]
    stored_map = rec["map"]
    stored_state = rec["state"]
    warnings = []

    def warn(code, msg, seq=None):
        warnings.append({"seq": seq, "code": code, "message": msg})

    # 旧日志判定：create 事件未带 rule_version 即视为旧日志
    # （旧格式 create 只有 {"seed":...}；新格式 create 与每个动作都带 rule_version）
    create_version = None
    for e in events:
        if e["action"] == "create":
            create_version = e["payload"].get("rule_version")
    legacy = create_version is None
    rule_version = create_version or (rules.LEGACY_RULE_VERSION if legacy else rules.RULE_VERSION)

    # 地图旁证：种子应能重新生成同一张地图
    try:
        if mapgen.generate_map(seed) != stored_map:
            warn("map_mismatch", "种子重新生成的地图与存档地图不一致（可能来自旧规则）")
    except Exception as exc:  # 地图生成失败不应阻断回放
        warn("map_regen_failed", f"地图重建异常：{exc}")

    # 从全新状态开始重放（与建局同构）；迁移逻辑与实时路径一致
    state = service._new_run_state(seed)
    service._migrate_state(state)

    steps = []
    path = []
    battle_ctx = None  # 当前/最近一场战斗的 {index,enemy,node}，供击杀帧分组

    # 第 0 帧：建局
    steps.append(_step(
        seq=0, action="create", payload=(events[0]["payload"] if events else {"seed": seed}),
        label=f"建局（种子 {seed}）", kind="system", state=state, map_data=stored_map,
        run_id=run_id, path=list(path), battle_key=None, events=[],
        checkpoint=None,
    ))

    for e in events:
        action = e["action"]
        payload = dict(e["payload"])
        if action == "create":
            continue
        applied = {"action": action, **payload}

        # 旧日志兼容：play 的裸卡牌 id -> 当前手牌中的实例 uid（按手牌出现序）
        card_id_before = None
        reward_name_before = None
        if action == "play":
            applied["card"] = _translate_legacy_card(state, applied.get("card"), e["seq"], warn)
            uid = applied.get("card")
            inst = state.get("card_instances", {}).get(uid)
            card_id_before = inst["id"] if inst else None
        elif action == "claim_reward":
            # 在领奖推进之前记录选项名（推进后选项即清空）
            idx = applied.get("option")
            opts = state.get("reward_options", [])
            if isinstance(idx, int) and 0 <= idx < len(opts):
                reward_name_before = opts[idx].get("name")

        # 纯推进：与实时对局完全相同的规则路径；任何存档副作用都不会发生。
        # 若日志与规则不一致（旧规则差异、日志被直接改库等），该步降级为“分叉帧”：
        # 回放停在该动作之前的已知良好状态并继续，保证整条时间线仍可逐步浏览。
        step_warning = None
        try:
            log = service._apply_action(state, applied, map_data=stored_map)
        except Exception as exc:
            step_warning = {"seq": e["seq"], "code": "step_diverged",
                            "message": f"动作 #{e['seq']}（{action}）无法按当前规则重放：{exc}"}
            warnings.append(step_warning)
            log = []

        if action == "choose_node" and step_warning is None:
            node = applied.get("node")
            if node:
                path.append(node)
            nd = stored_map["nodes"].get(node, {})
            if nd.get("type") in (mapgen.ENCOUNTER, mapgen.ELITE, mapgen.BOSS):
                battle_ctx = {"index": state.get("battle_index"),
                              "enemy": nd.get("enemy"), "node": node}
            else:
                # 非战斗节点：击杀帧仅属于移动帧本身之前的战斗段；
                # 其后所有帧不再归属任何战斗
                battle_ctx = None

        # 校验点比对（分叉帧不参与：状态未推进）
        recorded = applied.get("checkpoint")
        actual = rules.state_hash(state)
        checkpoint = None
        if step_warning is None:
            if recorded is not None:
                ok = recorded == actual
                checkpoint = {"hash": actual, "recorded": recorded, "ok": ok}
                if not ok:
                    warn("checkpoint_mismatch",
                         f"动作 #{e['seq']}（{action}）校验点不一致：记录 {recorded} / 重放 {actual}",
                         seq=e["seq"])
            elif not legacy:
                warn("checkpoint_missing", f"动作 #{e['seq']} 缺少校验点", seq=e["seq"])

        kind, label = _describe(action, applied, state, stored_map, log,
                                card_id_before, reward_name_before)
        battle_key = _battle_key(state, log, battle_ctx) if step_warning is None else None

        steps.append(_step(
            seq=e["seq"], action=action, payload=_safe_payload(applied),
            label=label, kind=kind, state=state, map_data=stored_map, run_id=run_id,
            path=list(path), battle_key=battle_key, events=log,
            checkpoint=checkpoint, diverged=bool(step_warning),
        ))

    # 末帧对账：重放终态应与数据库最终状态一致（旧档迁移后比较）
    stored = copy.deepcopy(stored_state)
    service._migrate_state(stored)
    if rules.state_hash(stored) != rules.state_hash(state):
        warn("final_state_mismatch",
             "重放终态与存档终态不一致（日志可能被裁剪或来自不同规则版本）")

    return {
        "run_id": run_id,
        "seed": seed,
        "rule_version": rule_version,
        "current_rule_version": rules.RULE_VERSION,
        "legacy": legacy,
        "warnings": warnings,
        "path": path,
        "status": state["status"],
        "step_count": len(steps),
        "steps": steps,
        # 兼容旧前端/旧测试：仍然返回原始动作序列
        "actions": events,
    }


# ---------- 单帧 ----------
def _step(seq, action, payload, label, kind, state, map_data, run_id, path,
          battle_key, events, checkpoint, diverged=False):
    view = service._public_view(state, map_data, run_id, include_profile=False)
    node = state.get("position")
    node_type = map_data["nodes"].get(node, {}).get("type")
    result = None
    for ev in events:
        if isinstance(ev, dict) and ev.get("result"):
            result = ev["result"]
    return {
        "seq": seq,
        "action": action,
        "payload": payload,
        "label": label,
        "kind": kind,
        "node": node,
        "node_type": node_type,
        "battle_key": battle_key,
        "result": result,
        "events": events,
        "path": path,
        "checkpoint": checkpoint,
        "rule_version": payload.get("rule_version"),
        "diverged": diverged,
        "view": view,
    }


# ---------- 旧日志：裸 id 映射 ----------
def _translate_legacy_card(state, ref, seq, warn):
    """把 play 动作的卡牌引用解析成手牌实例 uid。

    新日志里就是 uid；旧日志是裸卡牌 id，按“手牌中该 id 的第一个实例”
    （即手牌出现序）稳定映射——同名实例在旧规则下完全等价，映射确定且可复现。
    """
    if not state.get("in_battle") or not state.get("battle"):
        return ref
    hand = state["battle"].get("hand", [])
    if ref in hand:
        return ref
    instances = state.get("card_instances", {})
    for uid in hand:
        inst = instances.get(uid)
        if inst and inst.get("id") == ref:
            return uid
    # 防御性兜底：手牌中找不到同 id 实例（理论上旧日志不会出现）
    if hand:
        warn("legacy_card_unresolved",
             f"动作 #{seq} 的卡牌 {ref} 无法在手牌中定位，使用手牌首张替代", seq=seq)
        return hand[0]
    return ref


# ---------- 战斗段分组 ----------
def _battle_just_ended(log):
    return any(isinstance(ev, dict) and ev.get("result") in ("won", "lost", "run_won") for ev in (log or []))


def _battle_key(state, log, battle_ctx):
    """该帧归属的战斗段 key（同一场战斗的连续帧相同，前端据此保持场景挂载）。"""
    if state.get("in_battle") and state.get("battle"):
        b = state["battle"]
        return f"{b.get('index', 0)}:{b.get('enemy')}"
    if _battle_just_ended(log) and battle_ctx:
        return f"{battle_ctx['index']}:{battle_ctx['enemy']}"
    return None


# ---------- 动作 -> 中文摘要 ----------
def _describe(action, payload, state, map_data, log, card_id, reward_name):
    node = state.get("position")
    node_type = map_data["nodes"].get(node, {}).get("type")
    if action == "choose_node":
        return "route", f"前往「{_NODE_LABELS.get(node_type, node)}」"
    if action == "end_turn":
        return "battle", "结束回合（敌方行动）"
    if action == "play":
        cid = card_id or "card"
        try:
            name = get_card(cid)["name"]
        except Exception:
            name = cid
        return "battle", f"打出「{name}」"
    if action == "claim_reward":
        return "reward", f"领取奖励：{reward_name or '奖励'}"
    if action == "forge":
        forged = next((ev.get("forged") for ev in log if isinstance(ev, dict) and ev.get("forged")), None)
        if forged:
            try:
                cname = get_card(forged["card"])["name"]
            except Exception:
                cname = forged["card"]
            b = branch_name(forged["branch"]) if forged.get("branch") else forged.get("branch", "")
            return "forge", f"锻造「{cname}」· {b}"
        return "forge", "锻造"
    if action in ("shop_buy", "shop_remove"):
        tx = next((ev.get("shop_tx") for ev in log if isinstance(ev, dict) and ev.get("shop_tx")), None)
        if tx:
            return "shop", _shop_tx_label(tx)
        return "shop", "商店交易"
    return "other", action


def _shop_tx_label(tx):
    if tx.get("type") == "remove":
        try:
            cname = get_card(tx["card"])["name"]
        except Exception:
            cname = tx.get("card", "")
        return f"商店移除「{cname}」（-{tx.get('price')} 金币）"
    kind = "卡牌" if tx.get("kind") == "card" else "遗物"
    sku = tx.get("sku", "")
    name = sku.split(":", 1)[1] if ":" in sku else sku
    return f"商店购入{kind}「{name}」（-{tx.get('price')} 金币）"


def _safe_payload(payload):
    """剔除仅供推进的内部字段，保留原始动作参数。"""
    return {k: v for k, v in payload.items() if not k.startswith("_")}

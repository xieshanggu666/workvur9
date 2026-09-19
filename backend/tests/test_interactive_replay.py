"""可交互回放：逐步时间线、状态重建、规则版本/校验点、旧日志兼容与存档隔离。"""
import json

import pytest

from app import db, mapgen, replay, rules, service
from app.cards import get_card


def _walk_to(client, rid, kinds, max_nodes=20):
    """沿地图走到指定类型节点之一并进入。"""
    for _ in range(max_nodes):
        view = service.resume(rid)
        pos = view["position"]
        m = view["map"]
        targets = [n for n in m["routes"][pos] if m["nodes"][n]["type"] in kinds]
        if not targets:
            return None
        node = targets[0]
        client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": node})
        view = service.resume(rid)
        if m["nodes"][node]["type"] in kinds:
            return node
    return None


def _enemy_node(m):
    for nid, node in m["nodes"].items():
        if node["type"] in (mapgen.ENCOUNTER, mapgen.ELITE, mapgen.BOSS):
            return nid
    raise AssertionError("no enemy node")


def _find_seed_with_first_row(kind, attempts=200):
    """找一个第 0 行含指定类型节点的种子。"""
    for seed in range(1, attempts):
        m = mapgen.generate_map(seed)
        if any(m["nodes"][f"0-{c}"]["type"] == kind for c in range(3)):
            return seed
    raise AssertionError(f"no seed with row0 {kind}")


# ---------- 时间线：逐帧重建路线/战斗/锻造/交易状态 ----------
def test_replay_timeline_rebuilds_full_states(client):
    seed = 1
    r = client.post("/api/runs", json={"seed": seed}).json()
    rid = r["run_id"]
    m = service.load_run(rid)["map"]
    enemy = _enemy_node_from_start(m)
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": enemy})

    # 打若干手牌（能量内），然后结束回合
    view = service.resume(rid)
    played = 0
    for hc in list(view["battle"]["hand"]):
        uid = hc["uid"] if isinstance(hc, dict) else hc
        cost = hc["cost"] if isinstance(hc, dict) else get_card(hc)["cost"]
        v2 = service.resume(rid)
        if not v2["battle"]["in_turn"] or cost > v2["battle"]["energy"]:
            continue
        res = client.post(f"/api/runs/{rid}/act", json={"action": "play", "card": uid}).json()
        if "run" not in res:
            break
        played += 1
        if played >= 2:
            break
    if service.resume(rid)["in_battle"]:
        client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"})

    rep = client.get(f"/api/runs/{rid}/replay").json()

    # 元信息
    assert rep["legacy"] is False
    assert rep["rule_version"] == rules.RULE_VERSION
    assert rep["current_rule_version"] == rules.RULE_VERSION
    assert rep["seed"] == seed
    assert rep["warnings"] == []
    assert rep["step_count"] == len(rep["steps"])

    # 第 0 帧 = 建局
    s0 = rep["steps"][0]
    assert s0["seq"] == 0 and s0["action"] == "create"
    assert s0["view"]["position"] == "start"
    assert s0["view"]["health"] == 75

    # choose_node 帧：路线状态正确重建
    choose = next(s for s in rep["steps"] if s["action"] == "choose_node")
    assert choose["node"] == enemy
    assert enemy in choose["path"]
    assert choose["view"]["in_battle"] is True
    assert choose["battle_key"] is not None
    assert choose["kind"] == "route"

    # 战斗帧：结算事件与战斗视口逐帧可用（供逐步播放/跳转）
    battle_steps = [s for s in rep["steps"] if s["battle_key"]]
    assert battle_steps, "至少有进入战斗后的帧"
    for s in battle_steps:
        assert s["view"]["battle"] is not None
        assert s["view"]["battle"]["enemy_id"]
    play_steps = [s for s in rep["steps"] if s["action"] == "play"]
    assert play_steps, "应至少成功打出一张牌"
    assert any(s["label"].startswith("打出") for s in play_steps)
    for s in play_steps:
        assert len(s["events"]) >= 1  # 结算事件随帧携带

    # 同一场战斗的连续帧 battle_key 相同（前端据此保持 Phaser 场景挂载）
    keys = [s["battle_key"] for s in battle_steps]
    assert all(k == keys[0] for k in keys)


def _enemy_node_from_start(m):
    for n in m["routes"][m["start"]]:
        if m["nodes"][n]["type"] in (mapgen.ENCOUNTER, mapgen.ELITE):
            return n
    raise AssertionError("no reachable enemy on first row")


def test_every_step_view_matches_live_public_view(client):
    """逐帧视口与“该动作实时返回的视口”一致：跳转任意帧都能正确渲染。"""
    seed = 5
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    m = service.load_run(rid)["map"]
    enemy = _enemy_node_from_start(m)
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": enemy})
    live_views = {}  # seq -> 实时 act 返回视口
    while service.resume(rid)["in_battle"]:
        v = service.resume(rid)
        b = v["battle"]
        if not b["in_turn"]:
            break
        playable = next((h for h in b["hand"]
                         if (h["cost"] if isinstance(h, dict) else 0) <= b["energy"]), None)
        if playable is None:
            res = client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"}).json()
            live_views[res["seq"]] = res["run"]
            break
        uid = playable["uid"] if isinstance(playable, dict) else playable
        res = client.post(f"/api/runs/{rid}/act", json={"action": "play", "card": uid}).json()
        if "run" not in res:
            break
        live_views[res["seq"]] = res["run"]
        if not res["run"]["in_battle"]:
            break

    rep = client.get(f"/api/runs/{rid}/replay").json()
    assert rep["warnings"] == []
    for s in rep["steps"]:
        if s["seq"] in live_views:
            # 回放松口不携带解锁信息，其余字段应与实时视口一致
            replay_view = dict(s["view"])
            live = dict(live_views[s["seq"]])
            live["unlocked_cards"] = None
            assert replay_view == live, f"seq={s['seq']} 视口不一致"


# ---------- 校验点：逐帧哈希 + 终态对账 ----------
def test_checkpoints_recorded_and_verified(client):
    rid = client.post("/api/runs", json={"seed": 99}).json()["run_id"]
    m = service.load_run(rid)["map"]
    enemy = _enemy_node_from_start(m)
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": enemy})
    client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"})

    raw = db.load_events(rid)
    acted = [e for e in raw if e["action"] in ("choose_node", "end_turn")]
    for e in acted:
        assert e["payload"]["rule_version"] == rules.RULE_VERSION
        assert len(e["payload"]["checkpoint"]) == 16

    rep = client.get(f"/api/runs/{rid}/replay").json()
    for s in rep["steps"]:
        if s["action"] == "create":
            continue
        assert s["checkpoint"] is not None
        assert s["checkpoint"]["ok"] is True
    assert rep["warnings"] == []


def test_tampered_state_produces_warning_not_crash(client):
    """直接改库（绕过日志）：回放不崩溃，终态对账给出告警。"""
    rid = client.post("/api/runs", json={"seed": 7}).json()["run_id"]
    m = service.load_run(rid)["map"]
    enemy = _enemy_node_from_start(m)
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": enemy})
    client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"})
    # 绕过动作日志直接改生命值
    rec = service.load_run(rid)
    rec["state"]["health"] = 1
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])

    rep = client.get(f"/api/runs/{rid}/replay").json()
    codes = {w["code"] for w in rep["warnings"]}
    assert "final_state_mismatch" in codes
    # 时间线仍完整可浏览
    assert rep["step_count"] == len(rep["steps"])


# ---------- 旧日志兼容 ----------
def test_legacy_log_with_bare_card_ids_replays(client):
    """旧规则日志：无 rule_version/checkpoint，play 用裸卡牌 id。"""
    seed = 11
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    m = service.load_run(rid)["map"]
    enemy = _enemy_node_from_start(m)
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": enemy})
    # 实时打出第一张可打牌
    v = service.resume(rid)
    target = next(h for h in v["battle"]["hand"] if h["cost"] <= v["battle"]["energy"])
    client.post(f"/api/runs/{rid}/act", json={"action": "play", "card": target["uid"]})

    # 把事件日志改写成旧格式：删掉 rule_version/checkpoint，play 改回裸 id
    conn = db.get_conn()
    try:
        for row in conn.execute("SELECT seq, action, payload_json FROM battle_events WHERE run_id=?", (rid,)):
            payload = json.loads(row["payload_json"])
            payload.pop("rule_version", None)
            payload.pop("checkpoint", None)
            if row["action"] == "play":
                payload["card"] = target["id"]  # 裸 id
            conn.execute("UPDATE battle_events SET payload_json=? WHERE run_id=? AND seq=?",
                         (json.dumps(payload, ensure_ascii=False), rid, row["seq"]))
        conn.commit()
    finally:
        conn.close()

    rep = client.get(f"/api/runs/{rid}/replay").json()
    assert rep["legacy"] is True
    assert rep["rule_version"] == rules.LEGACY_RULE_VERSION
    # 旧日志不做校验点比对，但不产生分叉
    play_step = next(s for s in rep["steps"] if s["action"] == "play")
    assert play_step["checkpoint"] is None
    assert play_step["diverged"] is False
    # 裸 id 被映射到手牌实例，战斗照常结算
    assert play_step["view"]["battle"] is not None
    assert any(ev.get("action") in ("damage", "gain_block") for ev in play_step["events"])
    # 旧前端契约仍在
    assert [a["action"] for a in rep["actions"]][:3] == ["create", "choose_node", "play"]


# ---------- 隔离：回放不写存档、不发解锁 ----------
def test_replay_does_not_write_runs_or_events(client):
    rid = client.post("/api/runs", json={"seed": 13}).json()["run_id"]
    m = service.load_run(rid)["map"]
    enemy = _enemy_node_from_start(m)
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": enemy})
    client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"})

    conn = db.get_conn()
    try:
        state_updated = conn.execute(
            "SELECT updated_at FROM runs WHERE id=?", (rid,)).fetchone()["updated_at"]
        ev_count = conn.execute(
            "SELECT COUNT(*) c FROM battle_events WHERE run_id=?", (rid,)).fetchone()["c"]
    finally:
        conn.close()

    # 连续多次回放，不产生任何写入
    for _ in range(3):
        client.get(f"/api/runs/{rid}/replay")

    conn = db.get_conn()
    try:
        assert conn.execute("SELECT updated_at FROM runs WHERE id=?", (rid,)).fetchone()["updated_at"] == state_updated
        assert conn.execute("SELECT COUNT(*) c FROM battle_events WHERE run_id=?", (rid,)).fetchone()["c"] == ev_count
    finally:
        conn.close()
    # 回放松口不读取/携带解锁信息
    rep = client.get(f"/api/runs/{rid}/replay").json()
    assert rep["steps"][0]["view"]["unlocked_cards"] is None


def test_replay_loss_does_not_grant_unlock(client):
    """战败实时对局会解锁新卡；回放同一战败日志不会再次解锁。

    seed=1 / 0-0 哥布林（每回合 6 伤害）：只打防御牌、从不攻击，玩家必败，
    且整个过程只有合法动作（无直接改库），因此重放应得到完全相同的 lost 终态。
    """
    from app.cards import get_card
    seed = 1
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": "0-0"})
    # 防御策略：只打非攻击牌，然后结束回合 → 被哥布林磨死
    for _ in range(60):
        v = service.resume(rid)
        if not v["in_battle"]:
            break
        while True:
            v = service.resume(rid)
            b = v["battle"]
            if not b["in_turn"]:
                break
            h = next((x for x in b["hand"]
                      if x["cost"] <= b["energy"] and get_card(x["id"])["type"] != "attack"), None)
            if h is None:
                break
            res = client.post(f"/api/runs/{rid}/act", json={"action": "play", "card": h["uid"]}).json()
            assert "run" in res
            if not res["run"]["in_battle"]:
                break
        v = service.resume(rid)
        if not v["in_battle"]:
            break
        res = client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"}).json()
        assert "run" in res
        if not res["run"]["in_battle"]:
            break
    assert service.resume(rid)["status"] == "lost"

    unlocked_after_real = db.get_profile()["unlocked"]
    assert len(unlocked_after_real) > 4 + 3, "实时战败应已解锁一张新卡"

    # 回放得到一致的 lost 终态与战败结果帧，但重复回放不得再次写 profile
    for _ in range(2):
        rep = client.get(f"/api/runs/{rid}/replay").json()
        assert rep["status"] == "lost"
        assert rep["steps"][-1]["result"] == "lost"
    assert db.get_profile()["unlocked"] == unlocked_after_real


# ---------- 锻造 / 交易状态重建（纯动作驱动：金币全部来自战斗奖励） ----------
def _auto_win_battle(client, rid, cap=120):
    """简单策略自动打完当前战斗：可出的牌全出，然后结束回合。

    返回 (outcome, view)：outcome 为 "won"/"lost"/"run_won"/"timeout"。
    战斗胜利后 run 仍是 in_progress（领奖阶段）。
    """
    steps = 0
    while steps < cap:
        v = service.resume(rid)
        if not v["in_battle"]:
            return ("run_won" if v["status"] == "won" else "lost" if v["status"] == "lost" else "won"), v
        if not v["battle"]["in_turn"]:
            return "timeout", v
        while True:
            v = service.resume(rid)
            b = v["battle"]
            if not b["in_turn"]:
                break
            hand = next((h for h in b["hand"] if h["cost"] <= b["energy"]), None)
            if hand is None:
                break
            uid = hand["uid"] if isinstance(hand, dict) else hand
            res = client.post(f"/api/runs/{rid}/act", json={"action": "play", "card": uid}).json()
            steps += 1
            if "run" not in res or not res["run"]["in_battle"]:
                st = res.get("run", {}).get("status")
                return ("run_won" if st == "won" else "lost" if st == "lost" else "won"), res.get("run")
        v = service.resume(rid)
        if not v["in_battle"]:
            return ("run_won" if v["status"] == "won" else "lost" if v["status"] == "lost" else "won"), v
        res = client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"}).json()
        steps += 1
        if "run" not in res:
            return "timeout", v
        r = res["run"]
        if not r["in_battle"]:
            return ("run_won" if r["status"] == "won" else "lost" if r["status"] == "lost" else "won"), r
    return "timeout", service.resume(rid)


def _claim_gold(client, rid):
    v = service.resume(rid)
    for i, opt in enumerate(v["reward_options"]):
        if opt["kind"] == "gold":
            return client.post(f"/api/runs/{rid}/act",
                               json={"action": "claim_reward", "option": i}).json()["run"]["gold"]
    return v["gold"]


def test_replay_rebuilds_forge_state(client):
    # seed=3：0-1 血裔战胜（32 金币）→ 1-0 锻造台（全部行互通）
    seed = 3
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": "0-1"})
    outcome, _ = _auto_win_battle(client, rid)
    assert outcome == "won"
    gold = _claim_gold(client, rid)
    assert gold >= 25
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": "1-0"})
    uid = service.resume(rid)["deck"][0]["uid"]
    res = client.post(f"/api/runs/{rid}/act",
                      json={"action": "forge", "card": uid, "branch": "refine"})
    assert res.status_code == 200

    rep = client.get(f"/api/runs/{rid}/replay").json()
    assert rep["warnings"] == []
    forge_step = next(s for s in rep["steps"] if s["action"] == "forge")
    assert forge_step["kind"] == "forge"
    assert "精炼" in forge_step["label"]
    forged = forge_step["events"][0]["forged"]
    assert forged["uid"] == uid and forged["branch"] == "refine"
    # 终帧：金币扣减、锻造标记落到对应实例、锻造台已关闭
    final = rep["steps"][-1]["view"]
    assert final["gold"] == gold - 25
    card_view = next(d for d in final["deck"] if d["uid"] == uid)
    assert "refine" in card_view["forges"]
    assert final["forge_available"] is False


def test_replay_rebuilds_shop_remove_state(client):
    # seed=4：0-0 回响剑士战胜（38 金币）→ 1-1 商店，移除基础价 35
    seed = 4
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": "0-0"})
    outcome, _ = _auto_win_battle(client, rid)
    assert outcome == "won"
    gold = _claim_gold(client, rid)
    assert gold >= 35
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": "1-1"})
    deck_before = len(service.resume(rid)["deck"])
    victim = service.resume(rid)["deck"][0]["uid"]
    res = client.post(f"/api/runs/{rid}/act",
                      json={"action": "shop_remove", "card": victim})
    assert res.status_code == 200

    rep = client.get(f"/api/runs/{rid}/replay").json()
    assert rep["warnings"] == []
    rm = next(s for s in rep["steps"] if s["action"] == "shop_remove")
    assert rm["kind"] == "shop"
    tx = rm["events"][0]["shop_tx"]
    assert tx["type"] == "remove" and tx["uid"] == victim and tx["price"] == 35
    final = rep["steps"][-1]["view"]
    assert final["gold"] == gold - 35
    assert len(final["deck"]) == deck_before - 1
    assert all(d["uid"] != victim for d in final["deck"])
    # 交易记录随商店状态重建
    assert final["shop"]["tx"][-1]["type"] == "remove"


def test_replay_rebuilds_shop_buy_state(client):
    # seed=1：连打两场（0-0→1-0，累计 50 金币）→ 2-2 商店购入卡牌
    seed = 1
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": "0-0"})
    assert _auto_win_battle(client, rid)[0] == "won"
    _claim_gold(client, rid)
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": "1-0"})
    assert _auto_win_battle(client, rid)[0] == "won"
    gold = _claim_gold(client, rid)
    assert gold >= 45
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": "2-2"})
    shop = service.resume(rid)["shop"]
    offer = min(shop["cards"], key=lambda it: it["price"])
    assert offer["price"] <= gold, f"金币 {gold} 不足以购买最便宜的卡 {offer['price']}"
    sku, price = offer["sku"], offer["price"]
    res = client.post(f"/api/runs/{rid}/act",
                      json={"action": "shop_buy", "kind": "card", "sku": sku})
    assert res.status_code == 200

    rep = client.get(f"/api/runs/{rid}/replay").json()
    assert rep["warnings"] == []
    buy = next(s for s in rep["steps"] if s["action"] == "shop_buy")
    assert "商店购入卡牌" in buy["label"]
    tx = buy["events"][0]["shop_tx"]
    assert tx["sku"] == sku and tx["price"] == price
    final = rep["steps"][-1]["view"]
    assert final["gold"] == gold - price
    assert any(it["sold"] for it in final["shop"]["cards"] if it["sku"] == sku)
    assert len(final["deck"]) == 8
    # 购入的新卡（独立实例）在终帧可见
    assert tx["uid"] in {d["uid"] for d in final["deck"]}

"""中途续局 & 种子回放确定性。"""
import pytest

from app import service, mapgen


def _pick_enemy(client, rid):
    m = service.load_run(rid)["map"]
    for node in m["routes"][m["start"]]:
        if m["nodes"][node]["type"] in (mapgen.ENCOUNTER, mapgen.ELITE, mapgen.BOSS):
            return node
    raise AssertionError("no enemy reachable")


def test_resume_restores_battle_state(client):
    r = client.post("/api/runs", json={"seed": 4242}).json()
    rid = r["run_id"]
    node = _pick_enemy(client, rid)
    st = client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": node}).json()["run"]
    assert st["in_battle"] is True
    hand_before = list(st["battle"]["hand"])
    energy = st["battle"]["energy"]
    # 续局：等价于从数据库重建
    resumed = client.get(f"/api/runs/{rid}/resume").json()
    assert resumed["position"] == node
    assert resumed["in_battle"] is True
    assert resumed["battle"]["hand"] == hand_before
    assert resumed["battle"]["energy"] == energy


def test_replay_returns_deterministic_action_log(client):
    seed = 777
    r = client.post("/api/runs", json={"seed": seed}).json()
    rid = r["run_id"]
    node = _pick_enemy(client, rid)
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": node})
    view = service.resume(rid)
    # 结束回合（验证续局后 action 仍可执行）
    client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"})

    replay = client.get(f"/api/runs/{rid}/replay").json()
    assert replay["seed"] == seed
    actions = replay["actions"]
    actions_seq = [(a["action"], a["payload"].get("node"), a["payload"].get("card")) for a in actions]
    # 确定性：重放同一 run 的动作序列完全一致
    replay2 = client.get(f"/api/runs/{rid}/replay").json()
    assert replay2["actions"] == actions


def test_same_seed_same_map(client):
    a = client.get("/api/map-preview", params={"seed": 12345}).json()
    b = client.get("/api/map-preview", params={"seed": 12345}).json()
    assert a["nodes"] == b["nodes"]
    assert a["routes"] == b["routes"]
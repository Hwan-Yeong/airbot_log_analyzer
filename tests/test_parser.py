from airbotlog.loader import load_session
from airbotlog.rules import RuleSet


def _session(demo_zip, ws, settings):
    return load_session([demo_zip], ws, settings)


def test_rules_consistent():
    rs = RuleSet.load()
    cats = {c.id for c in rs.categories}
    layers = {l.id for l in rs.layers}
    for r in rs.rules:
        if r.list and not r.track:
            assert r.category in cats, r.id
        if r.layer:
            assert r.layer in layers, r.id


def test_demo_session(demo_zip, ws, settings):
    S = _session(demo_zip, ws, settings)
    st = S.stats
    assert st["files"] == 3 and st["boots"] == 2
    assert set(st["errors"]) == {"S07-2", "S05"}
    assert st["battery_min"] is not None and st["battery_max"] == 100
    assert st["sw_versions"] == ["6.40"]
    assert len(S.pose_vt) > 30 and S.map is not None
    assert [j["kind"] for j in S.jumps] == ["forward", "back", "forward"]   # RTC 초기값 → 시간 동기화
    for seq in (S.event_vts, S.tl_vt, S.tl_rt, S.pose_rt):
        assert all(a <= b for a, b in zip(seq, seq[1:]))                   # 재생 축은 단조 증가


def test_active_errors_follow_occur_release(demo_zip, ws, settings):
    S = _session(demo_zip, ws, settings)
    occ = next(e for e in S.error_events if e.data.get("code") == "S07-2" and e.data["status"] == "OCCURRED")
    rel = next(e for e in S.error_events if e.data.get("code") == "S07-2" and e.data["status"] == "RELEASED")
    assert "S07-2" in S.active_errors(occ.vt)
    assert "S07-2" not in S.active_errors(rel.vt)
    assert occ.x is not None                      # 발생 위치(로봇 위치) 기록


def test_markers_and_tracks(demo_zip, ws, settings):
    S = _session(demo_zip, ws, settings)
    assert {"target", "cliff", "tof", "low_obs", "camera", "error"} <= set(S.markers)
    assert S.track_at("robot_state", S.vt_end) == "ONSTATION"
    assert S.track_at("battery", S.vt_end).endswith("%")
    ev = S.events[len(S.events) // 2]
    assert S.vt_at_line(S.global_line(ev.fi, ev.ln)) == ev.vt


def test_dedupe_repeated_errors(demo_zip, ws, settings):
    S = _session(demo_zip, ws, settings)
    planner = [e for e in S.events if e.rule == "planner_error"]
    assert len(planner) == 1                      # 2초 간격 3회 → 30초 중복 제거

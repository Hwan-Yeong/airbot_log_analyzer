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
    assert st["versions"] == ["6.40:2.10:3.36"]                           # SoC 로 보내는 SW:FW:AI
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


def test_lidar_on_while_driving(demo_zip, ws, settings):
    """주행 중 라이다 = ON ('scan state is' 줄은 라이다가 꺼져 있을 때만 찍히므로 그것만 보면 OFF 로 오인)."""
    S = _session(demo_zip, ws, settings)
    nav = [e for e in S.events if e.text.split("(")[0].strip().endswith("→ NAVIGATION")]
    assert nav
    for e in nav:
        assert S.track_at("lidar", e.vt).startswith("ON"), e.text
    assert "OFF" in S.track_at("lidar", S.vt_end)                       # 도킹 후 끔


def test_version_from_otalog_before_first_line(demo_zip, ws, settings):
    """airbot_log 에 버전 줄이 나오기 전 구간도 otalog 마지막 OTA 완료 버전으로 표시."""
    S = _session(demo_zip, ws, settings)
    assert S.track_at("version", 0.0) == "6.40:2.10:3.36"
    assert [v for _t, v, k in S.stats["ota_versions"] if k == "ota"] == ["6.38:2.10:3.36", "6.40:2.10:3.36"]


def test_ota_during_log_changes_version(monkeypatch):
    """로그 도중 OTA 완료 → 그 시각 이후 줄부터 새 버전. RTC 초기값 줄도 이전 OTA 버전으로 채움."""
    from types import SimpleNamespace

    from airbotlog import versions
    from airbotlog.analysis.sources import epoch
    t0 = epoch(2026, 11, 1, 9, 0, 0)
    S = SimpleNamespace(tracks={}, stats={}, tl_t=[epoch(2024, 10, 14, 7, 53, 55), t0, t0 + 600, t0 + 1200],
                        tl_vt=[0.0, 1.0, 100.0, 200.0])
    monkeypatch.setattr(versions, "ota_version_events", lambda _s: [
        (t0 - 86400, "6.38:2.10:3.36", "ota"), (t0 + 300, "6.38:2.10:3.36", "current"),
        (t0 + 900, "6.40:2.10:3.36", "ota")])
    versions.apply_versions(S)
    assert S.tracks["version"] == ([0.0, 100.0, 200.0], ["6.38:2.10:3.36", "6.38:2.10:3.36", "6.40:2.10:3.36"])
    assert S.stats["versions"] == ["6.38:2.10:3.36", "6.40:2.10:3.36"]


def test_resource_tracks(demo_zip, ws, settings):
    S = _session(demo_zip, ws, settings)
    mem, _ = S.track_point("memory", S.vt_end)
    assert mem.endswith("%")
    temps = S.tracks["ap_temp"][1]
    assert temps[0].startswith("80.6°C") and "Warning" in temps[0] and "Release" in temps[-1]
    assert any(e.rule == "ap_overheat" for e in S.events)


def test_home_demo_scenario(tmp_path, ws, settings):
    """32평형 집 데모(AI 생성): 장애물 회피 2회, 문턱 단차로 S05, 충전 복귀. 로그 파일마다 AI 생성 안내 줄."""
    import io
    import tarfile
    import zipfile

    from make_demo_home import NOTICE, make_demo
    zp = make_demo(tmp_path)
    S = load_session([zp], ws, settings)
    assert set(S.stats["errors"]) == {"S05"} and S.source.memo == "AI_GENERATED_DEMO"
    assert {"target", "camera", "tof", "low_obs", "step", "error"} <= set(S.markers)
    reached = [e for e in S.events if e.rule == "goal_reached"]
    assert len(reached) == 4                                          # 목적지 1~3 + 충전 복귀 지점
    assert S.track_at("robot_state", S.vt_end) == "ONSTATION" and not S.active_errors(S.vt_end)
    with zipfile.ZipFile(zp) as zf, tarfile.open(fileobj=io.BytesIO(zf.read("amr/AMR_LOG.tar.gz"))) as tf:
        logs = [m for m in tf.getmembers() if m.name.startswith("log/airbot_log")]
        assert len(logs) == 3
        for m in logs:
            assert NOTICE in tf.extractfile(m).readline().decode("utf-8")
        assert NOTICE in zf.read("NOTICE.txt").decode("utf-8")

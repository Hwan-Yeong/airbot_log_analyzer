"""[분석] 기능: 다중 로그 수집·시각 정렬·발췌·마스킹·Claude CLI(가짜)·검증·엑셀."""
import json
import os
import re
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from airbotlog.analysis import claude as ai
from airbotlog.analysis import context, masking, request, sources
from airbotlog.loader import load_session


@pytest.fixture()
def session(demo_zip, ws, settings):
    return load_session([demo_zip], ws, settings)


def _event_t(S, needle):
    return next(e.t for e in S.events if needle in e.text)


def test_discover_sources_and_soc_offset(session):
    sset = sources.discover(session)
    assert [len(sset.ota), len(sset.journal), len(sset.dmesg)] == [1, 1, 1]
    assert [f.name for f in sset.soc] == ["soc/logs/logcat.1.gz", "soc/logs/logcat"]   # 오래된 것 먼저
    off = sources.soc_offset(session, near_t=_event_t(session, "S07-2 발생"))
    assert off.known and len(off.pairs) == 2 and abs(off.value - 3.5) < 0.01          # 데모: SoC 가 3초(+0.5 보정) 빠름


def test_context_aligns_soc_to_amr_time(session):
    t = _event_t(session, "S07-2 발생")
    ctx = context.build_context(session, "낙하 에러", {}, context.Options(anchor=t))
    soc = [ln for ln in ctx.lines if ln.rec is not None and ln.rec.src == sources.SRC_SOC
           and "code = S07-2" in ln.rec.text and "status : 1" in ln.rec.text]
    assert len(soc) == 1 and soc[0].rec.basis == "SoC환산"
    assert abs(soc[0].rec.t - (t + 0.3)) < 0.01                 # SoC 시각 − 3.5초 = AMR 기준 (+0.3초 지연)
    occ = [e for e in ctx.window_events if e.type == "error_occur" and e.code == "S07-2"]
    assert {e.src for e in occ} == {sources.SRC_AMR, sources.SRC_SOC}
    rids = [ln.rid for ln in ctx.lines if ln.rec is not None]
    assert rids == [f"R{k:05d}" for k in range(1, len(rids) + 1)]
    assert all(e.rid in ctx.rmap for e in ctx.window_events if e.rid)
    msg = ctx.user_message()
    for block in ("<request>", "<request_extract>", "<coverage>", "<events>", "<timeline>"):
        assert block in msg


def test_context_e09_episode_all_sources(session):
    dock = _event_t(session, "DOCKING → ONSTATION")
    ctx = context.build_context(session, "E09", {}, context.Options(anchor=dock + 25 * 60 + 60, before=300, after=300))
    kinds = {(e.src, e.type) for e in ctx.window_events}
    assert (sources.SRC_SOC, "error_occur") in kinds and (sources.SRC_SOC, "error_clear") in kinds
    assert {(sources.SRC_OTA, "link"), (sources.SRC_JOURNAL, "link"), (sources.SRC_DMESG, "link"),
            (sources.SRC_SOC, "link")} <= kinds
    timeouts = [e for e in ctx.window_events if e.type == "timeout"]
    assert len(timeouts) == 1 and timeouts[0].count == 6          # 20초 간격 6회 → 한 건으로 접힘
    down = [ln.rec for ln in ctx.lines if ln.rec and "Link is Down" in ln.rec.text]
    assert {r.src for r in down} == {sources.SRC_JOURNAL, sources.SRC_DMESG}
    assert abs(down[0].t - down[1].t) < 1.0                       # dmesg(경과초)를 journal 시각에 맞춤
    e09 = [e for e in ctx.summary_events if e.code == "E09"]
    assert len(e09) == 1 and e09[0].count == 1


def test_fold_and_budget(session):
    t = _event_t(session, "S07-2 발생")
    full = context.build_context(session, "", {}, context.Options(anchor=t, before=3600, after=3600))
    assert full.counts["folded"] < full.counts["window"]          # 반복 줄 접힘
    small = context.build_context(session, "", {}, context.Options(anchor=t, before=3600, after=3600, max_lines=30))
    assert small.counts["selected"] <= 30 and small.counts["dropped"] > 0
    assert any(ln.rec is None and ln.note.startswith("…생략") for ln in small.lines)
    kept = {id(ln.rec) for ln in small.lines if ln.rec is not None}
    near = [ln for ln in full.lines if ln.rec is not None and abs(ln.rec.t - t) < 2.0]
    assert near and all(any(k.rec.text == n.rec.text for k in small.lines if k.rec) for n in near)
    assert kept


def test_request_mail_and_extract(demo_zip):
    req = request.read_mail(Path(demo_zip).parent / "demo_request.eml")
    assert "홍길동" in req.names and "reporter@example.com" in req.emails
    body = request.strip_signature(req.text)
    assert "010-1234-5678" not in body and "(서명 생략)" in body
    ref = request.datetime(2026, 11, 1, 15, 30)
    ex = request.extract(body, ref, "DEMOBOTSN000000001")
    assert ex["error_codes"] == ["S07-2", "S05", "E09"]
    assert [t["parsed"] for t in ex["claimed_times"]] == ["2026-11-01 09:30", "2026-11-01 10:00"]
    assert ex["hypotheses"] and "낙하센서" in ex["hypotheses"][0]


@pytest.mark.parametrize("text,expect", [
    ("2026. 10. 05 00:52 경", ["2026-10-05 00:52"]),
    ("10월 25일 17시 03분 경 발생", ["2026-10-25 17:03"]),
    ("07/08 15시 31분 : 전원 ON", ["2026-07-08 15:31"]),
    ("2026년 9월 27일 오후 12:52", ["2026-09-27 12:52"]),
    ("약 2시간 동안 지속, 12월 30일 오후 11시", ["2025-12-30 23:00"]),
])
def test_time_patterns(text, expect):
    ref = request.datetime(2026, 11, 1, 15, 30)
    assert [h.when.strftime("%Y-%m-%d %H:%M") for h in request.find_times(text, ref)] == expect


def test_masking_roundtrip():
    m = masking.Masker(words=["데모현장"], patterns=[r"\bDEV\d{4}\b"])
    m.add_serial("DEMOBOTSN000000001")
    m.add_memo("K12345_홍길동_S07_2")
    text = ("ABCD1E23FGHIJ4567K89012 / J4567K89012 / K89012 / DEMOBOTSN000000001 / 홍길동님 010-1234-5678 "
            "a@b.co http://x.y/z?t=1 데모현장 DEV1234 | log: 0x000001 E09 S07-2 10.0.0.1 ABK890123")
    out = m.mask(text)
    for secret in ("ABCD1E23FGHIJ4567K89012", "J4567K89012", "K89012", "DEMOBOTSN", "홍길동", "010-1234-5678",
                   "a@b.co", "http://", "데모현장", "DEV1234"):
        assert not re.search(r"(?<![A-Za-z0-9])" + re.escape(secret), out), secret
    for kept in ("0x000001", "E09", "S07-2", "10.0.0.1", "ABK890123"):
        assert kept in out
    assert m.unmask(out).count("ABCD1E23FGHIJ4567K89012") == 3     # 짧은 표기도 같은 시리얼 토큰
    assert "홍길동님" in m.unmask(out)


def _valid_result(rid="R00001", ev="EV001"):
    return {"schema_version": "1.0", "summary": "S07-2 발생 후 S05",
            "request_check": {"claimed_symptom": "낙하", "claimed_time": "", "log_time": "", "match": "일치",
                              "note": "", "evidence": [rid]},
            "phenomenon_detail": [{"no": 1, "text": "① 발생", "evidence": [rid, ev]}],
            "timeline": [{"time": "2026-11-01 09:31:49.140", "time_basis": "AMR", "source": "AMR airbot_log",
                          "file_line": "airbot_log.2.txt:L1", "event": "S07-2", "interpretation": "발생",
                          "importance": "핵심", "evidence": [rid]}],
            "error_codes": [{"code": "S07-2", "name": "Cliff", "occurred": "2026-11-01 09:31:49.140", "cleared": "",
                             "end_type": "정상 해제", "count": 1, "evidence": [rid]}],
            "hypothesis_check": [], "need_more_info": [],
            "confidence": {"level": "높음", "reason": "교차 확인", "evidence_lines": [rid]}}


def test_validate_result():
    schema = ai.load_schema()
    assert ai.validate(_valid_result(), schema, {"R00001", "EV001"}) == ([], [])
    errs, _ = ai.validate(_valid_result(rid="R09999"), schema, {"R00001", "EV001"})
    assert errs and "R09999" in errs[0]
    bad = _valid_result()
    bad["request_check"]["match"] = "아마도"
    assert any("허용되지 않은 값" in e for e in ai.validate(bad, schema, {"R00001", "EV001"})[0])
    causal = _valid_result()
    causal["summary"] = "낙하센서 때문에 정지"
    assert ai.validate(causal, schema, {"R00001", "EV001"})[1]


FAKE_CLI = textwrap.dedent('''\
    #!{python}
    import json, os, re, sys, time
    args = sys.argv[1:]
    msg = sys.stdin.read()
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if log:
        sp = args[args.index("--system-prompt-file") + 1]
        json.dump({{"args": args, "cwd": os.getcwd(), "cwd_files": os.listdir("."), "system": open(sp, encoding="utf-8").read(), "stdin": msg}},
                  open(log, "w", encoding="utf-8"), ensure_ascii=False)
    if os.environ.get("FAKE_CLAUDE_MODE") == "error":
        print(json.dumps({{"type": "result", "is_error": True, "result": "Not logged in"}})); sys.exit(1)
    if os.environ.get("FAKE_CLAUDE_MODE") == "slow":
        time.sleep(30)
    if os.environ.get("FAKE_CLAUDE_MODE") == "old" and "--safe-mode" in args:
        print("error: unknown option '--safe-mode'", file=sys.stderr); sys.exit(1)
    rid = re.findall(r"R\\d{{5}}", msg)[0]
    ev = re.findall(r"EV\\d{{3}}", msg)[0]
    out = {result}
    out = json.loads(json.dumps(out).replace("R00001", rid).replace("EV001", ev))
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "",
                      "structured_output": out, "duration_ms": 12, "total_cost_usd": 0.0,
                      "modelUsage": {{"fake-model": {{}}}}, "num_turns": 1}}, ensure_ascii=False))
''')


@pytest.fixture()
def fake_cli(tmp_path):
    p = tmp_path / "claude"
    p.write_text(FAKE_CLI.format(python=sys.executable, result=repr(_valid_result())), encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


@pytest.mark.skipif(sys.platform == "win32", reason="가짜 CLI 는 shebang 스크립트")
def test_claude_runner_with_fake_cli(session, fake_cli, tmp_path, monkeypatch):
    log = tmp_path / "call.json"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    ctx = context.build_context(session, "요청", {}, context.Options(anchor=_event_t(session, "S07-2 발생")))
    runner = ai.ClaudeRunner(str(fake_cli), "SYSTEM-PROMPT", ai.load_schema())
    result, meta = runner.run(ctx.user_message())
    assert ai.validate(result, ai.load_schema(), ctx.ids)[0] == []
    call = json.loads(log.read_text(encoding="utf-8"))
    a = call["args"]
    assert a[:3] == ["-p", "--output-format", "json"] and a[a.index("--tools") + 1] == ""
    assert "--safe-mode" in a and "--no-session-persistence" in a and "--json-schema" in a
    assert call["system"] == "SYSTEM-PROMPT" and "<timeline>" in call["stdin"]
    assert call["cwd_files"] == [] and "airbotlog_ai_" in call["cwd"]   # 빈 임시 폴더에서 실행
    assert not os.path.exists(call["cwd"])                          # 끝나면 삭제
    assert meta["models"] == ["fake-model"]

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "error")
    with pytest.raises(ai.ClaudeError, match="Not logged in"):
        ai.ClaudeRunner(str(fake_cli), "S", ai.load_schema()).run("R00001 EV001")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "old")                 # 옵션을 모르는 오래된 CLI → 빼고 다시
    res_old, meta_old = ai.ClaudeRunner(str(fake_cli), "S", ai.load_schema()).run("R00001 EV001")
    assert meta_old["minimal_flags"] and res_old["summary"]
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "slow")
    with pytest.raises(ai.ClaudeError, match="시간 초과"):
        ai.ClaudeRunner(str(fake_cli), "S", ai.load_schema(), timeout=1).run("R00001 EV001")


def test_excel_export(session, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    from airbotlog.analysis import excel
    ctx = context.build_context(session, "요청", {"error_codes": ["S07-2"]},
                                context.Options(anchor=_event_t(session, "S07-2 발생")))
    first = next(ln for ln in ctx.lines if ln.rec is not None)
    res = _valid_result(rid=first.rid, ev=ctx.window_events[0].id)
    path = excel.save(tmp_path / "out.xlsx", ctx, res, {"models": ["m"]}, "요청 원문")
    wb = openpyxl.load_workbook(path)
    assert wb.sheetnames == ["요약", "타임라인", "에러 이력", "로그 커버리지·시각정렬", "원문 발췌", "요청 원문"]
    tl = wb["타임라인"]
    assert [c.value for c in tl[1]][:3] == ["No", "시각(AMR 기준)", "시각 기준"]
    assert tl["B2"].value.year == 2026 and tl["B2"].number_format == excel.TIME_FMT
    assert tl["J2"].value == first.rec.text                        # 근거 줄 원문
    labels = {r[0].value: r[1].value for r in wb["요약"].iter_rows(min_row=4) if r[0].value}
    assert labels["원인"] == "(사람 작성)" and labels["현상 (한 줄)"] == res["summary"]
    src = wb["원문 발췌"]
    assert src["A2"].value == "R00001"
    name = excel.default_name("Airbot_{serial6}_{issue}_현상분석_{date}_AI.xlsx", "DEMOBOTSN000000001", "S07-2")
    assert re.fullmatch(r"Airbot_000001_S07-2_현상분석_\d{6}_AI\.xlsx", name)

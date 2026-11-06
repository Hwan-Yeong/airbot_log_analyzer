"""메일 파일 읽기: 형식은 내용으로 판별 (Outlook .msg · .eml/.mht · .html · 텍스트), Outlook 선택 메일 가져오기."""
import base64
import json
from datetime import datetime, timezone

import pytest

from airbotlog.analysis import outlook
from airbotlog.analysis import request as areq
from msgfile import make_msg

EML = ("From: =?utf-8?b?{f}?= <reporter@example.com>\r\nTo: analyst@example.com\r\n"
       "Subject: =?utf-8?b?{s}?=\r\nDate: Mon, 03 Nov 2026 16:10:00 +0900\r\nMIME-Version: 1.0\r\n"
       "Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: 8bit\r\n\r\n{b}")


def _eml(body="낙하 에러(S07-2) 검토 부탁드립니다.\r\n\r\n감사합니다.\r\n홍길동 매니저"):
    b64 = lambda t: base64.b64encode(t.encode()).decode()     # noqa: E731
    return EML.format(f=b64("홍길동/매니저"), s=b64("[필드 이슈] 낙하 에러 검토"), b=body).encode("utf-8")


def test_eml_detected_by_content_even_with_wrong_extension(tmp_path):
    p = tmp_path / "요청.txt"
    p.write_bytes(_eml())
    req = areq.read_mail(p)
    assert req.subject == "[필드 이슈] 낙하 에러 검토" and req.text.startswith("제목: [필드 이슈] 낙하 에러 검토")
    assert "홍길동" in req.names and req.emails == ["analyst@example.com", "reporter@example.com"]


@pytest.mark.parametrize("enc", ["utf-8", "utf-8-sig", "utf-16", "cp949"])
def test_text_mail_encodings(tmp_path, enc):
    p = tmp_path / "mail.txt"
    p.write_bytes("보낸 사람: 홍길동\r\n제목: 주행 중 S05\r\n\r\n똠방각하 햏 - 확장 한글\r\n".encode(enc))
    req = areq.read_mail(p)
    assert "제목: 주행 중 S05" in req.text and "똠방각하 햏" in req.text and "\r" not in req.text


def test_korean_mail_charset_uses_cp949(tmp_path):
    """한국 Outlook 메일의 ks_c_5601-1987 표기는 CP949 로 (EUC-KR 로 읽으면 확장 한글이 깨짐)."""
    body = base64.b64encode("본문 똠방각하 햏".encode("cp949")).decode()
    raw = ("From: a@example.com\r\nSubject: test\r\nMIME-Version: 1.0\r\n"
           "Content-Type: text/plain; charset=\"ks_c_5601-1987\"\r\nContent-Transfer-Encoding: base64\r\n\r\n" + body)
    p = tmp_path / "x.eml"
    p.write_bytes(raw.encode("ascii"))
    assert "본문 똠방각하 햏" in areq.read_mail(p).text


def test_mht_and_html(tmp_path):
    mht = ("From: <Saved by Edge>\r\nSubject: =?utf-8?b?" + base64.b64encode("주행 실패 문의".encode()).decode() + "?=\r\n"
           "MIME-Version: 1.0\r\nContent-Type: multipart/related; boundary=\"B\"\r\n\r\n--B\r\n"
           "Content-Type: text/html; charset=utf-8\r\n\r\n<html><body><p>주방에서&nbsp;멈춤</p><p>S05 발생</p>"
           "</body></html>\r\n--B--\r\n")
    p = tmp_path / "a.mht"
    p.write_bytes(mht.encode("utf-8"))
    req = areq.read_mail(p)
    assert req.subject == "주행 실패 문의" and "주방에서 멈춤" in req.text and "<p>" not in req.text
    h = tmp_path / "b.html"
    h.write_bytes('<html><head><meta charset="euc-kr"></head><body>본문<br>똠방각하</body></html>'.encode("cp949"))
    assert "본문\n똠방각하" in areq.read_mail(h).text


def test_outlook_msg_plain_body(tmp_path):
    p = tmp_path / "요청.msg"
    p.write_bytes(make_msg(subject="필드 이슈 검토 요청", body="시리얼 DEMO0001\r\n똠방각하\r\n" + "본문 " * 1500,
                           sender_name="홍길동/매니저", sender_smtp="reporter@example.com", to="김분석; 이담당",
                           sent=datetime(2026, 11, 3, 7, 10, tzinfo=timezone.utc),
                           recipients=[("김분석", "kim@example.com")]))
    req = areq.read_mail(p)
    assert req.subject == "필드 이슈 검토 요청" and "똠방각하" in req.text
    assert req.text.splitlines()[1].startswith("보낸 날짜: ") and "2026" in req.text.splitlines()[1]
    assert {"홍길동", "김분석", "이담당"} <= set(req.names)
    assert req.emails == ["kim@example.com", "reporter@example.com"]


def test_outlook_msg_html_only_and_unreadable(tmp_path):
    html = '<html><head><meta charset="ks_c_5601-1987"></head><body><p>HTML 본문 똠방</p></body></html>'
    p = tmp_path / "noext"                                  # 확장자 없어도 내용으로 판별
    p.write_bytes(make_msg(subject="html", html=html.encode("cp949"), codepage=949))
    assert "HTML 본문 똠방" in areq.read_mail(p).text
    rtf_only = tmp_path / "rtf.msg"
    rtf_only.write_bytes(make_msg(subject="rtf only"))
    with pytest.raises(ValueError, match="본문을 읽을 수 없는"):
        areq.read_mail(rtf_only)


def test_unsupported_files_explain(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.7\n...")
    with pytest.raises(ValueError, match="PDF"):
        areq.read_mail(pdf)
    binary = tmp_path / "a.bin"
    binary.write_bytes(b"\x00\x01\x02" * 100)
    with pytest.raises(ValueError, match="메일 형식이 아닙니다"):
        areq.read_mail(binary)


def test_multiple_mails_joined_and_signatures_handled(tmp_path):
    a, b = tmp_path / "1.eml", tmp_path / "2.txt"
    a.write_bytes(_eml("첫 메일 본문\r\n\r\n감사합니다.\r\n홍길동"))
    b.write_bytes("두 번째 메일: 추가로 E09 도 봐주세요\r\n".encode("utf-8"))
    req = areq.read_mails([a, b])
    text = areq.strip_signature(req.text)
    assert "첫 메일 본문" in text and "두 번째 메일: 추가로 E09" in text and areq.MAIL_SEP in text


def test_is_mail_file(tmp_path):
    log = tmp_path / "airbot_log.txt"
    log.write_text("[2026-11-01 09:00:00.000] [node] [INFO] x\n")
    note = tmp_path / "notes.txt"
    note.write_text("메일 본문\n")
    other_log = tmp_path / "session.txt"
    other_log.write_text("[2026-11-01 09:00:00.000] [node] [INFO] x\n")
    msg = tmp_path / "noext"
    msg.write_bytes(make_msg(subject="s", body="b"))
    eml = tmp_path / "a.eml"
    eml.write_bytes(_eml())
    zp = tmp_path / "20261101153000_DEMOBOTSN000000001.zip"
    zp.write_bytes(b"PK\x03\x04")
    assert [areq.is_mail_file(p) for p in (log, note, other_log, msg, eml, zp, tmp_path)] == \
        [False, True, False, True, True, False, False]


# ---------------------------------------------------------------- Outlook 선택 메일
def _ps_out(**data):
    return "준비 중...\r\n" + outlook.MARK + json.dumps(data, ensure_ascii=False) + "\r\n"


def test_outlook_output_to_request():
    req, count = outlook.parse_output(_ps_out(
        count=2, subject="필드 이슈", body="본문\r\n시리얼 DEMO0001", sender="홍길동/매니저",
        sender_email="reporter@example.com", to="김분석; 이담당", cc="", recipients=["김분석 <kim@example.com>"],
        sent="2026-11-03T16:10:00|540", received=""))
    assert count == 2 and req.subject == "필드 이슈"
    assert req.text.splitlines()[:2] == ["제목: 필드 이슈", "보낸 날짜: Tue, 03 Nov 2026 16:10:00 +0900"]
    assert {"홍길동", "김분석", "이담당"} <= set(req.names) and req.emails == ["kim@example.com", "reporter@example.com"]


@pytest.mark.parametrize("err,needle", [("not_running", "실행 중이 아닙니다"), ("no_selection", "선택한 메일이 없습니다")])
def test_outlook_errors_explained(err, needle):
    with pytest.raises(outlook.OutlookError, match=needle):
        outlook.parse_output(_ps_out(error=err))
    with pytest.raises(outlook.OutlookError, match="응답을 읽지 못했습니다"):
        outlook.parse_output("PowerShell 오류")


def test_outlook_fetch_runs_powershell(monkeypatch):
    calls = []

    class R:
        stdout = _ps_out(count=1, subject="s", body="b", sender="", sender_email="", to="", cc="",
                         recipients=[], sent="", received="").encode("utf-8")

    monkeypatch.setattr(outlook, "_powershell", lambda: "powershell.exe")
    monkeypatch.setattr(outlook.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or R())
    req, count = outlook.fetch_selected()
    assert req.subject == "s" and count == 1
    assert calls[0][0] == "powershell.exe" and "-EncodedCommand" in calls[0]


# ---------------------------------------------------------------- WSL 끌어다 놓기 창 (Windows 쪽 PowerShell 창)
def test_drop_window_output(tmp_path):
    from airbotlog import windrop
    local = tmp_path / "요청.eml"
    local.write_bytes(_eml())
    nas_data = base64.b64encode(_eml("NAS 메일")).decode()
    out = "\n".join([
        "DROPFILE:" + json.dumps({"path": r"C:\Users\me\요청.eml", "name": "요청.eml"}, ensure_ascii=False),
        "DROPFILE:" + json.dumps({"path": r"\\nas\share\b.eml", "name": "b.eml", "data": nas_data}),
        "DROPFILE:" + json.dumps({"path": r"\\nas\share\big.zip", "name": "big.zip"}),
        "DROPEND"])
    res = windrop.parse_output(out, to_wsl=lambda p: str(local) if p.startswith("C:") else "")
    try:
        assert res.files[0] == str(local) and not res.outlook and res.skipped == [r"\\nas\share\big.zip"]
        assert "NAS 메일" in areq.read_mail(res.files[1]).text          # 경로를 못 바꾸면 받은 내용으로 임시 파일
    finally:
        res.cleanup()
    assert res.tmpdir is None
    assert windrop.parse_output("DROPOUTLOOK\nDROPEND").outlook


def test_drop_window_script_filled():
    from airbotlog import windrop
    plain = windrop.DropWindow("제목 '따옴표'", "안내").script()
    assert "__" not in plain and "'제목 ''따옴표'''" in plain and "TEST_EFFECT" not in plain
    test = windrop.DropWindow("t", "h", test_files=[r"C:\a.eml"], snapshot=r"C:\s.png").script()
    assert "__" not in test and "SetFileDropList" in test and "DrawToBitmap" in test

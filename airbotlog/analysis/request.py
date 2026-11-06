"""분석 요청 읽기: 구어체 문장 또는 메일 파일 → 본문 + 추출 정보.

메일 파일은 확장자보다 내용으로 형식을 판별한다: Outlook .msg, .eml/.mht(MIME), .html, 텍스트(UTF-8·UTF-16·CP949).

추출: 시리얼, 에러 코드, 언급된 발생 시각(여러 표기), 증상, 요청자 가설/질문.
메일 날짜가 틀리는 경우가 있어 시각은 '후보'로만 쓰고, 기준 시각은 사용자가 확인한다.
"""
import email
import email.utils
import html
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import HeaderParser
from pathlib import Path

CODE_RE = re.compile(r"(?<![A-Za-z0-9])([ESF]\d{2}(?:-\d)?)(?![0-9])")
SERIAL_RE = re.compile(r"\b(?:[A-Z0-9]{12})?([A-Z]\d{4}[A-Z]\d{5})\b")
ZIP_RE = re.compile(r"\b(\d{14})_([A-Za-z0-9]{8,})([^\s\\/]*?)\.(?:zip|tar\.gz|tgz)\b")
SYMPTOM_LABEL_RE = re.compile(r"^\s*[-*·•]?\s*(?:현상|증상|이상\s*증상|접수\s*내역|이슈)\s*[:：]\s*(.+)$")
HYPO_RE = re.compile(r"(의심|보입니다|것 같|추정|로 보임|원인으로)")
QUESTION_RE = re.compile(r"(\?|요청|부탁|확인\s*(?:바랍|해\s*주|부탁)|검토)")
SIGNATURE_RE = re.compile(r"^\s*(?:-{2,}|_{3,}|감사합니다|감사드립니다|Best regards|Thanks)", re.IGNORECASE)
MAIL_SEP = "=== 다음 메일 ==="                     # 여러 메일을 이어 붙일 때 (서명 생략 판정에 걸리지 않는 표기)
MAIL_EXTS = {".eml", ".msg", ".mht", ".mhtml", ".htm", ".html", ".txt"}
MAIL_FILTER = "메일 (*.eml *.msg *.mht *.mhtml *.html *.htm *.txt);;모든 파일 (*)"
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"     # Outlook .msg (OLE 복합 문서)
MIME_HEADER_RE = re.compile(rb"^(?:from|to|cc|subject|date|mime-version|content-type|received|return-path|message-id|"
                            rb"reply-to|sender|thread-topic|thread-index|delivered-to|x-[\w-]+)[ \t]*:", re.I | re.M)
KOREAN_CHARSETS = {"ks_c_5601-1987", "ks_c_5601", "ksc5601", "euc-kr", "euc_kr", "korean", "uhc", "x-windows-949"}
LOG_NAME_RE = re.compile(r"^(?:airbot_log|otalog|logcat|journal|dmesg)", re.I)

# 시각 표기들 (연도 없는 것은 수집 시각으로 연도 추정)
TIME_PATTERNS = [
    # 2026-10-25 17:03(:12) / 2026.10.25 17:03 / 2026. 10. 25 17:03
    ("ymd", re.compile(r"(20\d\d)\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})\.?[ T]*(?:(오전|오후)\s*)?(\d{1,2})\s*[:시]\s*(\d{1,2})?(?:\s*[:분]\s*(\d{1,2}))?")),
    # 2026년 9월 27일 오후 12:52 / 2026년 9월 27일 12시 52분
    ("ymd_kr", re.compile(r"(20\d\d)\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일\s*(?:\(\S\)\s*)?(오전|오후)?\s*(\d{1,2})\s*[:시]\s*(\d{1,2}|반)?\s*분?")),
    # 10월 25일 17시 03분 / 10월 25일 오후 5시 반
    ("md_kr", re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일\s*(?:\(\S\)\s*)?(오전|오후)?\s*(\d{1,2})\s*[:시]\s*(\d{1,2}|반)?\s*분?")),
    # 07/08 15시 31분 / 10/25 17:03
    ("md_slash", re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})\s+(오전|오후)?\s*(\d{1,2})\s*(?::|시(?!간))\s*(\d{1,2})?\s*분?")),
    # 10-13 15:21
    ("md_dash", re.compile(r"(?<![\d-])(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?")),
    # 날짜 없는 시각 (같은 날 10시쯤 / 오후 3시 20분) → 바로 앞에 나온 날짜(없으면 수집일)
    # 영문·숫자 바로 뒤('E09 시료', 'K00731 시')는 시각이 아님
    ("hm_kr", re.compile(r"(?<![A-Za-z0-9:])(오전|오후)?\s*(\d{1,2})\s*시(?![간료작점])\s*(?:(\d{1,2}|반)\s*분?)?")),
]
# 메일 머리글(보낸·받은 날짜) 줄의 시각은 발생 시각이 아님 (전달·회신 메일 본문에 여러 번 들어 있음)
MAIL_DATE_LINE_RE = re.compile(r"(?im)^[ \t>]*(?:보낸\s*날짜|받은\s*날짜|보낸\s*시간|Date|Sent|Received)\s*[:：].*$")


@dataclass
class TimeHint:
    when: datetime
    text: str
    approx: bool = False       # '경', '쯤', '반' 등 대략적인 표현

    def label(self) -> str:
        return f"{self.when:%Y-%m-%d %H:%M}" + (" 경" if self.approx else "") + f"  ← \"{self.text}\""


@dataclass
class Request:
    text: str                                  # 요청 본문 (메일이면 제목·보낸 사람 포함)
    subject: str = ""
    sender: str = ""
    names: list = field(default_factory=list)  # 메일 머리글의 사람 이름 (마스킹 대상)
    emails: list = field(default_factory=list)


# ---------------------------------------------------------------- 메일
def _html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", "", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s)


def _names_from_header(value: str):
    names, addrs = [], []
    for name, addr in email.utils.getaddresses([value or ""]):
        if addr and "@" not in addr and not name:      # Outlook 표시 이름만 있는 경우 ("김분석; 이담당")
            name, addr = addr, ""
        if addr:
            addrs.append(addr)
        if name:
            first = re.split(r"[/(\[<,]", name)[0].strip()
            for part in first.split():
                if re.fullmatch(r"[가-힣]{2,4}", part):
                    names.append(part)
            if re.fullmatch(r"[A-Za-z][A-Za-z .'-]{2,40}", first):
                names.append(first)
    return names, addrs


def _charset(name: str) -> str:
    n = (name or "").strip().strip('"').lower()
    return "cp949" if n in KOREAN_CHARSETS else n          # 한국어 메일 표기(ks_c_5601-1987 등)는 CP949 로 (확장 한글 포함)


def _decode(data: bytes, *charsets) -> str:
    """BOM → 지정 문자셋 → UTF-8 → CP949 순으로 엄격하게 시도, 모두 안 되면 UTF-8(깨진 글자 대체)."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    for cs in [*(_charset(c) for c in charsets if c), "utf-8", "cp949"]:
        try:
            return data.decode(cs)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", "replace")


def _tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\xa0", " ")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def detect_kind(data: bytes, suffix: str = "") -> str:
    """메일 파일 형식: msg | mime | html | text | pdf | binary (확장자보다 내용 우선)."""
    if data.startswith(OLE_MAGIC):
        return "msg"
    if data.startswith(b"%PDF"):
        return "pdf"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):          # UTF-16 텍스트 (메모장 '유니코드')
        return "text"
    head = data[:8192].lstrip(b"\xef\xbb\xbf \t\r\n")
    if re.match(rb"(?i)<(?:!doctype\s+html|html|head|body|meta)\b", head):
        return "html"
    block = re.split(rb"\r?\n\r?\n", head, maxsplit=1)[0]
    if MIME_HEADER_RE.match(block) and len(MIME_HEADER_RE.findall(block)) >= 2:
        return "mime"
    if suffix in (".eml", ".mht", ".mhtml"):
        return "mime"
    if suffix in (".htm", ".html"):
        return "html"
    if b"\x00" in head[:2048] or head[:2] == b"PK":
        return "binary"
    return "text"


def is_mail_file(path) -> bool:
    """메인 창에 끌어다 놓은 파일이 (로그가 아니라) 메일인지."""
    p = Path(path)
    if not p.is_file():
        return False
    suffix = p.suffix.lower()
    if suffix in (".zip", ".tar", ".gz", ".tgz", ".log") or LOG_NAME_RE.match(p.name):
        return False
    try:
        with open(p, "rb") as f:
            head = f.read(8192)
    except OSError:
        return False
    if suffix == ".txt" and re.match(rb"\s*\[20\d\d-\d\d-\d\d ", head):   # 로그 줄 형식이면 로그
        return False
    kind = detect_kind(head, suffix)
    return kind in ("msg", "mime", "html") or (kind == "text" and suffix in MAIL_EXTS)


def read_mail(path) -> Request:
    """메일 파일 → Request. 형식은 내용으로 판별 (Outlook .msg · .eml/.mht · .html · 텍스트)."""
    path = Path(path)
    data = path.read_bytes()
    kind = detect_kind(data, path.suffix.lower())
    if kind == "msg":
        return _read_msg(data)
    if kind == "mime":
        return _read_mime(data)
    if kind == "html":
        return Request(text=_tidy(_html_to_text(_decode(data, _html_charset(data)))))
    if kind == "pdf":
        raise ValueError(f"PDF 는 읽을 수 없습니다: {path.name}\n메일을 .eml / .msg 로 저장하거나 본문을 복사해 붙여 넣으세요.")
    if kind == "binary":
        raise ValueError(f"메일 형식이 아닙니다: {path.name}\n지원: .eml .msg .mht .html .txt (또는 본문 붙여넣기)")
    return Request(text=_tidy(_decode(data)))


def read_mails(paths) -> Request:
    """메일 여러 개 → 하나로 (본문을 이어 붙이고 이름·주소는 합침)."""
    reqs = [read_mail(p) for p in paths]
    if len(reqs) == 1:
        return reqs[0]
    return Request(text=f"\n\n{MAIL_SEP}\n\n".join(r.text for r in reqs), subject=reqs[0].subject,
                   sender=reqs[0].sender, names=sorted({n for r in reqs for n in r.names}),
                   emails=sorted({e for r in reqs for e in r.emails}))


def _mail_text(subject, date, body) -> str:
    return f"제목: {subject}\n보낸 날짜: {date}\n\n{_tidy(body)}"


def _people(sender, *lists):
    """보낸 사람·받는 사람 표시 문자열들 → (이름, 주소). Outlook 은 ';' 로 구분."""
    return _names_from_header(", ".join(x.replace(";", ",") for x in (sender, *lists) if x))


def _part_text(part) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        try:
            return part.get_content()
        except (LookupError, UnicodeDecodeError, KeyError):
            return ""
    return _decode(payload, part.get_content_charset() or "")


def _read_mime(data: bytes) -> Request:
    msg = email.message_from_bytes(data, policy=policy.default)
    body = msg.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        text = _part_text(body)
        if body.get_content_type() == "text/html":
            text = _html_to_text(text)
    names, addrs = [], []
    for h in ("From", "To", "Cc"):
        n, a = _names_from_header(str(msg.get(h, "")))
        names += n
        addrs += a
    subject = str(msg.get("Subject", ""))
    return Request(text=_mail_text(subject, msg.get("Date", ""), text), subject=subject, sender=str(msg.get("From", "")),
                   names=sorted(set(names)), emails=sorted(set(addrs)))


def _html_charset(data: bytes) -> str:
    m = re.search(rb"""(?i)<meta[^>]+charset\s*=\s*["']?([\w-]+)""", data[:8192])
    return m.group(1).decode("ascii", "ignore") if m else ""


def _filetime(value: int) -> str:
    """MAPI 시각(FILETIME, UTC) → 메일 Date 머리글 형식 (이 PC 시간대)."""
    if not value:
        return ""
    dt = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=value // 10)
    return email.utils.format_datetime(dt.astimezone())


def _read_msg(data: bytes) -> Request:
    """Outlook .msg (OLE 복합 문서, MAPI 속성) → Request. 본문: 텍스트 → HTML 순 (압축 RTF 만 있는 메일은 미지원)."""
    try:
        import olefile
    except ImportError as e:
        raise ValueError("Outlook .msg 를 읽는 패키지(olefile)가 없습니다.\n"
                         "run.bat / run.sh 를 다시 실행하면 자동 설치됩니다.") from e
    try:
        ole = olefile.OleFileIO(io.BytesIO(data))
    except OSError as e:
        raise ValueError(f"Outlook .msg 를 열 수 없습니다: {e}") from e
    with ole:
        def stream(name):
            return ole.openstream(name).read() if ole.exists(name) else None

        props = {}
        raw = stream("__properties_version1.0") or b""
        for off in range(32, len(raw) - 15, 16):          # 최상위 메시지: 머리 32바이트 뒤 16바이트씩
            tag = int.from_bytes(raw[off:off + 4], "little")
            props[tag] = raw[off + 8:off + 16]
        cp = int.from_bytes(props.get(0x3FFD0003, props.get(0x3FDE0003, b""))[:4] or b"\0", "little")
        ansi = "utf-8" if cp == 65001 else (f"cp{cp}" if cp else "cp949")

        def prop(pid, where=""):
            b = stream(f"{where}__substg1.0_{pid}001F")
            if b is not None:
                return b.decode("utf-16-le", "replace").rstrip("\x00")
            b = stream(f"{where}__substg1.0_{pid}001E")
            return _decode(b, ansi).rstrip("\x00") if b is not None else ""

        subject = prop("0037")
        body = prop("1000")
        if not body.strip():
            h = stream("__substg1.0_10130102")
            body = _html_to_text(_decode(h, _html_charset(h), ansi)) if h else _html_to_text(prop("1013"))
        headers = HeaderParser().parsestr(prop("007D")) if prop("007D") else {}
        sender_name = prop("0C1A") or prop("0042")
        sender_addr = next((a for a in (prop("5D01"), prop("0C1F"), prop("0065"), prop("5D02")) if "@" in a), "")
        sender = f"{sender_name} <{sender_addr}>" if sender_addr else sender_name
        names, addrs = _people(sender, prop("0E04"), prop("0E03"), headers.get("From", "") if headers else "",
                               headers.get("To", "") if headers else "", headers.get("Cc", "") if headers else "")
        for entry in ole.listdir(streams=False, storages=True):
            if len(entry) == 1 and entry[0].startswith("__recip_version1.0_"):
                where = entry[0] + "/"
                addr = next((a for a in (prop("39FE", where), prop("3003", where)) if "@" in a), "")
                n, a = _people(f"{prop('3001', where)} <{addr}>" if addr else prop("3001", where))
                names += n
                addrs += a
        date = (headers.get("Date", "") if headers else "") or \
            _filetime(int.from_bytes(props.get(0x00390040, props.get(0x0E060040, b"")) or b"\0", "little"))
    if not body.strip():
        raise ValueError("이 Outlook 메일은 본문을 읽을 수 없는 형식입니다 (서식 있는 텍스트만 저장된 메일).\n"
                         "Outlook 에서 본문을 복사해 붙여 넣으세요.")
    return Request(text=_mail_text(subject, date, body), subject=subject, sender=sender,
                   names=sorted(set(names)), emails=sorted(set(addrs)))


def strip_signature(text: str) -> str:
    """서명(감사합니다/-- 이후 연락처 블록)은 분석에 필요 없어 줄임."""
    lines = text.splitlines()
    out = []
    skipping = False
    for line in lines:
        if SIGNATURE_RE.match(line):
            skipping = True
            out.append("(서명 생략)")
            continue
        if skipping and re.match(r"^\s*(?:From|보낸 사람|-----Original|> ?From|발신|On .* wrote|=== 다음 메일)", line):
            skipping = False
        if not skipping:
            out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------- 추출
def _hour(ampm, h):
    h = int(h)
    if ampm == "오후" and h < 12:
        h += 12
    if ampm == "오전" and h == 12:
        h = 0
    return h


def _year_for(month, day, ref: datetime) -> int:
    y = ref.year
    try:
        cand = datetime(y, month, day)
    except ValueError:
        return y
    return y - 1 if cand > ref + timedelta(days=2) else y


def find_times(text: str, ref: datetime) -> list:
    """요청 문장에서 시각 후보 (중복·겹침 제거, 등장 순서). 메일 머리글의 보낸·받은 날짜 줄은 뺀다."""
    found = []
    spans = [m.span() for m in MAIL_DATE_LINE_RE.finditer(text)]
    for kind, rx in TIME_PATTERNS:
        for m in rx.finditer(text):
            if any(a < m.end() and m.start() < b for a, b in spans):
                continue
            g = m.groups()
            try:
                if kind in ("ymd", "ymd_kr"):
                    y, mo, d = int(g[0]), int(g[1]), int(g[2])
                    if kind == "ymd":
                        ampm, h, mi = g[3], g[4], g[5]
                    else:
                        ampm, h, mi = g[3], g[4], g[5]
                elif kind in ("md_kr", "md_slash"):
                    mo, d, ampm, h, mi = int(g[0]), int(g[1]), g[2], g[3], g[4]
                    y = _year_for(mo, d, ref)
                elif kind == "md_dash":
                    mo, d, h, mi = int(g[0]), int(g[1]), g[2], g[3]
                    ampm = None
                    y = _year_for(mo, d, ref)
                else:
                    prev = [w for p, w in found if p < m.start()]
                    base = prev[-1].when if prev else ref
                    y, mo, d = base.year, base.month, base.day
                    ampm, h, mi = g[0], g[1], g[2]
                minute = 30 if mi == "반" else int(mi or 0)
                when = datetime(y, mo, d, _hour(ampm, h), minute)
            except (TypeError, ValueError):
                continue
            tail = text[m.end():m.end() + 4]
            approx = mi in (None, "", "반") or bool(re.match(r"\s*분?\s*(경|쯤|정도|즈음)", tail))
            spans.append((m.start(), m.end()))
            found.append((m.start(), TimeHint(when, m.group(0).strip(), approx)))
    found.sort(key=lambda x: x[0])
    out, seen = [], set()
    for _pos, h in found:
        if h.when not in seen:
            seen.add(h.when)
            out.append(h)
    return out


def extract(text: str, ref: datetime, known_serial: str = "") -> dict:
    """request_extract (Claude 입력용 JSON) - 값은 원문 그대로, 마스킹은 나중에 한꺼번에."""
    serials = sorted({m.group(0) for m in SERIAL_RE.finditer(text)} | ({known_serial} if known_serial else set()))
    codes = []
    for c in CODE_RE.findall(text):
        if c not in codes:
            codes.append(c)
    times = find_times(text, ref)
    symptom = ""
    hypotheses, questions = [], []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        m = SYMPTOM_LABEL_RE.match(s)
        if m and not symptom:
            symptom = m.group(1).strip()
        if HYPO_RE.search(s) and len(hypotheses) < 8:
            hypotheses.append(s[:200])
        elif QUESTION_RE.search(s) and len(questions) < 8 and not s.startswith("제목"):
            questions.append(s[:200])
    if not symptom:
        subj = re.search(r"^제목:\s*(.+)$", text, re.MULTILINE)
        base = subj.group(1) if subj else next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        symptom = re.sub(r"\[[^\]]*\]\s*", "", base).strip()[:200]
    zips = [m.group(0) for m in ZIP_RE.finditer(text)]
    return {
        "serials": serials,
        "sample_files": zips,
        "error_codes": codes,
        "claimed_times": [{"text": h.text, "parsed": h.when.strftime("%Y-%m-%d %H:%M"), "approx": h.approx}
                          for h in times],
        "symptom": symptom,
        "hypotheses": hypotheses,
        "questions": questions,
        "collected": ref.strftime("%Y-%m-%d %H:%M:%S"),
    }

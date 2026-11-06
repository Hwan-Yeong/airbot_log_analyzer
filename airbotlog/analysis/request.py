"""분석 요청 읽기: 구어체 문장 또는 메일(.eml / .txt / .msg) → 본문 + 추출 정보.

추출: 시리얼, 에러 코드, 언급된 발생 시각(여러 표기), 증상, 요청자 가설/질문.
메일 날짜가 틀리는 경우가 있어 시각은 '후보'로만 쓰고, 기준 시각은 사용자가 확인한다.
"""
import email
import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email import policy
from pathlib import Path

CODE_RE = re.compile(r"(?<![A-Za-z0-9])([ESF]\d{2}(?:-\d)?)(?![0-9])")
SERIAL_RE = re.compile(r"\b(?:[A-Z0-9]{12})?([A-Z]\d{4}[A-Z]\d{5})\b")
ZIP_RE = re.compile(r"\b(\d{14})_([A-Za-z0-9]{8,})([^\s\\/]*?)\.(?:zip|tar\.gz|tgz)\b")
SYMPTOM_LABEL_RE = re.compile(r"^\s*[-*·•]?\s*(?:현상|증상|이상\s*증상|접수\s*내역|이슈)\s*[:：]\s*(.+)$")
HYPO_RE = re.compile(r"(의심|보입니다|것 같|추정|로 보임|원인으로)")
QUESTION_RE = re.compile(r"(\?|요청|부탁|확인\s*(?:바랍|해\s*주|부탁)|검토)")
SIGNATURE_RE = re.compile(r"^\s*(?:-{2,}|_{3,}|감사합니다|감사드립니다|Best regards|Thanks)", re.IGNORECASE)

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
    ("hm_kr", re.compile(r"(?<![\d:])(오전|오후)?\s*(\d{1,2})\s*시(?!간)\s*(?:(\d{1,2}|반)\s*분?)?")),
]


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


def read_mail(path) -> Request:
    """.eml / .txt / .msg(extract_msg 설치 시) → Request."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".msg":
        try:
            import extract_msg  # type: ignore
        except ImportError as e:
            raise ValueError("Outlook .msg 는 지원 패키지(extract-msg)가 없어 열 수 없습니다.\n"
                             "Outlook 에서 '다른 이름으로 저장 → .eml' 하거나 본문을 복사해 붙여 넣으세요.") from e
        m = extract_msg.Message(str(path))
        names, addrs = _names_from_header(f"{m.sender}, {m.to}, {m.cc or ''}")
        body = m.body or _html_to_text(m.htmlBody.decode("utf-8", "replace") if m.htmlBody else "")
        text = f"제목: {m.subject}\n보낸 날짜: {m.date}\n\n{body}"
        return Request(text=text, subject=m.subject or "", sender=m.sender or "", names=names, emails=addrs)
    raw = path.read_bytes()
    if suffix != ".eml" and not raw[:2000].lower().count(b"subject:"):
        for enc in ("utf-8", "cp949"):
            try:
                return Request(text=raw.decode(enc))
            except UnicodeDecodeError:
                continue
        return Request(text=raw.decode("utf-8", "replace"))
    msg = email.message_from_bytes(raw, policy=policy.default)
    body = msg.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        try:
            text = body.get_content()
        except (LookupError, UnicodeDecodeError):
            text = body.get_payload(decode=True).decode("utf-8", "replace")
        if body.get_content_type() == "text/html":
            text = _html_to_text(text)
    names, addrs = [], []
    for h in ("From", "To", "Cc"):
        n, a = _names_from_header(str(msg.get(h, "")))
        names += n
        addrs += a
    subject = str(msg.get("Subject", ""))
    head = f"제목: {subject}\n보낸 날짜: {msg.get('Date', '')}\n"
    return Request(text=head + "\n" + text.strip(), subject=subject, sender=str(msg.get("From", "")),
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
        if skipping and re.match(r"^\s*(?:From|보낸 사람|-----Original|> ?From|발신|On .* wrote)", line):
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
    """요청 문장에서 시각 후보 (중복·겹침 제거, 등장 순서)."""
    found, spans = [], []
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

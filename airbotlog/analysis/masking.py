"""전송 전 마스킹과 응답 후 복원.

같은 값은 항상 같은 토큰(SN_01, NAME_01, PHONE_01 …)으로 바꾸고, 원래 값은 이 PC 에만 둔다.
- 시리얼(세션 시리얼, 23자리/11자리 형식, 그 끝 6자리), 압축 이름 메모(현장명·이름 등)
- 메일 머리글의 이름·주소, 전화번호, 이메일, URL
- 사용자 지정 단어(설정 analysis.mask_words)와 사용자 지정 정규식(analysis.mask_patterns)
IP·노드명·에러 코드·SW 버전은 분석에 필요해 그대로 둔다.
"""
import re
from collections import OrderedDict

SERIAL_LONG_RE = re.compile(r"\b[A-Z0-9]{12}[A-Z]\d{4}[A-Z]\d{5}\b")
SERIAL_SHORT_RE = re.compile(r"\b[A-Z]\d{4}[A-Z]\d{5}\b")
PHONE_RE = re.compile(r"(?<!\d)(?:\+82[- ]?)?(?:01[016789][- .]?\d{3,4}[- .]?\d{4}|0\d{1,2}[- .)]\d{3,4}[- .]\d{4})(?!\d)")
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
URL_RE = re.compile(r"\bhttps?://[^\s<>\"']+")
TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])(?:SN|MEMO|NAME|PHONE|EMAIL|URL|WORD|ID)_\d{2,3}(?![0-9])")


class Masker:
    def __init__(self, words=(), patterns=()):
        self.map = OrderedDict()        # 원래 값 → 토큰
        self.kinds = {}                 # 토큰 → 종류 설명
        self.counts = {}
        self.words = [w for w in (words or []) if w and len(w.strip()) >= 2]
        self.patterns = []
        for p in patterns or []:
            try:
                self.patterns.append(re.compile(p))
            except re.error:
                pass

    # ------------------------------------------------------------ 등록
    def token(self, value: str, kind: str, label: str) -> str:
        value = value.strip()
        if not value:
            return value
        if value in self.map:
            return self.map[value]
        n = self.counts.get(kind, 0) + 1
        self.counts[kind] = n
        tok = f"{kind}_{n:02d}"
        self.map[value] = tok
        self.kinds[tok] = label
        return tok

    def add_serial(self, serial: str):
        if not serial:
            return
        tok = self.token(serial, "SN", "시리얼")
        m = re.search(r"[A-Z]\d{4}[A-Z]\d{5}$", serial)
        if m:                                       # 짧은 시리얼(11자리)·끝 6자리(K12345 형식)도 같은 토큰
            self.map.setdefault(m.group(0), tok)
            self.map.setdefault(serial[-6:], tok)

    def add_memo(self, memo: str):
        for part in re.split(r"[_()\[\]\s]+", memo or ""):
            if re.fullmatch(r"[A-Z]\d{5}|[A-Z]\d{4}[A-Z]\d{5}", part):     # 짧은 시리얼
                self.add_serial(part)
            elif len(part) >= 2 and not re.fullmatch(r"[A-Z]\d{2}(?:-\d)?|\d+", part):
                self.token(part, "MEMO", "압축 이름 메모(현장명·이름 등)")

    def add_names(self, names):
        for n in names or []:
            if len(n) >= 2:
                self.token(n, "NAME", "이름")

    def add_emails(self, emails):
        for e in emails or []:
            self.token(e, "EMAIL", "이메일")

    # ------------------------------------------------------------ 적용
    def mask(self, text: str) -> str:
        if not text:
            return text
        for w in self.words:
            self.token(w, "WORD", "지정 단어")
        for rx, kind, label in ((URL_RE, "URL", "링크"), (EMAIL_RE, "EMAIL", "이메일"),
                                (SERIAL_LONG_RE, "SN", "시리얼"), (PHONE_RE, "PHONE", "전화번호")):
            for m in rx.finditer(text):
                if kind == "SN":
                    self.add_serial(m.group(0))
                else:
                    self.token(m.group(0), kind, label)
        for m in SERIAL_SHORT_RE.finditer(text):
            if m.group(0) not in self.map:
                self.add_serial(m.group(0))
        for rx in self.patterns:
            for m in rx.finditer(text):
                self.token(m.group(0), "ID", "지정 패턴")
        # 긴 값부터 치환 (짧은 값이 긴 값의 일부를 먼저 바꾸지 않도록)
        values = sorted(self.map, key=len, reverse=True)
        if not values:
            return text
        # 영문·숫자 값은 앞뒤가 영문·숫자가 아닐 때만 (K12345 가 다른 문자열 일부일 때 바꾸지 않음)
        rx = re.compile("(?<![A-Za-z0-9])(?:" + "|".join(re.escape(v) for v in values) + ")(?![A-Za-z0-9])")
        return rx.sub(lambda m: self.map[m.group(0)], text)

    def unmask(self, obj):
        """응답(JSON 값들)의 토큰을 원래 값으로 (토큰마다 대표값 하나)."""
        rev = {}
        for value, tok in self.map.items():
            if tok not in rev or len(value) > len(rev[tok]):
                rev[tok] = value
        if isinstance(obj, str):
            return TOKEN_RE.sub(lambda m: rev.get(m.group(0), m.group(0)), obj)
        if isinstance(obj, list):
            return [self.unmask(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self.unmask(v) for k, v in obj.items()}
        return obj

    def table(self):
        """미리보기용 (토큰, 종류, 원래 값) 목록."""
        seen = OrderedDict()
        for value, tok in self.map.items():
            seen.setdefault(tok, []).append(value)
        return [(tok, self.kinds.get(tok, ""), " / ".join(vals)) for tok, vals in seen.items()]

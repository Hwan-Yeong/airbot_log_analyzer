"""분석용 다중 로그 소스: airbot_log(세션) + otalog · journal/dmesg 스냅샷 · SoC logcat.

모든 시각은 'AMR 기준 시각'(airbot_log 와 같은 naive epoch)으로 맞춘다.
- otalog, journal : AMR 시스템 시계. journal 은 연도가 없어 수집 시각 기준으로 연도를 붙인다.
- dmesg           : 부팅 후 경과초 → 같은 스냅샷 journal 의 같은 커널 줄과 맞춰 절대 시각으로 환산.
- SoC logcat      : SoC 시계(연도 없음). SoC−AMR 시계 차이(offset)를 빼서 AMR 기준으로 환산.
  offset 은 AMR 로그의 'reqSetTimeData'(SoC 가 보낸 그 순간의 SoC 시각)와 그 줄의 AMR 시각 차이로 구한다.

원본은 읽기만 한다. SoC 로그는 압축(zip) 안에서 바로 읽고(.gz 포함) 따로 풀지 않는다.
"""
import calendar
import gzip
import re
import statistics
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

SRC_AMR = "AMR airbot_log"
SRC_OTA = "AMR otalog"
SRC_JOURNAL = "AMR journal"
SRC_DMESG = "AMR dmesg"
SRC_SOC = "SoC logcat"
SOURCE_KEYS = {"amr": SRC_AMR, "ota": SRC_OTA, "journal": SRC_JOURNAL, "dmesg": SRC_DMESG, "soc": SRC_SOC}

MONTHS = {m: i + 1 for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split())}
AMR_RE = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?)\] \[([^\]]*)\] \[([A-Z]+)\] ?(.*)")
OTA_RE = re.compile(r"^\[(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)\] \[(\w+)\] ?(.*)")
JOURNAL_RE = re.compile(r"^([A-Z][a-z]{2}) +(\d{1,2}) (\d\d):(\d\d):(\d\d) (\S+) ([^:\[]+?)(?:\[(\d+)\])?: ?(.*)")
DMESG_RE = re.compile(r"^\[ *(\d+\.\d+)\] ?(.*)")
LOGCAT_RE = re.compile(r"^(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)\.(\d{3}) +(\d+) +(\d+) ([VDIWEFS]) ([^:]*?) *: ?(.*)")
SYNC_RE = re.compile(r"reqSetTimeData year ?: ?(\d+), month ?: ?(\d+), day ?: ?(\d+), hour ?: ?(\d+), "
                     r"minute ?: ?(\d+), second ?: ?(\d+)")
LOGCAT_NAME_RE = re.compile(r"(?:^|/)logcat(?:\.(\d+))?(?:\.gz)?$")
SYNC_MAX_SEC = 600.0          # 이보다 큰 차이는 AMR 시계가 아직 안 맞춰진 것(RTC 초기값)으로 보고 버림
SYNC_NEAR_SEC = 12 * 3600     # 분석 구간 근처(±12시간)의 기준점을 우선 사용


def _noop(*_args, **_kwargs):
    pass


def epoch(y, mo, d, h, mi, s, ms=0.0) -> float:
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0)) + ms


def to_dt(t: float) -> datetime:
    return datetime(1970, 1, 1) + timedelta(seconds=t)


def year_for(month: int, day: int, ref: datetime) -> int:
    """연도 없는 날짜(MM-DD)에 연도를 붙인다: 수집 시각(ref)보다 이틀 넘게 미래면 전년도."""
    y = ref.year
    try:
        cand = datetime(y, month, day)
    except ValueError:
        return y
    return y - 1 if cand > ref + timedelta(days=2) else y


@dataclass(slots=True)
class Rec:
    t: float              # AMR 기준 시각 (naive epoch)
    src: str              # SRC_*
    file: str             # 표시용 파일 경로
    line: int             # 1부터
    text: str             # 원문 한 줄
    tag: str = ""         # 노드 / 태그 / 프로세스
    level: str = ""
    msg: str = ""         # 시각·태그를 뺀 본문
    raw_t: str = ""       # 원본 시각 표기 (SoC 시각, dmesg 경과초 등)
    basis: str = "AMR"    # AMR | SoC환산 | SoC원본 | stale | 추정


@dataclass
class FileRef:
    src: str
    name: str                 # 표시용 (예 soc/logs/logcat.3.gz)
    open: object              # () -> 바이너리 스트림
    age: int = 0              # 클수록 오래됨 (logcat.N, otalog_시각 정렬용)
    snapshot: str = ""        # journal/dmesg 스냅샷 번호


@dataclass
class FileStat:
    src: str
    name: str
    first: float = None
    last: float = None
    lines: int = 0
    note: str = ""


@dataclass
class Offset:
    value: float = 0.0        # SoC − AMR (초). AMR 기준 = SoC 시각 − value
    known: bool = False
    pairs: list = field(default_factory=list)   # [(AMR 시각, 차이)]
    used: int = 0
    spread: float = 0.0
    method: str = ""

    def describe(self) -> str:
        if not self.known:
            return "SoC↔AMR 시계 차이 미확인 (기준점 없음) → SoC 행은 SoC 원본 시각"
        return (f"SoC 시각 ≈ AMR 시각 {self.value:+.1f}초 (기준점 {self.used}개, 산포 {self.spread:.1f}초, {self.method})"
                " → SoC 행은 이 값을 빼서 AMR 기준으로 환산")


# ---------------------------------------------------------------- 소스 찾기
def _opener_path(path: Path, gz: bool):
    return (lambda: gzip.open(path, "rb")) if gz else (lambda: open(path, "rb"))


def _opener_zip(zpath: Path, member: str, gz: bool):
    def op():
        zf = zipfile.ZipFile(zpath)
        raw = zf.open(member)
        return gzip.GzipFile(fileobj=raw) if gz else raw
    return op


def _logcat_age(name: str) -> int:
    m = LOGCAT_NAME_RE.search(name)
    return int(m.group(1)) if m and m.group(1) else 0


@dataclass
class SourceSet:
    ref: datetime                                  # 연도 추론 기준 (수집 시각)
    ota: list = field(default_factory=list)        # FileRef
    journal: list = field(default_factory=list)
    dmesg: list = field(default_factory=list)
    soc: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def available(self) -> dict:
        return {"amr": True, "ota": bool(self.ota), "journal": bool(self.journal), "dmesg": bool(self.dmesg),
                "soc": bool(self.soc)}


def reference_time(session) -> datetime:
    c = getattr(session.source, "collected", "") if session.source else ""
    if c:
        try:
            return datetime.strptime(c, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    if session.tl_t:
        return to_dt(max(session.tl_t))
    return datetime.now()


def _find_ota_dir(log_dir: Path, roots) -> Path:
    if log_dir and (log_dir / "airbot_ota").is_dir():
        return log_dir / "airbot_ota"
    for r in roots:
        for d in r.rglob("airbot_ota"):
            if d.is_dir():
                return d
    return None


def discover(session) -> SourceSet:
    """세션이 불러온 입력에서 분석에 쓸 수 있는 다른 로그를 찾는다 (원본은 읽기만)."""
    src = session.source
    extras = getattr(src, "extras", {}) or {}
    roots = [Path(p) for p in extras.get("roots", []) if Path(p).exists()]
    archives = [Path(p) for p in extras.get("archives", []) if Path(p).is_file()]
    log_dir = session.files[0].path.parent if session.files else None
    out = SourceSet(ref=reference_time(session))

    ota_dir = _find_ota_dir(log_dir, roots)
    if ota_dir:
        olds = sorted(ota_dir.glob("otalog_*.log"))
        for k, p in enumerate(olds):
            out.ota.append(FileRef(SRC_OTA, f"airbot_ota/{p.name}", _opener_path(p, False), age=len(olds) - k))
        if (ota_dir / "otalog.log").is_file():
            out.ota.append(FileRef(SRC_OTA, "airbot_ota/otalog.log", _opener_path(ota_dir / "otalog.log", False), 0))
        snap_root = ota_dir / "network_system_logs"
        if snap_root.is_dir():
            for d in sorted((p for p in snap_root.iterdir() if p.is_dir()), key=lambda p: p.name):
                for kind, lst in (("journal", out.journal), ("dmesg", out.dmesg)):
                    f = d / f"{kind}.log"
                    if f.is_file():
                        lst.append(FileRef(SRC_JOURNAL if kind == "journal" else SRC_DMESG,
                                           f"network_system_logs/{d.name}/{kind}.log", _opener_path(f, False),
                                           snapshot=d.name))

    # SoC logcat: 풀린 폴더 → 원본 압축(zip) 순서로 찾음
    seen = set()
    search_dirs = list(roots)
    if log_dir:
        search_dirs += [p for p in list(log_dir.parents)[:4]]
    for r in search_dirs:
        for p in r.rglob("logcat*") if r.is_dir() else []:
            if not p.is_file() or "soc" not in p.as_posix().lower() or not LOGCAT_NAME_RE.search(p.as_posix()):
                continue
            key = p.name
            if key in seen:
                continue
            seen.add(key)
            out.soc.append(FileRef(SRC_SOC, f"soc/logs/{p.name}", _opener_path(p, p.suffix == ".gz"),
                                   age=_logcat_age(p.name)))
    for z in archives:
        if not zipfile.is_zipfile(z):
            continue
        try:
            with zipfile.ZipFile(z) as zf:
                names = zf.namelist()
        except (OSError, zipfile.BadZipFile):
            continue
        for n in names:
            if n.endswith("/") or "soc/" not in n.lower() or not LOGCAT_NAME_RE.search(n):
                continue
            base = n.rsplit("/", 1)[-1]
            if base in seen:
                continue
            seen.add(base)
            out.soc.append(FileRef(SRC_SOC, f"soc/logs/{base}", _opener_zip(z, n, n.endswith(".gz")),
                                   age=_logcat_age(base)))
    out.soc.sort(key=lambda f: -f.age)          # 오래된 것 먼저
    out.ota.sort(key=lambda f: -f.age)
    if not out.soc:
        out.notes.append("SoC logcat 없음 (입력에 soc/logs 가 없거나 AMR_LOG 만 불러옴)")
    if not out.ota:
        out.notes.append("otalog 없음")
    return out


# ---------------------------------------------------------------- 시계 차이
def soc_offset(session, near_t: float = None) -> Offset:
    """AMR 로그의 reqSetTimeData(SoC 시각) − 그 줄의 AMR 시각."""
    pairs = []
    for lf in session.files:
        for line in lf.lines:
            if "reqSetTimeData" not in line:
                continue
            m, h = SYNC_RE.search(line), AMR_RE.match(line)
            if not (m and h):
                continue
            ts = h.group(1)
            try:
                a = epoch(int(ts[0:4]), int(ts[5:7]), int(ts[8:10]), int(ts[11:13]), int(ts[14:16]), int(ts[17:19]),
                          float("0" + ts[19:]) if len(ts) > 19 else 0.0)
                p = epoch(*(int(v) for v in m.groups()))
            except ValueError:
                continue
            d = p - a
            if abs(d) <= SYNC_MAX_SEC:
                pairs.append((a, d))
    off = Offset(pairs=pairs)
    if not pairs:
        return off
    # 시계 차이는 가동 기간에 따라 변함 → 분석 구간에 가까운 기준점 우선 (±12시간, 없으면 가장 가까운 3개)
    ranked = sorted(pairs, key=lambda p: abs(p[0] - near_t) if near_t is not None else 0.0)
    near = [p for p in ranked if near_t is None or abs(p[0] - near_t) <= SYNC_NEAR_SEC] or ranked[:3]
    use = [d for _a, d in near]
    off.value = statistics.median(use) + 0.5     # 페이로드는 초 단위(내림) → 평균 0.5초 보정
    off.known, off.used = True, len(use)
    off.spread = max(use) - min(use) if len(use) > 1 else 0.0
    gap_h = abs(near[0][0] - near_t) / 3600 if near_t is not None else 0.0
    off.method = (f"AMR 'reqSetTimeData'(SoC 시각) − AMR 기록 시각, 분석 구간에서 가장 가까운 기준점 {gap_h:.1f}시간 거리"
                  if near_t is not None else "AMR 'reqSetTimeData'(SoC 시각) − AMR 기록 시각, 중앙값")
    return off


# ---------------------------------------------------------------- 읽기
def _lines(ref: FileRef):
    with ref.open() as f:
        for i, raw in enumerate(f, 1):
            yield i, raw.decode("utf-8", "replace").rstrip("\r\n").replace("\x00", "")


def read_ota(ref: FileRef, stat: FileStat):
    for i, line in _lines(ref):
        m = OTA_RE.match(line)
        if not m:
            continue
        y, mo, d, h, mi, s = (int(v) for v in m.groups()[:6])
        t = epoch(y, mo, d, h, mi, s)
        stat.lines += 1
        stat.first = t if stat.first is None else min(stat.first, t)
        stat.last = t if stat.last is None else max(stat.last, t)
        yield Rec(t, SRC_OTA, ref.name, i, line, tag="otalog", level=m.group(7), msg=m.group(8))


def read_journal(ref: FileRef, stat: FileStat, sset: SourceSet):
    for i, line in _lines(ref):
        m = JOURNAL_RE.match(line)
        if not m or m.group(1) not in MONTHS:
            continue
        mo, d = MONTHS[m.group(1)], int(m.group(2))
        t = epoch(year_for(mo, d, sset.ref), mo, d, int(m.group(3)), int(m.group(4)), int(m.group(5)))
        stat.lines += 1
        stat.first = t if stat.first is None else min(stat.first, t)
        stat.last = t if stat.last is None else max(stat.last, t)
        yield Rec(t, SRC_JOURNAL, ref.name, i, line, tag=m.group(7).strip(), msg=m.group(9))


def dmesg_boot_time(journal_recs, dmesg_lines):
    """dmesg(경과초) ↔ journal 커널 줄(절대 시각)에서 같은 메시지를 찾아 부팅 시각 추정."""
    kmap, kcount = {}, {}
    for r in journal_recs:
        if r.tag == "kernel":
            k = r.msg.strip()
            kmap[k] = r.t                      # 같은 메시지는 마지막(최근 부팅) 것
            kcount[k] = kcount.get(k, 0) + 1
    dcount = {}
    for _rel, msg in dmesg_lines:
        dcount[msg.strip()] = dcount.get(msg.strip(), 0) + 1
    uniq, cands = [], []
    for rel, msg in dmesg_lines:
        k = msg.strip()
        t = kmap.get(k)
        if t is None:
            continue
        c = t + 0.5 - rel                      # journal 은 초 단위(내림)
        cands.append(c)
        if kcount.get(k) == 1 and dcount.get(k) == 1:
            uniq.append(c)                     # 양쪽에 한 번만 나오는 줄이 가장 확실한 기준
    use = uniq or cands
    return statistics.median(use) if use else None


def read_dmesg(ref: FileRef, stat: FileStat, journal_recs):
    raw = []
    for i, line in _lines(ref):
        m = DMESG_RE.match(line)
        if m:
            raw.append((i, float(m.group(1)), m.group(2), line))
    boot = dmesg_boot_time(journal_recs, [(rel, msg) for _i, rel, msg, _l in raw])
    if boot is None:
        stat.note = "journal 커널 줄과 맞출 기준이 없어 절대 시각 환산 불가 (제외)"
        stat.lines = len(raw)
        return
    for i, rel, msg, line in raw:
        t = boot + rel
        stat.lines += 1
        stat.first = t if stat.first is None else min(stat.first, t)
        stat.last = t if stat.last is None else max(stat.last, t)
        yield Rec(t, SRC_DMESG, ref.name, i, line, tag="kernel", msg=msg, raw_t=f"+{rel:.3f}s", basis="추정")


def read_logcat(ref: FileRef, stat: FileStat, sset: SourceSet, off: Offset, pre, keep, progress=_noop):
    """SoC logcat 읽기. pre(t_amr, line) 가 참인 줄만 정규식으로 나누고, keep(rec) 가 참이면 반환.

    시각 파싱은 분 단위 캐시 (수백만 줄도 수 초).
    """
    cache = {}
    ref_dt = sset.ref
    shift = off.value if off.known else 0.0
    basis = "SoC환산" if off.known else "SoC원본"
    for i, line in _lines(ref):
        if len(line) < 20 or line[2] != "-" or line[5] != " " or line[8] != ":":
            continue
        key = line[:11]
        base = cache.get(key)
        if base is None:
            try:
                mo, d = int(line[0:2]), int(line[3:5])
                base = epoch(year_for(mo, d, ref_dt), mo, d, int(line[6:8]), int(line[9:11]), 0)
            except ValueError:
                continue
            cache[key] = base
        try:
            t = base + int(line[12:14]) + int(line[15:18]) / 1000.0 - shift
        except ValueError:
            continue
        stat.lines += 1
        if stat.first is None or t < stat.first:
            stat.first = t
        if stat.last is None or t > stat.last:
            stat.last = t
        if i % 250000 == 0:
            progress(f"SoC 로그 읽는 중: {ref.name} {i:,}줄", None)
        if not pre(t, line):
            continue
        m = LOGCAT_RE.match(line)
        if m is None:
            continue
        rec = Rec(t, SRC_SOC, ref.name, i, line, tag=m.group(10).strip(), level=m.group(9), msg=m.group(11),
                  raw_t=line[:18], basis=basis)
        if keep(rec):
            yield rec

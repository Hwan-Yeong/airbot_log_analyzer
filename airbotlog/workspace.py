"""임시 작업 폴더와 압축 해제.

- 입력(압축/폴더/txt)을 받아 분석할 로그 파일과 맵 폴더를 찾는다.
- 압축은 항상 임시 작업 폴더에만 푼다. 원본 위치(로컬 폴더, NAS)에는 쓰지 않는다.
- 작업 폴더는 앱 종료/다른 로그 로드 시 삭제하고, 비정상 종료로 남은 폴더는 다음 실행 때 정리한다.
"""
import fnmatch
import os
import re
import shutil
import sys
import tarfile
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar", ".zip")
LOG_FILE_RE = re.compile(r"^airbot_log(?:\.(\d+))?\.txt$", re.IGNORECASE)
# 예: <수집시각 14자리>_<시리얼>[_메모].zip, <수집시각>_<시리얼>(메모).zip
ARCHIVE_NAME_RE = re.compile(r"^(?P<ts>\d{14})_(?P<serial>[A-Za-z0-9]{8,})(?P<memo>.*)$")
NESTED_DEPTH = 3
_COPY_CHUNK = 1 << 20


class SourceError(Exception):
    """분석할 로그를 준비하지 못함."""


def _noop(*_args, **_kwargs):
    pass


# ---------------------------------------------------------------- 작업 폴더
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes  # Windows 에서 os.kill(pid, 0) 은 프로세스를 종료시키므로 사용 금지
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class Workspace:
    """앱 실행 단위의 임시 작업 폴더 (<temp>/airbotlog_xxxx)."""

    PREFIX = "airbotlog_"
    MARKER = ".airbotlog_owner"
    STALE_SEC = 3 * 86400

    def __init__(self, temp_root=None):
        self.temp_root = Path(temp_root) if temp_root else Path(tempfile.gettempdir())
        self.path = None

    def ensure(self) -> Path:
        if self.path is None or not self.path.exists():
            self.temp_root.mkdir(parents=True, exist_ok=True)
            self.path = Path(tempfile.mkdtemp(prefix=self.PREFIX, dir=self.temp_root))
            (self.path / self.MARKER).write_text(f"{os.getpid()}\n{int(time.time())}\n")
        return self.path

    def new_dir(self, name: str) -> Path:
        base = self.ensure()
        safe = re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "data"
        out = base / safe
        n = 1
        while out.exists():
            n += 1
            out = base / f"{safe}_{n}"
        out.mkdir(parents=True)
        return out

    def contains(self, p) -> bool:
        if self.path is None:
            return False
        try:
            Path(p).resolve().relative_to(self.path.resolve())
            return True
        except ValueError:
            return False

    def clear(self):
        """작업 폴더 내용 삭제 (새 로그를 불러오기 전)."""
        if self.path and self.path.exists():
            for child in self.path.iterdir():
                if child.name == self.MARKER:
                    continue
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    try:
                        child.unlink()
                    except OSError:
                        pass

    def cleanup(self):
        if self.path and self.path.exists():
            shutil.rmtree(self.path, ignore_errors=True)
        self.path = None

    @classmethod
    def cleanup_stale(cls, temp_root=None) -> int:
        """이전 실행에서 남은 작업 폴더 삭제 (주인 프로세스가 없거나 오래된 것)."""
        root = Path(temp_root) if temp_root else Path(tempfile.gettempdir())
        removed = 0
        try:
            entries = list(root.iterdir())
        except OSError:
            return 0
        now = time.time()
        for d in entries:
            if not (d.name.startswith(cls.PREFIX) and d.is_dir()):
                continue
            marker = d / cls.MARKER
            try:
                pid_s, created_s = marker.read_text().split()[:2]
                pid, created = int(pid_s), int(created_s)
            except (OSError, ValueError):
                continue  # 마커 없는 폴더는 우리 것이 아닐 수 있으므로 건드리지 않음
            if pid == os.getpid():
                continue
            if not _pid_alive(pid) or now - created > cls.STALE_SEC:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        return removed


# ---------------------------------------------------------------- 압축 해제
def archive_kind(path) -> str:
    name = Path(path).name.lower()
    if name.endswith(".zip"):
        return "zip"
    if name.endswith((".tar.gz", ".tgz", ".tar")):
        return "tar"
    return ""


def strip_archive_suffix(name: str) -> str:
    low = name.lower()
    for suf in ARCHIVE_SUFFIXES:
        if low.endswith(suf):
            return name[: -len(suf)]
    return name


def _skipped(name: str, patterns) -> bool:
    name = name.replace("\\", "/").lstrip("./")
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def _safe_target(dest: Path, member_name: str) -> Path:
    """압축 내부 경로를 dest 아래 경로로 변환 (절대경로/상위 이동 차단)."""
    parts = [p for p in PurePosixPath(member_name.replace("\\", "/")).parts if p not in ("", ".", "/")]
    if not parts or any(p == ".." for p in parts) or ":" in parts[0]:
        return None
    return dest.joinpath(*parts)


def _zip_name(info: zipfile.ZipInfo) -> str:
    """UTF-8 플래그 없는 zip(Windows 압축)의 한글 이름 복원."""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("cp949")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def extract_archive(src, dest, skip=(), progress=_noop) -> Path:
    src, dest = Path(src), Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    kind = archive_kind(src)
    label = src.name
    if kind == "zip":
        with zipfile.ZipFile(src) as zf:
            infos = zf.infolist()
            total = sum(i.file_size for i in infos) or 1
            done = 0
            for info in infos:
                name = _zip_name(info)
                target = _safe_target(dest, name)
                if target is None or _skipped(name, skip):
                    continue
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as s, open(target, "wb") as d:
                    shutil.copyfileobj(s, d, _COPY_CHUNK)
                done += info.file_size
                progress(f"압축 해제: {label}", done / total)
    elif kind == "tar":
        size = max(src.stat().st_size, 1)
        with open(src, "rb") as raw, tarfile.open(fileobj=raw, mode="r:*") as tf:
            for m in tf:  # 순차 처리 (gzip 역방향 탐색 방지)
                target = _safe_target(dest, m.name)
                if target is None or _skipped(m.name, skip):
                    continue
                if m.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif m.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    s = tf.extractfile(m)
                    with s, open(target, "wb") as d:
                        shutil.copyfileobj(s, d, _COPY_CHUNK)
                    try:
                        os.utime(target, (m.mtime, m.mtime))
                    except (OSError, OverflowError):
                        pass
                # 링크/장치 파일은 무시 (보안)
                progress(f"압축 해제: {label}", min(raw.tell() / size, 1.0))
    else:
        raise SourceError(f"지원하지 않는 압축 형식: {src.name}")
    return dest


def expand_nested(root: Path, skip=(), progress=_noop):
    """작업 폴더 안의 중첩 압축(amr/AMR_LOG.tar.gz 등)을 풀고 원본 압축은 지운다."""
    for _ in range(NESTED_DEPTH):
        nested = [p for p in root.rglob("*") if p.is_file() and archive_kind(p)
                  and not _skipped(str(p.relative_to(root)), skip)]
        if not nested:
            return
        for arc in nested:
            out = arc.parent / (strip_archive_suffix(arc.name) + "__x")
            extract_archive(arc, out, skip, progress)
            arc.unlink()


# ---------------------------------------------------------------- 로그/맵 탐색
def order_log_files(files):
    """airbot_log.9.txt(가장 오래됨) → ... → .1 → airbot_log.txt(최신) 순."""
    def key(p):
        m = LOG_FILE_RE.match(Path(p).name)
        idx = int(m.group(1)) if m and m.group(1) else 0
        return -idx
    return sorted((Path(f) for f in files), key=key)


def find_log_dirs(root: Path):
    found = {}
    for p in Path(root).rglob("*"):
        if p.is_file() and LOG_FILE_RE.match(p.name):
            found.setdefault(p.parent, []).append(p)
    return found


def is_map_dir(d: Path) -> bool:
    try:
        for y in d.glob("*.yaml"):
            with open(y, "r", encoding="utf-8", errors="replace") as f:
                head = f.read(2048)
            m = re.search(r"^image:\s*(\S+)", head, re.MULTILINE)
            if m and (d / m.group(1).strip("'\"")).is_file():
                return True
    except OSError:
        pass
    return False


def find_map_dir(roots, prefer: Path = None):
    if prefer is not None and is_map_dir(prefer / "map"):
        return prefer / "map"
    for r in roots:
        r = Path(r)
        if is_map_dir(r):
            return r
        for d in sorted(r.rglob("*")):
            if d.is_dir() and is_map_dir(d):
                return d
    return None


def parse_archive_name(name: str) -> dict:
    stem = strip_archive_suffix(Path(name).name)
    m = ARCHIVE_NAME_RE.match(stem)
    if not m:
        return {"serial": "", "collected": "", "memo": ""}
    try:
        collected = datetime.strptime(m.group("ts"), "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        collected = ""
    memo = m.group("memo").strip(" _")
    if memo.startswith("(") and memo.endswith(")"):
        memo = memo[1:-1]
    return {"serial": m.group("serial"), "collected": collected, "memo": memo}


@dataclass
class LogSource:
    label: str
    origin: str
    log_files: list
    map_dir: Path = None
    serial: str = ""
    collected: str = ""
    memo: str = ""
    extras: dict = field(default_factory=dict)


def prepare_source(inputs, ws: Workspace, skip=(), progress=_noop, origin: str = "") -> LogSource:
    """입력 경로들(압축/폴더/로그 txt)에서 분석할 로그 세트를 준비한다."""
    inputs = [Path(p) for p in inputs]
    missing = [str(p) for p in inputs if not p.exists()]
    if missing:
        raise SourceError("파일을 찾을 수 없습니다: " + ", ".join(missing))
    txts = [p for p in inputs if p.is_file() and LOG_FILE_RE.match(p.name)]
    archives = [p for p in inputs if p.is_file() and archive_kind(p)]
    dirs = [p for p in inputs if p.is_dir()]
    others = [p for p in inputs if p.is_file() and p not in txts and p not in archives]
    if others and not (txts or archives or dirs):
        raise SourceError("지원하지 않는 파일입니다: " + ", ".join(p.name for p in others))

    roots = []
    for arc in archives:
        out = ws.new_dir(strip_archive_suffix(arc.name))
        extract_archive(arc, out, skip, progress)
        expand_nested(out, skip, progress)
        roots.append(out)
    for d in dirs:
        if find_log_dirs(d):
            roots.append(d)  # 이미 풀린 폴더는 그대로 읽기만 함
            continue
        nested = [p for p in d.rglob("*") if p.is_file() and archive_kind(p)
                  and not _skipped(str(p.relative_to(d)), skip)]
        for arc in nested:  # 폴더 안 압축은 작업 폴더에 풀기
            out = ws.new_dir(strip_archive_suffix(arc.name))
            extract_archive(arc, out, skip, progress)
            expand_nested(out, skip, progress)
            roots.append(out)
        if not nested:
            roots.append(d)

    if txts:
        log_dir = txts[0].parent
        if len(txts) == 1:  # 하나만 고르면 같은 폴더의 로테이션 파일을 모두 포함
            txts = [p for p in log_dir.iterdir() if p.is_file() and LOG_FILE_RE.match(p.name)]
        log_files = order_log_files(txts)
    else:
        candidates = {}
        for r in roots:
            candidates.update(find_log_dirs(r))
        if not candidates:
            raise SourceError("airbot_log*.txt 로그 파일을 찾지 못했습니다.")
        log_dir = max(candidates, key=lambda d: (any(p.name.lower() == "airbot_log.txt"
                                                     for p in candidates[d]), len(candidates[d])))
        log_files = order_log_files(candidates[log_dir])

    map_dir = find_map_dir(roots + [log_dir], prefer=log_dir)
    named = archives[0] if archives else (dirs[0] if dirs else log_dir)
    meta = parse_archive_name(named.name)
    # 분석([분석] 버튼)에서 다른 로그(otalog·journal·SoC logcat)를 찾을 때 사용. 원본은 읽기만 함
    extras = {"roots": [str(r) for r in roots + dirs], "archives": [str(a) for a in archives]}
    return LogSource(label=named.name, origin=origin or str(named), log_files=log_files,
                     map_dir=map_dir, extras=extras, **meta)


def prepare_map(path, ws: Workspace, skip=(), progress=_noop) -> Path:
    """맵만 따로 교체할 때: 폴더 또는 압축에서 맵 폴더 찾기."""
    path = Path(path)
    if path.is_file() and archive_kind(path):
        out = ws.new_dir("map_" + strip_archive_suffix(path.name))
        extract_archive(path, out, skip, progress)
        expand_nested(out, skip, progress)
        path = out
    elif path.is_file():
        path = path.parent
    found = find_map_dir([path])
    if not found:
        raise SourceError("맵 파일(*.yaml + 이미지)을 찾지 못했습니다.")
    return found

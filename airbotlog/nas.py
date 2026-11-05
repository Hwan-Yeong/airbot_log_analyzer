"""NAS 로그 폴더 연결 (읽기 전용).

연결 방식
- local      : 파일시스템 경로로 직접 접근 (Windows UNC 경로, Linux 마운트 경로, GVFS)
- powershell : WSL 에서 Windows PowerShell 을 거쳐 UNC 경로 접근 (마운트/sudo 불필요)

NAS 에는 '목록 조회'와 '로컬 작업 폴더로 복사'만 한다. 쓰기/삭제/이동 코드는 없다.
"""
import base64
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .settings import user_cache_dir

ARCHIVE_EXTS = (".zip", ".tar", ".tar.gz", ".tgz")
_CHUNK = 1 << 20


class NasError(Exception):
    pass


def _noop(*_args, **_kwargs):
    pass


@dataclass
class NasEntry:
    rel: str            # NAS 루트 기준 상대 경로 ('/' 구분)
    size: int
    mtime: float
    extracted: bool = False   # 옆에 같은 이름의 폴더(압축 해제본)가 있는지

    @property
    def name(self) -> str:
        return self.rel.rsplit("/", 1)[-1]

    @property
    def folder(self) -> str:
        return self.rel.rsplit("/", 1)[0] if "/" in self.rel else ""


def detect_platform() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "mac"
    release = platform.uname().release.lower()
    if "microsoft" in release or "wsl" in release or os.environ.get("WSL_DISTRO_NAME"):
        return "wsl"
    return "linux"


def is_unc(path: str) -> bool:
    return path.startswith("\\\\") or path.startswith("//")


def is_archive_name(name: str) -> bool:
    return name.lower().endswith(ARCHIVE_EXTS)


def _strip_ext(name: str) -> str:
    low = name.lower()
    for ext in (".tar.gz", ".tgz", ".tar", ".zip"):
        if low.endswith(ext):
            return name[: -len(ext)]
    return name


def _build_entries(files, dirs) -> list:
    out = []
    for rel, size, mtime in files:
        if is_archive_name(rel):
            out.append(NasEntry(rel, int(size), float(mtime), _strip_ext(rel) in dirs))
    out.sort(key=lambda e: e.mtime, reverse=True)
    return out


def _assert_local_dest(dest: Path, nas_root: str):
    """복사 대상이 NAS 가 아닌 로컬 경로인지 확인 (NAS 쓰기 방지)."""
    dest = Path(dest).resolve()
    if is_unc(str(dest)):
        raise NasError("복사 대상은 로컬 작업 폴더여야 합니다.")
    if not is_unc(nas_root):
        try:
            dest.relative_to(Path(nas_root).resolve())
        except ValueError:
            return
        raise NasError("NAS 폴더 안으로는 복사할 수 없습니다.")


class NasSource:
    kind = ""

    def __init__(self, root: str):
        self.root = root

    def describe(self) -> str:
        return self.root

    def list_archives(self, progress=_noop) -> list:
        raise NotImplementedError

    def copy_to(self, rel: str, dest_dir: Path, size: int = 0, progress=_noop) -> Path:
        raise NotImplementedError


class LocalNasSource(NasSource):
    """파일시스템으로 접근 가능한 NAS 경로 (Windows UNC, Linux 마운트)."""

    kind = "local"

    def list_archives(self, progress=_noop) -> list:
        root = Path(self.root)
        if not root.is_dir():
            raise NasError(f"NAS 경로에 접근할 수 없습니다: {self.root}")
        files, dirs = [], set()
        for cur, dnames, fnames in os.walk(root):
            rel_cur = Path(cur).relative_to(root).as_posix()
            rel_cur = "" if rel_cur == "." else rel_cur
            for d in dnames:
                dirs.add(f"{rel_cur}/{d}" if rel_cur else d)
            for f in fnames:
                if not is_archive_name(f):
                    continue
                try:
                    st = os.stat(os.path.join(cur, f))
                except OSError:
                    continue
                files.append((f"{rel_cur}/{f}" if rel_cur else f, st.st_size, st.st_mtime))
            progress(f"NAS 목록 조회 중... (압축 {len(files)}개)", None)
        return _build_entries(files, dirs)

    def copy_to(self, rel: str, dest_dir: Path, size: int = 0, progress=_noop) -> Path:
        _assert_local_dest(dest_dir, self.root)
        src = Path(self.root).joinpath(*rel.split("/"))
        dst = Path(dest_dir) / src.name
        total = size or src.stat().st_size or 1
        done = 0
        with open(src, "rb") as s, open(dst, "wb") as d:  # NAS 파일은 읽기 모드로만 연다
            while True:
                buf = s.read(_CHUNK)
                if not buf:
                    break
                d.write(buf)
                done += len(buf)
                progress(f"NAS에서 복사 중: {src.name}", min(done / total, 1.0))
        return dst


def find_powershell() -> str:
    for cand in ("powershell.exe", "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"):
        path = shutil.which(cand) or (cand if os.path.isfile(cand) else None)
        if path:
            return path
    return ""


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _wsl_to_windows(path: Path) -> str:
    out = subprocess.run(["wslpath", "-w", str(path)], capture_output=True, text=True, timeout=10)
    if out.returncode != 0 or not out.stdout.strip():
        raise NasError(f"Windows 경로 변환 실패: {path}")
    return out.stdout.strip()


class PowerShellNasSource(NasSource):
    """WSL 에서 Windows PowerShell 로 UNC 경로를 조회/복사 (마운트 불필요)."""

    kind = "powershell"

    def __init__(self, root: str, exe: str = ""):
        super().__init__(root.replace("/", "\\"))
        self.exe = exe or find_powershell()
        if not self.exe:
            raise NasError("powershell.exe 를 찾을 수 없습니다 (WSL interop 확인).")

    def _command(self, script: str) -> list:
        full = "[Console]::OutputEncoding=[Text.Encoding]::UTF8\n$ProgressPreference='SilentlyContinue'\n" + script
        enc = base64.b64encode(full.encode("utf-16-le")).decode("ascii")
        return [self.exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", enc]

    @staticmethod
    def _cwd():
        return "/mnt/c" if os.path.isdir("/mnt/c") else None

    def list_archives(self, progress=_noop) -> list:
        progress("NAS 목록 조회 중 (PowerShell)...", None)
        script = (
            f"$root = {_ps_quote(self.root)}\n"
            "if (-not (Test-Path -LiteralPath $root)) { Write-Output \"ERR`tNOT_FOUND\"; exit 0 }\n"
            "$len = $root.TrimEnd('\\').Length\n"
            "Get-ChildItem -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue | ForEach-Object {\n"
            "  $rel = $_.FullName.Substring($len).TrimStart('\\')\n"
            "  if ($_.PSIsContainer) { \"D`t$rel\" }\n"
            "  else {\n"
            "    $n = $_.Name.ToLower()\n"
            "    if ($n.EndsWith('.zip') -or $n.EndsWith('.tar') -or $n.EndsWith('.tar.gz') -or $n.EndsWith('.tgz')) {\n"
            "      $t = [long](($_.LastWriteTimeUtc - [datetime]'1970-01-01').TotalSeconds)\n"
            "      \"F`t$rel`t$($_.Length)`t$t\"\n"
            "    }\n"
            "  }\n"
            "}\n"
        )
        try:
            proc = subprocess.run(self._command(script), capture_output=True, timeout=600, cwd=self._cwd())
        except (OSError, subprocess.TimeoutExpired) as e:
            raise NasError(f"PowerShell 실행 실패: {e}")
        out = proc.stdout.decode("utf-8", errors="replace")
        if "ERR\tNOT_FOUND" in out:
            raise NasError(f"NAS 경로에 접근할 수 없습니다: {self.root}\n"
                           "Windows 탐색기에서 NAS 로그인(접속)이 되어 있는지 확인하세요.")
        if proc.returncode != 0 and not out.strip():
            err = proc.stderr.decode("utf-8", errors="replace").strip()
            raise NasError(f"NAS 목록 조회 실패: {err[:300]}")
        files, dirs = [], set()
        for line in out.splitlines():
            parts = line.rstrip("\r").split("\t")
            if parts[0] == "D" and len(parts) >= 2:
                dirs.add(parts[1].replace("\\", "/"))
            elif parts[0] == "F" and len(parts) >= 4:
                try:
                    files.append((parts[1].replace("\\", "/"), int(parts[2]), float(parts[3])))
                except ValueError:
                    continue
        return _build_entries(files, dirs)

    def copy_to(self, rel: str, dest_dir: Path, size: int = 0, progress=_noop) -> Path:
        _assert_local_dest(dest_dir, self.root)
        name = rel.rsplit("/", 1)[-1]
        dst = Path(dest_dir) / name
        src = self.root.rstrip("\\") + "\\" + rel.replace("/", "\\")
        script = (f"Copy-Item -LiteralPath {_ps_quote(src)} "
                  f"-Destination {_ps_quote(_wsl_to_windows(dst))} -Force")
        proc = subprocess.Popen(self._command(script), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=self._cwd())
        while proc.poll() is None:
            if size and dst.exists():
                progress(f"NAS에서 복사 중: {name}", min(dst.stat().st_size / size, 1.0))
            else:
                progress(f"NAS에서 복사 중: {name}", None)
            time.sleep(0.3)
        _, err = proc.communicate()
        if proc.returncode != 0 or not dst.exists():
            raise NasError("NAS 복사 실패: " + err.decode("utf-8", errors="replace")[:300])
        return dst


def gvfs_path_for(unc: str):
    """Ubuntu 파일 관리자(GVFS)로 연결된 smb 공유에서 같은 경로 찾기."""
    parts = [p for p in re.split(r"[\\/]+", unc) if p]
    if len(parts) < 2 or not hasattr(os, "getuid"):
        return None
    server, share, rest = parts[0].lower(), parts[1].lower(), parts[2:]
    base = Path(f"/run/user/{os.getuid()}/gvfs")
    if not base.is_dir():
        return None
    for d in base.iterdir():
        name = d.name.lower()
        if name.startswith("smb-share:") and f"server={server}" in name and f"share={share}" in name:
            cand = d.joinpath(*rest)
            if cand.is_dir():
                return cand
    return None


def _nonempty_dir(path: str) -> bool:
    try:
        with os.scandir(path) as it:
            return next(it, None) is not None
    except OSError:
        return False


def resolve_source(settings):
    """설정과 실행 환경으로 NAS 연결 방식을 정한다. (source 또는 None, 안내 문구)"""
    root = (settings.get("nas.root") or "").strip()
    linux_path = (settings.get("nas.linux_path") or "").strip()
    plat = detect_platform()
    if plat == "windows":
        if root:
            return LocalNasSource(root), f"Windows 네트워크 경로: {root}"
        return None, "NAS 경로가 설정되지 않았습니다. [NAS 경로 설정]에서 입력하세요."
    if linux_path and _nonempty_dir(linux_path):
        return LocalNasSource(linux_path), f"마운트 경로: {linux_path}"
    if root and not is_unc(root) and os.path.isdir(root):
        return LocalNasSource(root), f"로컬 경로: {root}"
    if root and is_unc(root):
        if plat == "wsl" and find_powershell():
            return PowerShellNasSource(root), f"WSL → Windows PowerShell 경유: {root}"
        found = gvfs_path_for(root)
        if found:
            return LocalNasSource(str(found)), f"GVFS 연결: {found}"
        return None, ("Linux 에서는 NAS 를 마운트해야 합니다 (docs/SETUP.md 참고).\n"
                      f"마운트 경로(nas.linux_path): {linux_path or '-'}")
    return None, "NAS 경로가 설정되지 않았습니다. [NAS 경로 설정]에서 입력하세요."


# ---------------------------------------------------------------- 목록 캐시
def _cache_file(root: str) -> Path:
    key = hashlib.sha1(root.encode("utf-8")).hexdigest()[:12]
    return user_cache_dir() / f"nas_index_{key}.json"


def load_index_cache(root: str):
    try:
        with open(_cache_file(root), "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("root") != root:
            return None, 0
        return [NasEntry(**e) for e in data.get("entries", [])], float(data.get("time", 0))
    except (OSError, ValueError, TypeError):
        return None, 0


def save_index_cache(root: str, entries):
    path = _cache_file(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"root": root, "time": time.time(), "entries": [asdict(e) for e in entries]},
                      f, ensure_ascii=False)
    except OSError:
        pass

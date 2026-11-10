#!/usr/bin/env python3
"""Windows 에서 바로 실행하는 아이콘 만들기 (WSL) — scripts/install_shortcut.sh 로 실행.

- 바탕화면 아이콘: wslg.exe 로 이 폴더의 앱을 실행 (콘솔 창 없음, WSLg 가 만드는 바로가기와 같은 방식)
- 시작 메뉴: 같은 바로가기 (WSLg 가 리눅스 앱 항목으로 자동 추가하는 메뉴와 겹치지 않게 그 항목은 메뉴에서 숨김)
- 리눅스 앱 항목(~/.local/share/applications): 창과 작업 표시줄 아이콘 연결용
- 아이콘: 앱 아이콘을 그려 %LOCALAPPDATA%\\<앱 ID>\\ 와 ~/.local/share/icons/ 에 저장
- 실행은 scripts/wsl_launch.sh (처음 실행·업데이트 직후에는 Windows 알림, 실패하면 로그 위치 알림)

    ./scripts/install_shortcut.sh            만들기
    ./scripts/install_shortcut.sh --remove   지우기
"""
import argparse
import base64
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from airbotlog import APP_NAME  # noqa: E402
from airbotlog.nas import detect_platform, find_powershell  # noqa: E402
from airbotlog.settings import APP_ID  # noqa: E402

WSLG = r"C:\Program Files\WSL\wslg.exe"
WSL = r"C:\Windows\System32\wsl.exe"


def _q(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def powershell(script: str) -> str:
    exe = find_powershell()
    if not exe:
        raise SystemExit("powershell.exe 를 찾을 수 없습니다 (WSL interop 확인).")
    full = "[Console]::OutputEncoding=[Text.Encoding]::UTF8\n$ProgressPreference='SilentlyContinue'\n" + script
    enc = base64.b64encode(full.encode("utf-16-le")).decode("ascii")
    r = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", enc], capture_output=True,
                       timeout=60, cwd="/mnt/c" if os.path.isdir("/mnt/c") else None)
    if r.returncode != 0:
        raise SystemExit("PowerShell 실패: " + r.stderr.decode("utf-8", "replace")[-500:])
    return r.stdout.decode("utf-8", "replace").replace("\r", "")


def to_wsl(win_path: str) -> Path:
    out = subprocess.run(["wslpath", "-u", win_path], capture_output=True, text=True, timeout=10)
    return Path(out.stdout.strip())


def launch_args(distro: str, root: Path = ROOT) -> str:
    """wslg.exe 인수: 배포판 · 시작 폴더 · 로그인 셸로 실행기 (PATH 등 터미널과 같은 환경)."""
    return f'-d {distro} --cd "{root}" -- /bin/bash -l "{root}/scripts/wsl_launch.sh"'


def desktop_entry(root: Path, icon_png: Path) -> str:
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        f"Name={APP_NAME}",
        "Comment=로봇 로그(AMR_LOG)를 맵 위에서 재생·분석",
        f'Exec=/bin/bash -l "{root}/scripts/wsl_launch.sh"',
        f"Path={root}",
        f"Icon={icon_png}",
        "Terminal=false",
        "NoDisplay=true",              # 시작 메뉴는 직접 만든 바로가기 하나만 (WSLg 자동 항목과 중복 방지)
        "Categories=Development;Utility;",
        f"StartupWMClass={APP_ID}",
        "",
    ])


def shortcut_script(lnk: str, target: str, args: str, icon: str, desc: str, folders=("Desktop",)) -> str:
    """바로가기 만들기 (OneDrive 로 옮긴 바탕화면도 GetFolderPath 로 찾음). folders: Desktop / Programs(시작 메뉴)."""
    dirs = ", ".join(f"[Environment]::GetFolderPath('{f}')" for f in folders)
    return f"""
$sh = New-Object -ComObject WScript.Shell
foreach ($dir in @({dirs})) {{
  $path = Join-Path $dir {_q(lnk)}
  $l = $sh.CreateShortcut($path)
  $l.TargetPath = {_q(target)}
  $l.Arguments = {_q(args)}
  $l.WorkingDirectory = $env:USERPROFILE
  $l.IconLocation = {_q(icon + ",0")}
  $l.Description = {_q(desc)}
  $l.Save()
  Write-Output ('LNK=' + $path)
}}
"""


def remove_script(lnk: str) -> str:
    """직접 만든 바로가기만 지움 (바탕화면·시작 메뉴)."""
    return f"""
foreach ($dir in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {{
  $path = Join-Path $dir {_q(lnk)}
  if (Test-Path -LiteralPath $path) {{ Remove-Item -LiteralPath $path; Write-Output ('REMOVED=' + $path) }}
}}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--remove", action="store_true", help="만든 아이콘·메뉴 지우기")
    args = ap.parse_args()
    if detect_platform() != "wsl":
        raise SystemExit("WSL 전용입니다. Windows 에서는 run.bat, Linux 에서는 ./run.sh 로 실행하세요.")
    distro = os.environ.get("WSL_DISTRO_NAME") or "Ubuntu"
    lnk = f"{APP_NAME}.lnk"
    apps = Path.home() / ".local/share/applications" / f"{APP_ID}.desktop"
    icon_png = Path.home() / ".local/share/icons" / f"{APP_ID}.png"
    local = powershell("Write-Output $env:LOCALAPPDATA").strip().splitlines()[-1]
    ico_win = f"{local}\\{APP_ID}\\{APP_ID}.ico"
    ico = to_wsl(ico_win)

    if args.remove:
        for line in powershell(remove_script(lnk)).splitlines():
            if line.startswith("REMOVED="):
                print("지움:", line.split("=", 1)[1])
        for p in (apps, icon_png, ico):                       # 리눅스 앱 항목을 지우면 WSLg 시작 메뉴 항목도 사라짐
            if p.exists():
                p.unlink()
                print("지움:", p)
        if ico.parent.exists() and not any(ico.parent.iterdir()):
            ico.parent.rmdir()
        return

    from PySide6.QtGui import QGuiApplication
    app = QGuiApplication.instance() or QGuiApplication([])     # 아이콘 그리기용 (화면 없음)
    from airbotlog.ui.appicon import write_ico, write_png
    ico.parent.mkdir(parents=True, exist_ok=True)
    write_ico(ico)
    icon_png.parent.mkdir(parents=True, exist_ok=True)
    write_png(icon_png)
    apps.parent.mkdir(parents=True, exist_ok=True)
    apps.write_text(desktop_entry(ROOT, icon_png), encoding="utf-8")
    target = WSLG if to_wsl(WSLG).exists() else WSL         # 오래된 WSL 은 wslg.exe 없음 → 콘솔 창이 잠깐 보임
    desc = f"{APP_NAME} (WSL {distro}: {ROOT})"
    out = powershell(shortcut_script(lnk, target, launch_args(distro), ico_win, desc))
    made = [line.split("=", 1)[1] for line in out.splitlines() if line.startswith("LNK=")]
    out = powershell(shortcut_script(lnk, target, launch_args(distro), ico_win, desc, folders=("Programs",)))
    menu = [line.split("=", 1)[1] for line in out.splitlines() if line.startswith("LNK=")]
    print(f"{APP_NAME} 실행 아이콘을 만들었습니다:")
    for path in made + menu:
        print("  -", path)
    print(f"  - 리눅스 앱 항목: {apps}")
    if target == WSL:
        print("  ※ wslg.exe 가 없어 wsl.exe 로 실행합니다 (실행할 때 콘솔 창이 함께 뜸). WSL 을 업데이트하면 없어집니다.")
    print("지우기: ./scripts/install_shortcut.sh --remove")
    sys.path.insert(0, str(ROOT / "scripts"))
    from win_notify import notify
    notify(APP_NAME, "바탕화면과 시작 메뉴에 실행 아이콘을 만들었습니다.")
    del app


if __name__ == "__main__":
    main()

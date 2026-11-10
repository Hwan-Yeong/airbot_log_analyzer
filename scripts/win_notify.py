#!/usr/bin/env python3
"""WSL 에서 Windows 알림(작업 표시줄 풍선) 띄우기 — 표준 라이브러리만 (앱 .venv 가 없어도 동작).

    python3 scripts/win_notify.py "제목" "내용" [info|error]
"""
import base64
import os
import shutil
import subprocess
import sys

SCRIPT = r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::__ICON__
$n.BalloonTipIcon = '__TIPICON__'
$n.BalloonTipTitle = __TITLE__
$n.BalloonTipText = __TEXT__
$n.Visible = $true
$n.ShowBalloonTip(10000)
Start-Sleep -Seconds 11
$n.Dispose()
"""


def _q(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def notify(title: str, text: str, kind: str = "info", wait: bool = False) -> bool:
    exe = shutil.which("powershell.exe") or "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    if not os.path.exists(exe) and not shutil.which(exe):
        return False
    icon, tip = ("Error", "Error") if kind == "error" else ("Information", "Info")
    script = (SCRIPT.replace("__ICON__", icon).replace("__TIPICON__", tip)
              .replace("__TITLE__", _q(title)).replace("__TEXT__", _q(text)))
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    cmd = [exe, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", enc]
    kw = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "cwd": "/mnt/c" if os.path.isdir("/mnt/c") else None}
    try:
        if wait:
            subprocess.run(cmd, timeout=30, **kw)
        else:
            subprocess.Popen(cmd, start_new_session=True, **kw)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


if __name__ == "__main__":
    a = sys.argv[1:] + ["", "", "info"]
    sys.exit(0 if notify(a[0], a[1], a[2] or "info") else 1)

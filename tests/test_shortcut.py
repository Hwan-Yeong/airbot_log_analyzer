"""Windows 실행 아이콘(WSL): 바로가기·리눅스 앱 항목 내용, 아이콘 파일, Windows 알림 호출."""
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


def test_shortcut_texts():
    import install_shortcut as ins

    from airbotlog import APP_NAME
    root = Path("/home/u/work/my app")
    args = ins.launch_args("Ubuntu-22.04", root)
    assert args == '-d Ubuntu-22.04 --cd "/home/u/work/my app" -- /bin/bash -l "/home/u/work/my app/scripts/wsl_launch.sh"'
    entry = ins.desktop_entry(root, Path("/home/u/.local/share/icons/x.png"))
    assert f"Name={APP_NAME}" in entry and 'Exec=/bin/bash -l "/home/u/work/my app/scripts/wsl_launch.sh"' in entry
    assert "Terminal=false" in entry and "Icon=/home/u/.local/share/icons/x.png" in entry
    assert "NoDisplay=true" in entry                       # 시작 메뉴는 직접 만든 바로가기 하나만
    ps = ins.shortcut_script("앱's.lnk", ins.WSLG, args, r"C:\Users\u\AppData\Local\x\x.ico", "설명")
    assert "'앱''s.lnk'" in ps and "GetFolderPath('Desktop')" in ps and "Programs" not in ps
    assert "'" + ins.WSLG + "'" in ps and r"x.ico,0" in ps
    assert "GetFolderPath('Programs')" in ins.shortcut_script("a.lnk", ins.WSLG, args, "i", "d", folders=("Programs",))
    assert "'a.lnk'" in ins.remove_script("a.lnk")


def test_icon_files(tmp_path):
    QtGui = pytest.importorskip("PySide6.QtGui")
    QtGui.QGuiApplication.instance() or QtGui.QGuiApplication([])
    from airbotlog.ui.appicon import SIZES, app_icon, write_ico, write_png
    ico = tmp_path / "a.ico"
    write_ico(ico)
    data = ico.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind, count) == (0, 1, len(SIZES))
    for k in range(count):
        size, offset = struct.unpack("<II", data[6 + 16 * k + 8:6 + 16 * k + 16])
        assert data[offset:offset + 8] == b"\x89PNG\r\n\x1a\n" and offset + size <= len(data)
    write_png(tmp_path / "a.png")
    assert (tmp_path / "a.png").read_bytes()[:4] == b"\x89PNG"
    assert len(app_icon().availableSizes()) == len(SIZES)


def test_windows_notification_command(monkeypatch):
    import base64

    import win_notify
    calls = []
    monkeypatch.setattr(win_notify.shutil, "which", lambda exe: "/mnt/c/powershell.exe")
    monkeypatch.setattr(win_notify.os.path, "exists", lambda p: True)
    monkeypatch.setattr(win_notify.subprocess, "Popen", lambda cmd, **kw: calls.append(cmd))
    assert win_notify.notify("제목 '따옴표'", "내용", "error")
    cmd = calls[0]
    script = base64.b64decode(cmd[cmd.index("-EncodedCommand") + 1]).decode("utf-16-le")
    assert "'제목 ''따옴표'''" in script and "SystemIcons]::Error" in script and "-WindowStyle" in cmd

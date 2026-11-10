"""실행 진입점: python run.py [로그 파일/폴더 ...]"""
import argparse
import atexit
import faulthandler
import os
import signal
import sys
from datetime import datetime

from . import APP_NAME, __version__
from .settings import APP_ID, user_cache_dir

_crash_file = None


def enable_crash_log():
    """비정상 종료(세그폴트 등) 시 모든 스레드의 파이썬 스택을 crash.log 에 남긴다."""
    global _crash_file
    try:
        d = user_cache_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / "crash.log"
        if path.exists() and path.stat().st_size > 1_000_000:
            path.unlink()
        _crash_file = open(path, "a", encoding="utf-8")
        _crash_file.write(f"=== {datetime.now():%Y-%m-%d %H:%M:%S} {APP_NAME} {__version__} 시작 (pid {os.getpid()})\n")
        _crash_file.flush()
        faulthandler.enable(file=_crash_file, all_threads=True)
        return path
    except OSError:
        faulthandler.enable(all_threads=True)
        return None


def binding_ok() -> bool:
    """PySide6 6.12 + Python 3.12 미만: void 메서드 호출마다 None 참조 카운트가 줄어 결국 비정상 종료됨."""
    if sys.version_info >= (3, 12):
        return True
    from PySide6.QtWidgets import QGraphicsPathItem
    item = QGraphicsPathItem()
    before = sys.getrefcount(None)
    for _ in range(200):
        item.setVisible(True)
    return sys.getrefcount(None) - before > -100


def main(argv=None):
    parser = argparse.ArgumentParser(prog="run.py", description=f"{APP_NAME} {__version__}")
    parser.add_argument("paths", nargs="*", help="로그 압축 파일(zip/tar.gz), 로그 폴더 또는 airbot_log*.txt")
    parser.add_argument("--nas", action="store_true", help="시작하면서 NAS 목록 열기")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args = parser.parse_args(argv)
    enable_crash_log()

    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("PySide6 가 설치되어 있지 않습니다:  pip install -r requirements.txt", file=sys.stderr)
        return 1
    from .settings import Settings
    from .ui.main_window import MainWindow
    from .ui.style import apply_style

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName(APP_ID)                        # 리눅스 앱 항목(.desktop)과 연결 → 작업 표시줄 아이콘
    from .ui.appicon import app_icon
    app.setWindowIcon(app_icon())
    settings = Settings()
    apply_style(app, settings.get("display.theme", "light"))
    if not binding_ok():
        import PySide6
        from PySide6.QtWidgets import QMessageBox
        msg = (f"PySide6 {PySide6.__version__} 는 Python {sys.version.split()[0]} 에서 동작 오류가 있습니다.\n"
               "run.sh / run.bat 로 실행하거나(전용 .venv 자동 구성), 아래로 설치 후 다시 실행하세요.\n\n"
               '    pip install "PySide6<6.12"')
        print(msg, file=sys.stderr)
        QMessageBox.critical(None, APP_NAME, msg)
        return 2
    win = MainWindow(settings)
    atexit.register(win.ws.cleanup)                       # 비정상 종료 대비
    signal.signal(signal.SIGINT, lambda *_: app.quit())   # Ctrl+C 로 종료해도 임시 폴더 정리
    keepalive = QTimer()                                   # Qt 루프 중에도 파이썬 시그널 처리
    keepalive.timeout.connect(lambda: None)
    keepalive.start(500)
    win.show()
    if args.paths:
        win.load_inputs(args.paths)
    elif args.nas:
        win.open_nas()
    return app.exec()

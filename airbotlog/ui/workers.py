"""백그라운드 작업 (로그 로드, NAS 조회/복사)."""
import traceback

from PySide6.QtCore import QThread, Signal


_ORPHANS = set()


def detach(worker):
    """창이 닫혀도 실행 중인 작업이 끝날 때까지 참조를 유지하고 결과는 버린다."""
    if worker is None or not worker.isRunning():
        return
    for sig in (worker.progress, worker.done, worker.failed):
        try:
            sig.disconnect()
        except (RuntimeError, TypeError):
            pass
    worker.setParent(None)
    _ORPHANS.add(worker)
    worker.finished.connect(lambda: _ORPHANS.discard(worker))


class FuncWorker(QThread):
    """fn(progress) 를 별도 스레드에서 실행. progress(msg, frac|None)."""

    progress = Signal(str, float)   # frac < 0 = 진행률 모름
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def _progress(self, msg, frac=None):
        self.progress.emit(str(msg), -1.0 if frac is None else float(frac))

    def run(self):
        try:
            result = self.fn(self._progress)
        except Exception as e:  # noqa: BLE001 - 사용자에게 메시지로 전달
            detail = traceback.format_exc(limit=3)
            self.failed.emit(f"{e}\n\n{detail}" if not str(e) else str(e))
            return
        self.done.emit(result)

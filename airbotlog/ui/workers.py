"""백그라운드 작업 (로그 로드, NAS 조회/복사).

작업 스레드는 순수 파이썬 스레드이며 Qt 를 전혀 사용하지 않는다.
진행 상황/결과는 메인 스레드의 QTimer 가 주기적으로 읽어 UI 에 반영한다.
(파이썬 스레드에서 Qt 시그널을 보내면 PySide 내부 경합으로 비정상 종료될 수 있음)
"""
import gc
import threading
import traceback

from PySide6.QtCore import QObject, QTimer

_gc_lock = threading.Lock()
_gc_jobs = 0


def _gc_pause():
    """작업 중 순환 GC 정지: 작업 스레드에서 Qt 객체가 해제되는 일을 막고 파싱도 빨라진다."""
    global _gc_jobs
    with _gc_lock:
        _gc_jobs += 1
        if _gc_jobs == 1:
            gc.disable()


def _gc_resume():
    global _gc_jobs
    with _gc_lock:
        _gc_jobs = max(_gc_jobs - 1, 0)
        if _gc_jobs == 0:
            gc.enable()


class _State:
    """작업 스레드와 메인 스레드가 공유하는 순수 파이썬 상태."""

    def __init__(self):
        self.lock = threading.Lock()
        self.progress = None     # 가장 최근 (msg, frac|None)
        self.result = None
        self.error = None
        self.done = False


def _run(state: _State, fn):
    def progress(msg, frac=None):
        with state.lock:
            state.progress = (str(msg), frac)

    _gc_pause()
    try:
        result = fn(progress)
    except Exception as e:  # noqa: BLE001 - 사용자에게 메시지로 전달
        msg = str(e) or traceback.format_exc(limit=3)
        with state.lock:
            state.error = msg
    else:
        with state.lock:
            state.result = result
    finally:
        _gc_resume()
        with state.lock:
            state.done = True


class BackgroundJob(QObject):
    """fn(progress) 를 파이썬 스레드에서 실행. on_progress(msg, frac), on_done(result), on_failed(msg) 는 메인 스레드에서 호출."""

    def __init__(self, fn, parent=None, interval_ms=100):
        super().__init__(parent)
        self.on_progress = None
        self.on_done = None
        self.on_failed = None
        self._state = _State()
        self._thread = threading.Thread(target=_run, args=(self._state, fn), daemon=True, name="airbotlog-job")
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._poll)

    def start(self):
        self._thread.start()
        self._timer.start()

    def is_running(self) -> bool:
        return self._thread.is_alive()

    def wait(self, timeout=None):
        if self._thread.is_alive():
            self._thread.join(timeout)

    def detach(self):
        """결과를 더 이상 받지 않음 (창이 닫힐 때). 스레드는 스스로 끝난다."""
        self._timer.stop()
        self.on_progress = self.on_done = self.on_failed = None

    def _poll(self):
        st = self._state
        with st.lock:
            prog, st.progress = st.progress, None
            done, result, error = st.done, st.result, st.error
        if prog is not None and self.on_progress:
            self.on_progress(*prog)
        if not done:
            return
        self._timer.stop()
        cb_done, cb_failed = self.on_done, self.on_failed
        self.on_progress = self.on_done = self.on_failed = None
        with st.lock:              # 결과를 넘긴 뒤에는 붙잡지 않음 (로그 세션이 메모리에 계속 남던 문제)
            st.result = st.error = None
        try:
            if error is not None:
                if cb_failed:
                    cb_failed(error)
            elif cb_done:
                cb_done(result)
        finally:
            self.deleteLater()     # 부모(메인 창)가 닫힐 때까지 작업 객체가 쌓이지 않도록

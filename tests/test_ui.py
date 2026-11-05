"""UI 회귀 테스트 (화면 없이). PySide6 바인딩이 정상인 환경(.venv)에서만 실행."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402


@pytest.fixture(scope="module")
def app():
    from airbotlog.app import binding_ok
    a = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    if not binding_ok():
        pytest.skip("PySide6 6.12 + Python 3.12 미만 바인딩 버그 → .venv 에서 실행하세요")
    return a


@pytest.fixture()
def win(app, demo_zip, settings):
    from airbotlog.loader import load_session
    from airbotlog.ui.main_window import MainWindow
    w = MainWindow(settings)
    w.resize(1400, 900)
    w.show()
    w.set_session(load_session([demo_zip], w.ws, w.settings, w.rules))
    app.processEvents()
    yield w
    w.close()


def _map_items(w):
    return [it for it in w.map_view.scene().items() if isinstance(it, QtWidgets.QGraphicsPixmapItem)]


def test_map_survives_zoom_and_pan(app, win):
    v = win.map_view
    assert len(_map_items(win)) == 1
    v.scale(1.5, 1.5)
    c = v.viewport().rect().center()
    QTest.mousePress(v.viewport(), Qt.LeftButton, Qt.NoModifier, c)
    for k in range(1, 6):
        QTest.mouseMove(v.viewport(), c + QPoint(12 * k, 8 * k))
        app.processEvents()
    QTest.mouseRelease(v.viewport(), Qt.LeftButton, Qt.NoModifier, c + QPoint(60, 40))
    app.processEvents()
    assert len(_map_items(win)) == 1          # 드래그 후에도 맵 이미지 유지


def test_jump_seconds_pauses_and_moves(app, win):
    S = win.session
    win.jump_to_event(S.events[len(S.events) // 3])
    t0 = S.time_at(win.vt)
    win.toggle_play()
    assert win.playing
    win.jump_seconds(10)
    assert not win.playing                     # 재생 중 점프 → 일시정지
    assert abs(S.time_at(win.vt) - t0 - 10) < 0.01


def test_trail_modes_and_sizes(app, win):
    lp, mv = win.layer_panel, win.map_view
    win.set_vt(win.session.vt_end, "seek")
    lp.cmb_trail.setCurrentIndex(1)            # 포즈 모드
    lp.spn_trail_count.setValue(10)
    assert sum(it.isVisible() for it in mv._pose_pool) == 10
    lp.cmb_history.setCurrentIndex(lp.cmb_history.count() - 1)   # 누적 표시 '전체'
    lp.cmb_trail.setCurrentIndex(0)            # 선 모드
    assert sum(it.isVisible() for it in mv._pose_pool) == 0
    assert not mv.trail.path().isEmpty()
    lp.layer_spins["target"].setValue(25)
    assert mv.layer_sizes["target"] == 25


def test_theme_toggle_keeps_map(app, win):
    from airbotlog.ui import style
    win.apply_theme("dark")
    assert style.T().dark and len(_map_items(win)) == 1
    assert win.map_view.backgroundBrush().color().name() == style.DARK.map_bg
    win.apply_theme("light")
    assert not style.T().dark and len(_map_items(win)) == 1

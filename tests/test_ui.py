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


def _wait(app, cond, timeout=60.0):
    import time
    end = time.time() + timeout
    while time.time() < end and not cond():
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    return cond()


def test_reload_releases_previous_session(app, demo_zip, settings):
    """로그를 여러 번 열어도 이전 세션이 메모리에 남지 않음 (1.1.3: 열 때마다 세션 하나씩 누적)."""
    import gc
    import weakref

    from airbotlog.ui.main_window import MainWindow
    from airbotlog.ui.workers import BackgroundJob
    w = MainWindow(settings)
    refs = []
    for _ in range(3):
        w.load_inputs([demo_zip])
        assert _wait(app, lambda: w._worker is None and w.session is not None)
        refs.append(weakref.ref(w.session))
    _wait(app, lambda: not w.findChildren(BackgroundJob), 5)
    gc.collect()
    assert not w.findChildren(BackgroundJob)
    assert [r() is None for r in refs] == [True, True, False]   # 마지막 세션만 남음
    w.close()


def test_view_prefs_saved_on_close(app, demo_zip, settings):
    """설정 변경 직후(저장 대기 0.8초 안) 닫아도 표시 설정이 저장됨."""
    from airbotlog.settings import Settings
    from airbotlog.ui.main_window import MainWindow
    w = MainWindow(settings)
    w.layer_panel.spn_robot.setValue(37)
    w.close()
    assert Settings(user_path=settings.user_path).get("display.view")["robot_size"] == 37


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="가짜 CLI 는 shebang 스크립트")
def test_analysis_dialog_flow(app, win, tmp_path, monkeypatch):
    """[분석]: 요청 입력 → 미리보기(마스킹) → (가짜) Claude → 결과 → 행 더블클릭 이동 → 엑셀."""
    import sys

    from test_analysis import FAKE_CLI, _valid_result

    from airbotlog.ui import analysis_dialog as ad
    cli = tmp_path / "claude"
    cli.write_text(FAKE_CLI.format(python=sys.executable, result=repr(_valid_result())), encoding="utf-8")
    cli.chmod(0o755)
    win.settings.set_user("analysis.claude_path", str(cli))
    shown = {}

    def fake_exec(dlg):
        shown["message"] = dlg.message()
        return ad.QDialog.Accepted

    monkeypatch.setattr(ad.PreviewDialog, "exec", fake_exec)
    win.open_analysis()
    dlg = win._analysis
    dlg.ed_request.setPlainText("11월 1일 오전 9시 반쯤 주방 가다가 낙하 에러(S07-2) 나고 멈췄대요. 시리얼 DEMOBOTSN000000001")
    dlg.update_extract()
    assert dlg.cmb_hint.count() == 1 and dlg.dt_anchor.dateTime().toString("HH:mm") == "09:30"
    dlg.preview()
    assert _wait(app, lambda: dlg.result_dialogs, 30)
    assert "DEMOBOTSN000000001" not in shown["message"] and "SN_01" in shown["message"]
    res = dlg.result_dialogs[0]
    assert res.tbl.rowCount() == 1
    win.set_vt(0.0, "seek")
    res.jump_row(0)
    assert win.vt > 0.0
    out = tmp_path / "x.xlsx"
    monkeypatch.setattr(ad.QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    monkeypatch.setattr(ad.QMessageBox, "information", lambda *a, **k: None)
    res.save_excel()
    assert out.exists() and out.stat().st_size > 5000
    res.close()
    dlg.close()

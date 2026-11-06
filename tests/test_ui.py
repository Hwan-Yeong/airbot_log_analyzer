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


def test_hud_toggles_and_resource_hud(app, demo_zip, settings):
    """기본 상태창·자원 상태창(메모리·AP 온도)을 각각 끄고 켬, 설정 저장. 버전은 SW:FW:AI 로 표시."""
    from airbotlog.loader import load_session
    from airbotlog.settings import Settings
    from airbotlog.ui.main_window import MainWindow
    w = MainWindow(settings)
    w.show()
    w.set_session(load_session([demo_zip], w.ws, w.settings, w.rules))
    w.set_vt(w.session.vt_end, "seek")
    app.processEvents()
    lp = w.layer_panel
    assert w.hud.isVisible() and w.hud_res.isVisible()
    assert "6.40:2.10:3.36" in w.hud.text()
    assert "메모리" in w.hud_res.text() and "AP 온도" in w.hud_res.text()
    lp.chk_hud_res.setChecked(False)
    app.processEvents()
    assert w.hud.isVisible() and not w.hud_res.isVisible()
    lp.chk_hud.setChecked(False)
    app.processEvents()
    assert not w.hud.isVisible()
    lp.chk_hud_res.setChecked(True)
    app.processEvents()
    assert w.hud_res.isVisible() and "메모리" in w.hud_res.text()
    w.close()
    view = Settings(user_path=settings.user_path).get("display.view")
    assert view["hud"] is False and view["hud_res"] is True


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
    win.open_analysis()
    dlg = win._analysis
    dlg.ed_request.setPlainText("11월 1일 오전 9시 반쯤 주방 가다가 낙하 에러(S07-2) 나고 멈췄대요. 시리얼 DEMOBOTSN000000001")
    dlg.update_extract()
    assert dlg.cmb_hint.count() == 1 and dlg.dt_anchor.dateTime().toString("HH:mm") == "09:30"
    win.toggle_play()                                   # 분석 창을 열어 둔 채 재생
    dlg.preview()
    assert app.activeModalWidget() is None              # 진행 중에도 메인 화면을 막지 않음
    assert _wait(app, lambda: dlg._preview is not None and dlg._preview.isVisible(), 30)
    assert app.activeModalWidget() is None and win.playing
    message = dlg._preview.message()
    assert "DEMOBOTSN000000001" not in message and "SN_01" in message
    dlg._preview.accept()                               # [Claude로 보내기]
    assert _wait(app, lambda: dlg.result_dialogs, 30)
    win.pause()
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


def _mime(paths):
    from PySide6.QtCore import QMimeData, QUrl
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    return mime


def _drop(handler, paths):
    """파일 끌어다 놓기: 드래그 진입이 받아들여지는지 이벤트로 확인하고, 놓기는 처리기를 직접 호출.
    (Qt 는 실제 드래그 중이 아니면 Drop 이벤트를 전달하지 않음)"""
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QDragEnterEvent, QDropEvent
    mime = _mime(paths)                 # 이벤트가 쓰는 동안 잡아 둠 (이벤트는 소유하지 않음 → 해제되면 비정상 종료)
    target = getattr(handler, "__self__", None)
    if isinstance(target, QtWidgets.QWidget):
        enter = QDragEnterEvent(QPoint(10, 10), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
        QtWidgets.QApplication.sendEvent(target, enter)
        assert enter.isAccepted()
    ev = QDropEvent(QPointF(10, 10), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    handler(ev)
    return ev


def test_analysis_dialog_reads_dropped_mails(app, win, tmp_path):
    """[분석] 창: 요청 칸이나 창 어디에 메일 파일을 놓아도 읽음 (.msg 포함, 여러 개면 이어 붙임)."""
    from test_mail import _eml

    from msgfile import make_msg
    eml = tmp_path / "요청.eml"
    eml.write_bytes(_eml("주방 이동 중 S07-2 발생"))
    msg = tmp_path / "추가.msg"
    msg.write_bytes(make_msg(subject="추가 문의", body="E09 도 확인 부탁"))
    win.open_analysis()
    dlg = win._analysis
    from PySide6.QtGui import QDragEnterEvent
    vp, mime = dlg.ed_request.viewport(), _mime([eml])
    enter = QDragEnterEvent(QPoint(5, 5), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    QtWidgets.QApplication.sendEvent(vp, enter)
    assert enter.isAccepted()                               # 요청 입력 칸 위에서도 파일을 받음
    _drop(lambda ev: dlg._drop_filter.eventFilter(vp, ev), [eml])   # 경로 글자가 들어가지 않고 메일로 읽음
    text = dlg.ed_request.toPlainText()
    assert text.startswith("제목: [필드 이슈] 낙하 에러 검토") and "file://" not in text
    assert "홍길동" in dlg.request.names
    _drop(dlg.dropEvent, [eml, msg])                        # 창의 다른 곳, 두 개
    text = dlg.ed_request.toPlainText()
    assert "주방 이동 중 S07-2" in text and "E09 도 확인 부탁" in text
    dlg.close()


def test_main_window_routes_dropped_mail(app, demo_zip, settings, tmp_path, monkeypatch):
    """메인 창: 메일만 놓으면 [분석] 창으로, 로그가 없으면 로그를 연 뒤 넣음 (로그와 함께 놓아도 같음)."""
    from test_mail import _eml

    from airbotlog.loader import load_session
    from airbotlog.ui import main_window as mw
    eml = tmp_path / "요청.eml"
    eml.write_bytes(_eml("오전 9시 반쯤 낙하 에러"))
    infos = []
    monkeypatch.setattr(mw.QMessageBox, "information", lambda *a, **k: infos.append(a[2]))
    w = mw.MainWindow(settings)
    w.show()
    _drop(w.dropEvent, [eml])                               # 로그 없음 → 대기
    assert getattr(w, "_analysis", None) is None and "로그를 열면" in infos[-1]
    w.set_session(load_session([demo_zip], w.ws, w.settings, w.rules))
    assert w._analysis.isVisible() and "오전 9시 반쯤 낙하 에러" in w._analysis.ed_request.toPlainText()
    w._analysis.ed_request.clear()
    _drop(w.dropEvent, [eml])                               # 로그 있음 → 바로
    assert "오전 9시 반쯤 낙하 에러" in w._analysis.ed_request.toPlainText()
    w._analysis.close()
    w.close()


def test_outlook_button_fills_request(app, win, monkeypatch):
    """[Outlook 선택 메일]: 작업 스레드에서 가져와 요청 칸을 채움 (여러 개 선택이면 안내)."""
    from airbotlog.analysis import outlook
    from airbotlog.analysis.request import Request
    from airbotlog.ui import analysis_dialog as ad
    infos = []
    monkeypatch.setattr(ad.QMessageBox, "information", lambda *a, **k: infos.append(a[2]))
    monkeypatch.setattr(outlook, "fetch_selected",
                        lambda: (Request(text="제목: 선택 메일\n보낸 날짜: \n\n본문 S05", subject="선택 메일",
                                         names=["홍길동"]), 3))
    win.open_analysis()
    dlg = win._analysis
    dlg.fetch_outlook()
    assert _wait(app, lambda: dlg._job is None, 10)
    assert dlg.ed_request.toPlainText().startswith("제목: 선택 메일") and dlg.request.names == ["홍길동"]
    assert "3개를 선택" in infos[-1]
    monkeypatch.setattr(outlook, "fetch_selected", lambda: (_ for _ in ()).throw(outlook.OutlookError("실행 중이 아닙니다")))
    warns = []
    monkeypatch.setattr(ad.QMessageBox, "warning", lambda *a, **k: warns.append(a[2]))
    dlg.fetch_outlook()
    assert _wait(app, lambda: dlg._job is None, 10) and "실행 중이 아닙니다" in warns[-1]
    dlg.close()


class _FakeDropWindow:
    """끌어다 놓기 창 대신 (정해 둔 결과를 돌려줌)."""
    result = None

    def __init__(self, *a, **k):
        self.cancelled = False

    def run(self):
        return _FakeDropWindow.result

    def cancel(self):
        self.cancelled = True


def test_drop_window_and_outlook_drag_in_dialog(app, win, tmp_path, monkeypatch):
    """WSL '끌어다 놓기 창'에 놓은 메일·Outlook 메일, Windows 에서 Outlook 목록에서 끌어온 메일."""
    from test_mail import _eml

    from airbotlog import windrop
    from airbotlog.analysis import outlook
    from airbotlog.analysis.request import Request
    from airbotlog.ui import analysis_dialog as ad
    eml = tmp_path / "요청.eml"
    eml.write_bytes(_eml("창으로 받은 메일 본문"))
    monkeypatch.setattr(ad.windrop, "DropWindow", _FakeDropWindow)
    monkeypatch.setattr(outlook, "fetch_selected", lambda: (Request(text="제목: Outlook 끌어온 메일\n\n본문"), 1))
    win.open_analysis()
    dlg = win._analysis
    _FakeDropWindow.result = windrop.DropResult(files=[str(eml)])
    dlg.open_drop_window()
    assert _wait(app, lambda: dlg._job is None, 10) and "창으로 받은 메일 본문" in dlg.ed_request.toPlainText()
    _FakeDropWindow.result = windrop.DropResult(outlook=True)
    dlg.open_drop_window()
    assert _wait(app, lambda: dlg._job is None, 10) and dlg.ed_request.toPlainText().startswith("제목: Outlook 끌어온 메일")
    dlg.ed_request.clear()
    from PySide6.QtCore import QMimeData
    from PySide6.QtGui import QDragEnterEvent
    mime = QMimeData()                                       # Windows 의 Outlook 끌어오기 (파일 아님)
    mime.setData('application/x-qt-windows-mime;value="FileGroupDescriptorW"', b"\0" * 8)
    assert ad._is_outlook_drag(mime) and not ad._local_files(mime)
    enter = QDragEnterEvent(QPoint(5, 5), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    QtWidgets.QApplication.sendEvent(dlg, enter)
    assert enter.isAccepted()
    dlg._handle_drop(mime)
    assert _wait(app, lambda: dlg._job is None, 10) and dlg.ed_request.toPlainText().startswith("제목: Outlook 끌어온 메일")
    dlg.close()


def test_main_window_drop_window_routes(app, win, tmp_path, monkeypatch):
    """메인 창 [끌어다 놓기] (WSL): 메일은 [분석] 창으로, 읽을 수 없는 파일은 안내."""
    from test_mail import _eml

    from airbotlog import windrop
    from airbotlog.ui import main_window as mw
    eml = tmp_path / "요청.eml"
    eml.write_bytes(_eml("메인 창 끌어다 놓기 창"))
    monkeypatch.setattr(mw.windrop, "DropWindow", _FakeDropWindow)
    _FakeDropWindow.result = windrop.DropResult(files=[str(eml)])
    win.open_drop_window()
    assert _wait(app, lambda: win._drop_job is None, 10)
    assert "메인 창 끌어다 놓기 창" in win._analysis.ed_request.toPlainText()
    warns = []
    monkeypatch.setattr(mw.QMessageBox, "warning", lambda *a, **k: warns.append(a[2]))
    _FakeDropWindow.result = windrop.DropResult(skipped=[r"\\nas\share\x.zip"])
    win.open_drop_window()
    assert _wait(app, lambda: win._drop_job is None, 10) and "[NAS] 버튼" in warns[-1]
    win._analysis.close()


def test_analysis_anchor_prefers_log_period_and_confirms_empty_window(app, win, monkeypatch):
    """기준 시각은 로그 기간 안의 시각 우선 (기간 밖 후보는 표시), 구간에 로그가 없으면 보내기 전에 확인."""
    from airbotlog.ui import analysis_dialog as ad
    win.open_analysis()
    dlg = win._analysis
    dlg.ed_request.setPlainText("11월 6일 오후 1시에 메일 드립니다. 11월 1일 오전 9시 반쯤 낙하 에러가 났습니다.")
    dlg.update_extract()
    assert dlg.dt_anchor.dateTime().toString("MM-dd HH:mm") == "11-01 09:30"
    labels = [dlg.cmb_hint.itemText(i) for i in range(dlg.cmb_hint.count())]
    assert any("로그 기간 밖" in t for t in labels) and dlg.cmb_hint.currentText().find("로그 기간 밖") < 0
    from PySide6.QtCore import QDate, QDateTime, QTime
    dlg.dt_anchor.setDateTime(QDateTime(QDate(2026, 11, 20), QTime(12, 0)))  # 로그 없는 구간
    asked = []
    monkeypatch.setattr(ad.QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or ad.QMessageBox.No)
    dlg.preview()
    assert _wait(app, lambda: dlg._job is None, 30)
    assert asked and "로그가 한 줄도 없습니다" in asked[-1] and dlg._preview is None
    monkeypatch.setattr(ad.QMessageBox, "question", lambda *a, **k: ad.QMessageBox.Yes)
    dlg.preview()
    assert _wait(app, lambda: dlg._preview is not None, 30)
    dlg._preview.close()
    dlg.close()

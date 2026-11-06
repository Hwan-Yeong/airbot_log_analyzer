"""메인 윈도우: 로드 → 맵/타임라인/이벤트/원본 로그 동기화 재생."""
import bisect
import calendar
import csv
import html
import time
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QGridLayout, QLabel, QMainWindow, QMessageBox,
                               QMenu, QProgressBar, QScrollArea, QSizePolicy, QSplitter, QStyle, QTabWidget,
                               QToolBar, QToolButton, QVBoxLayout, QWidget)

from .. import APP_NAME, __version__, windrop
from ..analysis import request as areq
from ..loader import load_session
from ..mapdata import load_map
from ..model import fmt_time
from ..rules import RuleError, RuleSet
from ..settings import Settings, user_config_dir
from ..workspace import SourceError, Workspace, prepare_map
from . import style
from .log_view import LogView
from .map_view import MapView
from .nas_dialog import NasDialog, NasPathDialog
from .panels import ErrorPanel, EventPanel, LayerPanel, SummaryPanel
from .timeline import PlaybackBar
from .workers import BackgroundJob

TICK_MS = 16                # 재생 화면 갱신 간격 (약 60fps — 로봇·카메라가 부드럽게 움직이도록)
HUD_SEC = 0.08              # 재생 중 상태창·시각 글자 갱신 간격 (글자는 자주 바꿀 필요 없음)
SLOW_UI_SEC = 0.2


class LoadingDialog(QDialog):
    """불러오기 진행 창. QProgressDialog 와 달리 값 갱신 중 이벤트를 중첩 처리하지 않는다."""

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.setWindowTitle("불러오기")
        self.setWindowModality(Qt.WindowModal)
        self.setMinimumWidth(480)
        lay = QVBoxLayout(self)
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        lay.addWidget(self.label)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        lay.addWidget(self.bar)

    def set_progress(self, msg, frac):
        self.label.setText(msg)
        if frac is None:
            self.bar.setRange(0, 0)
        else:
            if self.bar.maximum() == 0:
                self.bar.setRange(0, 1000)
            self.bar.setValue(int(min(max(frac, 0.0), 1.0) * 1000))

    def closeEvent(self, e):  # 사용자가 닫아도 작업은 계속, 끝나면 결과 표시
        e.accept()


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings = None):
        super().__init__()
        self.settings = settings or Settings()
        self.rules = None
        self.rules_error = ""
        try:
            self.rules = RuleSet.load()
        except (RuleError, OSError, KeyError) as e:
            self.rules_error = str(e)
        self.ws = Workspace(self.settings.get("workspace.temp_root") or None)
        Workspace.cleanup_stale(self.settings.get("workspace.temp_root") or None)
        self.session = None
        self._pending_mails = None      # 로그와 함께 끌어다 놓은 메일 (로그를 연 뒤 [분석] 창에 넣음), "outlook" 가능
        self._drop_job = None
        self._drop_tmp = []             # 끌어다 놓기 창에서 받은 임시 파일 (종료 시 삭제)
        self.vt = 0.0
        self.playing = False
        self.speed = float(self.settings.get("playback.speed", 10))
        self._last_tick = 0.0
        self._last_slow = 0.0
        self._worker = None
        self._inputs = []
        self._origin = ""
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1600, 980)
        self.setAcceptDrops(True)
        self._build_ui()
        self._build_actions()
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self._tick)
        self._last_hud = 0.0
        if self.rules_error:
            QTimer.singleShot(0, lambda: QMessageBox.critical(self, "규칙 파일 오류", self.rules_error))

    # ------------------------------------------------------------ UI 구성
    def _build_ui(self):
        self.map_view = MapView()
        self.map_view.mouseWorld.connect(self._on_mouse_world)
        self.map_view.markerClicked.connect(self.jump_to_event)
        self.map_view.mouseDoubleClickEvent = lambda _e: self.map_view.fit()

        self.map_box = QWidget()
        grid = QGridLayout(self.map_box)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.addWidget(self.map_view, 0, 0)
        self.hud = QLabel("")
        self.hud.setObjectName("hud")
        self.hud.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.hud.setTextFormat(Qt.RichText)
        grid.addWidget(self.hud, 0, 0, Qt.AlignTop | Qt.AlignLeft)
        self.hud_err = QLabel("")
        self.hud_err.setObjectName("hudErr")
        self.hud_err.setAttribute(Qt.WA_TransparentForMouseEvents)
        grid.addWidget(self.hud_err, 0, 0, Qt.AlignTop | Qt.AlignRight)
        self.hud_res = QLabel("")                       # 자원 상태창 (메모리·AP 온도)
        self.hud_res.setObjectName("hud")
        self.hud_res.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.hud_res.setTextFormat(Qt.RichText)
        grid.addWidget(self.hud_res, 0, 0, Qt.AlignBottom | Qt.AlignRight)
        self.empty = QLabel("로그 압축 파일(zip / AMR_LOG.tar.gz)이나 폴더를 여기에 끌어다 놓거나\n"
                            "[열기] 또는 [NAS] 버튼으로 불러오세요.")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setObjectName("empty")
        self.empty.setAttribute(Qt.WA_TransparentForMouseEvents)
        grid.addWidget(self.empty, 0, 0, Qt.AlignCenter)
        for w in (self.hud, self.hud_err, self.hud_res):
            w.hide()

        self.playbar = PlaybackBar()
        self.playbar.set_speed(self.speed)
        self.playbar.playToggled.connect(self.toggle_play)
        self.playbar.stepEvent.connect(self.step_event)
        self.playbar.gotoEdge.connect(lambda e: self.set_vt(0.0 if e == 0 else self._vt_end(), "seek"))
        self.playbar.speedChanged.connect(self._set_speed)
        self.playbar.jumpToTime.connect(self.jump_to_time)
        self.playbar.jumpPressed.connect(self.pause)
        self.playbar.jumpSeconds.connect(self.jump_seconds)
        self.playbar.bar.seek.connect(lambda vt: self.set_vt(vt, "seek"))
        self.playbar.bar.time_text = lambda vt: fmt_time(self.session.time_at(vt)) if self.session else ""

        center = QWidget()
        cl = QVBoxLayout(center)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        cl.addWidget(self.map_box, 1)
        cl.addWidget(self.playbar)

        self.layer_panel = LayerPanel()
        self.layer_panel.set_history_minutes(int(self.settings.get("display.history_minutes", 10)))
        self.layer_panel.changed.connect(self._apply_display)
        self._restore_view_prefs()
        self.layer_panel.trailChanged.connect(self._on_trail_changed)
        for chk in (self.layer_panel.chk_hud, self.layer_panel.chk_hud_res):
            chk.toggled.connect(lambda _on: self._save_view_prefs())
        self.layer_panel.robotSizeChanged.connect(lambda v: (self.map_view.set_robot_size(v), self._save_view_prefs()))
        self.layer_panel.stationSizeChanged.connect(lambda v: (self.map_view.set_station_size(v), self._save_view_prefs()))
        self.layer_panel.layerSizeChanged.connect(lambda lid, v: (self.map_view.set_layer_size(lid, v), self._save_view_prefs()))
        left = QScrollArea()
        left.setWidget(self.layer_panel)
        left.setWidgetResizable(True)
        left.setMinimumWidth(270)

        self.event_panel = EventPanel()
        self.event_panel.activated.connect(self.jump_to_event)
        self.error_panel = ErrorPanel()
        self.error_panel.activated.connect(self.jump_to_event)
        self.summary = SummaryPanel()
        self.summary.activated.connect(self.jump_to_event)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.event_panel, "이벤트")
        self.tabs.addTab(self.error_panel, "에러")
        self.tabs.addTab(self.summary, "요약")
        self.tabs.setMinimumWidth(360)

        top = QSplitter(Qt.Horizontal)
        top.addWidget(left)
        top.addWidget(center)
        top.addWidget(self.tabs)
        top.setStretchFactor(1, 1)
        top.setSizes([285, 860, 455])
        top.setChildrenCollapsible(False)

        self.log_view = LogView()
        self.log_view.jumpRequested.connect(self._jump_to_line)
        main = QSplitter(Qt.Vertical)
        main.addWidget(top)
        main.addWidget(self.log_view)
        main.setStretchFactor(0, 1)
        main.setSizes([700, 250])
        self.setCentralWidget(main)

        self.lbl_source = QLabel("")
        self.lbl_mouse = QLabel("")
        self.lbl_ws = QLabel("")
        self.lbl_ws.setObjectName("hint")
        sb = self.statusBar()
        sb.addWidget(self.lbl_source, 1)
        sb.addPermanentWidget(self.lbl_mouse)
        sb.addPermanentWidget(self.lbl_ws)

    def _build_actions(self):
        tb = QToolBar("메인")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.addToolBar(tb)
        sp = self.style().standardIcon

        def act(icon, text, slot, shortcut=None, tip=""):
            a = QAction(sp(icon), text, self)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            a.setToolTip(tip or text)
            tb.addAction(a)
            return a

        act(QStyle.SP_DialogOpenButton, "열기", self.open_files, "Ctrl+O", "로그 압축 파일(zip/tar.gz) 또는 airbot_log*.txt 열기")
        act(QStyle.SP_DirOpenIcon, "폴더 열기", self.open_folder, "Ctrl+Shift+O", "압축이 풀린 로그 폴더 열기")
        act(QStyle.SP_DriveNetIcon, "NAS", self.open_nas, "Ctrl+N", "NAS 로그 폴더에서 불러오기 (읽기 전용)")
        if windrop.available():          # WSL: 앱 창에 바로 끌어다 놓을 수 없음 (WSLg 미지원)
            act(QStyle.SP_ArrowDown, "끌어다 놓기", self.open_drop_window, None,
                "Windows 쪽에 작은 창을 띄워, 거기에 놓은 로그·메일 파일이나 Outlook 메일을 받습니다\n"
                "(WSL 에서는 이 창에 바로 끌어다 놓을 수 없음)")
        tb.addSeparator()
        act(QStyle.SP_FileDialogContentsView, "맵 교체", self.replace_map, None, "다른 맵 폴더/압축으로 교체")
        act(QStyle.SP_DialogSaveButton, "화면 저장", self.export_png, "Ctrl+S", "현재 맵 화면을 PNG 로 저장")
        act(QStyle.SP_FileIcon, "이벤트 CSV", self.export_csv, None, "현재 필터의 이벤트 목록을 CSV 로 저장")
        tb.addSeparator()
        self.act_analysis = act(QStyle.SP_FileDialogInfoView, "분석", self.open_analysis, None,
                                "AI 현상 분석: 요청(구어체/메일) + 로그 타임라인 → Claude → 현상 분석표(엑셀)")
        tb.addSeparator()
        act(QStyle.SP_ComputerIcon, "NAS 경로", self.edit_nas_path, None, "NAS 경로 설정")
        act(QStyle.SP_MessageBoxInformation, "정보", self.show_about)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        # 테마: 아이콘 클릭 = 라이트/다크 전환, 화살표 = 메뉴에서 선택
        self.btn_theme = QToolButton()
        self.btn_theme.setPopupMode(QToolButton.MenuButtonPopup)
        self.btn_theme.setIconSize(QSize(20, 20))
        self.btn_theme.setToolTip("테마 (아이콘: 전환, 화살표: 선택)")
        menu = QMenu(self.btn_theme)
        group = QActionGroup(self)
        self.theme_actions = {}
        for key, label in (("light", "라이트"), ("dark", "다크")):
            a = QAction(style.theme_icon(key), label, self)
            a.setCheckable(True)
            a.triggered.connect(lambda _=False, k=key: self.apply_theme(k))
            group.addAction(a)
            menu.addAction(a)
            self.theme_actions[key] = a
        self.btn_theme.setMenu(menu)
        self.btn_theme.clicked.connect(lambda: self.apply_theme("light" if style.T().dark else "dark"))
        tb.addWidget(self.btn_theme)
        self._update_theme_button()

        for key, fn in (("Space", self.toggle_play), ("Left", lambda: self.step_event(-1)),
                        ("Right", lambda: self.step_event(1)), ("Home", lambda: self.set_vt(0.0, "seek")),
                        ("End", lambda: self.set_vt(self._vt_end(), "seek")),
                        ("Ctrl+F", lambda: self.log_view.search.setFocus()),
                        ("Ctrl+E", lambda: self.event_panel.search.setFocus())):
            QShortcut(QKeySequence(key), self, activated=fn)

    # ------------------------------------------------------------ 불러오기
    def open_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "로그 열기", "", "로그 (*.zip *.tar.gz *.tgz *.tar *.txt);;모든 파일 (*)")
        if files:
            self.load_inputs(files)

    def open_folder(self):
        d = QFileDialog.getExistingDirectory(self, "로그 폴더 열기")
        if d:
            self.load_inputs([d])

    def open_nas(self):
        dlg = NasDialog(self.settings, self)
        if not dlg.exec() or dlg.selected is None:
            return
        entry, source = dlg.selected, dlg.source
        ws, settings, rules = self.ws, self.settings, self.rules

        def job(progress):
            ws.clear()
            local = source.copy_to(entry.rel, ws.new_dir("nas"), size=entry.size, progress=progress)
            # 복사본은 [분석]에서 SoC 로그를 읽을 때 쓰므로 남겨 둠 (다른 로그를 열거나 종료하면 임시 폴더와 함께 삭제)
            return load_session([local], ws, settings, rules, progress, origin="NAS: " + entry.rel)

        self._inputs, self._origin = [], "NAS: " + entry.rel
        self._run_load(job, f"NAS: {entry.name}")

    def load_inputs(self, paths, map_override=None):
        if self.rules is None:
            QMessageBox.critical(self, "규칙 파일 오류", self.rules_error)
            return
        paths = [str(p) for p in paths]
        ws, settings, rules = self.ws, self.settings, self.rules

        def job(progress):
            ws.clear()
            return load_session(paths, ws, settings, rules, progress, map_override=map_override)

        self._inputs, self._origin = paths, ""
        self._run_load(job, Path(paths[0]).name)

    def _run_load(self, job, title):
        if self._worker is not None:
            return
        self.pause()
        dlg = LoadingDialog(f"불러오는 중: {title}", self)
        dlg.show()
        self._worker = BackgroundJob(job, self)

        def finish():
            self._worker = None
            dlg.close()
            dlg.deleteLater()

        def done(session):
            finish()
            self.set_session(session)

        def failed(msg):
            finish()
            self._pending_mails = None
            QMessageBox.warning(self, "불러오기 실패", msg)

        self._worker.on_progress = dlg.set_progress
        self._worker.on_done = done
        self._worker.on_failed = failed
        self._worker.start()

    # ------------------------------------------------------------ 세션 표시
    def set_session(self, S):
        self.session = S
        self.vt = 0.0
        layers = S.rules.layers
        self.map_view.set_session(S, layers)
        self.layer_panel.set_layers(layers, {k: len(v) for k, v in S.markers.items()}, self.map_view.layer_sizes)
        self.event_panel.set_session(S)
        self.error_panel.set_session(S)
        self.summary.set_session(S)
        self.log_view.set_session(S)
        self.playbar.bar.set_range(S.vt_end)
        self.playbar.bar.set_marks(self._timeline_marks(S))
        self.empty.setVisible(False)
        self._apply_hud_visibility()
        src = S.source
        label = src.label + (f"  ·  {src.serial}" if src.serial else "")
        self.setWindowTitle(f"{APP_NAME} {__version__}  —  {label}")
        origin = self._origin or src.origin
        self.lbl_source.setText(f"{origin}  ·  {S.stats['lines']:,}줄  ·  이벤트 {len(S.events):,}건"
                                + ("" if S.map else "  ·  맵 없음"))
        self.lbl_ws.setText(f"임시 폴더: {self.ws.path} (종료 시 자동 삭제)" if self.ws.path else "")
        self._apply_display()
        self.set_vt(0.0, "seek")
        mails, self._pending_mails = self._pending_mails, None
        if mails == "outlook":                            # 로그를 열기 전에 끌어다 놓은 Outlook 메일
            self.open_analysis(outlook_mail=True)
        elif mails:                                       # 로그와 함께 끌어다 놓은 메일 → [분석] 창에 넣음
            self.open_analysis(mails)

    def _timeline_marks(self, S):
        marks = []
        last_day = None
        for vt, t in zip(S.tl_vt, S.tl_t):
            day = int(t // 86400)
            if last_day is None or day > last_day:
                if last_day is not None:
                    marks.append((vt, "day", datetime.fromtimestamp(t, tz=timezone.utc).strftime("%m/%d")))
                last_day = day
        marks += [(b.vt, "boot", f"부팅 {fmt_time(b.t, False)}") for b in S.boots]
        marks += [(j["vt"], "jump", f"시간 {'역행' if j['kind'] == 'back' else '점프'}: "
                                    f"{fmt_time(j['t_from'], False)} → {fmt_time(j['t_to'], False)}") for j in S.jumps]
        marks += [(e.vt, "error", e.text) for e in S.error_events if e.data.get("status") == "OCCURRED"]
        return marks

    def _apply_display(self):
        mv = self.map_view
        lp = self.layer_panel
        mv.show_trail = lp.chk_trail.isChecked()
        mv.follow = lp.chk_follow.isChecked()
        mv.layer_visible.update(lp.layer_visibility())
        mv.set_static_visible("keepout", lp.chk_keepout.isChecked())
        mv.set_static_visible("station", lp.chk_station.isChecked())
        self._apply_hud_visibility()
        if self.session:
            mv.update_frame(self.vt, lp.history_sec())

    def _apply_hud_visibility(self):
        lp = self.layer_panel
        has = self.session is not None
        self.hud.setVisible(has and lp.chk_hud.isChecked())
        self.hud_res.setVisible(has and lp.chk_hud_res.isChecked())
        if self.hud_res.isVisible():                      # 숨겨져 있던 동안 갱신되지 않았으므로 다시 그림
            self.hud_res.setText(self._resource_text(self.session, self.vt))

    # ---- 테마
    def _update_theme_button(self):
        cur = style.T().name
        self.btn_theme.setIcon(style.theme_icon(cur))
        self.theme_actions[cur].setChecked(True)

    def apply_theme(self, name: str):
        style.apply_style(QApplication.instance(), name)
        self._update_theme_button()
        lp = self.layer_panel
        self.map_view.apply_theme({"keepout": lp.chk_keepout.isChecked(), "station": lp.chk_station.isChecked()})
        self.playbar.bar.update()
        self.log_view.view.viewport().update()
        self.event_panel.apply_theme()
        S = self.session
        if S is not None:
            lp.set_layers(S.rules.layers, {k: len(v) for k, v in S.markers.items()}, self.map_view.layer_sizes)
            self.error_panel.set_session(S)
            self.summary.set_session(S)
            self._apply_display()
            self.set_vt(self.vt, "seek")
        try:
            self.settings.set_user("display.theme", name)
        except OSError:
            pass

    # ---- 표시 설정 (사용자 설정 파일에 저장)
    def _restore_view_prefs(self):
        p = self.settings.get("display.view", {}) or {}
        self.layer_panel.apply_prefs(p)
        mv = self.map_view
        mv.layer_sizes = {str(k): int(v) for k, v in (p.get("layer_sizes") or {}).items()}
        mv.robot_size = self.layer_panel.spn_robot.value()
        mv.station_size = self.layer_panel.spn_station.value()
        mv.set_trail(self.layer_panel.trail_mode(), self.layer_panel.spn_trail_count.value(),
                     float(self.layer_panel.spn_trail_width.value()))
        self._prefs_timer = QTimer(self)
        self._prefs_timer.setSingleShot(True)
        self._prefs_timer.setInterval(800)
        self._prefs_timer.timeout.connect(self._write_view_prefs)

    def _save_view_prefs(self):
        self._prefs_timer.start()

    def _write_view_prefs(self):
        try:
            self.settings.set_user("display.view", self.layer_panel.prefs())
        except OSError:
            pass

    def _on_trail_changed(self, mode, count, width):
        self.map_view.set_trail(mode, count, width)
        self._save_view_prefs()

    # ------------------------------------------------------------ 시간 이동
    def _vt_end(self):
        return self.session.vt_end if self.session else 0.0

    def set_vt(self, vt: float, origin: str = "seek", line: int = None):
        S = self.session
        if S is None:
            return
        self.vt = min(max(vt, 0.0), S.vt_end)
        play = origin == "play"
        # 재생·멈춤·±초/타임라인 이동은 카메라가 부드럽게 따라감, 이벤트·로그 줄로 건너뛸 때는 바로 맞춤
        self.map_view.update_frame(self.vt, self.layer_panel.history_sec(), smooth=origin in ("play", "pause", "seek"))
        self.playbar.bar.set_value(self.vt)
        t = S.time_at(self.vt)
        now = time.monotonic()
        if not play or now - self._last_hud > HUD_SEC:      # 재생 중 글자 갱신은 줄여 부하를 지도에 씀
            self._last_hud = now
            self.playbar.lbl_time.setText(fmt_time(t))
            self.playbar.lbl_pos.setText(f"  재생 {self._hms(self.vt)} / {self._hms(S.vt_end)}")
            self._update_hud(t)
            self.error_panel.update_vt(self.vt)
        if origin != "play" or now - self._last_slow > SLOW_UI_SEC:
            self._last_slow = now
            self.event_panel.highlight_vt(self.vt)
            self.log_view.show_line(S.line_at(self.vt) if line is None else line, force=line is not None)
            if not self.playbar.dt.hasFocus():
                self.playbar.set_time_hint(t)

    @staticmethod
    def _hms(sec):
        sec = int(sec)
        return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"

    def _update_hud(self, t):
        S = self.session
        vt = self.vt

        def v(name):
            val = S.track_at(name, vt)
            return html.escape(str(val)) if val is not None else "-"

        pose = S.pose_at(vt)
        ver = S.track_at("version", vt)                 # SW:FW:AI (SoC 로 보내는 버전, 없으면 otalog OTA 완료 버전)
        ver = html.escape(str(ver)) if ver is not None else v("sw_version")
        lines = [f"<span style='font-size:11pt;font-weight:bold'>{fmt_time(t)}</span>",
                 f"상태 <b>{v('robot_state')}</b> · 주행 <b>{v('navi_state')}</b> · <b>{v('robot_status')}</b>",
                 f"배터리 <b>{v('battery')}</b> · 스테이션 <b>{v('station')}</b>",
                 f"버전(SW:FW:AI) <b>{ver}</b>"]
        extra = [f"{label} <b>{v(name)}</b>" for label, name in
                 (("라이다", "lidar"), ("바닥ToF", "floor_tof"), ("팔로우", "follow"))
                 if S.track_at(name, vt) is not None]
        if extra:
            lines.append(" · ".join(extra))
        if pose:
            lines.append(f"<span style='color:#b0bec5'>위치 ({pose[0]:.2f}, {pose[1]:.2f}) {pose[2]:.0f}°</span>")
        self.hud.setText("<br>".join(lines))
        if self.hud_res.isVisible():
            self.hud_res.setText(self._resource_text(S, vt))
        active = S.active_errors(vt)
        if active:
            self.hud_err.setText("활성 에러: " + ", ".join(active.keys()))
            self.hud_err.show()
        else:
            self.hud_err.hide()

    @staticmethod
    def _ago(S, vt, vt_rec):
        sec = max(S.rt_at(vt) - S.rt_at(vt_rec), 0.0)
        if sec < 60:
            return f"{sec:.0f}초 전"
        if sec < 3600:
            return f"{sec / 60:.0f}분 전"
        return f"{sec / 3600:.1f}시간 전"

    def _resource_text(self, S, vt):
        """자원 상태: 메모리(10분 주기 기록), AP 온도(임계 초과 경고 때만 기록), SoC 온도(있으면)."""
        hint = style.T().hint
        rows = ["<b>자원 상태</b>"]
        mem, mvt = S.track_point("memory", vt)
        rows.append(f"메모리 <b>{html.escape(str(mem))}</b> <span style='color:{hint}'>({self._ago(S, vt, mvt)})</span>"
                    if mem is not None else f"메모리 <span style='color:{hint}'>기록 없음</span>")
        ap, avt = S.track_point("ap_temp", vt)
        if ap is None:
            rows.append(f"AP 온도 <span style='color:{hint}'>경고 기록 없음 (임계 미만)</span>")
        else:
            rows.append(f"AP 온도 <b>{html.escape(str(ap))}</b> <span style='color:{hint}'>({self._ago(S, vt, avt)} 경고 기록)</span>")
        soc, svt = S.track_point("soc_temp", vt)
        if soc is not None:
            rows.append(f"SoC 온도 <b>{html.escape(str(soc))}</b> <span style='color:{hint}'>({self._ago(S, vt, svt)})</span>")
        return "<br>".join(rows)

    def jump_to_event(self, ev):
        if self.session is None or ev is None:
            return
        self.pause()
        self.set_vt(ev.vt, "event", line=self.session.global_line(ev.fi, ev.ln))
        if ev.x is not None:
            self.map_view.center_on_world(ev.x, ev.y)

    def _jump_to_line(self, g):
        if self.session:
            self.pause()
            self.set_vt(self.session.vt_at_line(g), "line", line=g)

    def jump_to_time(self, qdt):
        if self.session is None:
            return
        d, tm = qdt.date(), qdt.time()
        t = calendar.timegm((d.year(), d.month(), d.day(), tm.hour(), tm.minute(), tm.second(), 0, 0, 0))
        vt = self.session.vt_for_time(t)
        if vt is None:
            self.statusBar().showMessage("해당 시각 이후의 로그가 없습니다.", 4000)
            return
        self.pause()
        self.set_vt(vt, "seek")

    def jump_seconds(self, sec: float):
        """실제 로그 시간 기준 ±sec 이동 (긴 공백은 건너뜀). 재생 중이면 일시정지."""
        S = self.session
        if S is None:
            return
        self.pause()
        self.set_vt(S.vt_at_rt(S.rt_at(self.vt) + sec), "seek")

    def step_event(self, direction: int):
        if self.session is None:
            return
        vts = self.event_panel.row_vts
        if not vts:
            return
        if direction > 0:
            i = bisect.bisect_right(vts, self.vt + 1e-6)
        else:
            i = bisect.bisect_left(vts, self.vt - 1e-6) - 1
        if 0 <= i < len(vts):
            self.jump_to_event(self.event_panel.model.event_at(i))

    # ------------------------------------------------------------ 재생
    def _set_speed(self, s):
        self.speed = s

    def toggle_play(self):
        if self.playing:
            self.pause()
        elif self.session:
            if self.vt >= self.session.vt_end:
                self.vt = 0.0
            self.playing = True
            self._last_tick = time.monotonic()
            self.timer.start(TICK_MS)
            self.playbar.set_playing(True)

    def pause(self):
        if self.playing:
            self.playing = False
            self.timer.stop()
            self.playbar.set_playing(False)
            if self.session:
                self.set_vt(self.vt, "pause")

    def _tick(self):
        now = time.monotonic()
        dt, self._last_tick = now - self._last_tick, now
        vt = self.vt + dt * self.speed
        if vt >= self.session.vt_end:
            self.set_vt(self.session.vt_end, "seek")
            self.pause()
            return
        self.set_vt(vt, "play")

    # ------------------------------------------------------------ 기타 기능
    def _on_mouse_world(self, x, y):
        self.lbl_mouse.setText(f"마우스 ({x:.2f}, {y:.2f}) m")

    def replace_map(self):
        if self.session is None:
            return
        path = QFileDialog.getExistingDirectory(self, "맵 폴더 선택 (airbot_map_00.yaml 이 있는 폴더)")
        if not path:
            return
        try:
            map_dir = prepare_map(path, self.ws, self.settings.get("workspace.skip_members", []))
            self.session.map = load_map(map_dir)
            self.session.source.map_dir = map_dir
        except (SourceError, OSError, ValueError, KeyError) as e:
            QMessageBox.warning(self, "맵 교체 실패", str(e))
            return
        self.map_view.set_session(self.session, self.session.rules.layers)
        self._apply_display()
        self.set_vt(self.vt, "seek")

    def export_png(self):
        if self.session is None:
            return
        name = f"{self.session.source.serial or 'airbotlog'}_{fmt_time(self.session.time_at(self.vt), False).replace(':', '').replace(' ', '_')}.png"
        path, _ = QFileDialog.getSaveFileName(self, "화면 저장", name, "PNG (*.png)")
        if path:
            self.map_box.grab().save(path)
            self.statusBar().showMessage(f"저장: {path}", 4000)

    def export_csv(self):
        if self.session is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "이벤트 CSV 저장", "events.csv", "CSV (*.csv)")
        if not path:
            return
        S = self.session
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["시각", "분류", "레벨", "노드", "내용", "x", "y", "파일", "줄"])
            for e in self.event_panel.visible_events():
                c = S.rules.cat_by_id.get(e.cat)
                w.writerow([fmt_time(e.t), c.name if c else e.cat, e.level, e.node, e.text,
                            "" if e.x is None else f"{e.x:.3f}", "" if e.y is None else f"{e.y:.3f}",
                            S.files[e.fi].name, e.ln + 1])
        self.statusBar().showMessage(f"저장: {path}", 4000)

    def open_analysis(self, mails=None, outlook_mail=False):
        if self.session is None:
            if mails or outlook_mail:
                self._pending_mails = list(mails) if mails else "outlook"
                QMessageBox.information(self, "분석", "먼저 로그를 불러오세요.\n"
                                        "로그를 열면 끌어다 놓은 메일을 [분석] 창에 바로 넣습니다.")
            else:
                QMessageBox.information(self, "분석", "먼저 로그를 불러오세요.")
            return
        from .analysis_dialog import AnalysisDialog
        dlg = getattr(self, "_analysis", None)
        if dlg is None or not dlg.isVisible() or dlg.S is not self.session:
            self._analysis = dlg = AnalysisDialog(self)      # 비모달: 열어 둔 채로 재생·이동 가능
        dlg.show()
        dlg.raise_()
        if mails:
            dlg.load_mail_paths(mails)
        elif outlook_mail:
            dlg.fetch_outlook()

    def edit_nas_path(self):
        NasPathDialog(self.settings, self).exec()

    def show_about(self):
        QMessageBox.information(
            self, "정보",
            f"<b>{APP_NAME} {__version__}</b><br><br>"
            f"규칙 파일: {self.rules.path if self.rules else '-'}<br>"
            f"사용자 설정: {user_config_dir() / 'settings.yaml'}<br>"
            f"임시 폴더: {self.ws.path or '-'} (종료 시 자동 삭제)<br><br>"
            "단축키: Space 재생/정지 · ←/→ 이전/다음 이벤트 · Home/End · Ctrl+F 로그 검색 · Ctrl+E 이벤트 검색")

    # ------------------------------------------------------------ 드래그앤드롭/종료
    def dragEnterEvent(self, e):
        from .analysis_dialog import _is_outlook_drag
        if e.mimeData().hasUrls() or _is_outlook_drag(e.mimeData()):
            e.acceptProposedAction()

    def dropEvent(self, e):
        from .analysis_dialog import _is_outlook_drag
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.toLocalFile()]
        if paths:
            e.acceptProposedAction()
            self.open_dropped(paths)
        elif _is_outlook_drag(e.mimeData()):                   # Outlook 목록에서 끌어온 메일
            e.acceptProposedAction()
            self.open_analysis(outlook_mail=True)

    def open_dropped(self, paths):
        """끌어다 놓은 파일: 메일은 [분석] 창으로, 나머지는 로그로 (로그와 함께면 로그를 연 뒤 메일)."""
        mails = [p for p in paths if areq.is_mail_file(p)]
        logs = [p for p in paths if p not in mails]
        if logs:
            self._pending_mails = mails or None
            self.load_inputs(logs)
        else:
            self.open_analysis(mails)

    def open_drop_window(self):
        """WSL: Windows 쪽 '끌어다 놓기 창'에 놓은 로그·메일 파일이나 Outlook 메일을 받음."""
        if self._drop_job is not None:
            self.statusBar().showMessage("이미 '끌어다 놓기' 창이 열려 있습니다 (Windows 화면 가운데).", 5000)
            return
        dw = windrop.DropWindow("끌어다 놓기 — 로그 분석",
                                "로그 압축(zip·tar.gz)·airbot_log, 메일 파일이나\nOutlook 메일을 여기에 끌어다 놓으세요")
        self.statusBar().showMessage("Windows 화면 가운데 '끌어다 놓기' 창에 로그나 메일을 놓으세요.")
        self._drop_window = dw
        self._drop_job = BackgroundJob(lambda _progress: dw.run(), self)

        def done(res):
            self._drop_job = None
            self.statusBar().clearMessage()
            if res.tmpdir is not None:
                self._drop_tmp.append(res)
            if res.outlook:
                self.open_analysis(outlook_mail=True)
            elif res.files:
                self.open_dropped(res.files)
            elif res.skipped:
                QMessageBox.warning(self, "끌어다 놓기", "읽을 수 없는 위치의 파일이라 가져오지 못했습니다:\n"
                                    + "\n".join(res.skipped) + "\n(NAS 로그는 [NAS] 버튼으로 여세요)")

        def failed(msg):
            self._drop_job = None
            self.statusBar().clearMessage()
            QMessageBox.warning(self, "끌어다 놓기", msg)

        self._drop_job.on_done = done
        self._drop_job.on_failed = failed
        self._drop_job.start()

    def closeEvent(self, e):
        self.pause()
        if self._drop_job is not None:                    # 열려 있는 끌어다 놓기 창 닫기
            self._drop_window.cancel()
            self._drop_job.detach()
            self._drop_job = None
        for res in self._drop_tmp:
            res.cleanup()
        if self._prefs_timer.isActive():   # 저장 대기 중인 표시 설정은 닫기 전에 저장
            self._prefs_timer.stop()
            self._write_view_prefs()
        if self._worker is not None:
            self._worker.detach()
            self._worker.wait(15)      # 로드 중이면 끝날 때까지 잠시 대기 (임시 폴더 정리 전, 초)
        self.ws.cleanup()
        super().closeEvent(e)

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

from .. import APP_NAME, __version__
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

TICK_MS = 40
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
        self.timer.timeout.connect(self._tick)
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
        self.empty = QLabel("로그 압축 파일(zip / AMR_LOG.tar.gz)이나 폴더를 여기에 끌어다 놓거나\n"
                            "[열기] 또는 [NAS] 버튼으로 불러오세요.")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setObjectName("empty")
        self.empty.setAttribute(Qt.WA_TransparentForMouseEvents)
        grid.addWidget(self.empty, 0, 0, Qt.AlignCenter)
        for w in (self.hud, self.hud_err):
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
        tb.addSeparator()
        act(QStyle.SP_FileDialogContentsView, "맵 교체", self.replace_map, None, "다른 맵 폴더/압축으로 교체")
        act(QStyle.SP_DialogSaveButton, "화면 저장", self.export_png, "Ctrl+S", "현재 맵 화면을 PNG 로 저장")
        act(QStyle.SP_FileIcon, "이벤트 CSV", self.export_csv, None, "현재 필터의 이벤트 목록을 CSV 로 저장")
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
            session = load_session([local], ws, settings, rules, progress, origin="NAS: " + entry.rel)
            if local.suffix.lower() in (".zip", ".tar", ".gz", ".tgz"):
                local.unlink()  # 압축 해제 후 복사본 삭제 (임시 폴더 용량 절약)
            return session

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
        for w in (self.hud,):
            w.show()
        src = S.source
        label = src.label + (f"  ·  {src.serial}" if src.serial else "")
        self.setWindowTitle(f"{APP_NAME} {__version__}  —  {label}")
        origin = self._origin or src.origin
        self.lbl_source.setText(f"{origin}  ·  {S.stats['lines']:,}줄  ·  이벤트 {len(S.events):,}건"
                                + ("" if S.map else "  ·  맵 없음"))
        self.lbl_ws.setText(f"임시 폴더: {self.ws.path} (종료 시 자동 삭제)" if self.ws.path else "")
        self._apply_display()
        self.set_vt(0.0, "seek")

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
        if self.session:
            mv.update_frame(self.vt, lp.history_sec())

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
        self.map_view.update_frame(self.vt, self.layer_panel.history_sec())
        self.playbar.bar.set_value(self.vt)
        t = S.time_at(self.vt)
        self.playbar.lbl_time.setText(fmt_time(t))
        self.playbar.lbl_pos.setText(f"  재생 {self._hms(self.vt)} / {self._hms(S.vt_end)}")
        self._update_hud(t)
        self.error_panel.update_vt(self.vt)
        now = time.monotonic()
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
        lines = [f"<span style='font-size:11pt;font-weight:bold'>{fmt_time(t)}</span>",
                 f"상태 <b>{v('robot_state')}</b> · 주행 <b>{v('navi_state')}</b> · <b>{v('robot_status')}</b>",
                 f"배터리 <b>{v('battery')}</b> · 스테이션 <b>{v('station')}</b> · SW <b>{v('sw_version')}</b>"]
        extra = [f"{label} <b>{v(name)}</b>" for label, name in
                 (("바닥ToF", "floor_tof"), ("라이다", "scan"), ("팔로우", "follow"))
                 if S.track_at(name, vt) is not None]
        if extra:
            lines.append(" · ".join(extra))
        if pose:
            lines.append(f"<span style='color:#b0bec5'>위치 ({pose[0]:.2f}, {pose[1]:.2f}) {pose[2]:.0f}°</span>")
        self.hud.setText("<br>".join(lines))
        active = S.active_errors(vt)
        if active:
            self.hud_err.setText("활성 에러: " + ", ".join(active.keys()))
            self.hud_err.show()
        else:
            self.hud_err.hide()

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
                self.set_vt(self.vt, "seek")

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
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.toLocalFile()]
        if paths:
            self.load_inputs(paths)
            e.acceptProposedAction()

    def closeEvent(self, e):
        self.pause()
        if self._worker is not None:
            self._worker.detach()
            self._worker.wait(15)      # 로드 중이면 끝날 때까지 잠시 대기 (임시 폴더 정리 전, 초)
        self.ws.cleanup()
        super().closeEvent(e)

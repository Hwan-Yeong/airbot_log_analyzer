"""좌측 표시 설정 / 우측 이벤트·에러·요약 패널."""
import bisect
import html
import re
from collections import Counter

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QGridLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QLayout, QLineEdit, QSpinBox, QTableView, QTableWidget,
                               QTableWidgetItem, QTextBrowser, QToolButton, QVBoxLayout, QWidget)

from ..model import fmt_duration, fmt_time
from . import style
from .map_view import shape_path

HISTORY_CHOICES = [("1분", 60), ("5분", 300), ("10분", 600), ("30분", 1800), ("1시간", 3600),
                   ("3시간", 10800), ("전체", 0)]


# ---------------------------------------------------------------- 검색어
def parse_query(text: str):
    """공백=AND, '|'·','·or=OR, '-단어'·'!단어'=제외."""
    low = (text or "").strip().lower()
    if not low:
        return [], []
    norm = re.sub(r"\bor\b", "|", low.replace(",", "|"))
    groups, excludes = [], []
    for part in norm.split("|"):
        terms = []
        for t in re.sub(r"\band\b", " ", part).split():
            if len(t) > 1 and t[0] in "-!":
                excludes.append(t[1:])
            else:
                terms.append(t)
        if terms:
            groups.append(terms)
    return groups, excludes


def match_query(text_low: str, groups, excludes) -> bool:
    if any(x in text_low for x in excludes):
        return False
    return not groups or any(all(t in text_low for t in g) for g in groups)


def marker_icon(shape: str, color: str, size: int = 14) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.translate(size / 2, size / 2)
    path = shape_path(shape, size - 3)
    if shape == "cross":
        p.setPen(QPen(QColor(color), 2))
    else:
        p.setPen(QColor(style.T().marker_edge))
        p.setBrush(QColor(color))
    p.drawPath(path)
    p.end()
    return QIcon(pix)


class FlowLayout(QLayout):
    """폭에 맞춰 줄바꿈되는 레이아웃 (분류 칩용)."""

    def __init__(self, parent=None, spacing=4):
        super().__init__(parent)
        self._items = []
        self._sp = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        return size

    def _layout(self, rect, test_only):
        x, y, line_h = rect.x(), rect.y(), 0
        for it in self._items:
            w, h = it.sizeHint().width(), it.sizeHint().height()
            if x + w > rect.right() and line_h > 0:
                x, y, line_h = rect.x(), y + line_h + self._sp, 0
            if not test_only:
                it.setGeometry(QRect(QPoint(x, y), it.sizeHint()))
            x += w + self._sp
            line_h = max(line_h, h)
        return y + line_h - rect.y()


# ---------------------------------------------------------------- 좌측: 표시 설정
TRAIL_MODES = [("선", "line"), ("포즈", "poses")]


def _spin(lo, hi, value, suffix="", tip=""):
    sp = QSpinBox()
    sp.setRange(lo, hi)
    sp.setValue(int(value))
    if suffix:
        sp.setSuffix(suffix)
    sp.setToolTip(tip)
    sp.setFixedWidth(64)
    sp.setKeyboardTracking(False)   # 입력 중에는 반영하지 않고 Enter/포커스 이동 시 반영
    return sp


class LayerPanel(QWidget):
    changed = Signal()                         # 표시/숨김, 누적 범위
    trailChanged = Signal(str, int, float)     # 모드, 포즈 개수, 선 굵기
    robotSizeChanged = Signal(int)
    stationSizeChanged = Signal(int)
    layerSizeChanged = Signal(str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        g = QGroupBox("표시")
        grid = QGridLayout(g)
        grid.setColumnStretch(0, 1)
        self.chk_trail = QCheckBox("로봇 궤적")
        self.cmb_trail = QComboBox()
        for name, _m in TRAIL_MODES:
            self.cmb_trail.addItem(name)
        self.cmb_trail.setToolTip("선: 누적 표시 범위(최근 N분)의 이동 경로를 선으로\n포즈: 최근 N개 위치를 방향과 함께 표시 (기존 분석기 방식)")
        self.lbl_trail_opt = QLabel("선 굵기 (px)")
        self.spn_trail_width = _spin(1, 10, 2, "", "궤적 선 굵기 (px)")
        self.spn_trail_count = _spin(1, 500, 50, "", "표시할 최근 포즈 개수")
        self.chk_follow = QCheckBox("로봇 따라가기")
        self.chk_keepout = QCheckBox("금지구역")
        self.chk_station = QCheckBox("충전 스테이션")
        self.spn_station = _spin(10, 60, 22, "", "스테이션 아이콘 크기 (px)")
        self.spn_robot = _spin(8, 60, 18, "", "로봇 아이콘 크기 (px)")
        self.cmb_history = QComboBox()
        for name, _sec in HISTORY_CHOICES:
            self.cmb_history.addItem(name)
        self.cmb_history.setToolTip("선 궤적과 마커를 현재 시점 기준 얼마 전까지 보여줄지 (실제 시간)")
        grid.addWidget(self.chk_trail, 0, 0)
        grid.addWidget(self.cmb_trail, 0, 1, 1, 2)
        grid.addWidget(self.lbl_trail_opt, 1, 0)
        grid.addWidget(self.spn_trail_width, 1, 1)
        grid.addWidget(self.spn_trail_count, 1, 2)
        grid.addWidget(QLabel("로봇 크기 (px)"), 2, 0)
        grid.addWidget(self.spn_robot, 2, 1)
        grid.addWidget(self.chk_station, 3, 0)
        grid.addWidget(self.spn_station, 3, 1)
        grid.addWidget(self.chk_keepout, 4, 0)
        grid.addWidget(self.chk_follow, 5, 0, 1, 3)
        grid.addWidget(QLabel("누적 표시"), 6, 0)
        grid.addWidget(self.cmb_history, 6, 1, 1, 2)
        self.chk_hud = QCheckBox("상태창 (시각·상태·버전·라이다)")
        self.chk_hud_res = QCheckBox("자원 상태창 (메모리·AP 온도)")
        self.chk_hud_res.setToolTip("메모리: 10분 주기 기록, AP 온도: 임계 초과 경고가 있을 때만 기록됨")
        grid.addWidget(self.chk_hud, 7, 0, 1, 3)
        grid.addWidget(self.chk_hud_res, 8, 0, 1, 3)
        for c in (self.chk_trail, self.chk_follow, self.chk_keepout, self.chk_station, self.chk_hud, self.chk_hud_res):
            c.setChecked(True)
            c.toggled.connect(self.changed)
        self.cmb_history.currentIndexChanged.connect(self.changed)
        self.cmb_trail.currentIndexChanged.connect(self._trail_changed)
        self.spn_trail_width.valueChanged.connect(self._trail_changed)
        self.spn_trail_count.valueChanged.connect(self._trail_changed)
        self.spn_robot.valueChanged.connect(self.robotSizeChanged)
        self.spn_station.valueChanged.connect(self.stationSizeChanged)
        lay.addWidget(g)

        self.grp_layers = QGroupBox("마커 (표시 / 크기 px)")
        self.layers_grid = QGridLayout(self.grp_layers)
        self.layers_grid.setColumnStretch(0, 1)
        lay.addWidget(self.grp_layers)
        hint = QLabel("마커를 클릭하면 그 시점으로 이동합니다.\n휠: 확대/축소, 드래그: 이동, 더블클릭: 전체 보기")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        lay.addStretch(1)
        self.layer_checks = {}
        self.layer_spins = {}
        self._update_trail_widgets()

    # ---- 궤적
    def trail_mode(self) -> str:
        return TRAIL_MODES[self.cmb_trail.currentIndex()][1]

    def _update_trail_widgets(self):
        poses = self.trail_mode() == "poses"
        self.lbl_trail_opt.setText("최근 포즈 개수" if poses else "선 굵기 (px)")
        self.spn_trail_count.setVisible(poses)
        self.spn_trail_width.setVisible(not poses)

    def _trail_changed(self, *_):
        self._update_trail_widgets()
        self.trailChanged.emit(self.trail_mode(), self.spn_trail_count.value(), float(self.spn_trail_width.value()))

    # ---- 누적 범위
    def set_history_minutes(self, minutes: int):
        sec = int(minutes) * 60
        for i, (_n, s) in enumerate(HISTORY_CHOICES):
            if s == sec:
                self.cmb_history.setCurrentIndex(i)
                return
        self.cmb_history.setCurrentIndex(2)

    def history_sec(self) -> int:
        return HISTORY_CHOICES[self.cmb_history.currentIndex()][1]

    # ---- 마커 레이어
    def set_layers(self, layers, counts: dict, sizes: dict):
        prev_states = {lid: c.isChecked() for lid, c in self.layer_checks.items()}
        while self.layers_grid.count():
            w = self.layers_grid.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.layer_checks, self.layer_spins = {}, {}
        for r, layer in enumerate(layers):
            n = counts.get(layer.id, 0)
            chk = QCheckBox(f"{layer.name}  ({n})")
            chk.setIcon(marker_icon(layer.shape, layer.color))
            chk.setChecked(prev_states.get(layer.id, layer.visible))
            chk.toggled.connect(self.changed)
            spn = _spin(3, 60, sizes.get(layer.id, layer.size), "", f"{layer.name} 마커 크기 (px)")
            spn.valueChanged.connect(lambda v, lid=layer.id: self.layerSizeChanged.emit(lid, v))
            for w in (chk, spn):
                w.setVisible(n > 0)
            self.layers_grid.addWidget(chk, r, 0)
            self.layers_grid.addWidget(spn, r, 1)
            self.layer_checks[layer.id] = chk
            self.layer_spins[layer.id] = spn

    def layer_visibility(self) -> dict:
        return {lid: c.isChecked() for lid, c in self.layer_checks.items()}

    # ---- 사용자 설정 저장/복원
    def prefs(self) -> dict:
        return {"trail_mode": self.trail_mode(), "trail_count": self.spn_trail_count.value(),
                "trail_width": self.spn_trail_width.value(), "robot_size": self.spn_robot.value(),
                "station_size": self.spn_station.value(),
                "hud": self.chk_hud.isChecked(), "hud_res": self.chk_hud_res.isChecked(),
                "layer_sizes": {lid: sp.value() for lid, sp in self.layer_spins.items()}}

    def apply_prefs(self, p: dict):
        widgets = (self.cmb_trail, self.spn_trail_count, self.spn_trail_width, self.spn_robot, self.spn_station,
                   self.chk_hud, self.chk_hud_res)
        for w in widgets:
            w.blockSignals(True)
        modes = [m for _n, m in TRAIL_MODES]
        self.cmb_trail.setCurrentIndex(modes.index(p.get("trail_mode")) if p.get("trail_mode") in modes else 0)
        self.spn_trail_count.setValue(int(p.get("trail_count", 50)))
        self.spn_trail_width.setValue(int(p.get("trail_width", 2)))
        self.spn_robot.setValue(int(p.get("robot_size", 18)))
        self.spn_station.setValue(int(p.get("station_size", 22)))
        self.chk_hud.setChecked(bool(p.get("hud", True)))
        self.chk_hud_res.setChecked(bool(p.get("hud_res", True)))
        for w in widgets:
            w.blockSignals(False)
        self._update_trail_widgets()


# ---------------------------------------------------------------- 우측: 이벤트 목록
class EventModel(QAbstractTableModel):
    HEAD = ["시각", "분류", "내용"]

    def __init__(self):
        super().__init__()
        self.session = None
        self.rows = []
        self.cats = {}

    def set_rows(self, session, rows):
        self.beginResetModel()
        self.session = session
        self.rows = rows
        self.cats = session.rules.cat_by_id if session else {}
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 3

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.HEAD[section]
        return None

    def event_at(self, row):
        return self.session.events[self.rows[row]]

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        ev = self.event_at(index.row())
        col = index.column()
        if role == Qt.DisplayRole:
            if col == 0:
                return fmt_time(ev.t)[5:]
            if col == 1:
                c = self.cats.get(ev.cat)
                return c.name if c else ev.cat
            return ev.text
        if role == Qt.ForegroundRole:
            if col == 1:
                c = self.cats.get(ev.cat)
                return QBrush(style.category_color(c.color)) if c else None
            return QBrush(style.level_color(ev.level))
        if role == Qt.ToolTipRole:
            f = self.session.files[ev.fi].name
            return f"{fmt_time(ev.t)}  [{ev.node}]\n{ev.text}\n{f} : {ev.ln + 1}줄"
        return None


class EventPanel(QWidget):
    activated = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.session = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self.cat_box = QWidget()
        self.cat_flow = FlowLayout(self.cat_box)
        lay.addWidget(self.cat_box)
        row = QHBoxLayout()
        self.cmb_level = QComboBox()
        self.cmb_level.addItems(["전체", "경고 이상", "에러만"])
        self.cmb_level.currentIndexChanged.connect(self.refilter)
        row.addWidget(self.cmb_level)
        self.search = QLineEdit()
        self.search.setPlaceholderText("검색 (공백=AND, |=OR, -단어=제외)")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refilter)
        row.addWidget(self.search, 1)
        lay.addLayout(row)
        self.model = EventModel()
        self.view = QTableView()
        self.view.setModel(self.model)
        self.view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.view.setAlternatingRowColors(True)
        self.view.setWordWrap(False)
        self.view.verticalHeader().setVisible(False)
        self.view.verticalHeader().setDefaultSectionSize(20)
        self.view.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        hh = self.view.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setStretchLastSection(True)
        self.view.clicked.connect(lambda idx: self.activated.emit(self.model.event_at(idx.row())))
        lay.addWidget(self.view, 1)
        bottom = QHBoxLayout()
        self.lbl_count = QLabel("")
        self.lbl_count.setObjectName("hint")
        bottom.addWidget(self.lbl_count, 1)
        self.chk_follow = QCheckBox("재생 위치 따라가기")
        self.chk_follow.setChecked(True)
        bottom.addWidget(self.chk_follow)
        lay.addLayout(bottom)
        self.cat_buttons = {}
        self.row_vts = []

    def set_session(self, session):
        self.session = session
        while self.cat_flow.count():
            w = self.cat_flow.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.cat_buttons = {}
        counts = Counter(e.cat for e in session.events) if session else Counter()
        cats = session.rules.categories if session else []
        for c in cats:
            b = QToolButton()
            b.setText(f"{c.name} {counts.get(c.id, 0)}")
            b.setCheckable(True)
            b.setChecked(c.visible)
            b.setToolButtonStyle(Qt.ToolButtonTextOnly)
            b.setProperty("cat_color", c.color)
            self._style_chip(b)
            b.toggled.connect(self.refilter)
            b.setToolTip("클릭: 이 분류 보이기/숨기기")
            self.cat_flow.addWidget(b)
            self.cat_buttons[c.id] = b
        self.refilter()

    @staticmethod
    def _style_chip(b):
        t = style.T()
        fg = style.category_color(b.property("cat_color")).name()
        bg = QColor(b.property("cat_color")).name()
        b.setStyleSheet(
            "QToolButton { border: 1px solid %s; border-radius: 9px; padding: 1px 7px; color: %s; background: %s; }"
            "QToolButton:checked { background: %s; color: #fff; }" % (fg, fg, t.base, bg))

    def apply_theme(self):
        for b in self.cat_buttons.values():
            self._style_chip(b)
        self.view.viewport().update()

    def refilter(self):
        S = self.session
        if S is None:
            self.model.set_rows(None, [])
            return
        on = {cid for cid, b in self.cat_buttons.items() if b.isChecked()}
        lvl = self.cmb_level.currentIndex()
        allowed_levels = {0: None, 1: {"warn", "error"}, 2: {"error"}}[lvl]
        groups, excludes = parse_query(self.search.text())
        rows = []
        for i, e in enumerate(S.events):
            if e.cat not in on:
                continue
            if allowed_levels and e.level not in allowed_levels:
                continue
            if (groups or excludes) and not match_query(f"{fmt_time(e.t)} {e.text} {e.node}".lower(), groups, excludes):
                continue
            rows.append(i)
        self.model.set_rows(S, rows)
        self.row_vts = [S.events[i].vt for i in rows]
        self.lbl_count.setText(f"표시 {len(rows):,} / 전체 {len(S.events):,}건")

    def highlight_vt(self, vt: float):
        if not self.chk_follow.isChecked() or not self.row_vts:
            return
        r = bisect.bisect_right(self.row_vts, vt) - 1
        if r < 0:
            self.view.clearSelection()
            return
        idx = self.model.index(r, 0)
        if self.view.currentIndex().row() != r:
            self.view.blockSignals(True)
            self.view.setCurrentIndex(idx)
            self.view.blockSignals(False)
            self.view.scrollTo(idx, QAbstractItemView.PositionAtCenter)

    def visible_events(self):
        return [self.session.events[i] for i in self.model.rows] if self.session else []


# ---------------------------------------------------------------- 우측: 에러
def _table(headers):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setSelectionMode(QAbstractItemView.SingleSelection)
    t.verticalHeader().setVisible(False)
    t.verticalHeader().setDefaultSectionSize(20)
    t.setWordWrap(False)
    hh = t.horizontalHeader()
    for i in range(len(headers) - 1):
        hh.setSectionResizeMode(i, QHeaderView.ResizeToContents)
    hh.setStretchLastSection(True)
    return t


class ErrorPanel(QWidget):
    activated = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.session = None
        self._active_sig = None
        self.history = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(QLabel("<b>현재 시점 활성 에러</b>"))
        self.tbl_active = _table(["코드", "발생 시각", "내용"])
        self.tbl_active.setMaximumHeight(140)
        self.tbl_active.cellClicked.connect(self._active_clicked)
        lay.addWidget(self.tbl_active)
        lay.addWidget(QLabel("<b>에러 발생/해제 이력</b>  (클릭: 그 시점으로 이동)"))
        self.tbl_hist = _table(["시각", "코드", "상태", "내용"])
        self.tbl_hist.cellClicked.connect(lambda r, _c: self.activated.emit(self.history[r]))
        lay.addWidget(self.tbl_hist, 1)
        self._active_events = []

    def set_session(self, session):
        self.session = session
        self._active_sig = None
        self.history = [e for e in session.error_events if e.rule != "boot"] if session else []
        t = self.tbl_hist
        t.setRowCount(len(self.history))
        for r, e in enumerate(self.history):
            st = e.data.get("status")
            label = {"OCCURRED": "발생", "RELEASED": "해제", "RESET": "전체 해제"}.get(st, st)
            color = QColor(style.T().error if st == "OCCURRED" else style.T().ok)
            vals = [fmt_time(e.t)[5:], e.data.get("code", "-"), label, e.data.get("desc", e.text)]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if c in (1, 2):
                    it.setForeground(color)
                t.setItem(r, c, it)
        self.update_vt(0.0)

    def update_vt(self, vt: float):
        if self.session is None:
            return
        active = self.session.active_errors(vt)
        sig = tuple((k, id(v)) for k, v in active.items())
        if sig == self._active_sig:
            return
        self._active_sig = sig
        self._active_events = list(active.values())
        t = self.tbl_active
        t.setRowCount(len(self._active_events))
        for r, e in enumerate(self._active_events):
            for c, v in enumerate([e.data["code"], fmt_time(e.t)[5:], e.data.get("desc", "")]):
                it = QTableWidgetItem(v)
                it.setForeground(QColor(style.T().error))
                t.setItem(r, c, it)

    def _active_clicked(self, r, _c):
        if 0 <= r < len(self._active_events):
            self.activated.emit(self._active_events[r])


# ---------------------------------------------------------------- 우측: 요약
class SummaryPanel(QTextBrowser):
    activated = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenLinks(False)
        self.anchorClicked.connect(self._on_link)
        self.session = None
        self._targets = []

    def _link(self, ev, text):
        self._targets.append(ev)
        return f'<a href="ev:{len(self._targets) - 1}">{html.escape(text)}</a>'

    def set_session(self, S):
        self.session = S
        self._targets = []
        if S is None:
            self.setHtml("")
            return
        st, src = S.stats, S.source
        esc = html.escape
        t = style.T()
        period = f"{fmt_time(S.t_first, False)} ~ {fmt_time(S.t_last, False)}" if S.tl_t else "-"
        rows = [
            ("파일", esc(src.label)),
            ("시리얼", esc(src.serial or "-")),
            ("수집 시각", esc(src.collected or "-")),
            ("메모", esc(src.memo or "-")),
            ("로그 기간", f"{period}<br><span style='color:{t.hint}'>재생 길이(공백 압축) {fmt_duration(S.vt_end)}</span>"),
            ("로그 파일", f"{st['files']}개 / {st['lines']:,}줄"),
            ("SW:FW:AI 버전", esc(" → ".join(st.get("versions") or st["sw_versions"]) or "-")),
            ("부팅", f"{st['boots']}회" + (" : " + ", ".join(self._link(b, fmt_time(b.t, False)[5:16]) for b in S.boots[:12]) if S.boots else "")),
            ("시간 역행/점프", f"{st['jumps']}회"),
            ("배터리", f"{st['battery_min']}% ~ {st['battery_max']}%" if st["battery_min"] is not None else "-"),
            ("맵", esc(str(src.map_dir.name) if src.map_dir else "없음")),
        ]
        out = [f"<style>td{{padding:2px 8px 2px 0;vertical-align:top}} th{{text-align:left;color:{t.hint};padding-right:8px}}"
               f"h3{{color:{t.accent};margin:10px 0 4px 0}} a{{color:{t.accent}}}</style>", "<h3>로그 정보</h3><table>"]
        out += [f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows]
        out.append("</table><h3>에러 발생 (코드별)</h3>")
        if st["errors"]:
            out.append("<table><tr><th>코드</th><th>횟수</th><th>내용</th><th>최초 발생</th></tr>")
            for code, n in st["errors"].most_common():
                first = st["error_first"][code]
                out.append(f"<tr><td style='color:{t.error}'><b>{esc(code)}</b></td><td>{n}</td>"
                           f"<td>{esc(st['error_desc'].get(code, ''))}</td>"
                           f"<td>{self._link(first, fmt_time(first.t, False)[5:])}</td></tr>")
            out.append("</table>")
        else:
            out.append("<p>없음</p>")
        hits = st["hits"]
        out.append("<h3>주행 / 도킹</h3><table>")
        for label, key in (("주행 목표 전송", "nav_goal"), ("목표 도착", "goal_reached"), ("목표 실패", "goal_failed"),
                           ("도킹 시작", "dock_start"), ("도킹 완료", "docked"), ("SOC 명령", "soc_cmd")):
            out.append(f"<tr><th>{label}</th><td>{hits.get(key, 0)}회</td></tr>")
        out.append("</table>")
        cmds = Counter(re.sub(r"\(.*", "", e.text.replace("SOC 명령: ", "")).strip()
                       for e in S.events if e.rule == "soc_cmd")
        if cmds:
            out.append("<h3>SOC 명령 종류</h3><table>")
            out += [f"<tr><td>{esc(k)}</td><td>{v}회</td></tr>" for k, v in cmds.most_common(10)]
            out.append("</table>")
        trans = Counter(e.text.split("  (")[0] for e in S.events if e.rule == "state_change")
        if trans:
            out.append("<h3>상태 전이 (상위 10)</h3><table>")
            out += [f"<tr><td>{esc(k)}</td><td>{v}회</td></tr>" for k, v in trans.most_common(10)]
            out.append("</table>")
        self.setHtml("".join(out))

    def _on_link(self, url):
        s = url.toString()
        if s.startswith("ev:"):
            i = int(s[3:])
            if 0 <= i < len(self._targets):
                self.activated.emit(self._targets[i])

"""좌측 표시 설정 / 우측 이벤트·에러·요약 패널."""
import bisect
import html
import re
from collections import Counter

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QGroupBox, QHBoxLayout, QHeaderView,
                               QLabel, QLayout, QLineEdit, QSlider, QTableView, QTableWidget, QTableWidgetItem,
                               QTextBrowser, QToolButton, QVBoxLayout, QWidget)

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
        p.setPen(QColor("#263238"))
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
class LayerPanel(QWidget):
    changed = Signal()
    markerSizeChanged = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        g = QGroupBox("표시")
        gl = QVBoxLayout(g)
        self.chk_trail = QCheckBox("로봇 궤적")
        self.chk_follow = QCheckBox("로봇 따라가기")
        self.chk_keepout = QCheckBox("금지구역")
        self.chk_station = QCheckBox("충전 스테이션")
        for c in (self.chk_trail, self.chk_follow, self.chk_keepout, self.chk_station):
            c.setChecked(True)
            c.toggled.connect(self.changed)
            gl.addWidget(c)
        row = QHBoxLayout()
        row.addWidget(QLabel("누적 표시"))
        self.cmb_history = QComboBox()
        for name, _sec in HISTORY_CHOICES:
            self.cmb_history.addItem(name)
        self.cmb_history.setToolTip("궤적과 마커를 현재 시점 기준으로 얼마 전까지 보여줄지")
        self.cmb_history.currentIndexChanged.connect(self.changed)
        row.addWidget(self.cmb_history, 1)
        gl.addLayout(row)
        lay.addWidget(g)

        self.grp_layers = QGroupBox("마커")
        self.layers_layout = QVBoxLayout(self.grp_layers)
        lay.addWidget(self.grp_layers)
        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("마커 크기"))
        self.sld_size = QSlider(Qt.Horizontal)
        self.sld_size.setRange(50, 250)
        self.sld_size.setValue(100)
        self.sld_size.sliderReleased.connect(lambda: self.markerSizeChanged.emit(self.sld_size.value() / 100))
        size_row.addWidget(self.sld_size, 1)
        lay.addLayout(size_row)
        hint = QLabel("마커를 클릭하면 그 시점으로 이동합니다.\n휠: 확대/축소, 드래그: 이동, 더블클릭: 전체 보기")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        lay.addStretch(1)
        self.layer_checks = {}

    def set_history_minutes(self, minutes: int):
        sec = int(minutes) * 60
        for i, (_n, s) in enumerate(HISTORY_CHOICES):
            if s == sec:
                self.cmb_history.setCurrentIndex(i)
                return
        self.cmb_history.setCurrentIndex(2)

    def history_sec(self) -> int:
        return HISTORY_CHOICES[self.cmb_history.currentIndex()][1]

    def set_layers(self, layers, counts: dict):
        prev_states = {lid: c.isChecked() for lid, c in self.layer_checks.items()}
        while self.layers_layout.count():
            w = self.layers_layout.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.layer_checks = {}
        for layer in layers:
            chk = QCheckBox(f"{layer.name}  ({counts.get(layer.id, 0)})")
            chk.setIcon(marker_icon(layer.shape, layer.color))
            chk.setChecked(prev_states.get(layer.id, layer.visible))
            chk.toggled.connect(self.changed)
            chk.setVisible(counts.get(layer.id, 0) > 0)
            self.layers_layout.addWidget(chk)
            self.layer_checks[layer.id] = chk

    def layer_visibility(self) -> dict:
        return {lid: c.isChecked() for lid, c in self.layer_checks.items()}


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
                return QBrush(QColor(c.color)) if c else None
            return QBrush(QColor(style.LEVEL_COLORS.get(ev.level, "#263238")))
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
            b.setStyleSheet(
                "QToolButton { border: 1px solid %s; border-radius: 9px; padding: 1px 7px; color: %s; background: #fff; }"
                "QToolButton:checked { background: %s; color: #fff; }" % (c.color, c.color, c.color))
            b.toggled.connect(self.refilter)
            b.setToolTip("클릭: 이 분류 보이기/숨기기")
            self.cat_flow.addWidget(b)
            self.cat_buttons[c.id] = b
        self.refilter()

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
            color = QColor("#c62828") if st == "OCCURRED" else QColor("#2e7d32")
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
                it.setForeground(QColor("#c62828"))
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
        period = f"{fmt_time(S.t_first, False)} ~ {fmt_time(S.t_last, False)}" if S.tl_t else "-"
        rows = [
            ("파일", esc(src.label)),
            ("시리얼", esc(src.serial or "-")),
            ("수집 시각", esc(src.collected or "-")),
            ("메모", esc(src.memo or "-")),
            ("로그 기간", f"{period}<br><span style='color:#607d8b'>재생 길이(공백 압축) {fmt_duration(S.vt_end)}</span>"),
            ("로그 파일", f"{st['files']}개 / {st['lines']:,}줄"),
            ("SW 버전", esc(" → ".join(st["sw_versions"]) or "-")),
            ("부팅", f"{st['boots']}회" + (" : " + ", ".join(self._link(b, fmt_time(b.t, False)[5:16]) for b in S.boots[:12]) if S.boots else "")),
            ("시간 역행/점프", f"{st['jumps']}회"),
            ("배터리", f"{st['battery_min']}% ~ {st['battery_max']}%" if st["battery_min"] is not None else "-"),
            ("맵", esc(str(src.map_dir.name) if src.map_dir else "없음")),
        ]
        out = ["<style>td{padding:2px 8px 2px 0;vertical-align:top} th{text-align:left;color:#455a64;padding-right:8px}"
               "h3{color:#1565c0;margin:10px 0 4px 0}</style>", "<h3>로그 정보</h3><table>"]
        out += [f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows]
        out.append("</table><h3>에러 발생 (코드별)</h3>")
        if st["errors"]:
            out.append("<table><tr><th>코드</th><th>횟수</th><th>내용</th><th>최초 발생</th></tr>")
            for code, n in st["errors"].most_common():
                first = st["error_first"][code]
                out.append(f"<tr><td style='color:#c62828'><b>{esc(code)}</b></td><td>{n}</td>"
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

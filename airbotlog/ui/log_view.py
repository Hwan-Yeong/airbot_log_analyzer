"""원본 로그 뷰 (전체 줄 가상 스크롤 + 검색 + 재생 위치 동기화)."""
import os
import subprocess
import sys

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QPushButton, QTableView, QVBoxLayout, QWidget)

from . import style

MAX_TEXT = 3000


class LogModel(QAbstractTableModel):
    HEAD = ["파일", "줄", "내용"]

    def __init__(self):
        super().__init__()
        self.session = None
        self.current = -1

    def set_session(self, session):
        self.beginResetModel()
        self.session = session
        self.current = -1
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() or self.session is None else self.session.total_lines

    def columnCount(self, parent=QModelIndex()):
        return 3

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.HEAD[section]
        return None

    def set_current(self, g: int):
        old, self.current = self.current, g
        for r in (old, g):
            if 0 <= r < self.rowCount():
                self.dataChanged.emit(self.index(r, 0), self.index(r, 2), [Qt.BackgroundRole])

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or self.session is None:
            return None
        g = index.row()
        if role == Qt.BackgroundRole:
            return QBrush(QColor(style.T().cur_line)) if g == self.current else None
        fi, ln = self.session.locate_line(g)
        if role == Qt.DisplayRole:
            if index.column() == 0:
                return self.session.files[fi].name
            if index.column() == 1:
                return ln + 1
            text = self.session.line_text(fi, ln)
            return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + " …"
        if role == Qt.ForegroundRole and index.column() == 2:
            head = self.session.line_text(fi, ln)[:70]
            t = style.T()
            if "] [ERROR]" in head:
                return QBrush(QColor(t.error))
            if "] [WARN]" in head:
                return QBrush(QColor(t.warn))
            if not head.startswith("["):
                return QBrush(QColor(t.cont))
        if role == Qt.TextAlignmentRole and index.column() == 1:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None


class LogView(QWidget):
    jumpRequested = Signal(int)   # 전역 줄 번호

    def __init__(self, parent=None):
        super().__init__(parent)
        self.session = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(2)
        bar = QHBoxLayout()
        title = QLabel("<b>원본 로그</b>")
        bar.addWidget(title)
        self.lbl_file = QLabel("")
        self.lbl_file.setObjectName("hint")
        bar.addWidget(self.lbl_file, 1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("로그 검색 (Enter: 다음, Shift+Enter: 이전)")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(260)
        self.search.returnPressed.connect(lambda: self.find(1))
        bar.addWidget(self.search)
        b_prev = QPushButton("▲")
        b_next = QPushButton("▼")
        b_prev.setToolTip("이전 찾기")
        b_next.setToolTip("다음 찾기")
        b_prev.clicked.connect(lambda: self.find(-1))
        b_next.clicked.connect(lambda: self.find(1))
        bar.addWidget(b_prev)
        bar.addWidget(b_next)
        self.lbl_find = QLabel("")
        self.lbl_find.setObjectName("hint")
        bar.addWidget(self.lbl_find)
        self.chk_sync = QCheckBox("재생 위치 동기화")
        self.chk_sync.setChecked(True)
        bar.addWidget(self.chk_sync)
        self.btn_folder = QPushButton("폴더 열기")
        self.btn_folder.setToolTip("현재 로그 파일이 있는 (임시) 폴더 열기")
        self.btn_folder.clicked.connect(self.open_folder)
        bar.addWidget(self.btn_folder)
        lay.addLayout(bar)
        self.model = LogModel()
        self.view = QTableView()
        self.view.setModel(self.model)
        self.view.setFont(style.mono_font(9))
        self.view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.view.setWordWrap(False)
        self.view.setShowGrid(False)
        vh = self.view.verticalHeader()
        vh.setVisible(False)
        vh.setSectionResizeMode(QHeaderView.Fixed)
        vh.setDefaultSectionSize(18)
        hh = self.view.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Interactive)
        hh.setSectionResizeMode(1, QHeaderView.Interactive)
        hh.resizeSection(0, 120)
        hh.resizeSection(1, 64)
        hh.setStretchLastSection(True)
        self.view.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.view.doubleClicked.connect(lambda idx: self.jumpRequested.emit(idx.row()))
        self.view.setToolTip("더블클릭: 이 줄 시점으로 재생 위치 이동")
        lay.addWidget(self.view, 1)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and e.modifiers() & Qt.ShiftModifier:
            self.find(-1)
            return
        super().keyPressEvent(e)

    def set_session(self, session):
        self.session = session
        self.model.set_session(session)
        self.lbl_find.setText("")

    def show_line(self, g: int, force: bool = False):
        if self.session is None or (not force and not self.chk_sync.isChecked()):
            return
        g = min(max(g, 0), self.session.total_lines - 1)
        self.model.set_current(g)
        idx = self.model.index(g, 2)
        self.view.scrollTo(idx, QAbstractItemView.PositionAtCenter)
        fi, ln = self.session.locate_line(g)
        self.lbl_file.setText(f"  {self.session.files[fi].name} : {ln + 1}줄")

    def find(self, direction: int):
        if self.session is None:
            return
        key = self.search.text().strip().lower()
        if not key:
            return
        total = self.session.total_lines
        cur = self.view.currentIndex().row()
        if cur < 0:
            cur = self.model.current if self.model.current >= 0 else 0
        files, offs = self.session.files, self.session.line_offsets
        nf = len(files)
        fi, ln = self.session.locate_line(cur)
        for _ in range(total):
            ln += direction
            while ln >= len(files[fi].lines):   # 다음 파일로 (빈 파일 건너뜀)
                fi, ln = (fi + 1) % nf, 0
            while ln < 0:
                fi = (fi - 1) % nf
                ln = len(files[fi].lines) - 1
            if key in files[fi].lines[ln].lower():
                g = offs[fi] + ln
                idx = self.model.index(g, 2)
                self.view.setCurrentIndex(idx)
                self.view.scrollTo(idx, QAbstractItemView.PositionAtCenter)
                self.lbl_find.setText(f"{files[fi].name}:{ln + 1}")
                return
        self.lbl_find.setText("찾을 수 없음")

    def open_folder(self):
        if self.session is None or not self.session.files:
            return
        g = self.model.current if self.model.current >= 0 else 0
        fi, _ = self.session.locate_line(g)
        folder = str(self.session.files[fi].path.parent)
        try:
            if sys.platform == "win32":
                os.startfile(folder)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except OSError:
            pass

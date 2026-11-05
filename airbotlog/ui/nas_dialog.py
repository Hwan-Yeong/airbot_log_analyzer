"""NAS 로그 폴더 탐색 (읽기 전용) 대화상자."""
import time
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QMessageBox, QPushButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout)

from ..nas import load_index_cache, resolve_source, save_index_cache
from .panels import match_query, parse_query
from .workers import FuncWorker, detach


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return str(n)


class NasPathDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("NAS 경로 설정")
        self.settings = settings
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.ed_root = QLineEdit(settings.get("nas.root", ""))
        self.ed_root.setMinimumWidth(560)
        self.ed_root.setPlaceholderText(r"\\서버\공유\...\로그폴더")
        self.ed_linux = QLineEdit(settings.get("nas.linux_path", ""))
        self.ed_linux.setPlaceholderText("/mnt/nas_log")
        form.addRow("NAS 경로 (UNC)", self.ed_root)
        form.addRow("Linux 마운트 경로", self.ed_linux)
        lay.addLayout(form)
        hint = QLabel("Windows/WSL 은 UNC 경로만 있으면 됩니다. Linux 는 마운트 경로를 사용합니다.\n"
                      "변경값은 사용자 설정 파일에 저장됩니다 (레포 파일은 바뀌지 않음).")
        hint.setObjectName("hint")
        lay.addWidget(hint)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def accept(self):
        try:
            self.settings.set_user("nas.root", self.ed_root.text().strip())
            self.settings.set_user("nas.linux_path", self.ed_linux.text().strip())
        except OSError as e:
            QMessageBox.warning(self, "저장 실패", f"사용자 설정을 저장하지 못했습니다:\n{e}")
            return
        super().accept()


class NasDialog(QDialog):
    """NAS 압축 로그 목록에서 하나를 골라 반환 (self.selected)."""

    def __init__(self, settings, parent=None, source=None):
        super().__init__(parent)
        self.setWindowTitle("NAS에서 로그 불러오기 (읽기 전용)")
        self.resize(980, 640)
        self.settings = settings
        self.source = source
        self.entries = []
        self.selected = None
        self._worker = None

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.lbl_conn = QLabel("")
        self.lbl_conn.setWordWrap(True)
        self.lbl_conn.setTextInteractionFlags(Qt.TextSelectableByMouse)
        top.addWidget(self.lbl_conn, 1)
        self.btn_path = QPushButton("경로 설정")
        self.btn_path.clicked.connect(self._edit_path)
        top.addWidget(self.btn_path)
        self.btn_refresh = QPushButton("목록 새로고침")
        self.btn_refresh.clicked.connect(self.refresh)
        top.addWidget(self.btn_refresh)
        lay.addLayout(top)

        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("검색 (폴더/파일명/시리얼, 공백=AND, |=OR, -단어=제외)")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.populate)
        row.addWidget(self.search, 1)
        self.chk_packed = QCheckBox("압축 해제본 없는 것만")
        self.chk_packed.toggled.connect(self.populate)
        row.addWidget(self.chk_packed)
        lay.addLayout(row)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["이름", "크기", "수정일", "비고"])
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        hh = self.tree.header()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in (1, 2, 3):
            hh.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self.tree.itemDoubleClicked.connect(self._on_double)
        lay.addWidget(self.tree, 1)

        bottom = QHBoxLayout()
        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("hint")
        bottom.addWidget(self.lbl_status, 1)
        self.btn_open = QPushButton("불러오기")
        self.btn_open.setDefault(True)
        self.btn_open.clicked.connect(self._open_selected)
        bottom.addWidget(self.btn_open)
        btn_close = QPushButton("닫기")
        btn_close.clicked.connect(self.reject)
        bottom.addWidget(btn_close)
        lay.addLayout(bottom)
        self._connect()

    # ------------------------------------------------------------ 연결/목록
    def _connect(self):
        if self.source is None:
            self.source, msg = resolve_source(self.settings)
        else:
            msg = self.source.describe()
        self.lbl_conn.setText(f"<b>연결</b>: {msg}" if self.source else f"<b style='color:#c62828'>연결 안 됨</b>: {msg}")
        self.btn_refresh.setEnabled(self.source is not None)
        if self.source is None:
            return
        cached, ts = load_index_cache(self.source.root)
        if cached is not None:
            self.entries = cached
            self.populate()
            self._status_cached(ts)
        else:
            self.refresh()

    def _status_cached(self, ts):
        when = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"
        self.lbl_status.setText(f"압축 로그 {len(self.entries):,}개  ·  목록 기준: {when} (새로고침으로 갱신)")

    def refresh(self):
        if self.source is None or self._worker is not None:
            return
        self.btn_refresh.setEnabled(False)
        self.lbl_status.setText("NAS 목록 조회 중...")
        src = self.source
        self._worker = FuncWorker(lambda prog: src.list_archives(prog), self)
        self._worker.progress.connect(lambda m, _f: self.lbl_status.setText(m))
        self._worker.done.connect(self._on_listed)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_listed(self, entries):
        self._worker = None
        self.btn_refresh.setEnabled(True)
        self.entries = entries
        save_index_cache(self.source.root, entries)
        self.populate()
        self._status_cached(time.time())

    def _on_failed(self, msg):
        self._worker = None
        self.btn_refresh.setEnabled(True)
        self.lbl_status.setText("조회 실패")
        self.lbl_conn.setText(f"<b style='color:#c62828'>오류</b>: {msg}")

    def _edit_path(self):
        if NasPathDialog(self.settings, self).exec():
            self.source = None
            self.entries = []
            self.tree.clear()
            self._connect()

    # ------------------------------------------------------------ 트리
    def populate(self):
        self.tree.clear()
        groups, excludes = parse_query(self.search.text())
        filtering = bool(groups or excludes)
        packed_only = self.chk_packed.isChecked()
        root = {"subs": {}, "files": [], "mtime": 0.0, "count": 0}
        shown = 0
        for e in self.entries:
            if packed_only and e.extracted:
                continue
            if filtering and not match_query(e.rel.lower(), groups, excludes):
                continue
            node = root
            node["mtime"], node["count"] = max(node["mtime"], e.mtime), node["count"] + 1
            for part in (e.folder.split("/") if e.folder else []):
                node = node["subs"].setdefault(part, {"subs": {}, "files": [], "mtime": 0.0, "count": 0})
                node["mtime"], node["count"] = max(node["mtime"], e.mtime), node["count"] + 1
            node["files"].append(e)
            shown += 1

        def fmt(ts):
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

        def add(node, parent):  # 최근 수정된 것이 위로
            for name, sub in sorted(node["subs"].items(), key=lambda kv: kv[1]["mtime"], reverse=True):
                it = QTreeWidgetItem([name, "", fmt(sub["mtime"]), f"{sub['count']}개"])
                it.setData(0, Qt.UserRole, None)
                (parent.addChild(it) if parent else self.tree.addTopLevelItem(it))
                add(sub, it)
            for e in sorted(node["files"], key=lambda x: x.mtime, reverse=True):
                it = QTreeWidgetItem([e.name, _size(e.size), fmt(e.mtime), "압축 해제본 있음" if e.extracted else ""])
                it.setData(0, Qt.UserRole, e)
                it.setToolTip(0, e.rel)
                (parent.addChild(it) if parent else self.tree.addTopLevelItem(it))

        add(root, None)
        if filtering:
            self.tree.expandAll()
        if filtering or packed_only:
            self.lbl_status.setText(f"검색 결과 {shown:,}개 / 전체 {len(self.entries):,}개")

    def done(self, result):
        detach(self._worker)   # 목록 조회 중 닫아도 안전하게
        self._worker = None
        super().done(result)

    def _on_double(self, item, _col):
        if item.data(0, Qt.UserRole) is not None:
            self._open_selected()

    def _open_selected(self):
        it = self.tree.currentItem()
        entry = it.data(0, Qt.UserRole) if it else None
        if entry is None:
            self.lbl_status.setText("압축 파일을 선택하세요.")
            return
        self.selected = entry
        self.accept()

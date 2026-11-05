"""타임라인(재생 막대)과 재생 컨트롤."""
from datetime import datetime, timezone

from PySide6.QtCore import QDate, QDateTime, QPointF, QRectF, QRegularExpression, Qt, QTime, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QRegularExpressionValidator
from PySide6.QtWidgets import (QComboBox, QDateTimeEdit, QHBoxLayout, QLabel, QPushButton, QStyle, QToolTip,
                               QVBoxLayout, QWidget)

from . import style

SPEEDS = [1, 2, 5, 10, 15, 20, 30, 60, 120, 300, 600]
JUMPS = [-30, -10, -5, -1, 1, 5, 10, 30]   # 초 단위 점프 버튼
PAD = 12


class TimelineBar(QWidget):
    """재생 위치 막대. 부팅(빨강)·시간 점프(회색)·에러(주황)·날짜(파랑) 표시."""

    seek = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(44)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.vt_end = 1.0
        self.value = 0.0
        self.marks = []          # (vt, kind, label)
        self.time_text = None    # vt -> 표시 문자열
        self._drag = False

    def set_range(self, vt_end: float):
        self.vt_end = max(vt_end, 1e-6)
        self.update()

    def set_marks(self, marks):
        self.marks = marks
        self.update()

    def set_value(self, vt: float):
        self.value = vt
        self.update()

    def _x(self, vt):
        return PAD + (self.width() - 2 * PAD) * min(max(vt / self.vt_end, 0.0), 1.0)

    def _vt(self, x):
        return min(max((x - PAD) / max(self.width() - 2 * PAD, 1), 0.0), 1.0) * self.vt_end

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        cy = h * 0.62
        t = style.T()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(t.tl_track))
        p.drawRoundedRect(QRectF(PAD, cy - 3, w - 2 * PAD, 6), 3, 3)
        p.setBrush(QColor(t.tl_played))
        p.drawRoundedRect(QRectF(PAD, cy - 3, self._x(self.value) - PAD, 6), 3, 3)
        f = p.font()
        f.setPointSize(7)
        p.setFont(f)
        last_label_x = -100
        for vt, kind, label in self.marks:
            x = self._x(vt)
            if kind == "day":
                p.setPen(QPen(QColor(t.tl_day), 1))
                p.drawLine(QPointF(x, 12), QPointF(x, cy - 4))
                if x - last_label_x > 34:
                    p.drawText(QPointF(x + 2, 10), label)
                    last_label_x = x
            elif kind == "boot":
                p.setPen(QPen(QColor(t.tl_boot), 2))
                p.drawLine(QPointF(x, 12), QPointF(x, h - 2))
            elif kind == "jump":
                p.setPen(QPen(QColor(t.tl_jump), 1, Qt.DashLine))
                p.drawLine(QPointF(x, 12), QPointF(x, h - 2))
            elif kind == "error":
                p.setPen(QPen(QColor(t.tl_error), 2))
                p.drawLine(QPointF(x, cy + 5), QPointF(x, h - 3))
        x = self._x(self.value)
        p.setPen(QPen(QColor(t.window), 2))
        p.setBrush(QColor(t.tl_handle))
        p.drawEllipse(QPointF(x, cy), 7, 7)
        p.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = True
            self.seek.emit(self._vt(e.position().x()))

    def mouseMoveEvent(self, e):
        vt = self._vt(e.position().x())
        if self._drag:
            self.seek.emit(vt)
        if self.time_text:
            near = [lbl for v, k, lbl in self.marks if k != "day" and abs(self._x(v) - e.position().x()) < 4]
            tip = self.time_text(vt) + ("\n" + "\n".join(near[:5]) if near else "")
            QToolTip.showText(e.globalPosition().toPoint(), tip, self)

    def mouseReleaseEvent(self, _e):
        self._drag = False


class PlaybackBar(QWidget):
    """재생 버튼 + 초 단위 점프 + 배속 + 현재 시각 + 시각 이동 + 타임라인."""

    playToggled = Signal()
    stepEvent = Signal(int)        # -1 이전 이벤트, +1 다음 이벤트
    gotoEdge = Signal(int)         # 0 처음, 1 끝
    speedChanged = Signal(float)
    jumpToTime = Signal(object)    # QDateTime
    jumpPressed = Signal()         # 점프 버튼 누름 (재생 중이면 일시정지)
    jumpSeconds = Signal(float)    # 실제 시간 기준 ±초 이동 (누르고 있으면 반복)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 6)
        lay.setSpacing(3)
        row = QHBoxLayout()
        row.setSpacing(4)
        sp = self.style().standardIcon
        self._icon_play, self._icon_pause = sp(QStyle.SP_MediaPlay), sp(QStyle.SP_MediaPause)
        self.btn_first = QPushButton(sp(QStyle.SP_MediaSkipBackward), "")
        self.btn_prev = QPushButton(sp(QStyle.SP_MediaSeekBackward), "이전")
        self.btn_play = QPushButton(self._icon_play, "재생")
        self.btn_next = QPushButton(sp(QStyle.SP_MediaSeekForward), "다음")
        self.btn_last = QPushButton(sp(QStyle.SP_MediaSkipForward), "")
        self.btn_first.setToolTip("처음으로 (Home)")
        self.btn_prev.setToolTip("이전 이벤트 (←)")
        self.btn_play.setToolTip("재생 / 일시정지 (Space)")
        self.btn_next.setToolTip("다음 이벤트 (→)")
        self.btn_last.setToolTip("끝으로 (End)")
        self.btn_play.setMinimumWidth(96)
        for b in (self.btn_first, self.btn_prev, self.btn_play, self.btn_next, self.btn_last):
            b.setFocusPolicy(Qt.NoFocus)
            row.addWidget(b)
        self.btn_play.clicked.connect(self.playToggled)
        self.btn_prev.clicked.connect(lambda: self.stepEvent.emit(-1))
        self.btn_next.clicked.connect(lambda: self.stepEvent.emit(1))
        self.btn_first.clicked.connect(lambda: self.gotoEdge.emit(0))
        self.btn_last.clicked.connect(lambda: self.gotoEdge.emit(1))

        row.addSpacing(10)
        for sec in JUMPS:
            b = QPushButton(f"{sec:+d}".replace("-", "−"))
            b.setFocusPolicy(Qt.NoFocus)
            b.setFixedWidth(44)
            b.setStyleSheet("padding: 3px 2px;")
            b.setAutoRepeat(True)
            b.setAutoRepeatDelay(400)
            b.setAutoRepeatInterval(150)
            b.setToolTip(f"{abs(sec)}초 {'뒤로' if sec < 0 else '앞으로'} (누르고 있으면 계속, 재생 중이면 일시정지)")
            b.pressed.connect(self.jumpPressed)
            b.clicked.connect(lambda _=False, s=sec: self.jumpSeconds.emit(float(s)))
            row.addWidget(b)
            if sec == -1:
                row.addSpacing(4)
        row.addWidget(QLabel("초"))

        row.addSpacing(10)
        row.addWidget(QLabel("배속"))
        self.speed = QComboBox()
        self.speed.setEditable(True)
        self.speed.setInsertPolicy(QComboBox.NoInsert)
        self.speed.addItems([f"{s}x" for s in SPEEDS])
        self.speed.lineEdit().setValidator(
            QRegularExpressionValidator(QRegularExpression(r"^\s*\d{0,4}(\.\d{0,2})?\s*[xX]?\s*$")))
        self.speed.setToolTip("목록에서 고르거나 숫자를 입력하고 Enter (0.1 ~ 1000배)")
        self.speed.setMinimumWidth(84)
        self.speed.activated.connect(self._speed_edited)
        self.speed.lineEdit().editingFinished.connect(self._speed_edited)
        row.addWidget(self.speed)
        row.addStretch(1)
        lay.addLayout(row)

        row2 = QHBoxLayout()
        self.lbl_time = QLabel("-")
        self.lbl_time.setObjectName("title")
        row2.addWidget(self.lbl_time)
        self.lbl_pos = QLabel("")
        self.lbl_pos.setObjectName("hint")
        row2.addWidget(self.lbl_pos)
        row2.addStretch(1)
        row2.addWidget(QLabel("시각 이동"))
        self.dt = QDateTimeEdit()
        self.dt.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.dt.setCalendarPopup(True)
        row2.addWidget(self.dt)
        btn_go = QPushButton("이동")
        btn_go.clicked.connect(lambda: self.jumpToTime.emit(self.dt.dateTime()))
        row2.addWidget(btn_go)
        lay.addLayout(row2)
        self.bar = TimelineBar()
        lay.addWidget(self.bar)
        self._speed = 10.0

    def _speed_edited(self, *_):
        txt = self.speed.currentText().strip().lower().rstrip("x").strip()
        try:
            v = float(txt)
        except ValueError:
            v = 0.0
        if v <= 0:
            self.speed.setEditText(f"{self._speed:g}x")
            return
        v = min(max(v, 0.1), 1000.0)
        self.speed.setEditText(f"{v:g}x")
        self.speed.lineEdit().clearFocus()   # Space 단축키가 다시 재생/정지로 동작하도록
        if v != self._speed:
            self._speed = v
            self.speedChanged.emit(v)

    def set_speed(self, speed: float):
        self._speed = float(speed)
        self.speed.setEditText(f"{self._speed:g}x")

    def set_playing(self, playing: bool):
        self.btn_play.setIcon(self._icon_pause if playing else self._icon_play)
        self.btn_play.setText("일시정지" if playing else "재생")

    def set_time_hint(self, t_epoch: float):
        """로그 시각(naive epoch)을 그대로 표시 (시간대 변환 없음)."""
        if t_epoch is None:
            return
        d = datetime.fromtimestamp(int(t_epoch), tz=timezone.utc)
        self.dt.setDateTime(QDateTime(QDate(d.year, d.month, d.day), QTime(d.hour, d.minute, d.second)))

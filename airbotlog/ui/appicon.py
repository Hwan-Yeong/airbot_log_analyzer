"""앱 아이콘 — 지도의 로봇 모양을 코드로 그림 (그림 파일 없음). 창·작업 표시줄·Windows 바로가기 공용."""
import struct

from PySide6.QtCore import QBuffer, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap

from .map_view import robot_path

BG, TRAIL, ROBOT = "#305496", "#9cc3ff", "#e53935"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def icon_image(size: int = 256) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    s = size / 256.0
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(BG))
    p.drawRoundedRect(QRectF(8 * s, 8 * s, 240 * s, 240 * s), 46 * s, 46 * s)
    trail = QPainterPath(QPointF(46 * s, 206 * s))                      # 지나온 궤적
    trail.cubicTo(QPointF(64 * s, 120 * s), QPointF(132 * s, 196 * s), QPointF(150 * s, 122 * s))
    pen = QPen(QColor(TRAIL), max(14 * s, 1.5))
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawPath(trail)
    p.translate(170 * s, 98 * s)                                        # 로봇 (V자 홈 = 진행 방향)
    p.rotate(-40)
    p.setPen(QPen(QColor("white"), max(9 * s, 1.0)))
    p.setBrush(QColor(ROBOT))
    p.drawPath(robot_path(112 * s))
    p.end()
    return img


def app_icon() -> QIcon:
    icon = QIcon()
    for size in SIZES:
        icon.addPixmap(QPixmap.fromImage(icon_image(size)))
    return icon


def _png(img: QImage) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def write_ico(path, sizes=SIZES):
    """여러 크기를 담은 .ico (PNG 항목, Windows Vista 이상) — 작은 아이콘도 흐리지 않게."""
    pngs = [(size, _png(icon_image(size))) for size in sizes]
    head = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries, data = b"", b""
    for size, png in pngs:
        dim = 0 if size >= 256 else size                                # 0 = 256
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    with open(path, "wb") as f:
        f.write(head + entries + data)


def write_png(path, size: int = 256):
    icon_image(size).save(str(path), "PNG")

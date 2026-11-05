"""맵 뷰: 맵/금지구역/스테이션 + 로봇 위치·궤적 + 이벤트 마커."""
import bisect
import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (QGraphicsItem, QGraphicsLineItem, QGraphicsPathItem, QGraphicsPixmapItem,
                               QGraphicsPolygonItem, QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView)

from ..model import fmt_time
from . import style

NO_MAP_RES = 0.05            # 맵이 없을 때 1px = 5cm
TRAIL_BREAK_SEC = 60.0       # 이보다 긴 공백/점프는 궤적 선을 끊음
TRAIL_BREAK_DIST = 2.0
GOAL_END_RULES = ("goal_reached", "goal_failed")


def _cosmetic(color, width=1.0, style_=Qt.SolidLine) -> QPen:
    pen = QPen(QColor(color), width, style_)
    pen.setCosmetic(True)
    return pen


def shape_path(shape: str, size: float) -> QPainterPath:
    h = size / 2.0
    p = QPainterPath()
    if shape == "rect" or shape == "label":
        p.addRect(-h, -h, size, size)
    elif shape == "diamond":
        p.addPolygon(QPolygonF([QPointF(0, -h), QPointF(h, 0), QPointF(0, h), QPointF(-h, 0), QPointF(0, -h)]))
    elif shape == "triangle":
        p.addPolygon(QPolygonF([QPointF(0, -h * 1.15), QPointF(h, h * 0.85), QPointF(-h, h * 0.85),
                                QPointF(0, -h * 1.15)]))
    elif shape == "cross":
        p.moveTo(-h, -h)
        p.lineTo(h, h)
        p.moveTo(h, -h)
        p.lineTo(-h, h)
    else:
        p.addEllipse(QPointF(0, 0), h, h)
    return p


def robot_path(size: float = 16.0) -> QPainterPath:
    r = size / 2.0
    body = QPainterPath()
    body.addEllipse(QPointF(0, 0), r, r)
    needle = QPainterPath()
    needle.addRect(0, -r * 0.22, r * 2.0, r * 0.44)
    return body.united(needle)


def station_path(size: float = 18.0) -> QPainterPath:
    w, h = size, size * 0.7
    p = QPainterPath()
    p.addRect(-w / 2, -h / 2, w * 0.7, h)
    p.addPolygon(QPolygonF([QPointF(w * 0.2, -h / 2), QPointF(w / 2 + 2, 0), QPointF(w * 0.2, h / 2)]))
    return p


class MapView(QGraphicsView):
    mouseWorld = Signal(float, float)
    markerClicked = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        self.setBackgroundBrush(QColor("#cfd8dc"))
        self.setMouseTracking(True)
        self.session = None
        self.map = None
        self.follow = True
        self.show_trail = True
        self.marker_scale = 1.0
        self.layer_visible = {}
        self.marker_items = {}
        self.marker_range = {}
        self._static = {"keepout": [], "wall": [], "station": []}
        self._cur_target = None
        self._goal_end_vts = []
        self._layer_color = {}
        self._reset_items()

    # ------------------------------------------------------------ 좌표
    def world_to_scene(self, x, y):
        if self.map is not None:
            return self.map.world_to_px(x, y)
        return x / NO_MAP_RES, -y / NO_MAP_RES

    def scene_to_world(self, px, py):
        if self.map is not None:
            return self.map.px_to_world(px, py)
        return px * NO_MAP_RES, -py * NO_MAP_RES

    @property
    def resolution(self):
        return self.map.resolution if self.map is not None else NO_MAP_RES

    # ------------------------------------------------------------ 구성
    def _reset_items(self):
        sc = self.scene()
        sc.clear()
        self.marker_items, self.marker_range = {}, {}
        self._static = {"keepout": [], "wall": [], "station": []}
        self._cur_target = None
        self.trail = QGraphicsPathItem()
        self.trail.setPen(_cosmetic(QColor(30, 136, 229, 210), 2.0))
        self.trail.setZValue(10)
        sc.addItem(self.trail)
        self.target_line = QGraphicsLineItem()
        self.target_line.setPen(_cosmetic("#ff6f00", 1.5, Qt.DashLine))
        self.target_line.setZValue(11)
        self.target_line.setVisible(False)
        sc.addItem(self.target_line)
        self.robot = QGraphicsPathItem(robot_path())
        self.robot.setBrush(QColor(style.ROBOT_COLOR))
        self.robot.setPen(_cosmetic("#212121", 1.2))
        self.robot.setFlag(QGraphicsItem.ItemIgnoresTransformations)
        self.robot.setZValue(200)
        self.robot.setVisible(False)
        sc.addItem(self.robot)

    def set_session(self, session, layers):
        self.session = session
        self.map = session.map if session else None
        self._reset_items()
        if session is None:
            return
        sc = self.scene()
        rect = QRectF()
        if self.map is not None:
            pix = QPixmap(str(self.map.image_path))
            if not pix.isNull():
                if not self.map.width:
                    self.map.width, self.map.height = pix.width(), pix.height()
                item = QGraphicsPixmapItem(pix)
                item.setZValue(-100)
                sc.addItem(item)
                rect = QRectF(0, 0, pix.width(), pix.height())
            for _zid, pts in self.map.keepouts:
                poly = QPolygonF([QPointF(*self.world_to_scene(x, y)) for x, y in pts])
                it = QGraphicsPolygonItem(poly)
                it.setBrush(style.KEEPOUT_COLOR)
                it.setPen(_cosmetic(style.KEEPOUT_EDGE, 1.0))
                it.setZValue(-50)
                sc.addItem(it)
                self._static["keepout"].append(it)
            for pts in self.map.walls:
                path = QPainterPath(QPointF(*self.world_to_scene(*pts[0])))
                for x, y in pts[1:]:
                    path.lineTo(QPointF(*self.world_to_scene(x, y)))
                it = QGraphicsPathItem(path)
                it.setPen(_cosmetic(style.WALL_COLOR, 2.5))
                it.setZValue(-40)
                sc.addItem(it)
                self._static["wall"].append(it)
            if self.map.station:
                sx, sy, th = self.map.station
                it = QGraphicsPathItem(station_path())
                it.setBrush(QColor(style.STATION_COLOR))
                it.setPen(_cosmetic("#1b5e20", 1.0))
                it.setFlag(QGraphicsItem.ItemIgnoresTransformations)
                it.setPos(*self.world_to_scene(sx, sy))
                it.setRotation(-math.degrees(th))
                it.setZValue(-30)
                it.setToolTip(f"충전 스테이션 ({sx:.2f}, {sy:.2f})")
                sc.addItem(it)
                self._static["station"].append(it)

        self._layer_color = {l.id: l.color for l in layers}
        for z, layer in enumerate(layers):
            items = [self._make_marker(layer, ev, 50 + z) for ev in session.markers.get(layer.id, [])]
            self.marker_items[layer.id] = items
            self.marker_range[layer.id] = (0, 0)
            self.layer_visible.setdefault(layer.id, layer.visible)
        self._goal_end_vts = sorted(e.vt for e in session.events if e.rule in GOAL_END_RULES)

        if session.pose_x:
            xs, ys = [], []
            for x, y in zip(session.pose_x, session.pose_y):
                px, py = self.world_to_scene(x, y)
                xs.append(px)
                ys.append(py)
            prect = QRectF(min(xs), min(ys), max(max(xs) - min(xs), 1), max(max(ys) - min(ys), 1))
            rect = rect.united(prect) if not rect.isNull() else prect
        if rect.isNull():
            rect = QRectF(-100, -100, 200, 200)
        margin = max(rect.width(), rect.height()) * 0.15 + 20
        sc.setSceneRect(rect.adjusted(-margin, -margin, margin, margin))
        self.fit()

    def _make_marker(self, layer, ev, z):
        size = float(layer.size) * self.marker_scale
        item = QGraphicsPathItem(shape_path(layer.shape, size))
        color = QColor(layer.color)
        if layer.shape == "cross":
            item.setPen(_cosmetic(color, 2.5))
        else:
            item.setBrush(color)
            item.setPen(_cosmetic("#263238", 0.8))
        item.setFlag(QGraphicsItem.ItemIgnoresTransformations)
        item.setPos(*self.world_to_scene(ev.x, ev.y))
        item.setZValue(z)
        if layer.shape == "label" and layer.label and ev.data:
            txt = QGraphicsSimpleTextItem(str(ev.data.get(layer.label, "")), item)
            f = QFont()
            f.setPointSizeF(max(size * 0.45, 5.0))
            f.setBold(True)
            txt.setFont(f)
            br = txt.boundingRect()
            txt.setPos(-br.width() / 2, -br.height() / 2)
        item.setToolTip(f"[{layer.name}] {fmt_time(ev.t)}\n{ev.text}\n({ev.x:.2f}, {ev.y:.2f})")
        item.setData(0, ev)
        item.setVisible(False)
        self.scene().addItem(item)
        return item

    def rebuild_markers(self, layers):
        """마커 크기 변경 시 다시 생성."""
        if not self.session:
            return
        for items in self.marker_items.values():
            for it in items:
                self.scene().removeItem(it)
        self._cur_target = None
        for z, layer in enumerate(layers):
            self.marker_items[layer.id] = [self._make_marker(layer, ev, 50 + z)
                                           for ev in self.session.markers.get(layer.id, [])]
            self.marker_range[layer.id] = (0, 0)

    def set_static_visible(self, kind: str, visible: bool):
        for it in self._static.get(kind, []):
            it.setVisible(visible)

    # ------------------------------------------------------------ 프레임 갱신
    @staticmethod
    def _apply_range(items, old, new):
        a, b = old
        c, d = new
        for i in range(a, min(b, c)):
            items[i].setVisible(False)
        for i in range(max(a, d), b):
            items[i].setVisible(False)
        for i in range(c, min(d, a)):
            items[i].setVisible(True)
        for i in range(max(c, b), d):
            items[i].setVisible(True)

    def update_frame(self, vt: float, history_sec: float):
        """vt 시점 표시. history_sec: 실제 경과 시간 기준 누적 표시 범위 (0 = 전체)."""
        S = self.session
        if S is None:
            return
        lo = S.rt_at(vt) - history_sec if history_sec > 0 else -1e18
        for lid, items in self.marker_items.items():
            if self.layer_visible.get(lid, True) and items:
                new = (bisect.bisect_left(S.marker_rts.get(lid, []), lo),
                       bisect.bisect_right(S.marker_vts.get(lid, []), vt))
                new = (min(new[0], new[1]), new[1])
            else:
                new = (0, 0)
            self._apply_range(items, self.marker_range[lid], new)
            self.marker_range[lid] = new

        pose = S.pose_at(vt)
        if pose is not None:
            px, py = self.world_to_scene(pose[0], pose[1])
            self.robot.setPos(px, py)
            self.robot.setRotation(-pose[2])
            self.robot.setVisible(True)
        else:
            self.robot.setVisible(False)

        self._update_trail(S, lo, vt, pose)
        self._update_target(S, vt, pose)
        if self.follow and pose is not None:
            self._keep_visible(self.robot.pos())

    def _update_trail(self, S, lo, vt, pose):
        if not self.show_trail or not S.pose_vt:
            self.trail.setPath(QPainterPath())
            return
        i0 = bisect.bisect_left(S.pose_rt, lo)
        i1 = bisect.bisect_right(S.pose_vt, vt)
        path = QPainterPath()
        prev = None
        for i in range(i0, i1):
            x, y, t = S.pose_x[i], S.pose_y[i], S.pose_rt[i]
            pt = QPointF(*self.world_to_scene(x, y))
            if prev is None or t - prev[2] > TRAIL_BREAK_SEC or math.hypot(x - prev[0], y - prev[1]) > TRAIL_BREAK_DIST:
                path.moveTo(pt)
            else:
                path.lineTo(pt)
            prev = (x, y, t)
        if pose is not None and prev is not None and math.hypot(pose[0] - prev[0], pose[1] - prev[1]) <= TRAIL_BREAK_DIST:
            path.lineTo(QPointF(*self.world_to_scene(pose[0], pose[1])))
        self.trail.setPath(path)

    def _update_target(self, S, vt, pose):
        items = self.marker_items.get("target") or []
        vts = S.marker_vts.get("target", [])
        k = bisect.bisect_right(vts, vt) - 1
        cur = items[k] if 0 <= k < len(items) else None
        if cur is not self._cur_target:
            if self._cur_target is not None:
                self._cur_target.setPen(_cosmetic(self._layer_color.get("target", "#2e7d32"), 2.5))
                self._cur_target.setScale(1.0)
            if cur is not None:
                cur.setPen(_cosmetic("#d50000", 3.0))
                cur.setScale(1.3)
            self._cur_target = cur
        active = False
        if cur is not None and pose is not None and self.layer_visible.get("target", True):
            j = bisect.bisect_right(self._goal_end_vts, vt) - 1
            active = j < 0 or self._goal_end_vts[j] < vts[k]
        if active:
            rp = self.robot.pos()
            self.target_line.setLine(rp.x(), rp.y(), cur.pos().x(), cur.pos().y())
        self.target_line.setVisible(active)

    def _keep_visible(self, scene_pt):
        vp = self.viewport().rect()
        p = self.mapFromScene(scene_pt)
        mx, my = vp.width() * 0.15, vp.height() * 0.15
        if not (mx <= p.x() <= vp.width() - mx and my <= p.y() <= vp.height() - my):
            self.centerOn(scene_pt)

    # ------------------------------------------------------------ 조작
    def fit(self):
        r = self.scene().sceneRect()
        if not r.isNull():
            self.fitInView(r, Qt.KeepAspectRatio)

    def center_on_world(self, x, y):
        self.centerOn(QPointF(*self.world_to_scene(x, y)))

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        cur = self.transform().m11()
        if 0.02 < cur * factor < 60:
            self.scale(factor, factor)

    def mouseMoveEvent(self, event):
        sp = self.mapToScene(event.position().toPoint())
        self.mouseWorld.emit(*self.scene_to_world(sp.x(), sp.y()))
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            for it in self.items(event.position().toPoint()):
                ev = it.data(0) if it.parentItem() is None else it.parentItem().data(0)
                if ev is not None and it.isVisible():
                    self.markerClicked.emit(ev)
                    event.accept()
                    return
        super().mousePressEvent(event)

    def drawForeground(self, painter, rect):
        """좌하단 축척 막대."""
        if self.session is None:
            return
        px_per_m = self.transform().m11() / self.resolution
        if px_per_m <= 0:
            return
        length = 1.0
        for cand in (0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100):
            if cand * px_per_m >= 70:
                length = cand
                break
        bar = length * px_per_m
        painter.save()
        painter.resetTransform()
        h = self.viewport().height()
        x0, y0 = 14, h - 18
        painter.setPen(QPen(QColor("#263238"), 2))
        painter.drawLine(QPointF(x0, y0), QPointF(x0 + bar, y0))
        painter.drawLine(QPointF(x0, y0 - 5), QPointF(x0, y0 + 1))
        painter.drawLine(QPointF(x0 + bar, y0 - 5), QPointF(x0 + bar, y0 + 1))
        painter.drawText(QPointF(x0 + 4, y0 - 6), f"{length:g} m")
        painter.restore()

"""맵 뷰: 맵/금지구역/스테이션 + 로봇 위치·궤적 + 이벤트 마커.

주의: 씬에 넣은 아이템은 반드시 파이썬 참조를 보관한다.
(참조가 없으면 클릭 처리 중 PySide 가 C++ 아이템을 지워 맵이 사라지는 문제가 있었음)
"""
import bisect
import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (QGraphicsItem, QGraphicsLineItem, QGraphicsPathItem, QGraphicsPixmapItem,
                               QGraphicsPolygonItem, QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView)

from ..model import fmt_time
from . import style

NO_MAP_RES = 0.05            # 맵이 없을 때 1px = 5cm
TRAIL_BREAK_SEC = 60.0       # 이보다 긴 공백/점프는 궤적 선을 끊음
TRAIL_BREAK_DIST = 2.0
GOAL_END_RULES = ("goal_reached", "goal_failed")
TRAIL_MAX_POSES = 500


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


def robot_path(size: float = 18.0) -> QPainterPath:
    """원형 몸체에 헤딩(+x) 방향으로 V자 홈이 파인 모양."""
    r = size / 2.0
    body = QPainterPath()
    body.addEllipse(QPointF(0, 0), r, r)
    notch = QPainterPath()
    notch.addPolygon(QPolygonF([QPointF(r * 0.3, 0), QPointF(r * 1.4, -r * 0.62), QPointF(r * 1.4, r * 0.62),
                                QPointF(r * 0.3, 0)]))
    return body.subtracted(notch)


def station_path(size: float = 22.0) -> QPainterPath:
    """충전 스테이션: 뒤(-x)는 평평한 벽, 앞(+x, station theta 방향)은 둥근 D자."""
    h = size / 2.0
    back = -h * 0.75
    p = QPainterPath(QPointF(back, -h))
    p.lineTo(0, -h)
    p.arcTo(QRectF(-h, -h, size, size), 90, -180)
    p.lineTo(back, h)
    p.closeSubpath()
    return p


def station_arrow(size: float = 22.0) -> QPainterPath:
    """스테이션 안쪽 흰 화살표 (theta 방향)."""
    h = size / 2.0
    p = QPainterPath()
    p.addPolygon(QPolygonF([QPointF(h * 0.55, 0), QPointF(-h * 0.25, -h * 0.45), QPointF(-h * 0.05, 0),
                            QPointF(-h * 0.25, h * 0.45), QPointF(h * 0.55, 0)]))
    return p


def map_pixmap(path) -> QPixmap:
    """맵 이미지 로드. 다크 테마는 점유/미탐색/빈 공간을 테마 색으로 바꾼다 (ROS 맵 임계값 기준)."""
    img = QImage(str(path))
    if img.isNull():
        return QPixmap()
    colors = style.T().map_colors
    if not colors:
        return QPixmap.fromImage(img)
    g = img.convertToFormat(QImage.Format_Grayscale8)
    w, h, bpl = g.width(), g.height(), g.bytesPerLine()
    data = bytes(g.constBits())[: bpl * h]
    idx = QImage(data, w, h, bpl, QImage.Format_Indexed8).copy()
    occ, unknown, free = (QColor(c).rgb() for c in colors)
    # 값 v: 0=점유(검정), 205=미탐색, 254=빈 공간. 점유 확률 p=(255-v)/255, 점유>0.65, 빈 공간<0.196
    idx.setColorTable([occ if v < 89 else (free if v > 205 else unknown) for v in range(256)])
    return QPixmap.fromImage(idx)


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
        self.setBackgroundBrush(QColor(style.T().map_bg))
        self.setMouseTracking(True)
        self.session = None
        self.map = None
        self.layers = []
        self.follow = True
        self.show_trail = True
        self.trail_mode = "line"      # line: 최근 N분 선 / poses: 최근 N개 포즈
        self.trail_count = 50
        self.trail_width = 2.0
        self.robot_size = 18
        self.station_size = 22
        self.layer_sizes = {}         # layer id -> px
        self.layer_visible = {}
        self.marker_items = {}
        self.marker_range = {}
        self._static = {"map": [], "keepout": [], "wall": [], "station": []}
        self._cur_target = None
        self._goal_end_vts = []
        self._layer_color = {}
        self._last = None             # 마지막 update_frame 인자 (설정 변경 시 다시 그리기)
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
        self._static = {"map": [], "keepout": [], "wall": [], "station": []}
        self._cur_target = None
        self._pose_pool = []
        self.trail = QGraphicsPathItem()
        self.trail.setZValue(10)
        sc.addItem(self.trail)
        self.target_line = QGraphicsLineItem()
        self.target_line.setZValue(11)
        self.target_line.setVisible(False)
        sc.addItem(self.target_line)
        self.robot = QGraphicsPathItem()
        self.robot.setFlag(QGraphicsItem.ItemIgnoresTransformations)
        self.robot.setZValue(200)
        self.robot.setVisible(False)
        sc.addItem(self.robot)
        self._apply_styles()

    def _apply_styles(self):
        t = style.T()
        self.setBackgroundBrush(QColor(t.map_bg))
        self.trail.setPen(_cosmetic(t.qcolor("trail"), self.trail_width))
        self.target_line.setPen(_cosmetic(t.target_line, 1.5, Qt.DashLine))
        self.robot.setPath(robot_path(self.robot_size))
        self.robot.setBrush(QColor(t.robot))
        self.robot.setPen(_cosmetic(t.robot_edge, 1.2))
        for it in self._pose_pool:
            it.setBrush(QColor(t.pose))
            it.setPen(_cosmetic(t.pose_edge, 0.8))

    def set_session(self, session, layers):
        self.session = session
        self.layers = list(layers)
        self.map = session.map if session else None
        self._reset_items()
        if session is None:
            return
        sc = self.scene()
        rect = QRectF()
        if self.map is not None:
            self._build_static()
            pm = self._static["map"][0].pixmap() if self._static["map"] else None
            if pm is not None:
                rect = QRectF(0, 0, pm.width(), pm.height())
        self._layer_color = {l.id: l.color for l in self.layers}
        for z, layer in enumerate(self.layers):
            self.marker_items[layer.id] = [self._make_marker(layer, ev, 50 + z)
                                           for ev in session.markers.get(layer.id, [])]
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

    def _build_static(self):
        """맵 이미지·금지구역·가상벽·스테이션 (테마 변경 시 다시 생성)."""
        sc = self.scene()
        for kind in ("map", "keepout", "wall"):
            for it in self._static[kind]:
                sc.removeItem(it)
            self._static[kind] = []
        t = style.T()
        pix = map_pixmap(self.map.image_path)
        if not pix.isNull():
            if not self.map.width:
                self.map.width, self.map.height = pix.width(), pix.height()
            item = QGraphicsPixmapItem(pix)
            item.setZValue(-100)
            sc.addItem(item)
            self._static["map"].append(item)
        for _zid, pts in self.map.keepouts:
            poly = QPolygonF([QPointF(*self.world_to_scene(x, y)) for x, y in pts])
            it = QGraphicsPolygonItem(poly)
            it.setBrush(t.qcolor("keepout_fill"))
            it.setPen(_cosmetic(t.qcolor("keepout_edge"), 1.0))
            it.setZValue(-50)
            sc.addItem(it)
            self._static["keepout"].append(it)
        for pts in self.map.walls:
            path = QPainterPath(QPointF(*self.world_to_scene(*pts[0])))
            for x, y in pts[1:]:
                path.lineTo(QPointF(*self.world_to_scene(x, y)))
            it = QGraphicsPathItem(path)
            it.setPen(_cosmetic(t.wall, 2.5))
            it.setZValue(-40)
            sc.addItem(it)
            self._static["wall"].append(it)
        if self.map.station:
            self._make_station()

    def apply_theme(self, visibility: dict = None):
        """테마 변경: 정적 요소·마커·로봇/궤적 색을 다시 적용."""
        self._apply_styles()
        if self.session is None:
            return
        if self.map is not None:
            self._build_static()
            for kind, vis in (visibility or {}).items():
                self.set_static_visible(kind, vis)
        for layer in self.layers:
            self.set_layer_size(layer.id, self.layer_sizes.get(layer.id, layer.size))

    def _make_station(self):
        for it in self._static["station"]:
            self.scene().removeItem(it)
        self._static["station"] = []
        sx, sy, th = self.map.station
        body = QGraphicsPathItem(station_path(self.station_size))
        t = style.T()
        body.setBrush(QColor(t.station))
        body.setPen(_cosmetic(t.station_edge, 1.4))
        body.setFlag(QGraphicsItem.ItemIgnoresTransformations)
        body.setPos(*self.world_to_scene(sx, sy))
        body.setRotation(-math.degrees(th))      # 맵 각도(반시계) → 화면(y 아래) 회전
        body.setZValue(-30)
        body.setToolTip(f"충전 스테이션 ({sx:.2f}, {sy:.2f}, {math.degrees(th):.0f}°)\n"
                        "화살표 = station_pose theta (도킹 시 로봇이 바라보는 방향)")
        arrow = QGraphicsPathItem(station_arrow(self.station_size), body)
        arrow.setBrush(QColor("#ffffff"))
        arrow.setPen(_cosmetic("#1b5e20", 0.8))
        body._arrow = arrow   # 자식 아이템 참조 보관
        self.scene().addItem(body)
        self._static["station"].append(body)

    def _make_marker(self, layer, ev, z):
        size = float(self.layer_sizes.get(layer.id, layer.size))
        item = QGraphicsPathItem(shape_path(layer.shape, size))
        color = QColor(layer.color)
        if layer.shape == "cross":
            item.setPen(_cosmetic(color, 2.5))
        else:
            item.setBrush(color)
            item.setPen(_cosmetic(style.T().marker_edge, 0.8))
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
            item._label = txt   # 자식 아이템 참조 보관
        item.setToolTip(f"[{layer.name}] {fmt_time(ev.t)}\n{ev.text}\n({ev.x:.2f}, {ev.y:.2f})")
        item.setData(0, ev)
        item.setVisible(False)
        self.scene().addItem(item)
        return item

    # ------------------------------------------------------------ 표시 설정
    def set_layer_size(self, layer_id: str, size: float):
        self.layer_sizes[layer_id] = size
        if not self.session:
            return
        for z, layer in enumerate(self.layers):
            if layer.id != layer_id:
                continue
            for it in self.marker_items.get(layer_id, []):
                self.scene().removeItem(it)
            if layer_id == "target":
                self._cur_target = None
            self.marker_items[layer_id] = [self._make_marker(layer, ev, 50 + z)
                                           for ev in self.session.markers.get(layer_id, [])]
            self.marker_range[layer_id] = (0, 0)
        self.redraw()

    def set_robot_size(self, size: int):
        self.robot_size = size
        self._apply_styles()
        for it in self._pose_pool:
            it.setPath(robot_path(max(size * 0.55, 6)))
        self.redraw()

    def set_station_size(self, size: int):
        self.station_size = size
        if self.map is not None and self.map.station:
            visible = all(it.isVisible() for it in self._static["station"])
            self._make_station()
            self.set_static_visible("station", visible)

    def set_trail(self, mode: str, count: int, width: float):
        self.trail_mode, self.trail_count, self.trail_width = mode, count, width
        self._apply_styles()
        self.redraw()

    def set_static_visible(self, kind: str, visible: bool):
        for it in self._static.get(kind, []):
            it.setVisible(visible)

    def redraw(self):
        if self._last is not None:
            self.update_frame(*self._last)

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
        self._last = (vt, history_sec)
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

        if self.show_trail and self.trail_mode == "poses":
            self.trail.setPath(QPainterPath())
            self._update_pose_trail(S, vt)
        else:
            self._hide_pose_pool(0)
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

    def _pose_item(self, j):
        while len(self._pose_pool) <= j:
            it = QGraphicsPathItem(robot_path(max(self.robot_size * 0.55, 6)))
            it.setBrush(QColor(style.T().pose))
            it.setPen(_cosmetic(style.T().pose_edge, 0.8))
            it.setFlag(QGraphicsItem.ItemIgnoresTransformations)
            it.setZValue(150)
            it.setVisible(False)
            self.scene().addItem(it)
            self._pose_pool.append(it)
        return self._pose_pool[j]

    def _hide_pose_pool(self, start):
        for it in self._pose_pool[start:]:
            if it.isVisible():
                it.setVisible(False)

    def _update_pose_trail(self, S, vt):
        """레거시 방식: 현재까지 최근 N개 포즈를 개별 표시 (오래될수록 흐리게)."""
        i1 = bisect.bisect_right(S.pose_vt, vt)
        i0 = max(0, i1 - min(self.trail_count, TRAIL_MAX_POSES))
        n = i1 - i0
        for j, i in enumerate(range(i0, i1)):
            it = self._pose_item(j)
            it.setPos(*self.world_to_scene(S.pose_x[i], S.pose_y[i]))
            it.setRotation(-S.pose_yaw[i])
            it.setOpacity(0.25 + 0.65 * (j + 1) / n)
            it.setVisible(True)
        self._hide_pose_pool(n)

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
                cur.setPen(_cosmetic(style.T().target_now, 3.0))
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
                owner = it.parentItem() or it
                ev = owner.data(0)
                if ev is not None and owner.isVisible():
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
        painter.setPen(QPen(QColor(style.T().scale_bar), 2))
        painter.drawLine(QPointF(x0, y0), QPointF(x0 + bar, y0))
        painter.drawLine(QPointF(x0, y0 - 5), QPointF(x0, y0 + 1))
        painter.drawLine(QPointF(x0 + bar, y0 - 5), QPointF(x0 + bar, y0 + 1))
        painter.drawText(QPointF(x0 + 4, y0 - 6), f"{length:g} m")
        painter.restore()

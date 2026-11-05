"""분석 결과 데이터 (Session).

시간 축
- t  : 로그에 적힌 시각 (naive epoch, 표시용. 재부팅/시간동기화로 역행할 수 있음)
- vt : 재생 축 시각. 로그 순서대로 단조 증가하며, idle_gap 보다 긴 공백은 idle_gap 으로 압축
- rt : 실제 경과 시간 누적 (단조 증가, 시간 역행/점프는 0으로 처리). '최근 N분' 표시 범위에 사용
"""
import bisect
import math
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(slots=True)
class Event:
    t: float
    vt: float
    rt: float
    fi: int          # 파일 인덱스
    ln: int          # 파일 내 줄 인덱스 (0부터)
    node: str
    rule: str
    cat: str
    level: str
    text: str
    x: float = None
    y: float = None
    data: dict = None


@dataclass
class LogFile:
    path: Path
    name: str
    lines: list


def fmt_time(t: float, ms: bool = True) -> str:
    if t is None:
        return "-"
    dt = datetime.fromtimestamp(t, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if ms else dt.strftime("%Y-%m-%d %H:%M:%S")


def fmt_duration(sec: float) -> str:
    sec = int(max(sec, 0))
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    return (f"{d}일 " if d else "") + f"{h:02d}:{m:02d}:{s:02d}"


class Session:
    POSE_INTERP_MAX_SEC = 6.0
    POSE_INTERP_MAX_DIST = 2.0

    def __init__(self, source=None, rules=None, idle_gap: float = 5.0):
        self.source = source
        self.rules = rules
        self.idle_gap = idle_gap
        self.files = []
        self.line_offsets = [0]
        self.events = []        # 이벤트 목록 (로그 순서)
        self.markers = {}       # layer id -> [Event]
        self.error_events = []  # 발생/해제/리셋 (활성 에러 계산용)
        self.boots = []
        self.jumps = []         # 시간 역행/점프 {'vt','t_from','t_to','kind'}
        self.tracks = {}        # 이름 -> ([vt], [값])
        self.pose_vt, self.pose_rt, self.pose_t = [], [], []
        self.pose_x, self.pose_y, self.pose_yaw, self.pose_g = [], [], [], []
        self.tl_vt, self.tl_rt, self.tl_t, self.tl_g = [], [], [], []
        self.vt_end = 0.0
        self.map = None
        self.stats = {}
        self.event_vts = []
        self.marker_vts = {}
        self.marker_rts = {}
        self.error_vts = []

    # ------------------------------------------------------------ 준비
    def finalize(self):
        self.event_vts = [e.vt for e in self.events]
        self.marker_vts = {k: [e.vt for e in v] for k, v in self.markers.items()}
        self.marker_rts = {k: [e.rt for e in v] for k, v in self.markers.items()}
        self.error_vts = [e.vt for e in self.error_events]

    # ------------------------------------------------------------ 줄
    @property
    def total_lines(self) -> int:
        return self.line_offsets[-1]

    def global_line(self, fi: int, ln: int) -> int:
        return self.line_offsets[fi] + ln

    def locate_line(self, g: int):
        fi = bisect.bisect_right(self.line_offsets, g) - 1
        fi = min(max(fi, 0), len(self.files) - 1)
        return fi, g - self.line_offsets[fi]

    def line_text(self, fi: int, ln: int) -> str:
        try:
            return self.files[fi].lines[ln]
        except IndexError:
            return ""

    # ------------------------------------------------------------ 시간
    def time_at(self, vt: float) -> float:
        if not self.tl_vt:
            return None
        k = bisect.bisect_right(self.tl_vt, vt) - 1
        if k < 0:
            return self.tl_t[0]
        nxt = self.tl_vt[k + 1] if k + 1 < len(self.tl_vt) else None
        dt = vt - self.tl_vt[k]
        if nxt is not None and nxt > self.tl_vt[k]:
            dt = min(dt, nxt - self.tl_vt[k])
        return self.tl_t[k] + max(dt, 0.0)

    def rt_at(self, vt: float) -> float:
        """vt 시점의 실제 경과 시간 누적값."""
        if not self.tl_vt:
            return 0.0
        k = bisect.bisect_right(self.tl_vt, vt) - 1
        if k < 0:
            return self.tl_rt[0]
        return self.tl_rt[k] + max(vt - self.tl_vt[k], 0.0)

    def line_at(self, vt: float) -> int:
        """vt 시점의 마지막 처리 줄 (전역 줄 번호)."""
        if not self.tl_vt:
            return 0
        k = max(bisect.bisect_right(self.tl_vt, vt) - 1, 0)
        return self.tl_g[k]

    def vt_at_line(self, g: int) -> float:
        if not self.tl_g:
            return 0.0
        k = max(bisect.bisect_right(self.tl_g, g) - 1, 0)
        return self.tl_vt[k]

    def vt_for_time(self, t: float):
        """로그 시각 t 이후 처음 나오는 지점 (로그 순서 기준)."""
        for vt, tt in zip(self.tl_vt, self.tl_t):
            if tt >= t:
                return vt
        return None

    @property
    def t_first(self):
        return self.tl_t[0] if self.tl_t else None

    @property
    def t_last(self):
        return self.tl_t[-1] if self.tl_t else None

    # ------------------------------------------------------------ 상태
    def pose_index(self, vt: float) -> int:
        return bisect.bisect_right(self.pose_vt, vt) - 1

    def pose_at(self, vt: float, interp: bool = True):
        i = self.pose_index(vt)
        if i < 0:
            return None
        x, y, yaw = self.pose_x[i], self.pose_y[i], self.pose_yaw[i]
        if interp and i + 1 < len(self.pose_vt):
            v0, v1 = self.pose_vt[i], self.pose_vt[i + 1]
            x1, y1 = self.pose_x[i + 1], self.pose_y[i + 1]
            if (v1 > v0 and self.pose_t[i + 1] - self.pose_t[i] <= self.POSE_INTERP_MAX_SEC
                    and math.hypot(x1 - x, y1 - y) <= self.POSE_INTERP_MAX_DIST):
                r = min(max((vt - v0) / (v1 - v0), 0.0), 1.0)
                dyaw = (self.pose_yaw[i + 1] - yaw + 180.0) % 360.0 - 180.0
                return x + (x1 - x) * r, y + (y1 - y) * r, yaw + dyaw * r
        return x, y, yaw

    def track_at(self, name: str, vt: float):
        tr = self.tracks.get(name)
        if not tr:
            return None
        i = bisect.bisect_right(tr[0], vt) - 1
        return tr[1][i] if i >= 0 else None

    def active_errors(self, vt: float) -> "OrderedDict":
        """vt 시점에 활성인 에러 {코드: Event(발생)}."""
        active = OrderedDict()
        n = bisect.bisect_right(self.error_vts, vt)
        for ev in self.error_events[:n]:
            st = ev.data.get("status")
            if st == "OCCURRED":
                active[ev.data["code"]] = ev
            elif st == "RELEASED":
                active.pop(ev.data["code"], None)
            elif st == "RESET":
                active.clear()
        return active

    def event_index_at(self, vt: float) -> int:
        return bisect.bisect_right(self.event_vts, vt) - 1

    def marker_count_at(self, layer: str, vt: float) -> int:
        return bisect.bisect_right(self.marker_vts.get(layer, []), vt)

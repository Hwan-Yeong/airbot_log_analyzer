"""로봇 로그 파싱: 로테이션 파일을 순서대로 읽어 Session 을 만든다."""
import calendar
import re
from collections import Counter

from .model import Event, LogFile, Session
from .rules import RuleSet, _Fmt

HEADER_RE = re.compile(r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?)\] \[([^\]]*)\] \[([A-Z]+)\] ?(.*)")
BACKWARD_JUMP_SEC = 2.0       # 이보다 크게 과거로 가면 '시간 역행' 표시 (재부팅 후 RTC 초기값 등)
FORWARD_JUMP_SEC = 6 * 3600   # 이보다 크게 미래로 가면 '시간 점프' 표시 (시간 동기화 등)
SAME_BOOT_SEC = 30.0
PROGRESS_STEP = 50000


def _noop(*_args, **_kwargs):
    pass


class _TsParser:
    """'YYYY-MM-DD HH:MM:SS.mmm' → epoch(UTC 기준, 로그 시각 그대로). 분 단위 캐시."""

    def __init__(self):
        self.cache = {}

    def __call__(self, s: str) -> float:
        key = s[:16]
        base = self.cache.get(key)
        if base is None:
            base = calendar.timegm((int(s[0:4]), int(s[5:7]), int(s[8:10]),
                                    int(s[11:13]), int(s[14:16]), 0, 0, 0, 0))
            self.cache[key] = base
        return base + float(s[17:])


def _to_float(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # nan 제외


def read_log_files(paths, progress=_noop):
    files = []
    for i, p in enumerate(paths):
        progress(f"로그 읽는 중: {p.name}", i / max(len(paths), 1))
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            files.append(LogFile(path=p, name=p.name, lines=f.read().splitlines()))
    return files


def parse_session(source, rules: RuleSet, idle_gap: float = 5.0, progress=_noop) -> Session:
    S = Session(source=source, rules=rules, idle_gap=idle_gap)
    S.files = read_log_files(source.log_files, progress)
    for lf in S.files:
        S.line_offsets.append(S.line_offsets[-1] + len(lf.lines))
    _Parser(S, rules, progress).run()
    S.finalize()
    S.stats = _stats(S)
    return S


class _Parser:
    def __init__(self, S: Session, rules: RuleSet, progress):
        self.S, self.rules, self.progress = S, rules, progress
        self.ts = _TsParser()
        self.prev_t = None
        self.vt = 0.0
        self.rt = 0.0
        self.last_pose = None
        self.last_boot_vt = None
        self.dedupe_last = {}
        self.dedupe_seen = set()
        self.track_last = {}
        self.hits = Counter()

    # 재생 축 진행 (일치한 줄마다 1회)
    def advance(self, t: float, g: int) -> float:
        S = self.S
        if self.prev_t is not None:
            dt = t - self.prev_t
            real = dt
            if dt < -BACKWARD_JUMP_SEC:
                S.jumps.append({"vt": self.vt, "t_from": self.prev_t, "t_to": t, "kind": "back"})
                dt = real = 0.0
            elif dt < 0:
                dt = real = 0.0
            elif dt > FORWARD_JUMP_SEC:
                S.jumps.append({"vt": self.vt, "t_from": self.prev_t, "t_to": t, "kind": "forward"})
                dt, real = S.idle_gap, 0.0
            elif dt > S.idle_gap:
                dt = S.idle_gap
            self.vt += dt
            self.rt += real
        self.prev_t = t
        S.tl_vt.append(self.vt)
        S.tl_rt.append(self.rt)
        S.tl_t.append(t)
        S.tl_g.append(g)
        return self.vt

    def run(self):
        S, rules = self.S, self.rules
        hdr = HEADER_RE.match
        cont_rules = rules.continuation
        total = max(S.total_lines, 1)
        for fi, lf in enumerate(S.files):
            base_g = S.line_offsets[fi]
            cur_ts = cur_node = None
            cur_t = None
            line_vt = None
            for ln, line in enumerate(lf.lines):
                g = base_g + ln
                if g % PROGRESS_STEP == 0:
                    self.progress(f"분석 중: {lf.name}", g / total)
                m = hdr(line) if line[:1] == "[" else None
                if m is not None:
                    cur_ts, cur_node, lvl, msg = m.group(1), m.group(2), m.group(3), m.group(4)
                    cur_t = None
                    line_vt = None
                    candidates = rules.rules_for(cur_node)
                else:
                    if cur_ts is None or not cont_rules:
                        continue
                    lvl, msg = "", line
                    candidates = cont_rules
                    line_vt = None
                for r in candidates:
                    if r.levels and lvl not in r.levels:
                        continue
                    cs = r.contains
                    if cs:
                        if cs[0] not in msg:
                            continue
                        if len(cs) > 1 and not all(c in msg for c in cs[1:]):
                            continue
                    mm = r.regex.search(msg)
                    if mm is None:
                        continue
                    if cur_t is None:
                        try:
                            cur_t = self.ts(cur_ts)
                        except ValueError:
                            break
                    if line_vt is None:
                        line_vt = self.advance(cur_t, g)
                    self.handle(r, mm, msg, cur_t, line_vt, fi, ln, cur_node)
        S.vt_end = self.vt
        S.stats_hits = self.hits

    def _event(self, r, t, vt, fi, ln, node, level, text, x=None, y=None, data=None):
        return Event(t, vt, self.rt, fi, ln, node, r.id, r.category, level, text, x, y, data)

    def handle(self, r, mm, msg, t, vt, fi, ln, node):
        S = self.S
        kind = r.kind
        if kind == "pose":
            x, y, yaw = _to_float(mm.group("x")), _to_float(mm.group("y")), _to_float(mm.group("yaw"))
            if x is None or y is None:
                return
            S.pose_vt.append(vt)
            S.pose_rt.append(self.rt)
            S.pose_t.append(t)
            S.pose_x.append(x)
            S.pose_y.append(y)
            S.pose_yaw.append(yaw or 0.0)
            S.pose_g.append(S.line_offsets[fi] + ln)
            self.last_pose = (x, y)
            return

        if kind == "error":
            for em in r.regex.finditer(msg):
                code, desc = em.group("code"), (em.group("desc") or "").strip()
                occurred = em.group("status").startswith("OCC")
                text = f"{code} {'발생' if occurred else '해제'}: {desc}"
                data = {"code": code, "status": "OCCURRED" if occurred else "RELEASED", "desc": desc}
                ev = self._event(r, t, vt, fi, ln, node, "error" if occurred else "info", text, data=data)
                if occurred and self.last_pose:
                    ev.x, ev.y = self.last_pose
                    if r.layer:
                        S.markers.setdefault(r.layer, []).append(ev)
                S.events.append(ev)
                S.error_events.append(ev)
                self.hits[r.id] += 1
            return

        groups = mm.groupdict()
        if kind == "error_reset":
            ev = self._event(r, t, vt, fi, ln, node, r.level, r.format_text(groups, msg),
                             data={"status": "RESET"})
            S.events.append(ev)
            S.error_events.append(ev)
            self.hits[r.id] += 1
            return

        if kind == "boot":
            # 재부팅 후 RTC 가 같은 초기값으로 돌아가는 경우가 있어 재생 축(vt) 기준으로 묶는다
            if self.last_boot_vt is not None and vt - self.last_boot_vt < SAME_BOOT_SEC:
                return
            self.last_boot_vt = vt
            ev = self._event(r, t, vt, fi, ln, node, r.level, r.format_text(groups, msg),
                             data={"status": "RESET"})
            S.events.append(ev)
            S.boots.append(ev)
            S.error_events.append(ev)  # 재부팅 시 활성 에러 초기화
            self.hits[r.id] += 1
            return

        text = r.format_text(groups, msg)
        if r.dedupe_sec:
            key = (r.id,) if r.dedupe_by == "rule" else (r.id, text)
            last = self.dedupe_last.get(key)
            if last is not None and 0.0 <= t - last < r.dedupe_sec:
                return
            self.dedupe_last[key] = t

        x = y = None
        if groups.get("x") is not None and groups.get("y") is not None:
            x, y = _to_float(groups["x"]), _to_float(groups["y"])
            if x is None or y is None:
                x = y = None
        elif r.at_robot and self.last_pose:
            x, y = self.last_pose

        if r.dedupe_pos and x is not None:
            layer = S.rules.layer_by_id.get(r.layer) if S.rules else None
            label = groups.get(layer.label) if layer and layer.label else ""
            key = (r.id, label, round(x, 2), round(y, 2))
            if key in self.dedupe_seen:
                return
            self.dedupe_seen.add(key)

        listed = r.list
        if r.track:
            val = r.value.format_map(_Fmt(groups)) if r.value else text
            if val != self.track_last.get(r.track):
                self.track_last[r.track] = val
                tr = S.tracks.setdefault(r.track, ([], []))
                tr[0].append(vt)
                tr[1].append(val)
            elif r.list_on_change:
                listed = False

        if not listed and not (r.layer and x is not None):
            self.hits[r.id] += 1
            return
        ev = self._event(r, t, vt, fi, ln, node, r.level, text, x, y,
                         data=groups if r.layer else None)
        if listed:
            S.events.append(ev)
        if r.layer and x is not None:
            S.markers.setdefault(r.layer, []).append(ev)
        self.hits[r.id] += 1


def _stats(S: Session) -> dict:
    hits = getattr(S, "stats_hits", Counter())
    err_count, err_desc, err_first = Counter(), {}, {}
    for e in S.error_events:
        if e.data.get("status") == "OCCURRED":
            code = e.data["code"]
            err_count[code] += 1
            err_desc.setdefault(code, e.data.get("desc", ""))
            err_first.setdefault(code, e)
    batt = []
    for v in S.tracks.get("battery", ([], []))[1]:
        try:
            batt.append(int(str(v).rstrip("%")))
        except ValueError:
            pass
    versions = []
    for v in S.tracks.get("sw_version", ([], []))[1]:
        if v not in versions:
            versions.append(v)
    return {
        "files": len(S.files),
        "lines": S.total_lines,
        "events": len(S.events),
        "poses": len(S.pose_vt),
        "boots": len(S.boots),
        "jumps": len(S.jumps),
        "errors": err_count,
        "error_desc": err_desc,
        "error_first": err_first,
        "hits": hits,
        "battery_min": min(batt) if batt else None,
        "battery_max": max(batt) if batt else None,
        "sw_versions": versions,
    }

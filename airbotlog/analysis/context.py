"""분석 구간 발췌 → Claude 입력 블록.

1) 분석 구간 [기준 시각 − 앞, 기준 시각 + 뒤] 의 줄을 소스별로 모은다 (모두 AMR 기준 시각)
2) 이벤트: airbot_log 는 기존 규칙(config/rules.yaml) 결과, 나머지 소스는 config/analysis/rules.yaml
3) 반복 줄 접기 → 분량 제한 (기준 ±1분 > 이벤트 > 나머지, 기준에 가까운 순) → R 번호
"""
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import yaml

from ..model import fmt_time
from ..settings import CONFIG_DIR
from . import sources as srcs
from .sources import FileStat, Rec

RULES_PATH = CONFIG_DIR / "analysis" / "rules.yaml"
FOLD_MIN = 4               # 같은 형태의 줄이 이보다 많으면 첫 줄/끝 줄만 남기고 접음
NUM_RE = re.compile(r"0x[0-9a-fA-F]+|\d+(?:\.\d+)?")
MAX_TEXT = 300             # 한 줄 최대 글자 수
ANCHOR_NEAR = 60.0         # 기준 시각 ±60초는 우선 포함
STALE_BEFORE = srcs.epoch(2025, 1, 1, 0, 0, 0)   # RTC 초기값(2024-10-14 등) 구간 표시
EVENT_LIMIT = 400


def _noop(*_args, **_kwargs):
    pass


@dataclass
class ARule:
    id: str
    source: str
    contains: str
    regex: re.Pattern
    text: str
    type: str
    level: str


def load_rules(path=RULES_PATH):
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    rules = []
    for r in data.get("rules", []):
        rules.append(ARule(r["id"], r["source"], r.get("contains", ""), re.compile(r["regex"]), r.get("text", r["id"]),
                           r.get("type", "info"), r.get("level", "info")))
    soc = data.get("soc", {}) or {}
    noise = soc.get("noise") or ""
    return rules, set(soc.get("tags", [])), (re.compile(noise) if noise else None)


@dataclass
class Options:
    anchor: float                    # 기준 시각 (AMR 기준 naive epoch)
    before: float = 600.0            # 초
    after: float = 300.0
    sources: tuple = ("amr", "ota", "journal", "dmesg", "soc")
    max_lines: int = 1500
    max_chars: int = 150_000

    @property
    def t0(self):
        return self.anchor - self.before

    @property
    def t1(self):
        return self.anchor + self.after


@dataclass
class Event:
    t: float
    src: str
    type: str
    code: str
    text: str
    level: str = "info"
    rec: Rec = None
    t_end: float = None              # 요약(구간 밖) 항목의 마지막 시각
    count: int = 1
    id: str = ""
    rid: str = ""


@dataclass
class Line:
    rec: Rec
    note: str = ""                   # 반복 접기 표시
    rid: str = ""
    g: int = None                    # airbot_log 전역 줄 번호 (화면 이동용)


@dataclass
class Context:
    options: Options
    session_label: str
    request_text: str
    request_extract: dict
    offset: srcs.Offset
    stats: list
    notes: list
    window_events: list
    summary_events: list
    lines: list                      # 발췌 Line (시간순), 생략 표시는 rec=None
    counts: dict
    meta: dict = field(default_factory=dict)

    @property
    def rmap(self) -> dict:
        return {ln.rid: ln for ln in self.lines if ln.rec is not None}

    @property
    def ids(self) -> set:
        return set(self.rmap) | {e.id for e in self.window_events + self.summary_events}

    # ------------------------------------------------------------ 블록
    def coverage_text(self) -> str:
        o = self.options
        m = self.meta
        out = [f"- 시료: {self.session_label}" + (f" (수집 {m['collected']})" if m.get("collected") else ""),
               f"- SW 버전(airbot_log): {', '.join(m.get('sw_versions') or []) or '-'}",
               f"- 분석 구간(AMR 기준): {fmt_time(o.t0, False)} ~ {fmt_time(o.t1, False)} "
               f"(기준 시각 {fmt_time(o.anchor, False)}, 앞 {o.before / 60:g}분 / 뒤 {o.after / 60:g}분)",
               f"- 시계: {self.offset.describe()}",
               "- 시각 표기: AMR 로그 시각 기준(KST 벽시계 값). otalog 는 초 단위, journal 은 연도 없음(수집 시각으로 연도 추정)."]
        boots = m.get("boots") or []
        if boots:
            out.append(f"- AMR 앱 시작(부팅/재시작) {len(boots)}회: " + ", ".join(boots[:12]) + (" …" if len(boots) > 12 else ""))
        jumps = m.get("jumps") or []
        if jumps:
            out.append("- AMR 시각 점프(RTC 초기값→동기화 등): " + "; ".join(jumps[:10]) + (" …" if len(jumps) > 10 else ""))
        out.append("- 파일별 커버 구간 (소스 | 파일 | 첫 시각 | 끝 시각 | 줄 수 | 비고):")
        for st in self.stats:
            first = fmt_time(st.first, False) if st.first is not None else "-"
            last = fmt_time(st.last, False) if st.last is not None else "-"
            out.append(f"  {st.src} | {st.name} | {first} | {last} | {st.lines:,} | {st.note}")
        c = self.counts
        out.append(f"- 구간 안 줄 수: 원래 {c.get('window', 0):,}줄 → 반복 접기 후 {c.get('folded', 0):,}줄 → "
                   f"발췌 {c.get('selected', 0):,}줄" + (f" (분량 제한으로 {c['dropped']:,}줄 생략)" if c.get("dropped") else ""))
        for n in self.notes:
            out.append(f"- 주의: {n}")
        return "\n".join(out)

    def events_text(self) -> str:
        out = ["[분석 구간 안] id | 시각(AMR 기준) | 소스 | 유형 | 코드 | 내용 | 근거"]
        for e in self.window_events:
            out.append(f"{e.id} | {fmt_time(e.t)} | {e.src} | {e.type} | {e.code or '-'} | {e.text} | {e.rid or '-'}")
        if not self.window_events:
            out.append("(없음)")
        out.append("[로그 전체 요약 (구간 밖 포함)] id | 첫 시각 ~ 마지막 시각 | 소스 | 유형 | 코드 | 내용 | 횟수")
        for e in self.summary_events:
            span = fmt_time(e.t, False) + (f" ~ {fmt_time(e.t_end, False)}" if e.t_end and e.t_end != e.t else "")
            out.append(f"{e.id} | {span} | {e.src} | {e.type} | {e.code or '-'} | {e.text} | {e.count}")
        if not self.summary_events:
            out.append("(없음)")
        return "\n".join(out)

    def timeline_text(self) -> str:
        out = ["id | 시각(AMR 기준) | 시각 기준 | 소스 | 파일:줄 | 원문"]
        for ln in self.lines:
            if ln.rec is None:
                out.append(ln.note)
                continue
            out.append(format_line(ln))
        return "\n".join(out)

    def user_message(self) -> str:
        return ("<request>\n" + (self.request_text.strip() or "(요청 문장 없음)") + "\n</request>\n"
                "<request_extract>\n" + json.dumps(self.request_extract, ensure_ascii=False, indent=1) + "\n</request_extract>\n"
                "<coverage>\n" + self.coverage_text() + "\n</coverage>\n"
                "<events>\n" + self.events_text() + "\n</events>\n"
                "<timeline>\n" + self.timeline_text() + "\n</timeline>\n")


def _basis_text(r: Rec) -> str:
    if r.src == srcs.SRC_SOC:
        return f"{r.basis}(원본 {r.raw_t})"
    if r.src == srcs.SRC_DMESG:
        return f"추정(dmesg {r.raw_t})"
    return r.basis


def _short_text(r: Rec) -> str:
    if r.src == srcs.SRC_AMR:
        text = f"[{r.tag}] [{r.level}] {r.msg}"
    elif r.src == srcs.SRC_OTA:
        text = f"[{r.level}] {r.msg}"
    elif r.src == srcs.SRC_SOC:
        text = f"{r.level} {r.tag}: {r.msg}"
    elif r.src == srcs.SRC_JOURNAL:
        text = f"{r.tag}: {r.msg}"
    else:
        text = r.msg
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + f"…(+{len(text) - MAX_TEXT}자)"
    return text


def format_line(ln: Line) -> str:
    r = ln.rec
    return (f"{ln.rid} | {fmt_time(r.t)} | {_basis_text(r)} | {r.src} | {r.file}:L{r.line} | {_short_text(r)}"
            + (f"  {ln.note}" if ln.note else ""))


# ---------------------------------------------------------------- 수집
def amr_window(S, t0, t1):
    """airbot_log 에서 구간 안 줄 (연속 줄은 앞 줄에 붙임)."""
    s0, s1 = fmt_time(t0), fmt_time(t1)
    out = []
    for fi, lf in enumerate(S.files):
        inwin = False
        cur = None
        for ln, line in enumerate(lf.lines):
            if line[:1] == "[" and len(line) > 25 and line[5] == "-" and line[24] == "]":
                ts = line[1:24]
                inwin = s0 <= ts <= s1
                cur = None
                if not inwin:
                    continue
                m = srcs.AMR_RE.match(line)
                if not m:
                    continue
                t = srcs.epoch(int(ts[0:4]), int(ts[5:7]), int(ts[8:10]), int(ts[11:13]), int(ts[14:16]),
                               int(ts[17:19]), int(ts[20:23]) / 1000.0)
                cur = Rec(t, srcs.SRC_AMR, lf.name, ln + 1, line, tag=m.group(2), level=m.group(3), msg=m.group(4),
                          basis="stale" if t < STALE_BEFORE else "AMR")
                out.append((cur, S.line_offsets[fi] + ln))
            elif inwin and cur is not None and line.strip():
                cur.msg += " / " + line.strip()
                cur.text += "\n" + line
    return out


def _amr_event_type(e) -> str:
    st = (e.data or {}).get("status") if e.data else None
    if st == "OCCURRED":
        return "error_occur"
    if st in ("RELEASED", "RESET"):
        return "error_clear"
    if e.rule == "boot":
        return "boot"
    return e.cat or "info"


class _Matcher:
    def __init__(self, rules):
        self.by_src = defaultdict(list)
        for r in rules:
            self.by_src[r.source].append(r)
        self.kw = {k: re.compile("|".join(re.escape(r.contains) for r in v if r.contains) or "(?!)")
                   for k, v in self.by_src.items()}

    def maybe(self, src_key, line) -> bool:
        kw = self.kw.get(src_key)
        return bool(kw and kw.search(line))

    def match(self, src_key, line):
        hits = []
        for r in self.by_src.get(src_key, []):
            if r.contains and r.contains not in line:
                continue
            m = r.regex.search(line)
            if m:
                g = {k: v for k, v in m.groupdict().items() if v is not None}
                try:
                    text = r.text.format(**g)
                except (KeyError, IndexError, ValueError):
                    text = r.text
                hits.append((r, g.get("code", ""), text))
        return hits


def build_context(session, request_text: str, request_extract: dict, options: Options, progress=_noop,
                  rules_path=RULES_PATH) -> Context:
    S = session
    rules, soc_tags, noise = load_rules(rules_path)
    matcher = _Matcher(rules)
    t0, t1 = options.t0, options.t1
    sset = srcs.discover(S)
    notes = list(sset.notes)
    offset = srcs.soc_offset(S, near_t=options.anchor)
    stats, recs, events = [], [], []
    want = set(options.sources)

    def in_win(t):
        return t is not None and t0 <= t <= t1

    def add_events(src_key, rec):
        for r, code, text in matcher.match(src_key, rec.text):
            events.append(Event(rec.t, rec.src, r.type, code, text, r.level, rec=rec))

    # ---- AMR airbot_log
    progress("airbot_log 구간 추출", None)
    amr = amr_window(S, t0, t1) if "amr" in want else []
    gmap = {}
    for rec, g in amr:
        recs.append(rec)
        gmap[id(rec)] = g
    for lf in S.files:
        st = FileStat(srcs.SRC_AMR, lf.name, lines=len(lf.lines))
        for line in lf.lines[:200]:
            m = srcs.AMR_RE.match(line)
            if m:
                st.first = _ts(m.group(1))
                break
        for line in reversed(lf.lines[-200:]):
            m = srcs.AMR_RE.match(line)
            if m:
                st.last = _ts(m.group(1))
                break
        stats.append(st)

    # ---- otalog
    if sset.ota:
        for ref in sset.ota:
            progress(f"otalog 읽는 중: {ref.name}", None)
            st = FileStat(srcs.SRC_OTA, ref.name)
            for rec in srcs.read_ota(ref, st):
                if matcher.maybe("ota", rec.text):
                    add_events("ota", rec)
                if "ota" in want and in_win(rec.t):
                    recs.append(rec)
            stats.append(st)

    # ---- journal / dmesg (스냅샷)
    jrecs_by_snap = {}
    for ref in sset.journal:
        progress(f"journal 읽는 중: {ref.name}", None)
        st = FileStat(srcs.SRC_JOURNAL, ref.name)
        lst = list(srcs.read_journal(ref, st, sset))
        jrecs_by_snap[ref.snapshot] = lst
        for rec in lst:
            if matcher.maybe("journal", rec.text):
                add_events("journal", rec)
            if "journal" in want and in_win(rec.t):
                recs.append(rec)
        stats.append(st)
    for ref in sset.dmesg:
        st = FileStat(srcs.SRC_DMESG, ref.name)
        for rec in srcs.read_dmesg(ref, st, jrecs_by_snap.get(ref.snapshot, [])):
            if matcher.maybe("dmesg", rec.text):
                add_events("dmesg", rec)
            if "dmesg" in want and in_win(rec.t):
                recs.append(rec)
        stats.append(st)

    # ---- SoC logcat
    if sset.soc and "soc" in want:
        def pre(t, line):
            return in_win(t) or matcher.maybe("soc", line)

        def keep(rec):
            hits = matcher.match("soc", rec.text)
            for r, code, text in hits:
                events.append(Event(rec.t, rec.src, r.type, code, text, r.level, rec=rec))
            if not in_win(rec.t):
                return False
            if noise is not None and noise.search(rec.text):
                return False
            return bool(hits) or rec.tag in soc_tags or rec.level == "F"

        for ref in sset.soc:
            progress(f"SoC 로그 읽는 중: {ref.name}", None)
            st = FileStat(srcs.SRC_SOC, ref.name)
            try:
                recs.extend(srcs.read_logcat(ref, st, sset, offset, pre, keep, progress))
            except (OSError, EOFError, ValueError) as e:
                st.note = f"읽기 실패: {e}"
            stats.append(st)
        if not offset.known:
            notes.append("SoC↔AMR 시계 차이를 구하지 못해 SoC 행은 SoC 원본 시각입니다 (최대 수십 초 어긋날 수 있음).")
    elif sset.soc:
        notes.append("SoC logcat 은 이번 분석에서 제외됨")

    # ---- airbot_log 이벤트 (기존 규칙)
    rec_by_g = {g: rec for rec, g in amr}
    for e in S.events:
        if in_win(e.t):
            g = S.global_line(e.fi, e.ln)
            events.append(Event(e.t, srcs.SRC_AMR, _amr_event_type(e), (e.data or {}).get("code", ""), e.text,
                                e.level, rec=rec_by_g.get(g)))

    # ---- 정렬 · 접기 · 분량 제한
    recs.sort(key=lambda r: (r.t, r.src, r.line))
    window_total = len(recs)
    folded = _fold(recs)
    event_recs = {id(e.rec) for e in events if e.rec is not None}
    lines, dropped = _select(folded, options, event_recs)
    for k, ln in enumerate(x for x in lines if x.rec is not None):
        ln.rid = f"R{k + 1:05d}"
        ln.g = gmap.get(id(ln.rec))
    rid_of = {id(ln.rec): ln.rid for ln in lines if ln.rec is not None}
    fold_head = {}
    for ln in lines:
        if ln.rec is not None and ln.note.startswith("(x"):
            fold_head[_fold_key(ln.rec)] = ln.rid

    # ---- 이벤트 정리 (같은 형태가 반복되면 첫 건에 횟수·구간 표시)
    win_events = _fold_events(sorted((e for e in events if in_win(e.t)), key=lambda e: e.t))
    if len(win_events) > EVENT_LIMIT:
        win_events.sort(key=lambda e: (e.level != "error", e.level != "warn", abs(e.t - options.anchor)))
        win_events = sorted(win_events[:EVENT_LIMIT], key=lambda e: e.t)
        notes.append(f"구간 안 이벤트가 많아 {EVENT_LIMIT}건만 표시")
    for k, e in enumerate(win_events):
        e.id = f"EV{k + 1:03d}"
        if e.rec is not None:
            e.rid = rid_of.get(id(e.rec)) or fold_head.get(_fold_key(e.rec), "")
    summary = _summary_events(S, events, start=len(win_events))

    meta = {"collected": getattr(S.source, "collected", ""), "sw_versions": S.stats.get("sw_versions", []),
            "boots": [fmt_time(b.t, False) for b in S.boots], "serial": getattr(S.source, "serial", ""),
            "jumps": [f"{fmt_time(j['t_from'], False)} → {fmt_time(j['t_to'], False)}" for j in S.jumps],
            "sources": sset.available()}
    counts = {"window": window_total, "folded": len(folded), "selected": sum(1 for x in lines if x.rec is not None),
              "dropped": dropped}
    return Context(options, S.source.label if S.source else "-", request_text, request_extract, offset, stats, notes,
                   win_events, summary, lines, counts, meta)


def _ts(s: str) -> float:
    ms = float("0" + s[19:]) if len(s) > 19 else 0.0
    return srcs.epoch(int(s[0:4]), int(s[5:7]), int(s[8:10]), int(s[11:13]), int(s[14:16]), int(s[17:19]), ms)


def _fold_key(r: Rec):
    return (r.src, r.tag, NUM_RE.sub("#", r.msg)[:200])


def _hms(t):
    return fmt_time(t)[11:19]


def _fold(recs):
    groups = defaultdict(list)
    for i, r in enumerate(recs):
        groups[_fold_key(r)].append(i)
    drop, note = set(), {}
    for idx in groups.values():
        if len(idx) <= FOLD_MIN:
            continue
        ts = [recs[i].t for i in idx]
        gap = (ts[-1] - ts[0]) / (len(ts) - 1)
        gap_s = f"{gap:.1f}초" if gap < 60 else f"{gap / 60:.1f}분"
        note[idx[0]] = f"(x{len(idx)}, {_hms(ts[0])}~{_hms(ts[-1])}, 간격 약 {gap_s})"
        note[idx[-1]] = "(반복 끝)"
        drop.update(idx[1:-1])
    return [Line(recs[i], note.get(i, "")) for i in range(len(recs)) if i not in drop]


def _fold_events(evs):
    groups = defaultdict(list)
    for i, e in enumerate(evs):
        groups[(e.src, e.type, NUM_RE.sub("#", e.text))].append(i)
    drop = set()
    for idx in groups.values():
        if len(idx) <= FOLD_MIN:
            continue
        first = evs[idx[0]]
        first.count = len(idx)
        first.t_end = evs[idx[-1]].t
        first.text += f"  (x{len(idx)}, {_hms(first.t)}~{_hms(first.t_end)})"
        drop.update(idx[1:])
    return [e for i, e in enumerate(evs) if i not in drop]


def _select(items, options: Options, event_recs):
    total_chars = sum(len(_short_text(x.rec)) + 60 for x in items)
    if len(items) <= options.max_lines and total_chars <= options.max_chars:
        return items, 0
    anchor = options.anchor

    def tier(x):            # 0: 기준 ±1분 전부 → 1: 이벤트·반복 요약 → 2: 나머지 (각 단계 안에서는 기준에 가까운 순)
        if abs(x.rec.t - anchor) <= ANCHOR_NEAR:
            return 0
        return 1 if id(x.rec) in event_recs or x.note.startswith("(x") else 2

    order = sorted(range(len(items)), key=lambda i: (tier(items[i]), abs(items[i].rec.t - anchor)))
    chosen, chars = set(), 0
    for i in order:
        c = len(_short_text(items[i].rec)) + 60
        if len(chosen) >= options.max_lines or chars + c > options.max_chars:
            continue
        chosen.add(i)
        chars += c
    out, gap = [], 0
    for i, x in enumerate(items):
        if i in chosen:
            if gap:
                out.append(Line(None, f"…생략({gap}줄)…"))
                gap = 0
            out.append(x)
        else:
            gap += 1
    if gap:
        out.append(Line(None, f"…생략({gap}줄)…"))
    return out, len(items) - len(chosen)


def _summary_events(S, events, start=0):
    """로그 전체 요약: 에러 코드별 발생 횟수·처음/마지막, 부팅, 링크·재부팅 등 (구간 밖 포함)."""
    out = []
    occ = defaultdict(list)
    for e in S.error_events:
        st = (e.data or {}).get("status")
        if st == "OCCURRED":
            occ[(srcs.SRC_AMR, e.data.get("code", "?"))].append(e.t)
    for e in events:
        if e.type == "error_occur" and e.src != srcs.SRC_AMR:     # airbot_log 에러는 위에서 전체를 셈
            occ[(e.src, e.code or "?")].append(e.t)
    for (src, code), ts in sorted(occ.items(), key=lambda kv: min(kv[1])):
        out.append(Event(min(ts), src, "error_occur", code, f"{code} 발생", "error", t_end=max(ts), count=len(ts)))
    kinds = Counter()
    span = {}
    for e in events:
        if e.type in ("boot", "link", "net", "timeout", "ota", "crash", "restart", "collect"):
            key = (e.src, e.type, e.text)
            kinds[key] += 1
            a, b = span.get(key, (e.t, e.t))
            span[key] = (min(a, e.t), max(b, e.t))
    for key, n in sorted(kinds.items(), key=lambda kv: span[kv[0]][0]):
        src, typ, text = key
        a, b = span[key]
        out.append(Event(a, src, typ, "", text, "info", t_end=b, count=n))
    for k, e in enumerate(out):
        e.id = f"EV{start + k + 1:03d}"
    return out

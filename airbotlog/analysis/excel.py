"""현상 분석표(xlsx) 저장 — 기존 분석 결과물 양식(요약 / 타임라인 / 로그 파일)에 맞춘 간결한 형식.

- 요약: 시료·버전·접수 현상·실제 발생 + [현상] No|항목|내용 + 관련 에러·검토 의견·추가 확인 + 원인/대책(사람 작성)
- 타임라인: No | 시각 | 출처 (파일:줄) | 실제 로그 (원문) | 설명  — 핵심 행 강조
- 로그 파일: 분석 대상 로그와 기간, 시각 기준
시각은 날짜 포함 엑셀 날짜/시각 값(yyyy-mm-dd hh:mm:ss.000). 시트 간 수식 없음.
"""
import re
import unicodedata
from datetime import datetime

from ..model import fmt_time

HEADER_FILL, HEADER_FONT = "305496", "FFFFFF"
SECTION_FILL = "D9E1F2"
AMR_FILL, SOC_FILL = "DDEBF7", "FFF2CC"
KEY_FILL, KEY_FONT = "FCE4D6", "9C0006"
NOTE_FONT = "595959"
TIME_FMT = "yyyy-mm-dd hh:mm:ss.000"
CELL_MAX = 32000
EVIDENCE_TAIL_RE = re.compile(r"\s*\[(?:R\d{5}|EV\d{3})(?:\s*,\s*(?:R\d{5}|EV\d{3}))*\]\s*$")
NUMBER_HEAD_RE = re.compile(r"^\s*(?:[①-⑳]|\d{1,2}[.)])\s*")      # 번호는 No 열에 있으므로 본문 앞 번호는 뺌


def _dt(text: str):
    """'YYYY-MM-DD hh:mm:ss(.mmm)' → datetime (아니면 None)."""
    m = re.match(r"^(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d)(?::(\d\d)(?:\.(\d{1,3}))?)?$", (text or "").strip())
    if not m:
        return None
    y, mo, d, h, mi = (int(v) for v in m.groups()[:5])
    s = int(m.group(6) or 0)
    ms = int((m.group(7) or "0").ljust(3, "0"))
    try:
        return datetime(y, mo, d, h, mi, s, ms * 1000)
    except ValueError:
        return None


def _device(source: str) -> str:
    return "SoC" if (source or "").startswith("SoC") else "AMR"


def text_lines(text, width: float) -> int:
    """엑셀 열 너비(문자 수 단위)에서 줄바꿈되는 줄 수 추정 — 한글 등 전각 문자는 2칸."""
    per_line = max(int(width * 0.88), 1)            # 맑은 고딕은 한 글자가 2칸보다 약간 넓음 → 여유
    total = 0
    for part in str(text if text is not None else "").split("\n"):
        w = sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in part)
        total += max(1, -(-w // per_line))
    return total


def _ev_note(e) -> str:
    """요약 이벤트(구간 밖 포함)의 횟수·기간 표시."""
    if getattr(e, "count", 1) > 1 and e.t_end is not None:
        return f"(×{e.count}, {fmt_time(e.t, False)} ~ {fmt_time(e.t_end, False)})"
    return ""


def default_name(pattern: str, serial: str, issue: str, when: datetime = None) -> str:
    when = when or datetime.now()
    s6 = serial[-6:] if serial else "시료"
    issue = re.sub(r'[\\/:*?"<>|\s]+', "", issue or "")[:20] or "이슈"
    return pattern.format(serial6=s6, issue=issue, date=when.strftime("%y%m%d"))


def save(path, ctx, result: dict, meta: dict = None, title_prefix: str = "Airbot"):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    wrap = Alignment(wrap_text=True, vertical="top")

    def fill(c):
        return PatternFill("solid", fgColor=c)

    def put(ws, row, col, value, bold=False, color=None, bg=None, fmt=None, italic=False):
        if isinstance(value, str) and len(value) > CELL_MAX:
            value = value[:CELL_MAX] + f"…({len(value) - CELL_MAX}자 생략)"
        c = ws.cell(row=row, column=col, value=value)
        c.border, c.alignment = border, wrap
        if bold or color or italic:
            c.font = Font(bold=bold, color=color, italic=italic)
        if bg:
            c.fill = fill(bg)
        if fmt:
            c.number_format = fmt
        return c

    def header(ws, row, names):
        for i, name in enumerate(names, 1):
            c = put(ws, row, i, name, bold=True, color=HEADER_FONT, bg=HEADER_FILL)
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    def section(ws, row, text, ncol):
        put(ws, row, 1, text, bold=True, bg=SECTION_FILL)
        for c in range(2, ncol + 1):
            put(ws, row, c, None, bg=SECTION_FILL)
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=ncol)

    def row_height(ws, row, *cells):
        """cells: (글, 열 너비) — 가장 많이 줄바꿈되는 칸에 맞춤 (한글 폭 반영, 엑셀 최대 409)."""
        lines = max((text_lines(t, w) for t, w in cells), default=1)
        ws.row_dimensions[row].height = min(max(15 * lines + 3, 18), 409)

    def banner(ws, row, text, ncol, width, **font):
        """시트 위 제목·주석: 여러 열을 합쳐 줄바꿈 (긴 글이 한 칸에서 잘리거나 옆으로 넘치지 않게)."""
        c = ws.cell(row=row, column=1, value=text)
        c.font = Font(**font)
        c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=ncol)
        ws.row_dimensions[row].height = min(max(int(font.get("size", 11) * 1.4) * text_lines(text, width) + 3, 18), 409)

    rc = result.get("request_check", {}) or {}
    conf = result.get("confidence", {}) or {}
    serial = ctx.meta.get("serial", "")
    wb = Workbook()

    # ---------------------------------------------------------------- 요약
    ws = wb.active
    ws.title = "요약"
    for col, w in (("A", 6), ("B", 26), ("C", 110)):
        ws.column_dimensions[col].width = w
    banner(ws, 1, f"{title_prefix} {serial[-6:] if serial else ''} — {result.get('summary', '')}".replace("  ", " "),
           3, 142 / 1.2, bold=True, size=13)
    banner(ws, 2, f"AI 1차 분석(검토 전) · 시각은 AMR 로그 기준 · {datetime.now():%Y-%m-%d}", 3, 142, color=NOTE_FONT)
    r = 4
    claimed = rc.get("claimed_symptom", "") + (f"  (접수 시각 {rc['claimed_time']})" if rc.get("claimed_time") else "")
    actual = (rc.get("log_time") or "-") + (f"  — 접수 대비 {rc.get('match')}" if rc.get("match") else "") + \
        (f"\n{rc['note']}" if rc.get("note") else "")
    for label, value in (("시료", ctx.session_label + (f" (수집 {ctx.meta['collected']})" if ctx.meta.get("collected") else "")),
                         ("버전 (SW:FW:AI)", " → ".join(ctx.meta.get("sw_versions") or []) or "-"),
                         ("접수 현상", claimed), ("실제 발생", actual)):
        put(ws, r, 2, label, bold=True)
        put(ws, r, 3, value)
        row_height(ws, r, (value, 110))
        r += 1
    r += 1
    section(ws, r, "[현상]", 3)
    r += 1
    header(ws, r, ["No", "항목", "내용"])
    r += 1
    for k, p in enumerate(result.get("phenomenon_detail", []), 1):
        text = NUMBER_HEAD_RE.sub("", EVIDENCE_TAIL_RE.sub("", p.get("text", "")))
        put(ws, r, 1, k)
        put(ws, r, 2, p.get("title", "") or "")
        put(ws, r, 3, text)
        row_height(ws, r, (p.get("title", "") or "", 26), (text, 110))
        r += 1
    errs = result.get("error_codes", [])
    if errs:
        r += 1
        section(ws, r, "[관련 에러]", 3)
        r += 1
        header(ws, r, ["코드", "명칭", "발생 ~ 해제 (종료 유형)"])
        r += 1
        for e in errs:
            span = f"{e.get('occurred') or '-'} ~ {e.get('cleared') or '-'}  ({e.get('end_type', '')}, {e.get('count', '')}회)"
            put(ws, r, 1, e.get("code", ""), bold=True)
            put(ws, r, 2, e.get("name", ""))
            put(ws, r, 3, span)
            row_height(ws, r, (e.get("code", ""), 6), (e.get("name", ""), 26), (span, 110))
            r += 1
    hyps = result.get("hypothesis_check", [])
    if hyps:
        r += 1
        section(ws, r, "[검토 의견] 요청자 가설", 3)
        r += 1
        for k, h in enumerate(hyps, 1):
            put(ws, r, 1, k)
            put(ws, r, 2, h.get("hypothesis", ""))
            v = f"[{h.get('verdict', '')}] {h.get('reason', '')}"
            put(ws, r, 3, v)
            row_height(ws, r, (h.get("hypothesis", ""), 26), (v, 110))
            r += 1
    more = result.get("need_more_info", [])
    if more:
        r += 1
        section(ws, r, "[추가 확인 필요]", 3)
        r += 1
        for k, m in enumerate(more, 1):
            put(ws, r, 1, k)
            put(ws, r, 2, m.get("item", ""))
            v = (m.get("how", "") + (f"  (이유: {m['reason']})" if m.get("reason") else "")).strip()
            put(ws, r, 3, v)
            row_height(ws, r, (m.get("item", ""), 26), (v, 110))
            r += 1
    for title in ("[원인]", "[대책]"):
        r += 1
        section(ws, r, title, 3)
        r += 1
        put(ws, r, 1, None)
        put(ws, r, 2, None)
        put(ws, r, 3, "(사람 작성)", italic=True, color="808080")
        r += 1
    r += 1
    banner(ws, r, f"※ 신뢰도 {conf.get('level', '-')}: {conf.get('reason', '')}", 3, 142, color=NOTE_FONT)

    # ---------------------------------------------------------------- 타임라인
    ws = wb.create_sheet("타임라인")
    for col, w in (("A", 5), ("B", 22), ("C", 30), ("D", 90), ("E", 44)):
        ws.column_dimensions[col].width = w
    banner(ws, 1, "시간순 근거 로그 (실제 원문) — AMR 시계 기준", 5, 191 / 1.1, bold=True, size=12)
    banner(ws, 2, ctx.offset.describe(), 5, 191, color=NOTE_FONT)
    header(ws, 4, ["No", "시각", "출처 (파일:줄)", "실제 로그 (원문)", "설명"])
    rmap, emap = ctx.rmap, ctx.emap
    rr = 5
    for k, row in enumerate(result.get("timeline", []), 1):
        key = row.get("importance") == "핵심"
        # 출처·원문은 앱이 보낸 근거 줄(R)과 이벤트(EV)의 원문 줄에서 직접 — 반복으로 접힌 줄·요약 이벤트는 횟수·기간 표시
        items, seen = [], set()
        for x in row.get("evidence", []):
            if x in rmap:
                rec, note = rmap[x].rec, rmap[x].note
            elif x in emap and emap[x].rec is not None:
                rec, note = emap[x].rec, _ev_note(emap[x])
            else:
                continue
            if id(rec) not in seen:
                seen.add(id(rec))
                items.append((rec, note))
        missing = not items
        if items:
            dev = _device(items[0][0].src)
            where = "\n".join(f"{_device(rec.src)} {rec.file}:L{rec.line}" for rec, _n in items)
            raw = "\n".join(rec.text + (f"  {note}" if note else "") for rec, note in items)
        else:           # 원문 줄이 없는 근거 (요약만 있는 이벤트) — 원문처럼 보이지 않게 표시
            dev = _device(row.get("source", ""))
            where = f"{dev} (원문 없음)"
            raw = f"(원문 없음 · 요약) {row.get('event', '')}".strip()
        d = _dt(row.get("time", ""))
        interp = ("★ " if key else "") + row.get("interpretation", "")
        put(ws, rr, 1, k, bg=KEY_FILL if key else None)
        put(ws, rr, 2, d if d is not None else row.get("time", ""), fmt=TIME_FMT if d is not None else None,
            bold=key, color=KEY_FONT if key else None, bg=KEY_FILL if key else None)
        put(ws, rr, 3, where, bg=SOC_FILL if dev == "SoC" else AMR_FILL)
        put(ws, rr, 4, raw, italic=missing, color="808080" if missing else None)
        put(ws, rr, 5, interp, bold=key, color=KEY_FONT if key else None, bg=KEY_FILL if key else None)
        row_height(ws, rr, (where, 30), (raw, 90), (interp, 44))
        rr += 1
    ws.freeze_panes = "C5"
    ws.auto_filter.ref = f"A4:E{max(rr - 1, 4)}"

    # ---------------------------------------------------------------- 로그 파일
    ws = wb.create_sheet("로그 파일")
    for col, w in (("A", 30), ("B", 44), ("C", 60)):
        ws.column_dimensions[col].width = w
    banner(ws, 1, "분석 대상 로그 파일 및 기간", 3, 134 / 1.1, bold=True, size=12)
    header(ws, 3, ["대상", "기간 (AMR 시각 기준)", "비고"])
    groups = {}
    for st in ctx.stats:
        g = groups.setdefault(st.src, {"files": [], "first": None, "last": None, "lines": 0, "notes": []})
        g["files"].append(st.name)
        g["lines"] += st.lines
        if st.first is not None:
            g["first"] = st.first if g["first"] is None else min(g["first"], st.first)
        if st.last is not None:
            g["last"] = st.last if g["last"] is None else max(g["last"], st.last)
        if st.note:
            g["notes"].append(f"{st.name}: {st.note}")
    rr = 4
    for src, g in groups.items():
        files = g["files"]
        name = files[0] if len(files) == 1 else f"{files[0]} ~ {files[-1]}"
        span = f"{fmt_time(g['first'], False)} ~ {fmt_time(g['last'], False)}" if g["first"] is not None else "-"
        notes = [f"{len(files)}개 파일 · {g['lines']:,}줄"] + g["notes"][:3]
        if src.startswith("SoC"):
            notes.insert(0, "시각은 AMR 기준 환산")
        put(ws, rr, 1, f"{src}\n{name}")
        put(ws, rr, 2, span)
        put(ws, rr, 3, "\n".join(notes))
        row_height(ws, rr, (f"{src}\n{name}", 30), (span, 44), ("\n".join(notes), 60))
        rr += 1
    o = ctx.options
    rr += 1
    for label, value in (("분석 구간", f"{fmt_time(o.t0, False)} ~ {fmt_time(o.t1, False)} (기준 {fmt_time(o.anchor, False)})"),
                         ("시각 기준", ctx.offset.describe()),
                         ("주의", "\n".join(ctx.notes) or "-")):
        put(ws, rr, 1, label, bold=True, bg=SECTION_FILL)
        put(ws, rr, 2, value)
        ws.merge_cells(start_row=rr, start_column=2, end_row=rr, end_column=3)
        row_height(ws, rr, (value, 104))
        rr += 1
    wb.save(path)
    return path

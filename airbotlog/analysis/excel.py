"""현상 분석표(xlsx) 저장.

시트: 요약 · 타임라인 · 에러 이력 · 로그 커버리지·시각정렬 · 원문 발췌 · 요청 원문
- 시각은 날짜 포함 엑셀 날짜/시각 값(yyyy-mm-dd hh:mm:ss.000), 해석할 수 없는 표기는 글자 그대로
- 시트 간 수식 없음 (값만 기록)
- 원인·대책 칸은 비워 둠 (AI 는 현상까지, 원인·대책은 사람이 작성)
"""
import re
from datetime import datetime

from ..model import fmt_time

HEADER_FILL, HEADER_FONT = "1F3864", "FFFFFF"
LABEL_FILL = "D9E1F2"
SRC_FILL = {"AMR airbot_log": "DDEBF7", "AMR otalog": "FFF2CC", "AMR journal": "EDEDED", "AMR dmesg": "EDEDED",
            "SoC logcat": "E2EFDA", "요청": "F2F2F2"}
KEY_FILL, KEY_FONT = "FFC7CE", "9C0006"
MAJOR_FILL = "FCE4D6"
TIME_FMT = "yyyy-mm-dd hh:mm:ss.000"
CELL_MAX = 32000


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


def _src_fill(source: str):
    for k, v in SRC_FILL.items():
        if source and source.startswith(k.split()[0]) and k.split()[-1] in source:
            return v
    if source and source.startswith("SoC"):
        return SRC_FILL["SoC logcat"]
    if source and source.startswith("AMR"):
        return SRC_FILL["AMR airbot_log"]
    return None


def default_name(pattern: str, serial: str, issue: str, when: datetime = None) -> str:
    when = when or datetime.now()
    s6 = serial[-6:] if serial else "시료"
    issue = re.sub(r'[\\/:*?"<>|\s]+', "", issue or "")[:20] or "이슈"
    return pattern.format(serial6=s6, issue=issue, date=when.strftime("%y%m%d"))


def save(path, ctx, result: dict, meta: dict, request_full: str = "", title_prefix: str = "Airbot"):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    wrap = Alignment(wrap_text=True, vertical="top")

    def fill(c):
        return PatternFill("solid", fgColor=c)

    def header(ws, row, cols, widths=None):
        for i, name in enumerate(cols, 1):
            c = ws.cell(row=row, column=i, value=name)
            c.fill, c.font, c.border = fill(HEADER_FILL), Font(bold=True, color=HEADER_FONT), border
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for i, w in enumerate(widths or [], 1):
            ws.column_dimensions[get_column_letter(i)].width = w

    def put(ws, row, col, value, **kw):
        if isinstance(value, str) and len(value) > CELL_MAX:
            value = value[:CELL_MAX] + f"…({len(value) - CELL_MAX}자 생략)"
        c = ws.cell(row=row, column=col, value=value)
        c.border, c.alignment = border, wrap
        if kw.get("fill"):
            c.fill = fill(kw["fill"])
        if kw.get("font"):
            c.font = kw["font"]
        if kw.get("fmt"):
            c.number_format = kw["fmt"]
        return c

    def time_cell(ws, row, col, text, **kw):
        d = _dt(text)
        if d is not None:
            return put(ws, row, col, d, fmt=TIME_FMT, **kw)
        return put(ws, row, col, text or "", **kw)

    wb = Workbook()
    rc = result.get("request_check", {})
    conf = result.get("confidence", {})
    o = ctx.options
    created = datetime.now()

    # ---------------------------------------------------------------- 요약
    ws = wb.active
    ws.title = "요약"
    ws["A1"] = f"{title_prefix} 로그 현상 분석 — AI 1차 분석 (검토 전)"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = (f"시료 {ctx.session_label} · 수집 {ctx.meta.get('collected') or '-'} · 작성 {created:%Y-%m-%d %H:%M} · "
                f"시각은 AMR 로그 기준")
    ws["A2"].font = Font(color="595959")
    ws.column_dimensions["A"].width = 20
    ws.column_dimensions["B"].width = 110
    errs = "\n".join(f"{e.get('code')} {e.get('name')} · 발생 {e.get('occurred') or '-'} · 해제 {e.get('cleared') or '-'} · "
                     f"{e.get('end_type')} · {e.get('count')}회" for e in result.get("error_codes", []))
    hyps = "\n".join(f"[{h.get('verdict')}] {h.get('hypothesis')} — {h.get('reason')}" for h in result.get("hypothesis_check", []))
    more = "\n".join(f"· {m.get('item')} — {m.get('reason')} (확인: {m.get('how')})" for m in result.get("need_more_info", []))
    detail = "\n".join(p.get("text", "") + (f"  [{', '.join(p.get('evidence', []))}]" if p.get("evidence") else "")
                       for p in result.get("phenomenon_detail", []))
    rows = [
        ("시료", f"{ctx.session_label}" + (f"  (시리얼 {ctx.meta.get('serial')})" if ctx.meta.get("serial") else "")),
        ("S/W 버전", ", ".join(ctx.meta.get("sw_versions") or []) or "-"),
        ("분석 구간", f"{fmt_time(o.t0, False)} ~ {fmt_time(o.t1, False)} (기준 {fmt_time(o.anchor, False)})"),
        ("분석 로그", ", ".join(sorted({st.src for st in ctx.stats if st.lines})) or "-"),
        ("접수 내용", f"{rc.get('claimed_symptom', '')}" + (f"\n접수 시각: {rc.get('claimed_time')}" if rc.get("claimed_time") else "")),
        ("실제 발생 시각", f"{rc.get('log_time') or '-'}  ·  접수 대비: {rc.get('match', '')}" + (f"\n{rc.get('note')}" if rc.get("note") else "")),
        ("현상 (한 줄)", result.get("summary", "")),
        ("현상 상세", detail),
        ("관련 에러 코드", errs or "-"),
        ("요청자 가설 검증", hyps or "-"),
        ("추가 확인 필요", more or "-"),
        ("시각 기준", ctx.offset.describe()),
        ("신뢰도", f"{conf.get('level', '')} — {conf.get('reason', '')}"),
        ("원인", ""),
        ("대책", ""),
        ("작성 방식", f"AI 1차 분석(검토 전) · {', '.join(meta.get('models') or []) or 'Claude'} · "
                  f"발췌 {ctx.counts.get('selected', 0):,}줄 · 마스킹 후 전송"),
    ]
    r = 4
    for label, value in rows:
        put(ws, r, 1, label, fill=LABEL_FILL, font=Font(bold=True))
        c = put(ws, r, 2, value)
        if label == "현상 (한 줄)":
            c.font = Font(bold=True, color=KEY_FONT)
        if label in ("원인", "대책"):
            c.value = "(사람 작성)"
            c.font = Font(italic=True, color="808080")
        r += 1
    for row in range(4, r):
        lines = str(ws.cell(row=row, column=2).value or "").count("\n") + 1
        ws.row_dimensions[row].height = min(max(15 * lines, 18), 400)

    # ---------------------------------------------------------------- 타임라인
    ws = wb.create_sheet("타임라인")
    cols = ["No", "시각(AMR 기준)", "시각 기준", "출처", "파일:줄", "이벤트/로그 요지", "해석", "중요도", "근거 ID", "로그 원문"]
    header(ws, 1, cols, [5, 23, 10, 15, 26, 50, 50, 7, 14, 80])
    rmap = ctx.rmap
    for i, row in enumerate(result.get("timeline", []), 1):
        rr = i + 1
        imp = row.get("importance", "")
        f = _src_fill(row.get("source", ""))
        font = Font(bold=True, color=KEY_FONT) if imp == "핵심" else None
        put(ws, rr, 1, i, fill=f)
        time_cell(ws, rr, 2, row.get("time", ""), fill=KEY_FILL if imp == "핵심" else f, font=font)
        put(ws, rr, 3, row.get("time_basis", ""), fill=f,
            font=Font(italic=True, color="808080") if row.get("time_basis") in ("추정", "stale", "범위") else None)
        put(ws, rr, 4, row.get("source", ""), fill=f)
        put(ws, rr, 5, row.get("file_line", ""), fill=f)
        put(ws, rr, 6, ("★ " if imp == "핵심" else "") + row.get("event", ""), fill=KEY_FILL if imp == "핵심" else (MAJOR_FILL if imp == "주요" else f), font=font)
        put(ws, rr, 7, row.get("interpretation", ""), fill=f)
        put(ws, rr, 8, imp, fill=f)
        ev = row.get("evidence", [])
        put(ws, rr, 9, ", ".join(ev), fill=f)
        raw = "\n".join(rmap[x].rec.text for x in ev if x in rmap)
        put(ws, rr, 10, raw, fill=f)
    last = len(result.get("timeline", [])) + 1
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:J{max(last, 1)}"
    lg = last + 2
    ws.cell(row=lg, column=2, value="범례").font = Font(bold=True)
    for k, (name, color) in enumerate([("AMR (airbot_log)", SRC_FILL["AMR airbot_log"]), ("AMR otalog", SRC_FILL["AMR otalog"]),
                                       ("AMR journal/dmesg", SRC_FILL["AMR journal"]), ("SoC logcat", SRC_FILL["SoC logcat"]),
                                       ("★ 핵심 이벤트", KEY_FILL), ("주요 이벤트", MAJOR_FILL)], 1):
        c = ws.cell(row=lg + k, column=2, value=name)
        c.fill = fill(color)
        c.border = border

    # ---------------------------------------------------------------- 에러 이력
    ws = wb.create_sheet("에러 이력")
    header(ws, 1, ["No", "코드", "명칭", "발생", "해제", "종료 유형", "횟수", "근거 ID"], [5, 10, 32, 23, 23, 20, 7, 20])
    for i, e in enumerate(result.get("error_codes", []), 1):
        put(ws, i + 1, 1, i)
        put(ws, i + 1, 2, e.get("code", ""), font=Font(bold=True))
        put(ws, i + 1, 3, e.get("name", ""))
        time_cell(ws, i + 1, 4, e.get("occurred", ""))
        time_cell(ws, i + 1, 5, e.get("cleared", ""))
        put(ws, i + 1, 6, e.get("end_type", ""))
        put(ws, i + 1, 7, e.get("count", ""))
        put(ws, i + 1, 8, ", ".join(e.get("evidence", [])))
    r = len(result.get("error_codes", [])) + 3
    ws.cell(row=r, column=1, value="앱이 로그 전체에서 찾은 에러 발생 (구간 밖 포함)").font = Font(bold=True)
    header(ws, r + 1, ["No", "코드", "소스", "처음", "마지막", "", "횟수", "id"])
    k = 0
    for e in ctx.summary_events:
        if e.type != "error_occur":
            continue
        k += 1
        rr = r + 1 + k
        put(ws, rr, 1, k)
        put(ws, rr, 2, e.code)
        put(ws, rr, 3, e.src, fill=_src_fill(e.src))
        time_cell(ws, rr, 4, fmt_time(e.t))
        time_cell(ws, rr, 5, fmt_time(e.t_end or e.t))
        put(ws, rr, 6, "")
        put(ws, rr, 7, e.count)
        put(ws, rr, 8, e.id)

    # ---------------------------------------------------------------- 커버리지·시각정렬
    ws = wb.create_sheet("로그 커버리지·시각정렬")
    header(ws, 1, ["소스", "파일", "첫 시각", "끝 시각", "줄 수", "비고"], [16, 46, 23, 23, 12, 60])
    for i, st in enumerate(ctx.stats, 2):
        f = _src_fill(st.src)
        put(ws, i, 1, st.src, fill=f)
        put(ws, i, 2, st.name, fill=f)
        time_cell(ws, i, 3, fmt_time(st.first) if st.first is not None else "-", fill=f)
        time_cell(ws, i, 4, fmt_time(st.last) if st.last is not None else "-", fill=f)
        put(ws, i, 5, st.lines, fill=f)
        put(ws, i, 6, st.note, fill=f)
    r = len(ctx.stats) + 3
    info = [("분석 구간", f"{fmt_time(o.t0, False)} ~ {fmt_time(o.t1, False)} (기준 {fmt_time(o.anchor, False)})"),
            ("SoC↔AMR 시계", ctx.offset.describe()),
            ("시계 기준점", "; ".join(f"{fmt_time(a, False)}: {d:+.1f}초" for a, d in ctx.offset.pairs[:20]) or "-"),
            ("AMR 앱 시작", ", ".join(ctx.meta.get("boots") or []) or "-"),
            ("AMR 시각 점프", "; ".join(ctx.meta.get("jumps") or []) or "-"),
            ("발췌 줄 수", f"원래 {ctx.counts.get('window', 0):,} → 접기 후 {ctx.counts.get('folded', 0):,} → "
                       f"발췌 {ctx.counts.get('selected', 0):,} (생략 {ctx.counts.get('dropped', 0):,})"),
            ("주의", "\n".join(ctx.notes) or "-")]
    for k, (label, value) in enumerate(info):
        put(ws, r + k, 1, label, fill=LABEL_FILL, font=Font(bold=True))
        put(ws, r + k, 2, value)
        ws.merge_cells(start_row=r + k, start_column=2, end_row=r + k, end_column=6)

    # ---------------------------------------------------------------- 원문 발췌
    ws = wb.create_sheet("원문 발췌")
    header(ws, 1, ["id", "시각(AMR 기준)", "시각 기준", "소스", "파일:줄", "원문", "반복"], [9, 23, 24, 15, 30, 120, 34])
    r = 2
    for ln in ctx.lines:
        if ln.rec is None:
            put(ws, r, 6, ln.note, font=Font(italic=True, color="808080"))
            r += 1
            continue
        rec = ln.rec
        f = _src_fill(rec.src)
        put(ws, r, 1, ln.rid, fill=f)
        time_cell(ws, r, 2, fmt_time(rec.t), fill=f)
        put(ws, r, 3, rec.basis + (f" ({rec.raw_t})" if rec.raw_t else ""), fill=f)
        put(ws, r, 4, rec.src, fill=f)
        put(ws, r, 5, f"{rec.file}:L{rec.line}", fill=f)
        put(ws, r, 6, rec.text, fill=f)
        put(ws, r, 7, ln.note, fill=f)
        r += 1
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = f"A1:G{max(r - 1, 1)}"

    # ---------------------------------------------------------------- 요청 원문
    ws = wb.create_sheet("요청 원문")
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 120
    put(ws, 1, 1, "요청", fill=LABEL_FILL, font=Font(bold=True))
    put(ws, 1, 2, request_full or ctx.request_text)
    import json as _json
    put(ws, 2, 1, "자동 추출", fill=LABEL_FILL, font=Font(bold=True))
    put(ws, 2, 2, _json.dumps(ctx.request_extract, ensure_ascii=False, indent=1))
    put(ws, 3, 1, "AI 응답(JSON)", fill=LABEL_FILL, font=Font(bold=True))
    put(ws, 3, 2, _json.dumps(result, ensure_ascii=False, indent=1))
    wb.save(path)
    return path

#!/usr/bin/env python3
"""문서용 화면 캡처: 실제 앱을 화면 없이(offscreen) 실행해 docs/images 에 저장.

    python scripts/capture_docs.py <로그 압축 파일> [--out docs/images]
        [--nas]               설정된 NAS 에 실제 접속해 NAS 창 캡처
        [--nas-search 단어]   NAS 창 검색어
        [--demo-nas]          데모 압축으로 가짜 NAS 폴더를 만들어 NAS 창 캡처
        [--origin 문구]       상태바 출처 표시 (기본: 파일 경로)
        [--theme dark]        다크 테마로 캡처
        [--analysis 예시.json] [분석] 창·보낼 내용·결과 캡처 (docs/examples/analysis_demo.json: 데모 데이터로 실제 분석한 결과)
        [--excel-out 파일]    --analysis 예시로 엑셀 분석표도 저장
        [--gif 파일]          빠른 배속 재생 GIF (첫 이동 명령 ~ 마지막 도킹, Pillow 필요: pip install pillow)
"""
import argparse
import bisect
import io
import json
import math
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QBuffer, QIODevice, QPoint, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QPainter, QPen  # noqa: E402
from PySide6.QtWidgets import QApplication, QToolBar  # noqa: E402

from airbotlog.loader import load_session  # noqa: E402
from airbotlog.nas import LocalNasSource  # noqa: E402
from airbotlog.settings import Settings  # noqa: E402
from airbotlog.ui.main_window import MainWindow  # noqa: E402
from airbotlog.ui.nas_dialog import NasDialog  # noqa: E402
from airbotlog.ui.style import apply_style  # noqa: E402

SIZE = (1600, 960)


def pump(app, sec=0.3):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def annotate(win, pix, items):
    """화면 구성 번호(①…) 표시."""
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    f = QFont(win.font())
    f.setPointSize(13)
    f.setBold(True)
    p.setFont(f)
    for n, (widget, dx, dy) in enumerate(items, 1):
        pos = widget.mapTo(win, QPoint(0, 0)) + QPoint(dx, dy)
        p.setPen(QPen(QColor("#ffffff"), 2))
        p.setBrush(QColor("#e53935"))
        p.drawEllipse(pos, 15, 15)
        p.setPen(QColor("#ffffff"))
        p.drawText(pos.x() - 15, pos.y() - 15, 30, 30, Qt.AlignCenter, str(n))
    p.end()
    return pix


def best_moment(S, window=1800.0):
    """대표 시점: 최근 window 초 동안 궤적이 길고 마커가 많은 시점."""
    if not S.pose_vt:
        return 0.0
    cum = [0.0]
    for k in range(1, len(S.pose_x)):
        d = math.hypot(S.pose_x[k] - S.pose_x[k - 1], S.pose_y[k] - S.pose_y[k - 1])
        ok = d <= 2.0 and S.pose_rt[k] - S.pose_rt[k - 1] <= 60
        cum.append(cum[-1] + (d if ok else 0.0))
    mrts = sorted(rt for lid, rts in S.marker_rts.items() if lid != "step" for rt in rts)
    best, best_vt = -1.0, S.pose_vt[0]
    for i in range(len(S.pose_vt)):
        rt = S.pose_rt[i]
        j = bisect.bisect_left(S.pose_rt, rt - window)
        n = bisect.bisect_right(mrts, rt) - bisect.bisect_left(mrts, rt - window)
        score = (cum[i] - cum[j]) + 2.0 * min(n, 30)
        if score > best:
            best, best_vt = score, S.pose_vt[i]
    return best_vt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("archive")
    ap.add_argument("--out", default=str(ROOT / "docs" / "images"))
    ap.add_argument("--nas", action="store_true")
    ap.add_argument("--nas-search", default="")
    ap.add_argument("--demo-nas", action="store_true")
    ap.add_argument("--history", type=int, default=3, help="누적 표시 콤보 인덱스 (3=30분, 6=전체)")
    ap.add_argument("--origin", default="", help="상태바에 표시할 출처 (예: NAS: 폴더/파일.zip)")
    ap.add_argument("--theme", default="light", choices=["light", "dark"])
    ap.add_argument("--analysis", default="", help="분석 예시 JSON (결과 캡처용, Claude 호출 없음)")
    ap.add_argument("--excel-out", default="", help="--analysis 예시로 만든 엑셀 분석표 저장 경로")
    ap.add_argument("--gif", default="", help="빠른 배속 재생 GIF 저장 경로")
    ap.add_argument("--gif-frames", type=int, default=90)
    ap.add_argument("--gif-ms", type=int, default=110, help="프레임 간격 (ms)")
    ap.add_argument("--gif-width", type=int, default=960)
    ap.add_argument("--gif-zoom", type=float, default=1.5, help="맵 전체 보기 대비 확대 배율")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    app = QApplication([])
    apply_style(app, args.theme)
    cfg_dir = tempfile.TemporaryDirectory(prefix="airbotcap_")     # 개인 설정·캐시 영향 없음 (끝나면 자동 삭제)
    os.environ["XDG_CACHE_HOME"] = cfg_dir.name                # NAS 목록 캐시도 임시 폴더에
    settings = Settings(user_path=Path(cfg_dir.name) / "settings.yaml")
    win = MainWindow(settings)
    win.resize(*SIZE)
    win.show()
    pump(app)

    S = load_session([args.archive], win.ws, win.settings, win.rules)
    win._origin = args.origin
    win.set_session(S)
    win.lbl_ws.setText("임시 폴더: <OS 임시 폴더>/airbotlog_… (종료 시 자동 삭제)")  # 캡처마다 바뀌지 않게
    win.layer_panel.cmb_history.setCurrentIndex(args.history)
    win.set_vt(best_moment(S, window=1800.0 if args.history == 3 else 1e9), "seek")
    win.map_view.fit()
    pump(app, 0.5)
    shot = win.grab()
    shot.save(str(out / "main.png"))
    tb = win.findChild(QToolBar)
    info = next((a for a in tb.actions() if a.text() == "정보"), tb.actions()[-1])   # 오른쪽 끝 테마 버튼 말고 '정보' 옆
    last = tb.actionGeometry(info)
    annotate(win, shot, [
        (tb, last.right() + 28, tb.height() // 2),
        (win.layer_panel, win.layer_panel.width() - 24, 18),
        (win.map_box, win.map_box.width() - 34, win.map_box.height() - 34),
        (win.playbar, win.playbar.width() // 2 + 40, win.playbar.height() - 18),
        (win.tabs, win.tabs.width() - 24, 16),
        (win.log_view, win.log_view.width() // 2 - 60, 12),
    ]).save(str(out / "main_annotated.png"))

    win.tabs.setCurrentIndex(1)
    pump(app)
    win.tabs.grab().save(str(out / "errors.png"))
    win.tabs.setCurrentIndex(2)
    pump(app)
    win.tabs.grab().save(str(out / "summary.png"))
    win.tabs.setCurrentIndex(0)

    if args.nas or args.demo_nas:
        tmp = None
        source = None
        if args.demo_nas:
            tmp = Path(tempfile.mkdtemp(prefix="demo_nas_"))
            for folder, name in (("261101_데모_낙하감지", Path(args.archive).name),
                                 ("261030_데모_주행실패", "20261030101500_DEMOBOTSN000000002.zip"),
                                 ("2026/2610/261015_데모_도킹", "20261015090000_DEMOBOTSN000000003.zip")):
                (tmp / folder).mkdir(parents=True, exist_ok=True)
                shutil.copy(args.archive, tmp / folder / name)

            class DemoNas(LocalNasSource):
                def describe(self):
                    return r"\\NAS-SERVER\share\LOG (데모)"

            source = DemoNas(str(tmp))
        dlg = NasDialog(win.settings, win, source=source)
        dlg.resize(1100, 560)
        dlg.show()
        deadline = time.time() + 120
        while dlg._worker is not None and time.time() < deadline:
            pump(app, 0.2)
        if args.nas_search:
            dlg.search.setText(args.nas_search)
        dlg.tree.expandAll()
        pump(app)
        dlg.grab().save(str(out / "nas.png"))
        dlg.close()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    if args.gif:
        capture_gif(app, win, S, Path(args.gif), args)
    if args.analysis:
        capture_analysis(app, win, S, Path(args.archive), Path(args.analysis), out,
                         Path(args.excel_out) if args.excel_out else None)

    win.close()
    cfg_dir.cleanup()
    print("saved:", ", ".join(sorted(p.name for p in out.glob("*.png"))))


def _pil(qimg):
    from PIL import Image
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    qimg.save(buf, "PNG")
    return Image.open(io.BytesIO(bytes(buf.data()))).convert("RGB")


def capture_gif(app, win, S, path: Path, args):
    """빠른 배속 재생 GIF: 첫 이동 명령 직전 ~ 마지막 도킹 완료 직후 (전체 궤적 누적, 단차 마커 표시)."""
    from PIL import Image
    moves = [e.vt for e in S.events if e.rule == "soc_cmd" and "Move to Target" in e.text]
    docks = [e.vt for e in S.events if e.rule == "docked"]
    start = max(moves[0] - 2.0, 0.0) if moves else 0.0
    end = min(docks[-1] + 4.0, S.vt_end) if docks and docks[-1] > start else S.vt_end
    lp = win.layer_panel
    lp.cmb_history.setCurrentIndex(lp.cmb_history.count() - 1)        # 전체
    lp.chk_follow.setChecked(False)                                   # 프레임마다 크게 건너뛰므로 화면 고정
    if "step" in lp.layer_checks:
        lp.layer_checks["step"].setChecked(True)
    win.tabs.setCurrentIndex(0)
    win.map_view.fit()
    if S.pose_x:                                                      # 주행 범위를 크게 (맵 전체 대신)
        xs, ys = S.pose_x, S.pose_y
        win.map_view.scale(args.gif_zoom, args.gif_zoom)
        win.map_view.center_on_world((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
    win.playbar.set_playing(True)
    frames = []
    n = max(args.gif_frames, 2)
    for k in range(n):
        win.set_vt(start + (end - start) * k / (n - 1), "seek")
        pump(app, 0.05)
        frames.append(_pil(win.grab().toImage().scaledToWidth(args.gif_width, Qt.SmoothTransformation)))
    win.playbar.set_playing(False)
    picks = [frames[i * (n - 1) // 5] for i in range(6)]                 # 공통 팔레트 (색 깜빡임 방지)
    mosaic = Image.new("RGB", (frames[0].width, frames[0].height * len(picks)))
    for i, f in enumerate(picks):
        mosaic.paste(f, (0, f.height * i))
    pal = mosaic.quantize(colors=255, method=Image.Quantize.MAXCOVERAGE)
    q = [f.quantize(palette=pal, dither=Image.Dither.NONE) for f in frames]
    durations = [args.gif_ms] * (n - 1) + [2000]                          # 마지막 장면 2초 유지
    path.parent.mkdir(parents=True, exist_ok=True)
    q[0].save(path, save_all=True, append_images=q[1:], duration=durations, loop=0, optimize=True)
    print(f"gif: {path} ({path.stat().st_size / 1e6:.1f} MB, {n} frames, vt {start:.0f}~{end:.0f}s)")


def capture_analysis(app, win, S, archive: Path, example: Path, out: Path, excel_out: Path = None):
    """[분석] 창 → 보낼 내용 확인 → 결과 (예시 JSON 의 실제 분석 결과를 그대로 표시, Claude 호출 없음)."""
    from PySide6.QtCore import QDate, QDateTime, QTime

    from airbotlog.analysis import claude as ai
    from airbotlog.analysis import context as actx
    from airbotlog.analysis import request as areq
    from airbotlog.analysis.masking import Masker
    from airbotlog.ui.analysis_dialog import AnalysisDialog, PreviewDialog, ResultDialog
    ex = json.loads(example.read_text(encoding="utf-8"))
    dlg = AnalysisDialog(win)
    req_path = archive.parent / ex.get("request_file", "demo_request.eml")
    if req_path.exists():
        dlg.request = areq.read_mail(req_path)
        dlg.ed_request.setPlainText(areq.strip_signature(dlg.request.text))
    dlg.update_extract()
    d, t = ex["anchor"].split()
    y, mo, dd = (int(v) for v in d.split("-"))
    h, mi, se = (int(v) for v in t.split(":"))
    dlg.dt_anchor.setDateTime(QDateTime(QDate(y, mo, dd), QTime(h, mi, se)))
    dlg.sp_before.setValue(ex["before_min"])
    dlg.sp_after.setValue(ex["after_min"])
    dlg.resize(820, 760)
    dlg.show()
    pump(app, 0.5)
    dlg.grab().save(str(out / "analysis.png"))
    text = areq.strip_signature(dlg.ed_request.toPlainText())
    ctx = actx.build_context(S, text, areq.extract(text, dlg.ref, S.source.serial), dlg.options())
    m = Masker()
    m.add_serial(S.source.serial)
    m.add_memo(S.source.memo)
    m.add_names(dlg.request.names)
    m.add_emails(dlg.request.emails)
    pv = PreviewDialog(dlg, m.mask(ai.load_system_prompt()), m.mask(ctx.user_message()), m, ctx)
    pv.resize(1000, 700)
    pv.show()
    pump(app, 0.4)
    pv.grab().save(str(out / "analysis_preview.png"))
    pv.close()
    if excel_out:
        from airbotlog.analysis import excel
        excel_out.parent.mkdir(parents=True, exist_ok=True)
        excel.save(excel_out, ctx, ex["result"], ex["meta"], title_prefix="Airbot")
        print("excel:", excel_out)
    rd = ResultDialog(win, ctx, ex["result"], ex["meta"], text)
    rd.resize(1300, 820)
    rd.show()
    tabs = rd.findChild(type(win.tabs))
    tabs.setCurrentIndex(1)
    pump(app, 0.5)
    rd.grab().save(str(out / "analysis_result.png"))
    tabs.setCurrentIndex(0)
    pump(app, 0.3)
    rd.grab().save(str(out / "analysis_result_summary.png"))
    rd.close()
    dlg.close()


if __name__ == "__main__":
    main()

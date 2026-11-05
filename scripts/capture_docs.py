#!/usr/bin/env python3
"""문서용 화면 캡처: 실제 앱을 화면 없이(offscreen) 실행해 docs/images 에 저장.

    python scripts/capture_docs.py <로그 압축 파일> [--out docs/images]
        [--nas]               설정된 NAS 에 실제 접속해 NAS 창 캡처
        [--nas-search 단어]   NAS 창 검색어
        [--demo-nas]          데모 압축으로 가짜 NAS 폴더를 만들어 NAS 창 캡처
        [--origin 문구]       상태바 출처 표시 (기본: 파일 경로)
"""
import argparse
import bisect
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

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QPainter, QPen  # noqa: E402
from PySide6.QtWidgets import QApplication, QToolBar  # noqa: E402

from airbotlog.loader import load_session  # noqa: E402
from airbotlog.nas import LocalNasSource  # noqa: E402
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
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    app = QApplication([])
    apply_style(app)
    win = MainWindow()
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
    last = tb.actionGeometry(tb.actions()[-1])
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

    win.close()
    print("saved:", ", ".join(sorted(p.name for p in out.glob("*.png"))))


if __name__ == "__main__":
    main()

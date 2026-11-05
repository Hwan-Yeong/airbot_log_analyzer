"""공통 색상/글꼴/스타일."""
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

LEVEL_COLORS = {"error": "#c62828", "warn": "#ef6c00", "info": "#263238"}
ROBOT_COLOR = "#e53935"
TRAIL_COLOR = "#1e88e5"
STATION_COLOR = "#43a047"
KEEPOUT_COLOR = QColor(229, 57, 53, 70)
KEEPOUT_EDGE = QColor(229, 57, 53, 160)
WALL_COLOR = "#fb8c00"
MARK_BOOT = "#d32f2f"
MARK_JUMP = "#757575"
MARK_ERROR = "#ef6c00"
MARK_DAY = "#1565c0"

UI_FONTS = ["Malgun Gothic", "맑은 고딕", "Noto Sans KR", "Noto Sans CJK KR", "NanumGothic", "Apple SD Gothic Neo"]
MONO_FONTS = ["D2Coding", "Consolas", "Noto Sans Mono CJK KR", "DejaVu Sans Mono", "Menlo", "Monospace"]

QSS = """
QMainWindow, QDialog { background: #f5f6f8; }
QToolBar { background: #ffffff; border-bottom: 1px solid #dfe3e8; spacing: 4px; padding: 3px; }
QToolBar QToolButton { padding: 4px 8px; border-radius: 4px; }
QToolBar QToolButton:hover { background: #e8f0fe; }
QGroupBox { font-weight: bold; border: 1px solid #dfe3e8; border-radius: 6px; margin-top: 10px; background: #ffffff; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; color: #37474f; }
QTabWidget::pane { border: 1px solid #dfe3e8; background: #ffffff; }
QTabBar::tab { padding: 6px 14px; }
QTabBar::tab:selected { font-weight: bold; color: #1565c0; }
QTableView { gridline-color: #eceff1; selection-background-color: #bbdefb; selection-color: #000000; }
QHeaderView::section { background: #eef1f4; border: none; border-right: 1px solid #dfe3e8; padding: 3px 6px; }
QPushButton { padding: 4px 10px; }
QLineEdit { padding: 3px 6px; }
QLabel#hud { background: rgba(20, 24, 28, 200); color: #ffffff; border-radius: 6px; padding: 6px 10px; }
QLabel#hudErr { background: rgba(198, 40, 40, 220); color: #ffffff; border-radius: 6px; padding: 6px 10px; font-weight: bold; }
QLabel#hint { color: #607d8b; }
QLabel#title { font-size: 13pt; font-weight: bold; color: #263238; }
"""


def _pick(families, fallback):
    available = set(QFontDatabase.families())
    for fam in families:
        if fam in available:
            return fam
    return fallback


def apply_style(app: QApplication):
    app.setStyle("Fusion")
    pal = app.palette()
    pal.setColor(QPalette.Window, QColor("#f5f6f8"))
    pal.setColor(QPalette.Base, QColor("#ffffff"))
    pal.setColor(QPalette.AlternateBase, QColor("#f7f9fb"))
    pal.setColor(QPalette.Highlight, QColor("#90caf9"))
    pal.setColor(QPalette.HighlightedText, QColor("#000000"))
    app.setPalette(pal)
    font = QFont(_pick(UI_FONTS, app.font().family()))
    font.setPointSize(9)
    app.setFont(font)
    app.setStyleSheet(QSS)


def mono_font(size: int = 9) -> QFont:
    f = QFont(_pick(MONO_FONTS, "Monospace"))
    f.setStyleHint(QFont.Monospace)
    f.setPointSize(size)
    return f

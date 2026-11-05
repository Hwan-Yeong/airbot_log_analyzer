"""테마(라이트/다크), 글꼴, 공통 스타일.

화면 요소는 그릴 때 style.T() 로 현재 테마 색을 읽는다. 테마를 바꾸면 MainWindow.apply_theme() 가
각 위젯을 다시 그리게 한다.
"""
from dataclasses import dataclass

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

UI_FONTS = ["Malgun Gothic", "맑은 고딕", "Noto Sans KR", "Noto Sans CJK KR", "NanumGothic", "Apple SD Gothic Neo"]
MONO_FONTS = ["D2Coding", "Consolas", "Noto Sans Mono CJK KR", "DejaVu Sans Mono", "Menlo", "Monospace"]


@dataclass(frozen=True)
class Theme:
    name: str
    dark: bool
    # 위젯
    window: str
    base: str
    alt_base: str
    text: str
    hint: str
    border: str
    header: str
    toolbar: str
    hover: str
    selection: str
    selection_text: str
    button: str
    accent: str
    # 로그/목록 글자
    info: str
    warn: str
    error: str
    ok: str
    cont: str
    cur_line: str
    # 맵
    map_bg: str
    map_colors: tuple      # (점유, 미탐색, 빈 공간) 색. None 이면 원본 회색조 그대로
    keepout_fill: tuple    # RGBA
    keepout_edge: tuple
    wall: str
    trail: tuple           # RGBA
    pose: str
    pose_edge: str
    robot: str
    robot_edge: str
    station: str
    station_edge: str
    marker_edge: str
    target_line: str
    target_now: str
    scale_bar: str
    # 타임라인
    tl_track: str
    tl_played: str
    tl_handle: str
    tl_day: str
    tl_boot: str
    tl_jump: str
    tl_error: str
    # HUD / 안내
    empty_bg: str
    empty_text: str

    def qcolor(self, key: str) -> QColor:
        v = getattr(self, key)
        return QColor(*v) if isinstance(v, tuple) else QColor(v)


LIGHT = Theme(
    name="light", dark=False,
    window="#f5f6f8", base="#ffffff", alt_base="#f7f9fb", text="#263238", hint="#607d8b", border="#dfe3e8",
    header="#eef1f4", toolbar="#ffffff", hover="#e8f0fe", selection="#bbdefb", selection_text="#000000",
    button="#f0f2f5", accent="#1565c0",
    info="#263238", warn="#ef6c00", error="#c62828", ok="#2e7d32", cont="#78909c", cur_line="#fff59d",
    map_bg="#cfd8dc", map_colors=None,
    keepout_fill=(229, 57, 53, 70), keepout_edge=(229, 57, 53, 160), wall="#fb8c00",
    trail=(30, 136, 229, 210), pose="#1e88e5", pose_edge="#0d47a1",
    robot="#e53935", robot_edge="#212121", station="#43a047", station_edge="#1b5e20",
    marker_edge="#263238", target_line="#ff6f00", target_now="#d50000", scale_bar="#263238",
    tl_track="#e0e4e8", tl_played="#42a5f5", tl_handle="#1565c0", tl_day="#1565c0", tl_boot="#d32f2f",
    tl_jump="#757575", tl_error="#ef6c00",
    empty_bg="rgba(255,255,255,210)", empty_text="#455a64",
)

DARK = Theme(
    name="dark", dark=True,
    window="#1e2227", base="#252a30", alt_base="#2b3138", text="#e3e7eb", hint="#8fa1ad", border="#3a424b",
    header="#2f363e", toolbar="#22272d", hover="#2f3b4a", selection="#2d5d8f", selection_text="#ffffff",
    button="#2f353c", accent="#64b5f6",
    info="#dfe4e8", warn="#ffb74d", error="#ff6b6b", ok="#81c784", cont="#8a99a6", cur_line="#4d4720",
    map_bg="#15181c", map_colors=("#d5dde4", "#1c2025", "#353d46"),
    keepout_fill=(239, 83, 80, 70), keepout_edge=(255, 112, 108, 190), wall="#ffb74d",
    trail=(79, 195, 247, 230), pose="#4fc3f7", pose_edge="#e1f5fe",
    robot="#ff5252", robot_edge="#fafafa", station="#66bb6a", station_edge="#e8f5e9",
    marker_edge="#eceff1", target_line="#ffa726", target_now="#ff5252", scale_bar="#cfd8dc",
    tl_track="#3a424b", tl_played="#42a5f5", tl_handle="#90caf9", tl_day="#64b5f6", tl_boot="#ff5252",
    tl_jump="#9e9e9e", tl_error="#ffb74d",
    empty_bg="rgba(37,42,48,220)", empty_text="#cfd8dc",
)

THEMES = {"light": LIGHT, "dark": DARK}
_current = LIGHT


def T() -> Theme:
    """현재 테마."""
    return _current


def level_color(level: str) -> QColor:
    t = _current
    return QColor({"error": t.error, "warn": t.warn}.get(level, t.info))


def category_color(color: str) -> QColor:
    """분류 색 (다크 테마에서는 밝게 보정)."""
    c = QColor(color)
    return c.lighter(165) if _current.dark and c.lightness() < 140 else c


def _qss(t: Theme) -> str:
    return f"""
QMainWindow, QDialog {{ background: {t.window}; }}
QToolBar {{ background: {t.toolbar}; border-bottom: 1px solid {t.border}; spacing: 4px; padding: 3px; }}
QToolBar QToolButton {{ padding: 4px 8px; border-radius: 4px; color: {t.text}; }}
QToolBar QToolButton:hover {{ background: {t.hover}; }}
QToolBar QToolButton:checked {{ background: {t.hover}; border: 1px solid {t.border}; }}
QGroupBox {{ font-weight: bold; border: 1px solid {t.border}; border-radius: 6px; margin-top: 10px; background: {t.base}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; color: {t.hint}; }}
QTabWidget::pane {{ border: 1px solid {t.border}; background: {t.base}; }}
QTabBar::tab {{ padding: 6px 14px; }}
QTabBar::tab:selected {{ font-weight: bold; color: {t.accent}; }}
QTableView, QTreeView, QTextBrowser {{ gridline-color: {t.border}; selection-background-color: {t.selection}; selection-color: {t.selection_text}; }}
QHeaderView::section {{ background: {t.header}; color: {t.text}; border: none; border-right: 1px solid {t.border}; padding: 3px 6px; }}
QPushButton {{ padding: 4px 10px; }}
QLineEdit {{ padding: 3px 6px; }}
QLabel#hud {{ background: rgba(20, 24, 28, 200); color: #ffffff; border: 1px solid {t.border}; border-radius: 6px; padding: 6px 10px; }}
QLabel#hudErr {{ background: rgba(198, 40, 40, 220); color: #ffffff; border-radius: 6px; padding: 6px 10px; font-weight: bold; }}
QLabel#hint {{ color: {t.hint}; }}
QLabel#title {{ font-size: 13pt; font-weight: bold; color: {t.text}; }}
QLabel#empty {{ color: {t.empty_text}; font-size: 13pt; background: {t.empty_bg}; border-radius: 8px; padding: 24px; }}
"""


def _palette(t: Theme) -> QPalette:
    pal = QPalette()
    c = QColor
    for role, col in ((QPalette.Window, t.window), (QPalette.WindowText, t.text), (QPalette.Base, t.base),
                      (QPalette.AlternateBase, t.alt_base), (QPalette.Text, t.text), (QPalette.Button, t.button),
                      (QPalette.ButtonText, t.text), (QPalette.ToolTipBase, t.base), (QPalette.ToolTipText, t.text),
                      (QPalette.Highlight, t.selection), (QPalette.HighlightedText, t.selection_text),
                      (QPalette.Link, t.accent), (QPalette.PlaceholderText, t.hint), (QPalette.BrightText, "#ff5252"),
                      (QPalette.Light, t.border), (QPalette.Midlight, t.border), (QPalette.Mid, t.hint),
                      (QPalette.Dark, t.window), (QPalette.Shadow, "#000000")):
        pal.setColor(role, c(col))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, c(t.hint))
    return pal


def _pick(families, fallback):
    available = set(QFontDatabase.families())
    for fam in families:
        if fam in available:
            return fam
    return fallback


def apply_style(app: QApplication, theme: str = "light"):
    global _current
    _current = THEMES.get(theme, LIGHT)
    app.setStyle("Fusion")
    app.setPalette(_palette(_current))
    font = QFont(_pick(UI_FONTS, app.font().family()))
    font.setPointSize(9)
    app.setFont(font)
    app.setStyleSheet(_qss(_current))


def mono_font(size: int = 9) -> QFont:
    f = QFont(_pick(MONO_FONTS, "Monospace"))
    f.setStyleHint(QFont.Monospace)
    f.setPointSize(size)
    return f

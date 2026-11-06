"""앱 자체 한글 입력(두벌식): WSL 에서 Windows 한글 입력기가 전달되지 않을 때."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from airbotlog.ui import hangul  # noqa: E402


def _type(keys):
    return "".join(s for s, _ in hangul.compose([hangul.KEYMAP[k] for k in keys]))


@pytest.mark.parametrize("keys,expect", [
    ("dkssudgktpdy", "안녕하세요"), ("ekfr", "닭"), ("rkqt", "값"), ("dhksfy", "완료"), ("ghkrdls", "확인"),
    ("Rk", "까"), ("qkfwkdtjstj", "발장선서"), ("dml", "의"), ("sdl", "ㄴ이"), ("k", "ㅏ"), ("rr", "ㄱㄱ"),
    ("rkfrk", "갈가"), ("akseks", "만단"), ("rpdlxm", "게이트"), ("dkswjsgkrp", "안전하게"),
])
def test_compose(keys, expect):
    assert _type(keys) == expect


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.mark.parametrize("cls", ["QPlainTextEdit", "QLineEdit"])
def test_typing_in_widgets(app, cls):
    w = getattr(QtWidgets, cls)()
    w.show()
    mode = hangul.HangulMode()
    hangul.attach(mode, w)
    text = lambda: w.toPlainText() if cls == "QPlainTextEdit" else w.text()   # noqa: E731
    QTest.keyClicks(w, "abc ")                       # 영문 그대로
    QTest.keyClick(w, Qt.Key_Space, Qt.ShiftModifier)  # Shift+Space → 한글
    assert mode.on
    QTest.keyClicks(w, "skrgk tpstj")                # 낙하 센서 (공백에서 조합 끝)
    assert text() == "abc 낙하 센서"
    QTest.keyClick(w, Qt.Key_Backspace)              # 조합 중 지우기: 서 → ㅅ
    assert text() == "abc 낙하 센ㅅ"
    QTest.keyClicks(w, "j")
    assert text() == "abc 낙하 센서"
    QTest.keyClick(w, Qt.Key_Hangul)                 # 한/영 키 → 영문
    assert not mode.on
    QTest.keyClicks(w, " E09")
    assert text() == "abc 낙하 센서 E09"
    w.close()

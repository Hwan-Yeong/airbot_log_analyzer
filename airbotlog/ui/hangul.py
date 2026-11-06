"""앱 자체 한글 입력 (두벌식).

WSL(WSLg)에서는 Windows 한글 입력기가 Linux 앱에 전달되지 않아 영문만 입력된다.
입력 칸에 이 필터를 붙이면 한/영 키·Shift+Space·[한/A] 버튼으로 전환해 영문 자판 입력을 한글로 조합한다.
(입력기가 한글을 직접 보내는 환경에서는 그 글자를 그대로 받는다)
"""
from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QLineEdit, QPlainTextEdit

KEYMAP = {
    "q": "ㅂ", "w": "ㅈ", "e": "ㄷ", "r": "ㄱ", "t": "ㅅ", "y": "ㅛ", "u": "ㅕ", "i": "ㅑ", "o": "ㅐ", "p": "ㅔ",
    "a": "ㅁ", "s": "ㄴ", "d": "ㅇ", "f": "ㄹ", "g": "ㅎ", "h": "ㅗ", "j": "ㅓ", "k": "ㅏ", "l": "ㅣ",
    "z": "ㅋ", "x": "ㅌ", "c": "ㅊ", "v": "ㅍ", "b": "ㅠ", "n": "ㅜ", "m": "ㅡ",
    "Q": "ㅃ", "W": "ㅉ", "E": "ㄸ", "R": "ㄲ", "T": "ㅆ", "O": "ㅒ", "P": "ㅖ",
}
for _k in "yuiahsdfgjklzxcvbnm":                     # 나머지 대문자는 소문자와 같음
    KEYMAP[_k.upper()] = KEYMAP[_k]

CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
JUNG = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
JONG = ["", "ㄱ", "ㄲ", "ㄳ", "ㄴ", "ㄵ", "ㄶ", "ㄷ", "ㄹ", "ㄺ", "ㄻ", "ㄼ", "ㄽ", "ㄾ", "ㄿ", "ㅀ", "ㅁ", "ㅂ",
        "ㅄ", "ㅅ", "ㅆ", "ㅇ", "ㅈ", "ㅊ", "ㅋ", "ㅌ", "ㅍ", "ㅎ"]
VOWEL_PAIRS = {("ㅗ", "ㅏ"): "ㅘ", ("ㅗ", "ㅐ"): "ㅙ", ("ㅗ", "ㅣ"): "ㅚ", ("ㅜ", "ㅓ"): "ㅝ", ("ㅜ", "ㅔ"): "ㅞ",
               ("ㅜ", "ㅣ"): "ㅟ", ("ㅡ", "ㅣ"): "ㅢ"}
JONG_PAIRS = {("ㄱ", "ㅅ"): "ㄳ", ("ㄴ", "ㅈ"): "ㄵ", ("ㄴ", "ㅎ"): "ㄶ", ("ㄹ", "ㄱ"): "ㄺ", ("ㄹ", "ㅁ"): "ㄻ",
              ("ㄹ", "ㅂ"): "ㄼ", ("ㄹ", "ㅅ"): "ㄽ", ("ㄹ", "ㅌ"): "ㄾ", ("ㄹ", "ㅍ"): "ㄿ", ("ㄹ", "ㅎ"): "ㅀ",
              ("ㅂ", "ㅅ"): "ㅄ"}
JONG_SPLIT = {v: k for k, v in JONG_PAIRS.items()}
HANGUL_KEY = Qt.Key_Hangul                         # 한/영 키 (Linux 에 전달되는 경우)


def _render(s) -> str:
    cho, jung, jong = s
    if cho and jung:
        return chr(0xAC00 + (CHO.index(cho) * 21 + JUNG.index(jung)) * 28 + JONG.index(jong or ""))
    return cho or jung or ""


def compose(jamos):
    """자모 목록 → 음절 목록 [(글자, 그 음절을 만든 자모들)]. 마지막 음절만 이후 입력으로 바뀔 수 있다."""
    out = []
    cur, cur_j = [None, None, None], []

    def flush():
        nonlocal cur, cur_j
        if any(cur):
            out.append((_render(cur), cur_j))
        cur, cur_j = [None, None, None], []

    for j in jamos:
        if j in CHO:                                   # 자음
            if cur[0] and cur[1] and not cur[2] and j in JONG:
                cur[2] = j
                cur_j = cur_j + [j]
            elif cur[2] and (cur[2], j) in JONG_PAIRS:
                cur[2] = JONG_PAIRS[(cur[2], j)]
                cur_j = cur_j + [j]
            else:
                flush()
                cur, cur_j = [j, None, None], [j]
        else:                                          # 모음
            if cur[0] and not cur[1]:
                cur[1] = j
                cur_j = cur_j + [j]
            elif cur[1] and not cur[2] and (cur[1], j) in VOWEL_PAIRS:
                cur[1] = VOWEL_PAIRS[(cur[1], j)]
                cur_j = cur_j + [j]
            elif cur[2]:                               # 받침이 다음 글자의 초성으로 넘어감
                if cur[2] in JONG_SPLIT:
                    first, second = JONG_SPLIT[cur[2]]
                    cur[2] = first
                else:
                    second, cur[2] = cur[2], None
                cur_j = cur_j[:-1]
                flush()
                cur, cur_j = [second, j, None], [second, j]
            else:
                flush()
                cur, cur_j = [None, j, None], [j]
    flush()
    return out


class HangulMode(QObject):
    """한/영 상태 (여러 입력 칸이 함께 씀)."""
    changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.on = False

    def toggle(self):
        self.set(not self.on)

    def set(self, on: bool):
        if on != self.on:
            self.on = on
            self.changed.emit(on)


class HangulInput(QObject):
    """QPlainTextEdit / QLineEdit 에 붙여 두벌식 조합."""

    def __init__(self, edit, mode: HangulMode):
        super().__init__(edit)
        self.edit, self.mode = edit, mode
        self.buf = []            # 조합 중인 마지막 음절의 자모
        self.start = None        # 조합 중인 글자의 시작 위치
        self.end = None
        mode.changed.connect(lambda _on: self.finish())
        edit.installEventFilter(self)

    # ------------------------------------------------------------ 위치·치환
    def _pos(self):
        e = self.edit
        return e.textCursor().position() if isinstance(e, QPlainTextEdit) else e.cursorPosition()

    def _replace(self, a, b, text):
        e = self.edit
        if isinstance(e, QPlainTextEdit):
            c = e.textCursor()
            c.setPosition(a)
            c.setPosition(b, QTextCursor.KeepAnchor)
            c.insertText(text)
            e.setTextCursor(c)
        else:
            e.setSelection(a, b - a)
            e.insert(text)

    def finish(self):
        self.buf, self.start, self.end = [], None, None

    def _feed(self, jamo):
        pos = self._pos()
        if not self.buf or self.end != pos:
            self.finish()
            self.start = pos
        self.buf.append(jamo)
        self._render()

    def _render(self):
        sy = compose(self.buf)
        text = "".join(s for s, _ in sy)
        end = self.end if self.end is not None else self.start
        self._replace(self.start, end, text)
        if len(sy) > 1:                                # 앞 글자는 확정, 마지막 글자만 계속 조합
            self.start += len(text) - len(sy[-1][0])
            self.buf = list(sy[-1][1])
        self.end = self.start + len(sy[-1][0]) if sy else self.start

    # ------------------------------------------------------------ 키
    def eventFilter(self, obj, ev):
        t = ev.type()
        if t in (QEvent.FocusOut, QEvent.MouseButtonPress):
            self.finish()
            return False
        if t != QEvent.KeyPress:
            return False
        key, mods = ev.key(), ev.modifiers()
        if key == HANGUL_KEY or (key == Qt.Key_Space and mods & Qt.ShiftModifier):
            self.mode.toggle()
            return True
        if not self.mode.on:
            return False
        if mods & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
            self.finish()
            return False
        text = ev.text()
        if len(text) == 1 and text in KEYMAP:
            self._feed(KEYMAP[text])
            return True
        if key == Qt.Key_Backspace and self.buf and self.end == self._pos():
            self.buf.pop()
            if self.buf:
                self._render()
            else:
                self._replace(self.start, self.end, "")
                self.finish()
            return True
        self.finish()
        return False


def attach(mode: HangulMode, *edits):
    return [HangulInput(e, mode) for e in edits if isinstance(e, (QPlainTextEdit, QLineEdit))]

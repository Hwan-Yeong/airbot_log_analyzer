"""[분석] 창: 요청 입력 → 보낼 내용 미리보기(마스킹) → Claude 분석 → 결과 확인·엑셀 저장.

데이터 원칙: 분석 구간 발췌만, 시리얼·이름·연락처는 토큰으로 바꿔 보내고, 보내기 전에 전체 내용을 보여준다.
"""
import calendar
import html
import json
import sys
import time
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QDate, QDateTime, QEvent, QObject, Qt, QTime, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDateTimeEdit, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QTableWidget,
                               QTableWidgetItem, QTabWidget, QTextBrowser, QVBoxLayout)

from ..analysis import claude as ai
from ..analysis import context as actx
from ..analysis import excel as axl
from ..analysis import outlook
from ..analysis import request as areq
from ..analysis import sources as asrc
from ..analysis.masking import Masker
from . import style
from .hangul import HangulMode, attach as attach_hangul
from .workers import BackgroundJob

SOURCE_LABELS = [("amr", "AMR airbot_log"), ("ota", "AMR otalog"), ("journal", "AMR journal"), ("dmesg", "AMR dmesg"),
                 ("soc", "SoC logcat")]
PLACEHOLDER = ("예) 11월 1일 오전 9시 반쯤 주방 가다가 낙하 에러(S07-2) 나고 멈췄다고 합니다. 낙하센서 불량 의심된다는데 "
               "현상 정리 부탁해요.\n\n또는 요청 메일 본문을 붙여 넣거나, 메일 파일(.eml .msg .mht .html .txt)을 "
               "이 창에 끌어다 놓거나, [Outlook 선택 메일]로 Outlook 에서 보고 있는 메일을 가져오세요.")


def _local_files(mime) -> list:
    return [u.toLocalFile() for u in mime.urls() if u.isLocalFile() and u.toLocalFile()] if mime.hasUrls() else []


class _FileDrop(QObject):
    """요청 입력 칸에 파일을 끌어다 놓으면 (경로 글자를 넣지 않고) 메일로 읽음. 글자 끌어다 놓기는 그대로."""

    def __init__(self, target, on_files):
        super().__init__(target)
        self.on_files = on_files
        target.installEventFilter(self)

    def eventFilter(self, obj, ev):
        if ev.type() in (QEvent.DragEnter, QEvent.DragMove) and _local_files(ev.mimeData()):
            ev.acceptProposedAction()
            return True
        if ev.type() == QEvent.Drop:
            files = _local_files(ev.mimeData())
            if files:
                ev.acceptProposedAction()
                self.on_files(files)
                return True
        return False


def _qdt(t: float) -> QDateTime:
    d = datetime(1970, 1, 1) + timedelta(seconds=t)
    return QDateTime(QDate(d.year, d.month, d.day), QTime(d.hour, d.minute, d.second))


def _epoch(qdt: QDateTime) -> float:
    d, tm = qdt.date(), qdt.time()
    return calendar.timegm((d.year(), d.month(), d.day(), tm.hour(), tm.minute(), tm.second(), 0, 0, 0))


class AnalysisDialog(QDialog):
    def __init__(self, mw):
        super().__init__(mw)
        self.mw = mw
        self.S = mw.session
        self.settings = mw.settings
        self.setWindowTitle("AI 현상 분석 (Claude)")
        self.resize(820, 760)
        self.request = areq.Request(text="")
        self.sset = asrc.discover(self.S)
        self.ref = asrc.reference_time(self.S)
        self._job = None
        self._runner = None
        self.result_dialogs = []
        lay = QVBoxLayout(self)

        src = self.S.source
        head = QLabel(f"<b>{src.label}</b>" + (f" · 시리얼 {src.serial}" if src.serial else "") +
                      (f" · 수집 {src.collected}" if src.collected else ""))
        lay.addWidget(head)

        g1 = QGroupBox("1. 요청 내용 (구어체로 쓰거나 메일 본문 붙여넣기 · 메일 파일 끌어다 놓기)")
        l1 = QVBoxLayout(g1)
        self.ed_request = QPlainTextEdit()
        self.ed_request.setPlaceholderText(PLACEHOLDER)
        self.ed_request.setMinimumHeight(170)
        l1.addWidget(self.ed_request)
        row = QHBoxLayout()
        b_mail = QPushButton("메일 불러오기…")
        b_mail.setToolTip("메일 파일: .eml .msg(Outlook) .mht .html .txt — 이 창에 끌어다 놓아도 됩니다\n"
                          "여러 개를 고르면 이어 붙입니다")
        b_mail.clicked.connect(self.load_mail)
        row.addWidget(b_mail)
        self.btn_outlook = QPushButton("Outlook 선택 메일")
        self.btn_outlook.setToolTip("Outlook(클래식)에서 지금 보고 있는 메일을 가져옵니다\n"
                                    "(메일 창이 앞에 있으면 그 메일, 목록이면 선택한 메일 · Outlook 실행 중이어야 함)")
        self.btn_outlook.clicked.connect(self.fetch_outlook)
        self.btn_outlook.setVisible(outlook.available())
        row.addWidget(self.btn_outlook)
        self.hangul = HangulMode(self)
        self.btn_hangul = QPushButton("A")
        self.btn_hangul.setCheckable(True)
        self.btn_hangul.setFixedWidth(34)
        self.btn_hangul.setToolTip("한글 입력 전환 (한/영 키 또는 Shift+Space)\n"
                                   "WSL 에서는 Windows 한글 입력기가 전달되지 않아 앱이 직접 한글을 조합합니다")
        self.btn_hangul.toggled.connect(self.hangul.set)
        self.hangul.changed.connect(self._hangul_changed)
        if sys.platform != "win32":          # Windows 는 OS 한글 입력기가 그대로 동작
            row.addWidget(self.btn_hangul)
        else:
            self.btn_hangul.hide()
        self.lbl_extract = QLabel("")
        self.lbl_extract.setObjectName("hint")
        self.lbl_extract.setWordWrap(True)
        row.addWidget(self.lbl_extract, 1)
        l1.addLayout(row)
        lay.addWidget(g1, 1)

        g2 = QGroupBox("2. 분석 구간 (AMR 로그 시각 기준)")
        f2 = QFormLayout(g2)
        self.cmb_hint = QComboBox()
        self.cmb_hint.activated.connect(self._hint_chosen)
        f2.addRow("요청에 나온 시각", self.cmb_hint)
        r2 = QHBoxLayout()
        self.dt_anchor = QDateTimeEdit()
        self.dt_anchor.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.dt_anchor.setCalendarPopup(False)
        cur = self.S.time_at(mw.vt) or self.S.t_last or time.time()
        self.dt_anchor.setDateTime(_qdt(cur))
        r2.addWidget(self.dt_anchor)
        b_now = QPushButton("현재 재생 위치")
        b_now.clicked.connect(lambda: self.dt_anchor.setDateTime(_qdt(self.S.time_at(self.mw.vt) or cur)))
        r2.addWidget(b_now)
        r2.addStretch(1)
        f2.addRow("기준 시각", r2)
        r3 = QHBoxLayout()
        self.sp_before = QSpinBox()
        self.sp_before.setRange(1, 24 * 60)
        self.sp_before.setSuffix(" 분 전")
        self.sp_before.setValue(int(self.settings.get("analysis.before_min", 10)))
        self.sp_after = QSpinBox()
        self.sp_after.setRange(1, 24 * 60)
        self.sp_after.setSuffix(" 분 후")
        self.sp_after.setValue(int(self.settings.get("analysis.after_min", 5)))
        r3.addWidget(self.sp_before)
        r3.addWidget(QLabel("~"))
        r3.addWidget(self.sp_after)
        r3.addStretch(1)
        f2.addRow("범위", r3)
        lay.addWidget(g2)

        g3 = QGroupBox("3. 함께 볼 로그")
        l3 = QHBoxLayout(g3)
        avail = self.sset.available()
        counts = {"ota": len(self.sset.ota), "journal": len(self.sset.journal), "dmesg": len(self.sset.dmesg),
                  "soc": len(self.sset.soc), "amr": len(self.S.files)}
        self.chk_src = {}
        for key, label in SOURCE_LABELS:
            c = QCheckBox(f"{label} ({counts[key]})" if avail.get(key) else f"{label} (없음)")
            c.setChecked(bool(avail.get(key)))
            c.setEnabled(bool(avail.get(key)) and key != "amr")
            self.chk_src[key] = c
            l3.addWidget(c)
        l3.addStretch(1)
        lay.addWidget(g3)

        g4 = QGroupBox("4. 보내는 내용 · 지침")
        f4 = QFormLayout(g4)
        self.ed_words = QLineEdit(", ".join(self.settings.get("analysis.mask_words", []) or []))
        self.ed_words.setPlaceholderText("현장명, 고객사 등 쉼표로 구분 (시리얼·이름·전화·이메일·링크는 자동)")
        f4.addRow("추가 마스킹 단어", self.ed_words)
        gp = ai.guideline_path(self.settings.get("analysis.guideline_file", "") or "")
        self.lbl_guide = QLabel(f"{gp.name} ({gp.stat().st_size // 1024} KB) — 전문을 함께 보내 모든 내용을 참고해 분석"
                                if gp.is_file() else "지침 파일 없음 — 앱 기본 판단 규칙만 사용")
        self.lbl_guide.setToolTip(f"{gp}\n(다른 파일을 쓰려면 사용자 설정 analysis.guideline_file 에 경로 지정)")
        f4.addRow("지침", self.lbl_guide)
        lay.addWidget(g4)

        note = QLabel("다음 단계에서 Claude 로 보낼 전체 내용을 확인한 뒤 보냅니다. 원래 값(시리얼·이름 등)은 이 PC 에만 남습니다.\n"
                      "분석하는 동안에도 메인 화면의 재생·이동은 그대로 쓸 수 있습니다.")
        note.setObjectName("hint")
        note.setWordWrap(True)
        lay.addWidget(note)
        busy = QHBoxLayout()                 # 진행 상태 (창 안에 표시 — 메인 화면을 막지 않음)
        self.lbl_busy = QLabel("")
        self.bar_busy = QProgressBar()
        self.bar_busy.setRange(0, 0)
        self.bar_busy.setMaximumWidth(160)
        self.btn_cancel = QPushButton("중단")
        self._on_cancel = None
        self.btn_cancel.clicked.connect(self._cancel_clicked)
        busy.addWidget(self.lbl_busy, 1)
        busy.addWidget(self.bar_busy)
        busy.addWidget(self.btn_cancel)
        lay.addLayout(busy)
        bb = QDialogButtonBox()
        self.btn_next = bb.addButton("미리보기 →", QDialogButtonBox.AcceptRole)
        bb.addButton("닫기", QDialogButtonBox.RejectRole)
        bb.accepted.connect(self.preview)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._extract_timer = QTimer(self)
        self._extract_timer.setSingleShot(True)
        self._extract_timer.setInterval(400)
        self._extract_timer.timeout.connect(self.update_extract)
        self.ed_request.textChanged.connect(self._extract_timer.start)
        self._hangul_inputs = attach_hangul(self.hangul, self.ed_request, self.ed_words)
        self.setAcceptDrops(True)                       # 메일 파일 끌어다 놓기 (창 어디든)
        self._drop_filter = _FileDrop(self.ed_request.viewport(), self.load_mail_paths)
        self._preview = None
        self._set_busy(None)
        self.update_extract()

    def _hangul_changed(self, on: bool):
        self.btn_hangul.blockSignals(True)
        self.btn_hangul.setChecked(on)
        self.btn_hangul.blockSignals(False)
        self.btn_hangul.setText("한" if on else "A")

    def _set_busy(self, msg, on_cancel=None):
        """msg=None 이면 대기 상태. 작업 중에는 [미리보기] 를 막고 진행 문구·중단 버튼 표시."""
        on = msg is not None
        self.lbl_busy.setText(msg or "")
        for w in (self.lbl_busy, self.bar_busy):
            w.setVisible(on)
        self.btn_cancel.setVisible(on and on_cancel is not None)
        self._on_cancel = on_cancel
        self.btn_next.setEnabled(not on)

    def _cancel_clicked(self):
        if self._on_cancel is not None:
            self._on_cancel()

    # ------------------------------------------------------------ 요청
    def load_mail(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "요청 메일 열기", "", areq.MAIL_FILTER)
        if paths:
            self.load_mail_paths(paths)

    def load_mail_paths(self, paths) -> bool:
        """메일 파일(여러 개면 이어 붙임) → 요청 내용. 형식은 내용으로 판별."""
        try:
            req = areq.read_mails(paths)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "메일 열기 실패", str(e))
            return False
        self._set_request(req)
        return True

    def _set_request(self, req):
        self.request = req
        self.ed_request.setPlainText(areq.strip_signature(req.text))
        self.raise_()
        self.activateWindow()

    def fetch_outlook(self):
        if self._job is not None:
            return
        self._set_busy("Outlook 에서 메일 가져오는 중…")

        def done(result):
            self._set_busy(None)
            self._job = None
            req, count = result
            self._set_request(req)
            if count > 1:
                QMessageBox.information(self, "Outlook 선택 메일",
                                        f"Outlook 에서 {count}개를 선택해 첫 번째 메일만 가져왔습니다.\n"
                                        "다른 메일이 필요하면 그 메일 하나만 선택한 뒤 다시 누르세요.")

        def failed(msg):
            self._set_busy(None)
            self._job = None
            QMessageBox.warning(self, "Outlook 선택 메일", msg)

        self._job = BackgroundJob(lambda _progress: outlook.fetch_selected(), self)
        self._job.on_done = done
        self._job.on_failed = failed
        self._job.start()

    # ------------------------------------------------------------ 끌어다 놓기 (메일 파일)
    def dragEnterEvent(self, e):
        if _local_files(e.mimeData()):
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if _local_files(e.mimeData()):
            e.acceptProposedAction()

    def dropEvent(self, e):
        files = _local_files(e.mimeData())
        if files:
            e.acceptProposedAction()
            self.load_mail_paths(files)

    def update_extract(self):
        text = self.ed_request.toPlainText()
        ex = areq.extract(text, self.ref, self.S.source.serial if self.S.source else "")
        hints = areq.find_times(text, self.ref)
        self.cmb_hint.clear()
        for h in hints:
            self.cmb_hint.addItem(h.label(), h.when)
        if not hints:
            self.cmb_hint.addItem("(시각 언급 없음 — 기준 시각을 직접 지정)", None)
        if hints and text != getattr(self, "_last_text", None):
            self._set_anchor(hints[0].when)
        self._last_text = text
        parts = []
        if ex["error_codes"]:
            parts.append("에러 코드 " + ", ".join(ex["error_codes"]))
        if ex["serials"]:
            parts.append("시리얼 " + ", ".join(ex["serials"]))
        if ex["symptom"]:
            parts.append("증상: " + ex["symptom"][:60])
        self.lbl_extract.setText(" · ".join(parts) or "")

    def _hint_chosen(self, i):
        when = self.cmb_hint.itemData(i)
        if when is not None:
            self._set_anchor(when)

    def _set_anchor(self, when: datetime):
        self.dt_anchor.setDateTime(QDateTime(QDate(when.year, when.month, when.day), QTime(when.hour, when.minute, 0)))

    # ------------------------------------------------------------ 미리보기
    def options(self) -> actx.Options:
        return actx.Options(anchor=_epoch(self.dt_anchor.dateTime()), before=self.sp_before.value() * 60.0,
                            after=self.sp_after.value() * 60.0,
                            sources=tuple(k for k, c in self.chk_src.items() if c.isChecked()),
                            max_lines=int(self.settings.get("analysis.max_lines", 1500)),
                            max_chars=int(self.settings.get("analysis.max_chars", 150000)))

    def preview(self):
        text = self.ed_request.toPlainText().strip()
        if not text:
            QMessageBox.information(self, "요청 내용", "분석 요청 내용을 입력하거나 메일을 불러오세요.")
            return
        words = [w.strip() for w in self.ed_words.text().split(",") if w.strip()]
        try:
            self.settings.set_user("analysis.mask_words", words)
        except OSError:
            pass
        opt = self.options()
        S, ref = self.S, self.ref
        serial = S.source.serial if S.source else ""
        req_text = areq.strip_signature(text)
        extract = areq.extract(req_text, ref, serial)
        if self._job is not None:
            return
        self._set_busy("분석할 로그 모으는 중…")

        def job(progress):
            return actx.build_context(S, req_text, extract, opt, progress)

        def done(ctx):
            self._set_busy(None)
            self._job = None
            self._show_preview(ctx, words)

        def failed(msg):
            self._set_busy(None)
            self._job = None
            QMessageBox.warning(self, "분석 준비 실패", msg)

        self._job = BackgroundJob(job, self)
        self._job.on_progress = lambda m, _f: self.lbl_busy.setText(m)
        self._job.on_done = done
        self._job.on_failed = failed
        self._job.start()

    def _show_preview(self, ctx, words):
        src = self.S.source
        m = Masker(words=words, patterns=self.settings.get("analysis.mask_patterns", []) or [])
        m.add_serial(src.serial if src else "")
        m.add_memo(src.memo if src else "")
        m.add_names(self.request.names)
        m.add_emails(self.request.emails)
        guideline = ai.load_guideline(self.settings.get("analysis.guideline_file", "") or "")
        system_prompt = m.mask(ai.load_system_prompt(guideline))
        message = m.mask(ctx.user_message())
        if self._preview is not None:
            self._preview.close()
        dlg = PreviewDialog(self, system_prompt, message, m, ctx)
        dlg.setModal(False)                  # 메인 화면(재생)을 막지 않음
        dlg.accepted.connect(lambda: self.run_claude(ctx, m, system_prompt, dlg.message()))
        self._preview = dlg
        dlg.show()
        dlg.raise_()

    # ------------------------------------------------------------ Claude
    def run_claude(self, ctx, masker, system_prompt, message):
        exe = ai.find_cli(self.settings.get("analysis.claude_path", "") or "")
        if not exe:
            QMessageBox.warning(self, "Claude Code 없음",
                                "Claude Code CLI(claude)를 찾지 못했습니다.\n설치 후 터미널에서 claude 를 한 번 실행해 로그인하세요.\n"
                                "설치 위치가 다르면 설정 analysis.claude_path 에 경로를 지정하세요 (docs/SETUP.md).")
            return
        schema = ai.load_schema()
        runner = ai.ClaudeRunner(exe, system_prompt, schema, model=self.settings.get("analysis.model", "") or "",
                                 timeout=float(self.settings.get("analysis.timeout_sec", 900)))
        if self._job is not None:
            return
        self._runner = runner
        known = ctx.ids
        self._set_busy("Claude 에 보내는 중…", on_cancel=runner.cancel)

        def job(progress):
            t0 = time.time()
            result, meta = runner.run(message, progress)
            errors, warnings = ai.validate(result, schema, known)
            meta["retried"] = False
            if errors:
                progress("응답 검증 오류 → 한 번 더 요청", None)
                result2, meta2 = runner.run(ai.retry_message(message, errors), progress)
                errors2, warnings2 = ai.validate(result2, schema, known)
                if len(errors2) <= len(errors):
                    result, meta, errors, warnings = result2, meta2, errors2, warnings2
                meta["retried"] = True
            meta["errors"], meta["warnings"] = errors, warnings
            meta["elapsed_s"] = round(time.time() - t0, 1)
            return result, meta

        def done(res):
            self._set_busy(None)
            self._job = None
            result, meta = res
            dlg = ResultDialog(self.mw, ctx, masker.unmask(result), meta, self.ed_request.toPlainText())
            self.result_dialogs.append(dlg)
            dlg.show()
            dlg.raise_()

        def failed(msg):
            self._set_busy(None)
            self._job = None
            QMessageBox.warning(self, "Claude 분석 실패", msg)

        self._job = BackgroundJob(job, self)
        self._job.on_progress = lambda m, _f: self.lbl_busy.setText(m)
        self._job.on_done = done
        self._job.on_failed = failed
        self._job.start()

    def done(self, r):
        if self._runner is not None and self._job is not None:
            self._runner.cancel()
        if self._job is not None:
            self._job.detach()
            self._job = None
        if self._preview is not None:
            self._preview.close()
            self._preview = None
        super().done(r)


class PreviewDialog(QDialog):
    """보낼 내용 전체 확인 (입력은 줄 단위로 지워서 뺄 수 있음)."""

    def __init__(self, parent, system_prompt, message, masker, ctx):
        super().__init__(parent)
        self.setWindowTitle("보낼 내용 확인 — Claude 로 전송 전")
        self.resize(1000, 760)
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        mono = style.mono_font(9)
        self.ed_msg = QPlainTextEdit(message)
        self.ed_msg.setFont(mono)
        self.ed_msg.setLineWrapMode(QPlainTextEdit.NoWrap)
        tabs.addTab(self.ed_msg, "분석 입력 (편집 가능)")
        sp = QPlainTextEdit(system_prompt)
        sp.setReadOnly(True)
        sp.setFont(mono)
        tabs.addTab(sp, "지침 (시스템 프롬프트)")
        rows = masker.table()
        tbl = QTableWidget(len(rows), 3)
        tbl.setHorizontalHeaderLabels(["토큰", "종류", "원래 값 (이 PC 에만 보관)"])
        tbl.verticalHeader().setVisible(False)
        tbl.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        for r, vals in enumerate(rows):
            for c, v in enumerate(vals):
                tbl.setItem(r, c, QTableWidgetItem(v))
        tabs.addTab(tbl, f"마스킹 목록 ({len(rows)})")
        lay.addWidget(tabs, 1)
        self.lbl = QLabel("")
        self.lbl.setWordWrap(True)
        lay.addWidget(self.lbl)
        warn = QLabel("‘Claude로 보내기’를 누르면 위 두 탭의 내용이 Claude(Anthropic)로 전송됩니다. "
                      "불필요한 줄은 지우고 보내세요.")
        warn.setStyleSheet(f"color: {style.T().warn}; font-weight: bold;")
        warn.setWordWrap(True)
        lay.addWidget(warn)
        bb = QDialogButtonBox()
        bb.addButton("Claude로 보내기", QDialogButtonBox.AcceptRole)
        bb.addButton("취소", QDialogButtonBox.RejectRole)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        c = ctx.counts
        self._base = (f"발췌 {c.get('selected', 0):,}줄 (구간 원래 {c.get('window', 0):,}줄, 반복 접기 후 {c.get('folded', 0):,}줄"
                      + (f", 분량 제한으로 {c['dropped']:,}줄 생략" if c.get("dropped") else "") + f") · 이벤트 {len(ctx.window_events)}건")
        self._sys_len = len(system_prompt)
        self.ed_msg.textChanged.connect(self._update)
        self._update()

    def message(self) -> str:
        return self.ed_msg.toPlainText()

    def _update(self):
        n = len(self.ed_msg.toPlainText()) + self._sys_len
        self.lbl.setText(f"{self._base} · 전송 글자 수 {n:,}자 (약 {n // 2:,} 토큰 추정)")


class ResultDialog(QDialog):
    """분석 결과. 타임라인 행을 더블클릭하면 메인 화면이 그 시점으로 이동."""

    def __init__(self, mw, ctx, result, meta, request_text):
        super().__init__(mw)
        self.mw, self.ctx, self.result, self.meta, self.request_text = mw, ctx, result, meta, request_text
        self.setWindowTitle("AI 현상 분석 결과 — " + (ctx.session_label or ""))
        self.setWindowModality(Qt.NonModal)
        self.resize(1200, 800)
        lay = QVBoxLayout(self)
        t = style.T()
        rc, conf = result.get("request_check", {}), result.get("confidence", {})
        title = QLabel(result.get("summary", ""))
        title.setObjectName("title")
        title.setWordWrap(True)
        lay.addWidget(title)
        info = QLabel(f"신뢰도 <b>{conf.get('level', '-')}</b> · 접수 대비 <b>{rc.get('match', '-')}</b>"
                      f"{' · 실제 시각 ' + rc.get('log_time') if rc.get('log_time') else ''} · "
                      f"{', '.join(meta.get('models') or [])} {meta.get('elapsed_s', '')}초"
                      + (" · 재요청 1회" if meta.get("retried") else ""))
        info.setWordWrap(True)
        lay.addWidget(info)
        problems = (meta.get("errors") or []) + (meta.get("warnings") or [])
        if problems:
            w = QLabel("검증 참고: " + " / ".join(problems[:6]))
            w.setWordWrap(True)
            w.setStyleSheet(f"color: {t.warn};")
            lay.addWidget(w)

        tabs = QTabWidget()
        # 현상
        tb = QTextBrowser()
        esc = html.escape
        parts = ["<h3>현상 상세</h3><ul style='list-style:none; margin-left:-24px'>"]   # 항목에 ①② 번호가 이미 있음
        for p in result.get("phenomenon_detail", []):
            title = f"<b>{esc(p.get('title', ''))}</b> — " if p.get("title") else ""
            parts.append(f"<li>{title}{esc(p.get('text', ''))} <span style='color:{t.hint}'>[{', '.join(p.get('evidence', []))}]</span></li>")
        parts.append("</ul><h3>접수 내용과 비교</h3>")
        parts.append(f"<p>접수: {esc(rc.get('claimed_symptom', ''))} ({esc(rc.get('claimed_time', '') or '-')})<br>"
                     f"로그: {esc(rc.get('log_time', '') or '-')} — <b>{esc(rc.get('match', ''))}</b><br>{esc(rc.get('note', ''))}</p>")
        parts.append(f"<h3>신뢰도: {esc(conf.get('level', ''))}</h3><p>{esc(conf.get('reason', ''))}</p>")
        parts.append(f"<p style='color:{t.hint}'>{esc(ctx.offset.describe())}</p>")
        tb.setHtml("".join(parts))
        tabs.addTab(tb, "현상")
        # 타임라인
        cols = ["시각(AMR 기준)", "기준", "출처", "파일:줄", "이벤트", "해석", "중요도", "근거"]
        rows = result.get("timeline", [])
        self.tbl = QTableWidget(len(rows), len(cols))
        self.tbl.setHorizontalHeaderLabels(cols)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setWordWrap(True)
        for r, row in enumerate(rows):
            vals = [row.get("time", ""), row.get("time_basis", ""), row.get("source", ""), row.get("file_line", ""),
                    row.get("event", ""), row.get("interpretation", ""), row.get("importance", ""),
                    ", ".join(row.get("evidence", []))]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if row.get("importance") == "핵심":
                    f = QFont()
                    f.setBold(True)
                    it.setFont(f)
                    it.setForeground(style.level_color("error"))
                self.tbl.setItem(r, c, it)
        hh = self.tbl.horizontalHeader()
        for c in (0, 1, 2, 3, 6, 7):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        hh.setSectionResizeMode(5, QHeaderView.Stretch)
        self.tbl.resizeRowsToContents()
        self.tbl.cellDoubleClicked.connect(self.jump_row)
        self.tbl.setToolTip("더블클릭: 메인 화면을 이 시점으로 이동")
        tabs.addTab(self.tbl, f"타임라인 ({len(rows)})")
        # 표 3개
        tabs.addTab(self._table(["코드", "명칭", "발생", "해제", "종료 유형", "횟수", "근거"],
                                [[e.get("code", ""), e.get("name", ""), e.get("occurred", ""), e.get("cleared", ""),
                                  e.get("end_type", ""), str(e.get("count", "")), ", ".join(e.get("evidence", []))]
                                 for e in result.get("error_codes", [])]), "에러 코드")
        tabs.addTab(self._table(["요청자 가설", "판정", "근거 요지", "근거"],
                                [[h.get("hypothesis", ""), h.get("verdict", ""), h.get("reason", ""),
                                  ", ".join(h.get("evidence", []))] for h in result.get("hypothesis_check", [])]), "가설 검증")
        tabs.addTab(self._table(["확인할 것", "이유", "확인 방법"],
                                [[m.get("item", ""), m.get("reason", ""), m.get("how", "")]
                                 for m in result.get("need_more_info", [])]), "추가 확인")
        raw = QPlainTextEdit(json.dumps(result, ensure_ascii=False, indent=1))
        raw.setReadOnly(True)
        raw.setFont(style.mono_font(9))
        tabs.addTab(raw, "원문(JSON)")
        lay.addWidget(tabs, 1)
        bb = QDialogButtonBox()
        b_xl = bb.addButton("엑셀 저장…", QDialogButtonBox.ActionRole)
        b_xl.clicked.connect(self.save_excel)
        bb.addButton("닫기", QDialogButtonBox.RejectRole)
        bb.rejected.connect(self.close)
        lay.addWidget(bb)

    @staticmethod
    def _table(cols, rows):
        tbl = QTableWidget(len(rows), len(cols))
        tbl.setHorizontalHeaderLabels(cols)
        tbl.verticalHeader().setVisible(False)
        tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tbl.setWordWrap(True)
        for r, vals in enumerate(rows):
            for c, v in enumerate(vals):
                tbl.setItem(r, c, QTableWidgetItem(v))
        tbl.horizontalHeader().setStretchLastSection(True)
        tbl.resizeRowsToContents()
        return tbl

    def jump_row(self, r, _c=None):
        rows = self.result.get("timeline", [])
        if not (0 <= r < len(rows)) or self.mw.session is not self.ctx_session():
            return
        rmap = self.ctx.rmap
        for rid in rows[r].get("evidence", []):
            ln = rmap.get(rid)
            if ln is None:
                continue
            if ln.g is not None:
                self.mw._jump_to_line(ln.g)
                return
            vt = self.mw.session.vt_for_time(ln.rec.t)
            if vt is not None:
                self.mw.pause()
                self.mw.set_vt(vt, "seek")
                return
        d = axl._dt(rows[r].get("time", ""))
        if d is not None:
            vt = self.mw.session.vt_for_time(d.replace(tzinfo=timezone.utc).timestamp())
            if vt is not None:
                self.mw.pause()
                self.mw.set_vt(vt, "seek")

    def ctx_session(self):
        return self.mw.session if self.mw.session is not None and self.mw.session.source is not None and \
            self.mw.session.source.label == self.ctx.session_label else None

    def save_excel(self):
        st = self.mw.settings
        codes = [e.get("code") for e in self.result.get("error_codes", []) if e.get("code")]
        issue = codes[0] if codes else (self.ctx.request_extract.get("error_codes") or [""])[0]
        name = axl.default_name(st.get("analysis.excel_name", "Airbot_{serial6}_{issue}_현상분석_{date}_AI.xlsx"),
                                self.ctx.meta.get("serial", ""), issue or "현상")
        path, _ = QFileDialog.getSaveFileName(self, "현상 분석표 저장", name, "Excel (*.xlsx)")
        if not path:
            return
        try:
            axl.save(path, self.ctx, self.result, self.meta, title_prefix=st.get("analysis.title", "Airbot") or "Airbot")
        except (OSError, ImportError) as e:
            QMessageBox.warning(self, "엑셀 저장 실패", str(e))
            return
        QMessageBox.information(self, "엑셀 저장", f"저장했습니다:\n{path}")

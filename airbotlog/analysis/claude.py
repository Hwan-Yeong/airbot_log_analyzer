"""Claude Code CLI 호출과 응답 검증.

- 비대화 모드(-p), 도구 없음(--tools ""), 사용자 설정·메모리·플러그인 끔(--safe-mode),
  세션 기록 안 남김(--no-session-persistence), 빈 임시 폴더에서 실행 → 앱이 넘긴 텍스트만 보고 답함
- 시스템 프롬프트는 파일로(--system-prompt-file), 분석 입력은 표준입력으로 보냄 (명령줄 길이 제한 회피)
- 응답은 JSON 스키마(--json-schema)로 받아 근거 id·열거값·시각 형식을 다시 검사
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from ..settings import CONFIG_DIR

PROMPT_PATH = CONFIG_DIR / "analysis" / "system_prompt.md"
PROMPT_GENERIC_PATH = CONFIG_DIR / "analysis" / "system_prompt_generic.md"
GUIDELINE_PATH = CONFIG_DIR / "analysis" / "guideline.md"
SCHEMA_PATH = CONFIG_DIR / "analysis" / "output_schema.json"
CAUSAL_RE = re.compile(r"원인은|때문에|로 인해|으로 인해")
ID_RE = re.compile(r"^(?:R\d{5}|EV\d{3})$")
TIME_RE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d(?::\d\d(?:\.\d{1,3})?)?$")


class ClaudeError(Exception):
    pass


def _noop(*_args, **_kwargs):
    pass


def guideline_path(path_setting: str = "") -> Path:
    """분석 지침 파일: 설정 analysis.guideline_file 이 있으면 그 파일(최신본), 없으면 config/analysis/guideline.md."""
    if path_setting and Path(path_setting).is_file():
        return Path(path_setting)
    return GUIDELINE_PATH


def load_guideline(path_setting: str = "") -> str:
    p = guideline_path(path_setting)
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def load_system_prompt(guideline: str = "") -> str:
    """역할·판단 규칙·출력 형식 + 분석 지침 전문 (지침의 모든 정보를 참고해 분석)."""
    path = PROMPT_PATH if PROMPT_PATH.exists() else PROMPT_GENERIC_PATH
    text = path.read_text(encoding="utf-8")
    if guideline.strip():
        text += ("\n\n[분석 지침 전문]\n아래 지침의 로그 형식·판정 규칙·시그니처·주의사항·과거 사례를 모두 참고해 분석합니다. "
                 "지침의 grep·스크립트 레시피는 참고용이며, 이번 분석의 근거는 사용자 메시지로 제공된 발췌뿐입니다. "
                 "지침과 위 [판단 규칙]·[출력]이 다르면 위 내용이 우선합니다.\n"
                 "<guideline>\n" + guideline.strip() + "\n</guideline>\n")
    return text


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def find_cli(path_setting: str = "") -> str:
    """claude 실행 파일 경로 (설정값 → PATH → 기본 설치 위치)."""
    if path_setting and Path(path_setting).exists():
        return str(path_setting)
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    for cand in (home / ".local" / "bin" / "claude", home / ".claude" / "local" / "claude",
                 home / ".local" / "bin" / "claude.exe", home / "AppData" / "Roaming" / "npm" / "claude.cmd"):
        if cand.exists():
            return str(cand)
    return ""


class ClaudeRunner:
    """한 번의 분석 호출. run() 은 작업 스레드에서, cancel() 은 화면에서 호출."""

    def __init__(self, exe: str, system_prompt: str, schema: dict, model: str = "", timeout: float = 900.0):
        self.exe, self.system_prompt, self.schema = exe, system_prompt, schema
        self.model, self.timeout = model, float(timeout)
        self._proc = None
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()
        p = self._proc
        if p is not None and p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass

    def _cmd(self, prompt_file: Path, with_schema: bool, minimal: bool = False):
        cmd = [self.exe, "-p", "--output-format", "json", "--tools", ""]
        if not minimal:                    # 오래된 CLI 에 없는 옵션은 오류 시 빼고 다시 시도
            cmd += ["--safe-mode", "--no-session-persistence"]
        cmd += ["--system-prompt-file", str(prompt_file)]
        if with_schema:
            cmd += ["--json-schema", json.dumps(self.schema, ensure_ascii=False, separators=(",", ":"))]
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    def run(self, user_message: str, progress=_noop) -> tuple:
        """→ (결과 dict, 메타 dict). 스키마 옵션이 안 되는 CLI 면 스키마 없이 한 번 더."""
        try:
            return self._run(user_message, progress, with_schema=True)
        except ClaudeError as e:
            msg = str(e).lower()
            if self._cancel.is_set():
                raise
            if "unknown option" in msg and ("safe-mode" in msg or "session-persistence" in msg):
                return self._run(user_message, progress, with_schema="json-schema" not in msg, minimal=True)
            if "json-schema" in msg:
                return self._run(user_message, progress, with_schema=False)
            raise

    def _run(self, user_message: str, progress, with_schema: bool, minimal: bool = False):
        if not self.exe:
            raise ClaudeError("Claude Code CLI(claude)를 찾지 못했습니다. 설치·로그인 후 다시 시도하세요 (docs/SETUP.md).")
        with tempfile.TemporaryDirectory(prefix="airbotlog_ai_") as tmp:
            tmp = Path(tmp)
            prompt_file = tmp / "system_prompt.md"
            prompt_file.write_text(self.system_prompt, encoding="utf-8")
            work = tmp / "work"            # 빈 작업 폴더 (도구가 없어도 혹시 모를 파일 접근 차단)
            work.mkdir()
            flags = 0x08000000 if sys.platform == "win32" else 0     # CREATE_NO_WINDOW
            env = dict(os.environ)
            env.pop("CLAUDECODE", None)    # 다른 Claude Code 세션 안에서 실행돼도 독립 실행
            try:
                self._proc = subprocess.Popen(self._cmd(prompt_file, with_schema, minimal), cwd=str(work), env=env,
                                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                              creationflags=flags)
            except OSError as e:
                raise ClaudeError(f"Claude Code CLI 실행 실패: {e}") from e
            out, err = [], []
            readers = [threading.Thread(target=lambda s=s, b=b: b.append(s.read()), daemon=True)
                       for s, b in ((self._proc.stdout, out), (self._proc.stderr, err))]
            for r in readers:
                r.start()
            try:
                self._proc.stdin.write(user_message.encode("utf-8"))
                self._proc.stdin.close()
            except OSError:
                pass
            t0 = time.time()
            while self._proc.poll() is None:
                el = time.time() - t0
                if self._cancel.is_set():
                    self._proc.kill()
                    raise ClaudeError("사용자가 분석을 중단했습니다.")
                if el > self.timeout:
                    self._proc.kill()
                    raise ClaudeError(f"Claude 응답 시간 초과 ({self.timeout:.0f}초)")
                progress(f"Claude 분석 중… {int(el)}초", None)
                time.sleep(0.5)
            for r in readers:
                r.join(5)
            stdout = (out[0] if out else b"").decode("utf-8", "replace")
            stderr = (err[0] if err else b"").decode("utf-8", "replace")
        if self._cancel.is_set():
            raise ClaudeError("사용자가 분석을 중단했습니다.")
        try:
            d = json.loads(stdout)
        except json.JSONDecodeError:
            msg = (stderr or stdout).strip()[-800:]
            raise ClaudeError(f"Claude CLI 오류 (종료 코드 {self._proc.returncode}):\n{msg}") from None
        if d.get("is_error"):
            raise ClaudeError(f"Claude 응답 오류: {str(d.get('result') or d.get('subtype'))[:800]}")
        result = d.get("structured_output")
        if not isinstance(result, dict):
            result = parse_json_text(d.get("result") or "")
        meta = {"duration_s": round((d.get("duration_ms") or 0) / 1000, 1), "cost_usd": d.get("total_cost_usd"),
                "models": sorted((d.get("modelUsage") or {}).keys()), "num_turns": d.get("num_turns"),
                "schema": with_schema, "minimal_flags": minimal}
        return result, meta


def parse_json_text(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b < 0:
        raise ClaudeError("응답에서 JSON 을 찾지 못했습니다.")
    try:
        return json.loads(t[a:b + 1])
    except json.JSONDecodeError as e:
        raise ClaudeError(f"응답 JSON 형식 오류: {e}") from e


# ---------------------------------------------------------------- 검증
def validate(result: dict, schema: dict, known_ids: set) -> tuple:
    """→ (errors, warnings). errors 가 있으면 한 번 다시 요청할 근거."""
    errors, warnings = [], []

    def check(obj, sch, path):
        t = sch.get("type")
        if t == "object":
            if not isinstance(obj, dict):
                errors.append(f"{path}: 객체가 아님")
                return
            for k in sch.get("required", []):
                if k not in obj:
                    errors.append(f"{path}.{k}: 없음")
            for k, v in obj.items():
                if k in sch.get("properties", {}):
                    check(v, sch["properties"][k], f"{path}.{k}")
        elif t == "array":
            if not isinstance(obj, list):
                errors.append(f"{path}: 배열이 아님")
                return
            for i, v in enumerate(obj):
                check(v, sch.get("items", {}), f"{path}[{i}]")
        elif t == "string":
            if not isinstance(obj, str):
                errors.append(f"{path}: 문자열이 아님")
            elif "enum" in sch and obj not in sch["enum"]:
                errors.append(f"{path}: 허용되지 않은 값 '{obj}'")
        elif t == "integer" and not isinstance(obj, int):
            errors.append(f"{path}: 정수가 아님")

    check(result, schema, "$")
    if errors:
        return errors, warnings
    bad = set()
    for sec in ("phenomenon_detail", "timeline", "error_codes", "hypothesis_check"):
        for i, item in enumerate(result.get(sec, [])):
            ev = item.get("evidence", [])
            if sec in ("phenomenon_detail", "timeline", "error_codes") and not ev:
                warnings.append(f"{sec}[{i}]: 근거 id 없음")
            bad.update(x for x in ev if x not in known_ids)
    bad.update(x for x in result["request_check"].get("evidence", []) if x not in known_ids)
    bad.update(x for x in result["confidence"].get("evidence_lines", []) if x not in known_ids)
    if bad:
        errors.append("입력에 없는 근거 id: " + ", ".join(sorted(bad)[:20]))
    for i, row in enumerate(result.get("timeline", [])):
        if row.get("time") and not TIME_RE.match(row["time"]) and row.get("time_basis") not in ("범위", "추정", "stale"):
            warnings.append(f"timeline[{i}].time 형식: {row['time']}")
    texts = [result.get("summary", "")] + [p.get("text", "") for p in result.get("phenomenon_detail", [])]
    if any(CAUSAL_RE.search(t) for t in texts):
        warnings.append("요약/현상에 인과 표현(원인은·때문에·로 인해)이 있습니다 — 로그 근거를 확인하세요.")
    return errors, warnings


def retry_message(user_message: str, errors) -> str:
    return (user_message + "\n<previous_output_problems>\n이전 출력에 아래 문제가 있었습니다. 고쳐서 다시 출력하세요.\n- "
            + "\n- ".join(errors[:20]) + "\n</previous_output_problems>\n")

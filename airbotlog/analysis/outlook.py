"""Outlook(클래식)에서 지금 보고 있는 메일 가져오기 — Windows PowerShell + Outlook COM (Windows·WSL 공통, 추가 패키지 없음).

가장 앞의 Outlook 창 기준: 메일 창이 열려 있으면 그 메일, 목록이면 선택한 메일(여러 개면 첫 번째).
Outlook 이 실행 중이어야 한다. 새 Outlook(olk)은 COM 을 지원하지 않아 쓸 수 없다.
메일 내용은 이 PC 안에서만 읽는다 (보내기 전에는 [분석] 미리보기에서 마스킹된 내용을 확인).
"""
import base64
import email.utils
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from ..nas import detect_platform, find_powershell
from .request import Request, _mail_text, _people

TIMEOUT_SEC = 90                 # Outlook 보안 확인 창이 뜨면 사용자가 허용할 때까지 기다림
MARK = "MAILJSON:"               # PowerShell 출력에서 결과 줄 표시
MAX_BODY = 300_000

SCRIPT = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'
function Emit($o) { Write-Output ('MAILJSON:' + ($o | ConvertTo-Json -Compress -Depth 3)) }
function Iso($t) {
  if ($t -eq $null -or $t.Year -gt 4000) { return '' }
  $off = [TimeZoneInfo]::Local.GetUtcOffset($t).TotalMinutes
  return $t.ToString('yyyy-MM-ddTHH:mm:ss') + '|' + [int]$off
}
try { $ol = [Runtime.InteropServices.Marshal]::GetActiveObject('Outlook.Application') }
catch { Emit @{ error = 'not_running' }; exit 0 }
try {
  $item = $null; $count = 0
  $win = $ol.ActiveWindow()
  if ($win -ne $null -and $win.Class -eq 35) { $item = $win.CurrentItem; $count = 1 }            # 열린 메일 창
  else {
    $exp = if ($win -ne $null -and $win.Class -eq 34) { $win } else { $ol.ActiveExplorer() }    # 목록 창
    if ($exp -ne $null) { $sel = $exp.Selection; $count = $sel.Count; if ($count -ge 1) { $item = $sel.Item(1) } }
  }
  if ($item -eq $null) { Emit @{ error = 'no_selection' }; exit 0 }
  $o = [ordered]@{ count = $count; subject = [string]$item.Subject; body = [string]$item.Body }
  try { $o.sender = [string]$item.SenderName } catch { $o.sender = '' }
  $smtp = ''
  try {
    if ($item.SenderEmailType -eq 'EX') { $smtp = $item.Sender.GetExchangeUser().PrimarySmtpAddress }
    else { $smtp = $item.SenderEmailAddress }
  } catch {}
  $o.sender_email = [string]$smtp
  try { $o.to = [string]$item.To; $o.cc = [string]$item.CC } catch { $o.to = ''; $o.cc = '' }
  $rcpt = @()
  try {
    foreach ($r in $item.Recipients) {
      $a = ''
      try { $a = $r.PropertyAccessor.GetProperty('http://schemas.microsoft.com/mapi/proptag/0x39FE001F') } catch {}
      if (-not $a) { $a = [string]$r.Address }
      $rcpt += ([string]$r.Name + ' <' + $a + '>')
    }
  } catch {}
  $o.recipients = $rcpt
  try { $o.sent = Iso $item.SentOn } catch { $o.sent = '' }
  try { $o.received = Iso $item.ReceivedTime } catch { $o.received = '' }
  Emit $o
} catch { Emit @{ error = 'exception'; message = $_.Exception.Message } }
"""

MESSAGES = {
    "not_running": "Outlook 이 실행 중이 아닙니다. Outlook 을 열고 메일을 선택한 뒤 다시 누르세요.\n"
                   "(새 Outlook 은 지원하지 않습니다 — 클래식 Outlook 필요)",
    "no_selection": "Outlook 에서 선택한 메일이 없습니다. 메일 목록에서 메일을 하나 선택한 뒤 다시 누르세요.",
    "no_powershell": "PowerShell 을 찾을 수 없습니다 (Windows 또는 WSL 에서만 사용 가능).",
}


class OutlookError(Exception):
    pass


def available() -> bool:
    """Windows 또는 WSL 이고 PowerShell 이 있으면 버튼 표시."""
    return detect_platform() in ("windows", "wsl") and bool(_powershell())


def _powershell() -> str:
    if sys.platform == "win32":
        return "powershell.exe"
    return find_powershell()


def _when(value: str) -> str:
    """'yyyy-MM-ddTHH:mm:ss|분' (Outlook 현지 시각 + UTC 차이) → 메일 Date 머리글 형식."""
    if not value or "|" not in value:
        return ""
    stamp, off = value.split("|", 1)
    try:
        dt = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone(timedelta(minutes=int(off))))
    except ValueError:
        return ""
    return email.utils.format_datetime(dt)


def parse_output(stdout: str):
    """PowerShell 출력(마지막 결과 줄의 JSON) → (Request, 선택한 메일 수)."""
    lines = [ln for ln in stdout.splitlines() if ln.startswith(MARK)]
    if not lines:
        raise OutlookError("Outlook 응답을 읽지 못했습니다.\n" + stdout.strip()[-300:])
    data = json.loads(lines[-1][len(MARK):])
    err = data.get("error")
    if err:
        raise OutlookError(MESSAGES.get(err) or f"Outlook 에서 메일을 읽지 못했습니다: {data.get('message', err)}")
    subject = data.get("subject") or ""
    body = (data.get("body") or "")[:MAX_BODY]
    if not body.strip():
        raise OutlookError("선택한 항목에 본문이 없습니다 (메일이 아니거나 아직 내려받지 않은 메일).")
    sender = data.get("sender") or ""
    if data.get("sender_email") and "@" in data["sender_email"]:
        sender = f"{sender} <{data['sender_email']}>"
    recipients = [r for r in data.get("recipients") or [] if isinstance(r, str)]
    names, addrs = _people(sender, data.get("to") or "", data.get("cc") or "", *recipients)
    date = _when(data.get("sent") or "") or _when(data.get("received") or "")
    req = Request(text=_mail_text(subject, date, body), subject=subject, sender=sender,
                  names=sorted(set(names)), emails=sorted({a for a in addrs if "@" in a}))
    return req, int(data.get("count") or 1)


def fetch_selected(timeout: int = TIMEOUT_SEC):
    """Outlook 에서 지금 보고 있는 메일 → (Request, 선택한 메일 수). 작업 스레드에서 호출."""
    exe = _powershell()
    if not exe:
        raise OutlookError(MESSAGES["no_powershell"])
    enc = base64.b64encode(SCRIPT.encode("utf-16-le")).decode("ascii")
    kw = {}
    if sys.platform == "win32":
        kw["creationflags"] = 0x08000000                       # CREATE_NO_WINDOW: 콘솔 창 깜빡임 없이
    elif os.path.isdir("/mnt/c"):
        kw["cwd"] = "/mnt/c"                                   # WSL: UNC 현재 폴더 경고 방지
    try:
        r = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
                           capture_output=True, timeout=timeout, **kw)
    except subprocess.TimeoutExpired as e:
        raise OutlookError(f"Outlook 응답이 {timeout}초 안에 오지 않았습니다.\n"
                           "Outlook 에 '프로그램이 전자 메일 정보에 액세스하려고 합니다' 확인 창이 떠 있으면 허용한 뒤 다시 누르세요.") from e
    except OSError as e:
        raise OutlookError(f"PowerShell 실행 실패: {e}") from e
    return parse_output(r.stdout.decode("utf-8", "replace"))

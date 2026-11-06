"""WSL 용 Windows '끌어다 놓기 창'.

WSLg 로 띄운 리눅스 앱 창에는 Windows 탐색기·Outlook 에서 끌어다 놓을 수 없다 (WSLg 미지원 → 마우스가 금지 표시).
그래서 PowerShell 로 작은 Windows 창을 띄워, 거기에 놓은 파일 · Outlook 메일을 받아 온다.
- 파일: Windows 경로를 WSL 경로로 바꿔 읽고, 바꿀 수 없으면(NAS 등) 30MB 이하 파일은 내용을 받아 임시 파일로 저장
- Outlook 메일(목록에서 끌어온 항목): 끌어온 메일 = 선택한 메일이므로 [Outlook 선택 메일]과 같은 방법으로 가져옴
"""
import base64
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .nas import detect_platform, find_powershell

MAX_DATA = 30 * 1024 * 1024      # 이보다 큰 파일(로그 압축 등)은 경로만
TIMEOUT_SEC = 300                # 놓지 않고 두면 창이 스스로 닫힘

SCRIPT = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ProgressPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()
$form = New-Object Windows.Forms.Form
$form.Text = __TITLE__
$form.TopMost = $true
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedToolWindow'
$form.ClientSize = New-Object Drawing.Size(460, 210)
$form.BackColor = [Drawing.Color]::FromArgb(236, 243, 252)
$form.AllowDrop = $true
$label = New-Object Windows.Forms.Label
$label.Dock = 'Fill'
$label.TextAlign = 'MiddleCenter'
$label.Font = New-Object Drawing.Font('Malgun Gothic', 11)
$label.Text = __HINT__
$label.AllowDrop = $true
$form.Controls.Add($label)
$note = New-Object Windows.Forms.Label
$note.Dock = 'Bottom'
$note.Height = 24
$note.TextAlign = 'MiddleCenter'
$note.ForeColor = [Drawing.Color]::FromArgb(110, 120, 135)
$note.Text = '닫으면 취소 · 5분 동안 놓지 않으면 자동으로 닫힘'
$note.AllowDrop = $true
$form.Controls.Add($note)
$fd = [Windows.Forms.DataFormats]::FileDrop
$onEnter = {
  param($s, $e)
  if ($e.Data.GetDataPresent($fd) -or $e.Data.GetDataPresent('FileGroupDescriptorW') -or
      $e.Data.GetDataPresent('FileGroupDescriptor')) { $e.Effect = [Windows.Forms.DragDropEffects]::Copy }
  else { $e.Effect = [Windows.Forms.DragDropEffects]::None }
}
$onDrop = {
  param($s, $e)
  if ($e.Data.GetDataPresent($fd)) {
    foreach ($p in $e.Data.GetData($fd)) {
      $o = [ordered]@{ path = [string]$p; name = [IO.Path]::GetFileName([string]$p) }
      try {
        $fi = Get-Item -LiteralPath $p
        if (-not $fi.PSIsContainer -and $fi.Length -le __MAXDATA__) { $o.data = [Convert]::ToBase64String([IO.File]::ReadAllBytes($p)) }
      } catch {}
      [Console]::Out.WriteLine('DROPFILE:' + ($o | ConvertTo-Json -Compress))
    }
  } elseif ($e.Data.GetDataPresent('FileGroupDescriptorW') -or $e.Data.GetDataPresent('FileGroupDescriptor')) {
    [Console]::Out.WriteLine('DROPOUTLOOK')
  }
  $form.Close()
}
foreach ($c in @($form, $label, $note)) { $c.add_DragEnter($onEnter); $c.add_DragDrop($onDrop) }
$timer = New-Object Windows.Forms.Timer
$timer.Interval = __TIMEOUT_MS__
$timer.add_Tick({ $form.Close() })
$timer.Start()
__TEST_HOOK__
$form.Add_Shown({ $form.Activate() })
[void]$form.ShowDialog()
[Console]::Out.WriteLine('DROPEND')
"""

# 테스트용: 창이 뜨면 파일을 놓은 것처럼 처리 (실제 마우스 드래그 없이 처리 경로 확인)
TEST_HOOK = r"""
$form.add_Shown({
  $do = New-Object Windows.Forms.DataObject
  $sc = New-Object Collections.Specialized.StringCollection
  foreach ($t in __TEST_FILES__) { [void]$sc.Add($t) }
  $do.SetFileDropList($sc)
  $ea = New-Object Windows.Forms.DragEventArgs($do, 0, 0, 0, [Windows.Forms.DragDropEffects]::Copy, [Windows.Forms.DragDropEffects]::Copy)
  & $onEnter $form $ea
  [Console]::Out.WriteLine('TEST_EFFECT:' + $ea.Effect)
  __SNAPSHOT__
  & $onDrop $form $ea
})
"""
SNAPSHOT = r"""
  $bmp = New-Object Drawing.Bitmap($form.Width, $form.Height)
  $form.DrawToBitmap($bmp, (New-Object Drawing.Rectangle(0, 0, $form.Width, $form.Height)))
  $bmp.Save(__SNAPSHOT_PATH__)
"""


def _ps_str(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


@dataclass
class DropResult:
    files: list = field(default_factory=list)      # WSL 에서 읽을 수 있는 경로
    outlook: bool = False                           # Outlook 목록에서 끌어온 메일
    skipped: list = field(default_factory=list)     # 읽을 수 없어 뺀 파일 (경로 변환 불가 + 너무 큼)
    tmpdir: Path = None

    def cleanup(self):
        if self.tmpdir is not None:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
            self.tmpdir = None


def available() -> bool:
    """WSL 에서만 필요 (Windows 에서 직접 실행하면 앱 창에 바로 끌어다 놓을 수 있음)."""
    return detect_platform() == "wsl" and bool(find_powershell())


def _to_wsl(win_path: str) -> str:
    try:
        out = subprocess.run(["wslpath", "-u", win_path], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    p = out.stdout.strip()
    return p if out.returncode == 0 and p and os.path.isfile(p) and os.access(p, os.R_OK) else ""


def parse_output(stdout: str, to_wsl=_to_wsl) -> DropResult:
    res = DropResult()
    for line in stdout.splitlines():
        if line.startswith("DROPOUTLOOK"):
            res.outlook = True
        elif line.startswith("DROPFILE:"):
            item = json.loads(line[len("DROPFILE:"):])
            local = to_wsl(item.get("path", ""))
            if local:
                res.files.append(local)
            elif item.get("data"):
                if res.tmpdir is None:
                    res.tmpdir = Path(tempfile.mkdtemp(prefix="airdrop_"))
                name = Path(item.get("name") or "dropped").name or "dropped"
                dst = res.tmpdir / f"{len(res.files)}_{name}"
                dst.write_bytes(base64.b64decode(item["data"]))
                res.files.append(str(dst))
            else:
                res.skipped.append(item.get("path", ""))
    return res


class DropWindow:
    """Windows 쪽 끌어다 놓기 창 (run 은 작업 스레드에서, cancel 은 화면 스레드에서)."""

    def __init__(self, title: str, hint: str, timeout_sec: int = TIMEOUT_SEC, test_files=None, snapshot=""):
        self.title, self.hint, self.timeout_sec = title, hint, timeout_sec
        self.test_files, self.snapshot = test_files, snapshot
        self.proc = None
        self.cancelled = False

    def script(self) -> str:
        hook = ""
        if self.test_files is not None:
            hook = TEST_HOOK.replace("__TEST_FILES__", "@(" + ", ".join(_ps_str(t) for t in self.test_files) + ")")
            hook = hook.replace("__SNAPSHOT__", SNAPSHOT.replace("__SNAPSHOT_PATH__", _ps_str(self.snapshot))
                                if self.snapshot else "")
        return (SCRIPT.replace("__TITLE__", _ps_str(self.title)).replace("__HINT__", _ps_str(self.hint))
                .replace("__MAXDATA__", str(MAX_DATA)).replace("__TIMEOUT_MS__", str(self.timeout_sec * 1000))
                .replace("__TEST_HOOK__", hook))

    def run(self) -> DropResult:
        exe = find_powershell()
        if not exe:
            raise RuntimeError("PowerShell 을 찾을 수 없습니다 (WSL interop 확인).")
        enc = base64.b64encode(self.script().encode("utf-16-le")).decode("ascii")
        self.proc = subprocess.Popen([exe, "-STA", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                      "-EncodedCommand", enc], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     cwd="/mnt/c" if os.path.isdir("/mnt/c") else None)
        try:
            out, _err = self.proc.communicate(timeout=self.timeout_sec + 30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            out, _err = self.proc.communicate()
        if self.cancelled:
            return DropResult()
        return parse_output(out.decode("utf-8", "replace"))

    def cancel(self):
        self.cancelled = True
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()

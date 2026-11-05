import shutil

import pytest

from airbotlog import nas
from airbotlog.nas import LocalNasSource, NasError, PowerShellNasSource, resolve_source


@pytest.fixture()
def fake_nas(tmp_path, demo_zip):
    root = tmp_path / "NAS"
    (root / "2026" / "issue_a").mkdir(parents=True)
    (root / "issue_b" / "20261101153000_DEMOBOTSN000000001").mkdir(parents=True)  # 압축 해제본 폴더
    shutil.copy(demo_zip, root / "2026" / "issue_a" / demo_zip.name)
    shutil.copy(demo_zip, root / "issue_b" / demo_zip.name)
    (root / "issue_b" / "report.xlsx").write_bytes(b"x")
    return root


def _snapshot(root):
    return {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*")}


def test_local_list_and_copy_is_read_only(fake_nas, ws):
    before = _snapshot(fake_nas)
    src = LocalNasSource(str(fake_nas))
    entries = src.list_archives()
    assert sorted(e.rel for e in entries) == [
        "2026/issue_a/20261101153000_DEMOBOTSN000000001.zip",
        "issue_b/20261101153000_DEMOBOTSN000000001.zip"]
    assert {e.folder: e.extracted for e in entries} == {"2026/issue_a": False, "issue_b": True}
    e = entries[0]
    out = src.copy_to(e.rel, ws.new_dir("nas"), size=e.size)
    assert out.read_bytes() == (fake_nas / e.rel).read_bytes()
    assert _snapshot(fake_nas) == before          # NAS 쪽 변경 없음


def test_copy_into_nas_is_refused(fake_nas):
    src = LocalNasSource(str(fake_nas))
    with pytest.raises(NasError):
        src.copy_to("issue_b/20261101153000_DEMOBOTSN000000001.zip", fake_nas / "2026")


def test_resolve_source_prefers_linux_mount(monkeypatch, tmp_path, fake_nas, settings):
    monkeypatch.setattr(nas, "detect_platform", lambda: "linux")
    settings.set_user("nas.root", r"\\server\share\logs")
    settings.set_user("nas.linux_path", str(fake_nas))
    src, msg = resolve_source(settings)
    assert isinstance(src, LocalNasSource) and src.root == str(fake_nas)
    settings.set_user("nas.linux_path", str(tmp_path / "not_mounted"))
    monkeypatch.setattr(nas, "gvfs_path_for", lambda unc: None)
    src, msg = resolve_source(settings)
    assert src is None and "마운트" in msg


def test_powershell_script_quoting():
    src = PowerShellNasSource(r"\\server\share\it's", exe="powershell.exe")
    cmd = src._command("Write-Output 'x'")
    assert cmd[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand"]
    assert nas._ps_quote("it's") == "'it''s'"

import io
import os
import tarfile
import time

from airbotlog.workspace import Workspace, extract_archive, parse_archive_name, prepare_source


def test_zip_nested_extract_and_order(demo_zip, ws):
    src = prepare_source([demo_zip], ws, skip=["soc/*"])
    assert [p.name for p in src.log_files] == ["airbot_log.2.txt", "airbot_log.1.txt", "airbot_log.txt"]
    assert src.map_dir is not None and (src.map_dir / "airbot_map_00.yaml").is_file()
    assert src.serial == "DEMOBOTSN000000001" and src.collected == "2026-11-01 15:30:00"
    assert ws.contains(src.log_files[0])
    assert not list(ws.path.rglob("logcat"))          # soc/* 는 풀지 않음
    assert not list(ws.path.rglob("*.tar.gz"))        # 중첩 압축은 풀고 지움


def test_single_txt_picks_rotation_siblings(demo_zip, ws):
    src = prepare_source([demo_zip], ws)
    one = src.log_files[-1]
    src2 = prepare_source([one], ws)
    assert [p.name for p in src2.log_files] == ["airbot_log.2.txt", "airbot_log.1.txt", "airbot_log.txt"]


def test_workspace_cleanup_and_stale(tmp_path):
    root = tmp_path / "tmp"
    w = Workspace(root)
    p = w.ensure()
    assert p.exists()
    w.cleanup()
    assert not p.exists()
    stale = root / "airbotlog_dead"
    stale.mkdir(parents=True)
    (stale / Workspace.MARKER).write_text(f"999999\n{int(time.time())}\n")
    foreign = root / "airbotlog_nomarker"
    foreign.mkdir()
    assert Workspace.cleanup_stale(root) == 1
    assert not stale.exists() and foreign.exists()     # 마커 없는 폴더는 건드리지 않음


def test_tar_blocks_path_traversal(tmp_path):
    arc = tmp_path / "evil.tar.gz"
    with tarfile.open(arc, "w:gz") as tf:
        for name in ("../evil.txt", "/abs.txt", "ok/fine.txt"):
            info = tarfile.TarInfo(name)
            info.size = 2
            tf.addfile(info, io.BytesIO(b"hi"))
    out = extract_archive(arc, tmp_path / "out")
    assert (out / "ok" / "fine.txt").is_file()
    assert not (tmp_path / "evil.txt").exists()          # 상위 폴더로 나가는 항목은 무시
    assert sorted(os.listdir(out)) == ["abs.txt", "ok"]   # 절대경로는 대상 폴더 안으로 (zipfile 과 동일)
    assert sorted(os.listdir(tmp_path)) == ["evil.tar.gz", "out"]


def test_archive_name_meta():
    m = parse_archive_name("20260310113542_DEMOBOTSN000000002(E09).zip")
    assert m == {"serial": "DEMOBOTSN000000002", "collected": "2026-03-10 11:35:42", "memo": "E09"}
    assert parse_archive_name("AMR_LOG.tar.gz")["serial"] == ""

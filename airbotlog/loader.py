"""입력 → 작업 폴더 준비 → 파싱 → Session (UI 없이도 사용 가능)."""
from pathlib import Path

from .mapdata import load_map
from .parser import parse_session
from .rules import RuleSet
from .versions import apply_versions
from .workspace import Workspace, prepare_map, prepare_source


def _noop(*_args, **_kwargs):
    pass


def load_session(inputs, ws: Workspace, settings, rules: RuleSet = None, progress=_noop,
                 origin: str = "", map_override=None):
    rules = rules or RuleSet.load()
    skip = settings.get("workspace.skip_members", []) or []
    source = prepare_source([Path(p) for p in inputs], ws, skip=skip, progress=progress, origin=origin)
    if map_override:
        source.map_dir = prepare_map(map_override, ws, skip=skip, progress=progress)
    session = parse_session(source, rules, idle_gap=float(settings.get("playback.idle_gap_sec", 5.0)),
                            progress=progress)
    progress("버전 정보 정리 (airbot_log + otalog)", None)
    apply_versions(session)
    if source.map_dir:
        try:
            session.map = load_map(source.map_dir)
        except (OSError, ValueError, KeyError) as e:
            session.map = None
            session.stats["map_error"] = str(e)
    return session

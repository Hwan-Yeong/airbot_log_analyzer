"""설정 로드/저장.

- 기본값: config/settings.yaml (레포에 포함, 앱이 수정하지 않음)
- 이 PC 전용: config/settings.local.yaml (있으면 기본값 위에 적용, git 제외 — 공개하면 안 되는 NAS 경로 등)
- 사용자 변경값: 사용자 설정 폴더의 settings.yaml (앱에서 저장)
    Windows : %APPDATA%/airbot-log-analyzer/settings.yaml
    Linux   : ~/.config/airbot-log-analyzer/settings.yaml
"""
import copy
import os
import sys
from pathlib import Path

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT_DIR / "config"
LOCAL_SETTINGS = CONFIG_DIR / "settings.local.yaml"
APP_ID = "airbot-log-analyzer"

DEFAULTS = {
    "nas": {
        "root": "",
        "linux_path": "/mnt/nas_log",
    },
    "playback": {
        "idle_gap_sec": 5.0,
        "speed": 10,
    },
    "display": {
        "history_minutes": 10,
    },
    "analysis": {
        "claude_path": "",          # 비우면 PATH 의 claude
        "model": "",                # 비우면 Claude Code 기본 모델
        "timeout_sec": 900,
        "before_min": 10,           # 기준 시각 앞/뒤 분석 구간 (분)
        "after_min": 5,
        "max_lines": 1500,          # 보낼 발췌 최대 줄 수 / 글자 수
        "max_chars": 150000,
        "guideline_file": "",       # 다른 지침 파일 경로 (비우면 config/analysis/guideline.md)
        "mask_words": [],           # 추가 마스킹 단어 (현장명 등)
        "mask_patterns": [],        # 추가 마스킹 정규식
        "excel_name": "Airbot_{serial6}_{issue}_현상분석_{date}_AI.xlsx",
        "title": "Airbot",
    },
    "workspace": {
        "temp_root": "",
        # 압축 해제 시 건너뛸 경로(fnmatch 패턴). SoC 로그는 v1에서 사용하지 않음.
        "skip_members": ["soc/*"],
    },
}


def user_config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_ID


def user_cache_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / APP_ID


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_yaml(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}


class Settings:
    """기본값 + 레포 설정 + 이 PC 전용 설정 + 사용자 설정을 합친 설정."""

    def __init__(self, defaults_path: Path = None, user_path: Path = None, local_path: Path = None):
        self.defaults_path = defaults_path or CONFIG_DIR / "settings.yaml"
        self.local_path = local_path or self.defaults_path.with_name("settings.local.yaml")
        self.user_path = user_path or user_config_dir() / "settings.yaml"
        self.reload()

    def reload(self):
        self._user = _read_yaml(self.user_path)
        base = _deep_merge(DEFAULTS, _read_yaml(self.defaults_path))
        self.data = _deep_merge(_deep_merge(base, _read_yaml(self.local_path)), self._user)

    def get(self, dotted: str, default=None):
        cur = self.data
        for key in dotted.split("."):
            if not isinstance(cur, dict) or key not in cur:
                return default
            cur = cur[key]
        return cur

    def set_user(self, dotted: str, value):
        """사용자 설정 파일에 값 저장 (레포 파일은 건드리지 않음)."""
        keys = dotted.split(".")
        cur = self._user
        for key in keys[:-1]:
            cur = cur.setdefault(key, {})
        cur[keys[-1]] = value
        self.user_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.user_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self._user, f, allow_unicode=True, sort_keys=False)
        self.reload()

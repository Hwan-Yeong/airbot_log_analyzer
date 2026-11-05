"""Airbot Log Analyzer - 로봇 로그(AMR_LOG) 시각화/재생 분석기.

버전은 레포 루트의 version.ini 에서 읽는다.
"""
import configparser
from pathlib import Path

_ini = configparser.ConfigParser()
_ini.read(Path(__file__).resolve().parent.parent / "version.ini", encoding="utf-8")

APP_NAME = _ini.get("app", "name", fallback="Airbot Log Analyzer")
__version__ = _ini.get("app", "version", fallback="0.0.0")
RELEASE_DATE = _ini.get("app", "date", fallback="")

#!/usr/bin/env bash
# Airbot Log Analyzer 실행 (Linux / WSL)
#   최초 실행 시 .venv 를 만들고 requirements.txt 를 설치한다 (requirements 가 바뀌면 다시 설치).
#   사용: ./run.sh [로그 압축/폴더 ...]
set -e
cd "$(dirname "$0")"
unset PYTHONPATH            # ROS 등 외부 패키지 경로가 섞이지 않도록 격리
export PYTHONNOUSERSITE=1
PY="${PYTHON:-python3}"
STAMP=".venv/.requirements.sha1"
REQ_HASH="$(sha1sum requirements.txt | cut -d' ' -f1)"

if [ ! -x .venv/bin/python ] || [ "$(cat "$STAMP" 2>/dev/null)" != "$REQ_HASH" ]; then
  echo "[setup] .venv 준비 중 (최초 1회, 수 분 걸릴 수 있음)..."
  if ! "$PY" -m venv .venv; then
    echo "[setup] venv 생성 실패: sudo apt install python3-venv 후 다시 실행하세요." >&2
    exit 1
  fi
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -r requirements.txt
  echo "$REQ_HASH" > "$STAMP"
fi
exec .venv/bin/python run.py "$@"

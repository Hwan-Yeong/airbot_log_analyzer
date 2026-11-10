#!/usr/bin/env bash
# Windows 바탕화면·시작 메뉴에 실행 아이콘 만들기 (WSL)
#   ./scripts/install_shortcut.sh            만들기 (처음이면 .venv 준비부터, 수 분)
#   ./scripts/install_shortcut.sh --remove   지우기
set -e
cd "$(dirname "$0")/.."
if [ "${1:-}" != "--remove" ]; then
  ./run.sh --setup-only
fi
exec .venv/bin/python scripts/install_shortcut.py "$@"

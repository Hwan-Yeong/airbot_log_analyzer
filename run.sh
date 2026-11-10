#!/usr/bin/env bash
# Airbot Log Analyzer 실행 (Linux / WSL)
#   최초 실행 시 .venv 를 만들고 requirements.txt 를 설치한다 (requirements 가 바뀌면 다시 설치).
#   사용: ./run.sh [로그 압축/폴더 ...]      ./run.sh --setup-only  (.venv 준비만)
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
if [ "${1:-}" = "--setup-only" ]; then
  exit 0
fi
# WSL(WSLg) 은 기본 wayland 로 실행 (X11 에서는 WSLg 가 마우스 포인터를 표시하지 않음 - Tk 등 일반 X11 앱도 동일)
# X11 로 실행하려면: QT_QPA_PLATFORM=xcb ./run.sh  (필요한 라이브러리·커서 테마는 sudo 없이 .venv 에 자동 준비)
case "${QT_QPA_PLATFORM:-}" in
  *xcb*)
    if ! ldconfig -p 2>/dev/null | grep -q "libxcb-cursor.so.0"; then
      XCB_DIR=".venv/xcb-libs"
      if [ -z "$(find "$XCB_DIR" -name 'libxcb-cursor.so.0' 2>/dev/null | head -1)" ]; then
        ( mkdir -p "$XCB_DIR" && cd "$XCB_DIR" && apt-get download libxcb-cursor0 >/dev/null 2>&1 \
          && dpkg-deb -x libxcb-cursor0_*.deb . && rm -f libxcb-cursor0_*.deb ) || true
      fi
      LIB="$(find "$XCB_DIR" -name 'libxcb-cursor.so.0' 2>/dev/null | head -1)"
      [ -n "$LIB" ] && export LD_LIBRARY_PATH="$PWD/$(dirname "$LIB")${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    fi
    ICON_DIR="$PWD/.venv/xcb-libs/icons"
    if [ ! -e "$ICON_DIR/default/index.theme" ]; then
      T="$(mktemp -d)"
      ( cd "$T" && apt-get download dmz-cursor-theme >/dev/null 2>&1 && dpkg-deb -x dmz-cursor-theme_*.deb x \
        && mkdir -p "$ICON_DIR/default" && cp -r x/usr/share/icons/DMZ-White "$ICON_DIR/" \
        && printf '[Icon Theme]\nInherits=DMZ-White\n' > "$ICON_DIR/default/index.theme" ) || true
      rm -rf "$T"
    fi
    [ -e "$ICON_DIR/default/index.theme" ] && export XCURSOR_PATH="$ICON_DIR:${XCURSOR_PATH:-$HOME/.icons:/usr/share/icons:/usr/share/pixmaps}"
    ;;
esac
exec .venv/bin/python run.py "$@"

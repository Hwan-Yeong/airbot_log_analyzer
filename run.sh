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
# WSL(WSLg): wayland 에서는 Qt 드롭다운·메뉴 팝업이 가끔 바로 닫힘 → X11(xcb) 우선, 안 되면 wayland 로 자동 전환
if [ -n "${WSL_DISTRO_NAME:-}" ] && [ -z "${QT_QPA_PLATFORM:-}" ]; then
  export QT_QPA_PLATFORM="xcb;wayland"
  if ! ldconfig -p 2>/dev/null | grep -q "libxcb-cursor.so.0"; then
    XCB_DIR=".venv/xcb-libs"     # xcb 에 필요한 libxcb-cursor 를 sudo 없이 .venv 안에 준비
    if [ -z "$(find "$XCB_DIR" -name 'libxcb-cursor.so.0' 2>/dev/null | head -1)" ]; then
      ( mkdir -p "$XCB_DIR" && cd "$XCB_DIR" && apt-get download libxcb-cursor0 >/dev/null 2>&1 \
        && dpkg-deb -x libxcb-cursor0_*.deb . && rm -f libxcb-cursor0_*.deb ) || true
    fi
    LIB="$(find "$XCB_DIR" -name 'libxcb-cursor.so.0' 2>/dev/null | head -1)"
    if [ -n "$LIB" ]; then
      export LD_LIBRARY_PATH="$PWD/$(dirname "$LIB")${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    fi
  fi
  # X11 커서 테마 (WSL 에는 없어서 마우스 포인터가 안 보임) → DMZ-White 를 .venv 안에 준비해 default 로 사용
  ICON_DIR="$PWD/.venv/xcb-libs/icons"
  if [ ! -e "$ICON_DIR/default/index.theme" ]; then
    T="$(mktemp -d)"
    ( cd "$T" && apt-get download dmz-cursor-theme >/dev/null 2>&1 && dpkg-deb -x dmz-cursor-theme_*.deb x \
      && mkdir -p "$ICON_DIR/default" && cp -r x/usr/share/icons/DMZ-White "$ICON_DIR/" \
      && printf '[Icon Theme]\nInherits=DMZ-White\n' > "$ICON_DIR/default/index.theme" ) || true
    rm -rf "$T"
  fi
  if [ -e "$ICON_DIR/default/index.theme" ]; then
    export XCURSOR_PATH="$ICON_DIR:${XCURSOR_PATH:-$HOME/.icons:/usr/share/icons:/usr/share/pixmaps}"
  fi
fi
exec .venv/bin/python run.py "$@"

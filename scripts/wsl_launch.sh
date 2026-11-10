#!/usr/bin/env bash
# Windows 바로가기(WSL)로 실행할 때 쓰는 실행기 — 콘솔 창이 없으므로
#   처음 실행·업데이트 직후(패키지 설치로 오래 걸림)에는 Windows 알림으로 알리고, 실패하면 로그 위치를 알린다.
#   로그: ~/.cache/airbot-log-analyzer/launch.log
cd "$(dirname "$0")/.." || exit 1
LOG_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/airbot-log-analyzer"
LOG="$LOG_DIR/launch.log"
mkdir -p "$LOG_DIR"
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 1048576 ]; then
  mv -f "$LOG" "$LOG.1"
fi
notify() { python3 scripts/win_notify.py "$@" >/dev/null 2>&1 || true; }

if [ ! -x .venv/bin/python ] || [ "$(cat .venv/.requirements.sha1 2>/dev/null)" != "$(sha1sum requirements.txt | cut -d' ' -f1)" ]; then
  notify "Airbot Log Analyzer 준비 중" "처음 실행(또는 업데이트 직후)이라 필요한 패키지를 설치합니다. 몇 분 걸릴 수 있습니다."
fi
echo "=== $(date '+%F %T') 실행: $*" >>"$LOG"
./run.sh "$@" >>"$LOG" 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "=== 종료 코드 $rc" >>"$LOG"
  notify "Airbot Log Analyzer 실행 실패" "로그: $(wslpath -w "$LOG" 2>/dev/null || echo "$LOG")" error
fi
exit "$rc"

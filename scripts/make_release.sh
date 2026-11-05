#!/usr/bin/env bash
# 배포 파일 생성 (커밋된 파일만): ./scripts/make_release.sh [출력 폴더, 기본 dist]
#   → Airbot_Log_Analyzer_v<버전>.zip + Airbot_Log_Analyzer_사용안내.txt
set -e
cd "$(dirname "$0")/.."
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "커밋되지 않은 변경이 있습니다. 먼저 커밋하세요." >&2
  exit 1
fi
VER="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' airbotlog/__init__.py)"
REPO="$(git remote get-url origin 2>/dev/null | sed -e 's/\.git$//' -e 's#^git@\([^:]*\):#https://\1/#')"
OUT="${1:-dist}"
mkdir -p "$OUT"
ZIP="$OUT/Airbot_Log_Analyzer_v$VER.zip"
git archive --format=zip --prefix=Airbot_Log_Analyzer/ -o "$ZIP" HEAD
{ printf '\xEF\xBB\xBF'; sed 's/$/\r/' <<TXT
Airbot Log Analyzer v$VER 사용 안내
로봇 로그(AMR_LOG)를 맵 위에서 재생하며 주행 이력·상태·에러를 분석하는 도구입니다.

■ 설치 (Windows, 최초 1회)
1. Python 3.10 이상 설치: https://www.python.org/downloads/
   - 설치 첫 화면에서 "Add python.exe to PATH" 체크
2. Airbot_Log_Analyzer_v$VER.zip 을 내 PC로 복사해 압축 해제 (예: C:\\Tools)
   ※ NAS에서 바로 실행하지 마세요. 실행 폴더에 설치 파일(.venv)이 만들어집니다.
3. Airbot_Log_Analyzer\\run.bat 더블클릭
   - 처음 실행 시 필요한 패키지를 자동 설치합니다 (인터넷 필요, 3~5분)

■ 사용
- [NAS] 버튼 → 로그 압축 파일 더블클릭 (NAS 로그 폴더 자동 연결, 읽기 전용)
  · 목록이 안 나오면 Windows 탐색기에서 NAS에 한 번 접속(로그인) 후 다시 시도
- 또는 zip / AMR_LOG.tar.gz 파일을 창에 끌어다 놓기
- 상세 사용법: docs\\USER_GUIDE.md / 설치·문제 해결: docs\\SETUP.md

■ 업데이트
- 새 버전 zip 을 받아 기존 폴더를 지우고 다시 압축 해제
- 저장소: $REPO (git 사용 시: git pull 후 run.bat)

■ Linux / WSL: 압축 해제 후 ./run.sh
TXT
} > "$OUT/Airbot_Log_Analyzer_사용안내.txt"
echo "$ZIP"
echo "$OUT/Airbot_Log_Analyzer_사용안내.txt"

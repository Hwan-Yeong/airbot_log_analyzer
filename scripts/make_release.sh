#!/usr/bin/env bash
# 배포용 zip 생성 (커밋된 파일만): ./scripts/make_release.sh [출력 폴더, 기본 dist]
set -e
cd "$(dirname "$0")/.."
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "커밋되지 않은 변경이 있습니다. 먼저 커밋하세요." >&2
  exit 1
fi
VER="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' airbotlog/__init__.py)"
OUT="${1:-dist}"
mkdir -p "$OUT"
ZIP="$OUT/Airbot_Log_Analyzer_v$VER.zip"
git archive --format=zip --prefix=Airbot_Log_Analyzer/ -o "$ZIP" HEAD
echo "$ZIP"

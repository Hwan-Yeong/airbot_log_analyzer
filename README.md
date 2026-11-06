# Airbot Log Analyzer

로봇 로그(AMR_LOG)를 맵 위에서 재생하며 주행 이력·상태 변화·에러를 분석하는 도구입니다.

![빠른 배속 재생](docs/images/demo_play.gif)

<sub>AI로 생성한 데모 로그(32평형 집)를 빠른 배속으로 재생한 화면 — 기능 소개: **[docs/FEATURES.md](docs/FEATURES.md)**</sub>

## 주요 기능
- **압축 로그 바로 열기**: NAS 수집 zip, `AMR_LOG.tar(.gz)`, 폴더, `airbot_log*.txt` (끌어다 놓기 지원)
  - 임시 폴더에 풀고, 다른 로그를 열거나 종료하면 자동 삭제
- **NAS 연결**: 로그 폴더 탐색·검색 후 바로 분석 (읽기 전용: 목록 조회와 로컬 복사만)
- **맵 재생**: 로봇 위치·방향·궤적(선/포즈), 주행 목표, 장애물·낙하·에러 위치 마커
- **상태창**: 시각·상태·배터리·SW:FW:AI 버전·라이다, 자원 상태창(메모리·AP 온도) — 각각 켜고 끄기
- **재생 조작**: 배속(선택/입력), ±1/5/10/30초 이동, 이전/다음 이벤트, 시각 이동
- **이벤트 목록**: 상태 전이·SOC 명령·주행·에러·도킹·센서 등 분류/검색, 클릭 시 해당 시점으로 이동
- **원본 로그 동기화**: 현재 시점 줄 강조, 전체 검색, 더블클릭 이동
- **에러/요약**: 시점별 활성 에러, 발생/해제 이력, 에러 코드별 집계, 부팅·주행·도킹 통계
- **AI 현상 분석 [분석]**: 요청(구어체/메일) + airbot_log·otalog·journal·SoC logcat 통합 타임라인 → Claude Code → '현상'까지 정리, 엑셀 분석표
  - 분석 구간 발췌만, 시리얼·이름·연락처 마스킹, 보내기 전 미리보기
- **규칙 파일**: `config/rules.yaml` 수정만으로 이벤트·마커 추가
- **라이트/다크 테마**: 툴바 오른쪽 해/달 아이콘

## 빠른 시작
| OS | 실행 |
|---|---|
| Windows | `run.bat` 더블클릭 |
| Linux / WSL | `./run.sh` |

처음 실행할 때 `.venv`를 만들고 필요한 패키지를 설치합니다 (수 분). 자세한 내용은 [설치 및 NAS 연결](docs/SETUP.md).

**데모로 바로 해보기**: [`examples/demo`](examples/demo/) 의 zip(AI 로 생성한 가상 로그)을 창에 끌어다 놓고 재생.

## 문서
- [기능 소개](docs/FEATURES.md)
- [설치 및 NAS 연결](docs/SETUP.md)
- [사용 가이드](docs/USER_GUIDE.md)
- [변경 이력](CHANGELOG.md)

## 개발
- 구조: `airbotlog/` (압축·NAS·파싱·모델), `airbotlog/analysis/` (분석: 다중 로그·발췌·마스킹·Claude·엑셀), `airbotlog/ui/` (PySide6 화면), `config/` (설정·규칙·분석 지침)
- 테스트: `.venv/bin/pip install pytest` 후 `.venv/bin/python -m pytest tests` (데모 로그 자동 생성, UI 테스트 포함)
- 데모 로그: `python scripts/make_demo_home.py` (32평형 집 시나리오 → `examples/demo`), `python scripts/make_demo_data.py <출력 폴더>` (테스트·분석 예시용)
- 문서 캡처: `python scripts/capture_docs.py <로그 압축> [--nas] [--analysis docs/examples/analysis_demo.json] [--gif docs/images/demo_play.gif]` (GIF 는 `pip install pillow`)
- 이 PC 전용 파일(git 제외): `config/settings.local.yaml`, `config/analysis/guideline.local.md`·`system_prompt.local.md` 가 있으면 레포 파일 대신 사용. 실제 로그·맵은 `local/` 에 두기
- 릴리스: 버전(`version.ini`) · `CHANGELOG.md` · 문서 캡처를 함께 갱신, 배포 파일은 `scripts/make_release.sh`

# 설치 및 NAS 연결

## 1. 설치·실행
필요: Python 3.10 이상, 인터넷 (최초 패키지 설치 시)

| OS | 준비 | 실행 |
|---|---|---|
| Windows 10/11 | [python.org](https://www.python.org/downloads/)에서 Python 설치 | `run.bat` 더블클릭 |
| WSL (Ubuntu) | 기본 python3 | `./run.sh` |
| Ubuntu 22.04 이상 | `sudo apt install python3-venv libxcb-cursor0` | `./run.sh` |

- 실행 스크립트가 `.venv`를 만들고 `requirements.txt`를 설치합니다.
- 로그를 인자로 주면 바로 엽니다: `./run.sh 로그.zip`
- 직접 설치: `pip install -r requirements.txt` 후 `python run.py`

## 2. NAS 연결 (읽기 전용)
기본 경로 (`config/settings.yaml`):
`\\<NAS 서버>\<공유 폴더>\<로그 폴더>`

| 환경 | 방법 |
|---|---|
| Windows | 탐색기에서 NAS에 로그인되어 있으면 바로 사용 |
| WSL | 설정 불필요. Windows PowerShell을 거쳐 접근 (Windows 쪽 NAS 로그인 필요) |
| Ubuntu | 아래처럼 읽기 전용(`ro`)으로 마운트하면 `/mnt/nas_log`를 자동 사용 |

```bash
sudo apt install cifs-utils
sudo mkdir -p /mnt/nas_log
sudo mount -t cifs "//<NAS 서버>/<공유 폴더>/<로그 폴더>" \
  /mnt/nas_log -o ro,username=<NAS 계정>,uid=$(id -u),gid=$(id -g),iocharset=utf8
```

- 경로가 다르면 툴바 **[NAS 경로]**에서 바꿉니다 (사용자 설정에 저장).
- 앱은 NAS에서 **목록 조회와 로컬 임시 폴더로 복사만** 합니다. NAS에 쓰거나 지우지 않습니다.

## 3. 설정 파일
| 파일 | 내용 |
|---|---|
| `config/settings.yaml` | 기본 설정 (NAS 경로, 재생, 임시 폴더) |
| `config/rules.yaml` | 이벤트·마커 규칙 |
| `config/analysis/` | [분석] 지침(`system_prompt.md`)·응답 형식·분석용 이벤트 규칙 |
| 사용자 설정 | Windows `%APPDATA%\airbot-log-analyzer\settings.yaml` / Linux `~/.config/airbot-log-analyzer/settings.yaml` |

## 4. 문제 해결
| 증상 | 해결 |
|---|---|
| 시작 시 "PySide6 … 동작 오류" | `run.sh`/`run.bat`로 실행하거나 `pip install "PySide6<6.12"` (PySide6 6.12 + Python 3.11 이하 버그) |
| `Could not load the Qt platform plugin "xcb"` | `sudo apt install libxcb-cursor0` |
| WSL 에서 드롭다운이 가끔 한 번에 안 열림 | 다시 클릭 (WSLg wayland 팝업 문제, 개선 진행 중). `QT_QPA_PLATFORM=xcb ./run.sh` 로 X11 실행 시 해결되나 WSLg 에 따라 마우스 포인터가 안 보일 수 있음 |
| NAS 접근 오류 | Windows 탐색기에서 NAS 경로를 한 번 열어 로그인 / Ubuntu는 마운트 확인 |
| 임시 폴더 위치 | OS 임시 폴더의 `airbotlog_*` (종료 시 삭제, 비정상 종료분은 다음 실행 때 정리) |
| [분석] "Claude Code CLI 를 찾지 못했습니다" | 아래 5절대로 설치 후 터미널에서 `claude` 를 한 번 실행해 로그인 |
| 프로그램이 갑자기 종료됨 | 사용자 캐시 폴더의 `crash.log` 전달 (Windows `%LOCALAPPDATA%\airbot-log-analyzer\`, Linux `~/.cache/airbot-log-analyzer/`) |

## 5. 분석 기능 (Claude Code)
[분석]은 이 PC 에 설치된 **Claude Code CLI**(`claude`)를 호출합니다. 로그인된 Claude Code 가 필요합니다.

| OS | 설치 |
|---|---|
| Windows | PowerShell: `irm https://claude.ai/install.ps1 \| iex` |
| Linux / WSL | `curl -fsSL https://claude.ai/install.sh \| bash` |

설치 후 터미널에서 `claude` 를 한 번 실행해 로그인합니다. 다른 위치에 설치했다면 사용자 설정에 경로를 적습니다.

```yaml
analysis:
  claude_path: 'C:\Users\<사용자>\.local\bin\claude.exe'   # 비우면 PATH 에서 찾음
  model: ''                 # 비우면 Claude Code 기본 모델
  guideline_file: ''        # 다른 지침 파일(예: 최신본) 경로. 비우면 config/analysis/guideline.md
  mask_words: [현장명, 고객사]  # 항상 마스킹할 단어
```

- 호출 방식: 도구 없음(`--tools ""`), 개인 설정·메모리 미사용(`--safe-mode`), 세션 기록 안 남김, 빈 임시 폴더에서 실행 → 앱이 보낸 텍스트만 봅니다.
- 보내기 전에 전체 내용을 미리보기로 확인합니다. 원래 값(시리얼·이름 등)은 PC 에만 남고 결과를 표시할 때 복원합니다.
- 분석 지침은 `config/analysis/guideline.md` 전문을 매번 함께 보냅니다 (지침을 고치면 다음 분석부터 반영).
- Claude Code 가 API 키(`ANTHROPIC_API_KEY`)로 인증돼 있으면 크레딧이 필요합니다 ("Credit balance is too low" 오류 시 충전 또는 다른 계정으로 로그인).

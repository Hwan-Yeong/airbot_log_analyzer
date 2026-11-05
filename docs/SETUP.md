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
| 사용자 설정 | Windows `%APPDATA%\airbot-log-analyzer\settings.yaml` / Linux `~/.config/airbot-log-analyzer/settings.yaml` |

## 4. 문제 해결
| 증상 | 해결 |
|---|---|
| 시작 시 "PySide6 … 동작 오류" | `run.sh`/`run.bat`로 실행하거나 `pip install "PySide6<6.12"` (PySide6 6.12 + Python 3.11 이하 버그) |
| `Could not load the Qt platform plugin "xcb"` | `sudo apt install libxcb-cursor0` |
| NAS 접근 오류 | Windows 탐색기에서 NAS 경로를 한 번 열어 로그인 / Ubuntu는 마운트 확인 |
| 임시 폴더 위치 | OS 임시 폴더의 `airbotlog_*` (종료 시 삭제, 비정상 종료분은 다음 실행 때 정리) |
| 프로그램이 갑자기 종료됨 | 사용자 캐시 폴더의 `crash.log` 전달 (Windows `%LOCALAPPDATA%\airbot-log-analyzer\`, Linux `~/.cache/airbot-log-analyzer/`) |

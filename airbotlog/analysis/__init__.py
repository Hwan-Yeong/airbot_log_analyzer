"""AI 현상 분석: 다중 로그 타임라인 → 발췌·마스킹 → Claude Code CLI → 결과 검증 → 엑셀.

모듈
- sources : airbot_log 외 로그(otalog, journal/dmesg 스냅샷, SoC logcat) 찾기·읽기, 시각 정렬
- context : 분석 구간 발췌(반복 접기·분량 제한)와 Claude 에 보낼 입력 블록 만들기
- request : 요청 문장/메일(.eml) 읽기, 시리얼·시각·에러 코드 추출
- masking : 시리얼·이름·연락처 등 치환(전송 전)과 복원(응답 후)
- claude  : Claude Code CLI 호출과 응답 검증
- excel   : 현상 분석표(xlsx) 저장
"""

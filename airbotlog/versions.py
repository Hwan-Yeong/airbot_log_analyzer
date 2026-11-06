"""SW:FW:AI 버전 시간축 (HUD·요약 표시용).

1) airbot_log 'Send to SocSoftware Version : SW:FW:AI' (SoC 로 보내는 버전, 규칙 soc_sw_version → track 'version')
2) otalog 의 OTA 기록: 'OTA Version : 목표' … 'OTA process completed successfully.' → 완료 시각부터 그 버전
   (OTA 시작 때의 'Current Version : 이전' 은 그 시각의 버전)
두 소스를 로그 시각으로 합쳐, 재생 위치마다 그 시점에 적용된 버전을 보여준다.
airbot_log 가 회전으로 잘려 버전 줄이 없어도 otalog 의 마지막 OTA 완료 버전으로 채워진다.
"""
import bisect
import calendar
import re

REAL_TIME_FROM = calendar.timegm((2025, 1, 1, 0, 0, 0, 0, 0, 0))   # 이보다 이른 로그 시각은 RTC 초기값 (분석 컨텍스트와 같은 기준)
OTA_TARGET_RE = re.compile(r"OTA Version : (\S+)")
OTA_CURRENT_RE = re.compile(r"Current Version : (\S+)")
OTA_DONE = "OTA process completed successfully"
VALID_RE = re.compile(r"^\d+\.\d+:\d+\.\d+:\d+\.\d+$")


def _valid(v: str) -> bool:
    return bool(VALID_RE.match(v)) and ":0.0" not in v


def ota_version_events(session):
    """otalog → [(로그 시각, 버전, 종류)] (종류: ota=OTA 완료, current=OTA 직전 버전)."""
    from .analysis import sources as srcs
    try:
        sset = srcs.discover(session)
    except (OSError, ValueError):
        return []
    out, target = [], None
    for ref in sset.ota:
        st = srcs.FileStat(srcs.SRC_OTA, ref.name)
        try:
            for rec in srcs.read_ota(ref, st):
                m = OTA_TARGET_RE.search(rec.msg)
                if m:
                    target = m.group(1)
                    continue
                m = OTA_CURRENT_RE.search(rec.msg)
                if m and _valid(m.group(1)):
                    out.append((rec.t, m.group(1), "current"))
                    continue
                if OTA_DONE in rec.msg and target and _valid(target):
                    out.append((rec.t, target, "ota"))
                    target = None
        except (OSError, EOFError):
            continue
    out.sort(key=lambda e: e[0])
    return out


def apply_versions(S):
    """track 'version' 에 otalog OTA 버전을 시각에 맞춰 합치고 stats['versions'] 를 채운다."""
    events = ota_version_events(S)
    vts, vals = S.tracks.get("version", ([], []))
    points = list(zip(vts, vals))
    if events and S.tl_t:
        real = [t for t in S.tl_t if t >= REAL_TIME_FROM]                 # RTC 초기값(과거 고정 시각) 제외
        t0 = min(real) if real else S.tl_t[0]
        before = [e for e in events if e[0] <= t0]
        if before:                                         # 로그 시작 전 마지막 OTA 결과 → 처음부터 적용
            points.append((0.0, before[-1][1]))
        order = sorted(range(len(S.tl_t)), key=lambda k: S.tl_t[k])
        sorted_t = [S.tl_t[k] for k in order]
        for t, v, _kind in events:
            if t <= t0 or t < REAL_TIME_FROM:
                continue
            k = bisect.bisect_left(sorted_t, t)
            if k >= len(order):
                continue                                   # 로그 끝 이후
            points.append((S.tl_vt[order[k]], v))
    if points:
        points.sort(key=lambda p: p[0])
        S.tracks["version"] = ([p[0] for p in points], [p[1] for p in points])
    versions = []
    for v in S.tracks.get("version", ([], []))[1]:
        if v not in versions:
            versions.append(v)
    S.stats["versions"] = versions
    S.stats["ota_versions"] = [(t, v, k) for t, v, k in events]
    return S

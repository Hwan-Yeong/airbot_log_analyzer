#!/usr/bin/env python3
"""데모 로그 — 32평형 집. AI 로 생성한 가상의 로봇 주행 로그입니다 (실제 로봇·고객·현장 데이터 아님).

    python scripts/make_demo_home.py [출력 폴더]          (기본: examples/demo)
    → <출력 폴더>/20261103150000_DEMOBOTHOME0000001_AI_GENERATED_DEMO.zip
      (amr/AMR_LOG.tar.gz + soc/logs, NAS 수집본과 같은 구조 — 앱에 끌어다 놓으면 바로 재생)

시나리오 (충전 스테이션 + 목적지 4곳):
  충전 중(ONSTATION) → 주행 시작 → ① 주방 도착
  → ② 작은방2 가는 길에 카메라 장애물 감지 → 회피 주행 → 도착
  → ③ 안방 가는 길에 1D ToF 장애물 감지 → 회피 주행 → 도착
  → ④ 작은방1 문턱에서 단차 감지 → 재시도 2회 실패 → 도착 실패(S05)
  → 충전 복귀 (복귀 경로는 뚫려 있음) → 도킹 완료
"""
import calendar
import gzip
import io
import json
import math
import random
import sys
import tarfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_demo_data import OTA_HISTORY, Log  # noqa: E402

NOTICE = "AI로 생성된 demo 용 로그파일입니다 (실제 로봇·고객·현장 데이터가 아닌 가상 로그)"
SERIAL = "DEMOBOTHOME0000001"
ZIP_NAME = f"20261103150000_{SERIAL}_AI_GENERATED_DEMO.zip"
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "examples" / "demo"

# ---------------------------------------------------------------- 맵 (32평형 아파트, 약 12.0 x 8.6 m)
RES = 0.05
ORIGIN = (-5.0, -3.4)
W, H = 270, 204                      # 13.5 m x 10.2 m
WALL = 0.1
STATION = (0.0, 0.0, 0.0)            # 도킹된 로봇 자세 (거실 서쪽 벽)
OUTER = (-4.25, -2.6, 7.75, 6.0)
# 수평 벽 (y, x0, x1, 문[(x0, x1)])
H_WALLS = [(-2.6, -4.25, 7.75, []), (6.0, -4.25, 7.75, []),
           (0.9, -4.25, -0.25, [(-1.65, -0.75)]),                  # 작은방1 | 복도 (작은방1 문: 문턱 단차)
           (2.3, -4.25, -0.25, [(-3.95, -3.25), (-1.75, -0.85)]),  # 복도 | 욕실1 · 작은방2
           (2.4, 4.15, 7.75, [(6.35, 7.05)]),                      # 안방 | 욕실2
           (3.8, 4.15, 5.8, [])]                                   # 드레스룸 | 현관
# 수직 벽 (x, y0, y1, 문[(y0, y1)])
V_WALLS = [(-4.25, -2.6, 6.0, []), (7.75, -2.6, 6.0, []),
           (-0.25, -2.6, 6.0, [(0.95, 2.25)]),                     # 작은방1·복도·작은방2 | 거실·주방 (복도 입구)
           (-2.6, 2.3, 6.0, []),                                   # 욕실1 | 작은방2
           (4.15, -2.6, 6.0, [(1.15, 2.1), (3.9, 5.1)]),           # 거실·주방 | 안방(문) · 현관(입구)
           (5.8, 2.4, 6.0, [])]                                    # 드레스룸·현관 | 욕실2
FURNITURE = [(0.9, -2.45, 3.1, -1.75),      # 거실 소파
             (3.7, -1.9, 4.0, -0.2),        # TV장
             (-0.17, 5.4, 3.7, 5.92),       # 주방 상판
             (3.4, 4.9, 4.07, 5.92),        # 냉장고
             (-4.17, -2.52, -2.5, -0.5),    # 작은방1 침대
             (-1.4, -2.52, -0.33, -1.95),   # 작은방1 책상
             (-2.52, 4.4, -1.25, 5.92),     # 작은방2 침대
             (-0.85, 4.2, -0.33, 5.92),     # 작은방2 옷장
             (5.6, -1.9, 7.67, 0.3),        # 안방 침대
             (4.23, -2.52, 4.8, -0.6)]      # 안방 옷장
LEGS = [(1.3, 3.3), (2.5, 3.3), (1.3, 4.1), (2.5, 4.1),      # 식탁 다리
        (1.5, -1.1), (2.5, -1.1), (1.5, -0.6), (2.5, -0.6)]  # 거실 테이블 다리
KEEPOUTS = [[(-4.17, 2.38), (-2.68, 2.38), (-2.68, 5.92), (-4.17, 5.92)],   # 욕실1
            [(4.23, 3.88), (5.72, 3.88), (5.72, 5.92), (4.23, 5.92)]]       # 현관 (신발 공간 단차)

# 목적지 (x, y, yaw°)
P1 = (2.9, 4.6, 90.0)      # 주방
P2 = (-1.5, 3.4, 90.0)     # 작은방2
P3 = (6.1, 1.2, 0.0)       # 안방
P4 = (-2.0, -0.2, 180.0)   # 작은방1
DOOR_STEP = (-1.2, 0.9)    # 작은방1 문턱 (단차)
CAM_OBS = (1.75, 2.2)      # 주방→작은방2 사이 바닥의 물건 (카메라 감지)
TOF_OBS = (1.95, 1.62)     # 거실 통로의 낮은 상자 (1D ToF 감지)


def make_map_pgm() -> bytes:
    free, occ, unknown = 254, 0, 205
    img = [[unknown] * W for _ in range(H)]

    def px(x, y):
        return int(round((x - ORIGIN[0]) / RES)), H - 1 - int(round((y - ORIGIN[1]) / RES))

    def fill(x0, y0, x1, y1, v):
        (a, b), (c, d) = px(x0, y0), px(x1, y1)
        for r in range(max(min(b, d), 0), min(max(b, d), H - 1) + 1):
            for col in range(max(min(a, c), 0), min(max(a, c), W - 1) + 1):
                img[r][col] = v

    fill(*OUTER, free)
    h = WALL / 2
    for y, x0, x1, doors in H_WALLS:
        edges = [x0] + [v for d in doors for v in d] + [x1]
        for a, b in zip(edges[::2], edges[1::2]):
            fill(a - h, y - h, b + h, y + h, occ)
    for x, y0, y1, doors in V_WALLS:
        edges = [y0] + [v for d in doors for v in d] + [y1]
        for a, b in zip(edges[::2], edges[1::2]):
            fill(x - h, a - h, x + h, b + h, occ)
    e = RES                                              # 가구: 라이다가 보는 외곽선만, 안쪽은 미탐색
    for x0, y0, x1, y1 in FURNITURE:
        fill(x0, y0, x1, y1, unknown)
        for box in ((x0, y0, x1, y0 + e), (x0, y1 - e, x1, y1), (x0, y0, x0 + e, y1), (x1 - e, y0, x1, y1)):
            fill(*box, occ)
    for x, y in LEGS:
        fill(x - 0.02, y - 0.02, x + 0.02, y + 0.02, occ)
    rng = random.Random(3)
    for _ in range(40):                                  # 약간의 스캔 잡음 (벽 근처)
        x, y = rng.uniform(OUTER[0], OUTER[2]), rng.uniform(OUTER[1], OUTER[3])
        c, r = px(x, y)
        near = any(img[rr][cc] == occ for rr in range(max(r - 2, 0), min(r + 3, H))
                   for cc in range(max(c - 2, 0), min(c + 3, W)))
        if near and img[r][c] == free:
            img[r][c] = occ
    return f"P5\n{W} {H}\n255\n".encode() + bytes(v for row in img for v in row)


def map_files() -> dict:
    area = {"keepout_zones": [{"id": f"demo-home-zone-{i + 1}", "coordinates": [{"x": x, "y": y} for x, y in z]}
                              for i, z in enumerate(KEEPOUTS)]}
    return {
        "map/airbot_map_00.pgm": make_map_pgm(),
        "map/airbot_map_00.yaml": (f"image: airbot_map_00.pgm\nmode: trinary\nresolution: {RES}\n"
                                   f"origin: [{ORIGIN[0]}, {ORIGIN[1]}, 0]\nnegate: 0\noccupied_thresh: 0.65\n"
                                   "free_thresh: 0.196").encode(),
        "map/area.json": json.dumps(area, indent=4).encode(),
        "map/wall.json": json.dumps({"keepout_zones": []}, indent=4).encode(),
        "map/station_pose.json": json.dumps({"station_pose": {"theta": STATION[2], "x": STATION[0],
                                                              "y": STATION[1]}}, indent=4).encode(),
    }


# ---------------------------------------------------------------- 로그
class HomeLog(Log):
    """make_demo_data.Log + 실제 로봇 로그 순서를 따른 주행 절차 (센서 켜기 → 목표 전송 → 도착/실패)."""

    def __init__(self, start):
        super().__init__(start)
        self.batt = 96
        self.step = 0
        self.goal_t = start
        self.marks = {}                       # SoC 로그를 맞출 기준 시각

    def mark(self, key):
        self.marks.setdefault(key, self.now)

    def go(self, path, speed=0.32, dt=0.6, yaw=None, on_reach=None):
        """경유점을 따라 이동: dt 초마다 Costmap check, 2번에 1번 RobotPose. yaw 고정 = 후진."""
        for k, ((x0, y0), (x1, y1)) in enumerate(zip(path, path[1:]), 1):
            dist = math.hypot(x1 - x0, y1 - y0)
            if dist < 1e-6:
                continue
            self.yaw = yaw if yaw is not None else math.degrees(math.atan2(y1 - y0, x1 - x0))
            n = max(int(round(dist / (speed * dt))), 1)
            for i in range(1, n + 1):
                self.x, self.y = x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n
                self.step += 1
                self.emit("A1_maneuver", f"[Costmap check] Robots({self.x:.2f}, {self.y:.2f}) cost(0), static(0)", dt=dt)
                if self.step % 2 == 0:
                    self.pose()
            if on_reach:
                on_reach(self, k)
        self.pose()

    def sensors_on(self):
        self.emit("state_manager", "OffSensorTimer is already disabled", dt=0.02)
        self.lidar_on()
        self.emit("state_manager", "publishMultiTofOn")
        self.emit("state_manager", "publishCameraOn")
        self.emit("state_manager", "publishAllSensorOn")
        self.emit("airbot_ai_interface_node", "cameraOn ", dt=0.01)
        self.emit("state_manager", "[cameraCallback]camera Sensor is ON", dt=0.06)
        self.emit("state_manager", "[monitor_sensor] All SenSor is ON", dt=0.04)

    def send_goal(self, goal, retry=0):
        x, y, yaw = goal
        if not retry:
            self.navi("READY")
            self.emit("state_manager", "SET ROBOT_STATUS STATUS : START")
            self.emit("state_manager", "publishManeuverOn", dt=0.3)
            self.emit("A1_perception", "[Perception] ON")
            self.emit("A1_maneuver", "[Maneuver] ON , State init")
        self.emit("state_manager", "publishClearCostMap", dt=0.4)
        self.emit("A1_localization", "Costmap clear service called successfully.")
        self.emit("A1_perception", "Perception's all layers cleared by UDP command.")
        self.emit("state_manager", f"[setMoveGoalPose] ({x:.2f},{y:.2f},{yaw:.2f}(DEG))", dt=0.6)
        self.emit("state_manager", f"[moveToTarget] robot ({self.x:.2f}, {self.y:.2f}, {self.yaw:.2f}(DEG)) "
                                   f"send goal({x:.2f}, {y:.2f}, {yaw:.2f}(DEG))")
        self.emit("state_manager", f"[goalResponseCallback] Goal accepted. robot({self.x:.2f}, {self.y:.2f}, "
                                   f"{self.yaw:.2f}(DEG)), goal({x:.2f}, {y:.2f}, {yaw:.2f}(DEG)) response time[0.00]")
        self.emit("bt_navigator", f"Begin navigating from current location ({self.x:.2f}, {self.y:.2f}) "
                                  f"to ({x:.2f}, {y:.2f})")
        self.emit("planner_server", f"Create path to original destination ({x:.6f},{y:.6f}).", dt=0.01)
        self.emit("controller_server", "Received a goal, begin computing control effort.", dt=0.02)
        if not retry:
            self.navi("MOVE_GOAL")
            self.emit("udp_interface", "setSocMovingState Status prev[READY]-->new[MOVE_GOAL]", dt=0.06)

    def move_cmd(self, goal, undock=False):
        """SoC 이동 명령 수신 → (충전 중이면 센서 켜고 언도킹) → 목표 전송."""
        x, y, yaw = goal
        self.emit("udp_interface", f"soc-cmd received : Move to Target ({x:.1f}, {y:.1f}, {yaw:.1f}(deg))")
        self.mark(f"move:{x:.1f}, {y:.1f}, {yaw:.1f}")
        self.emit("udp_interface", f"setSocTargetPosition ({x:.1f}, {y:.1f}, {yaw:.1f}(deg))", dt=0.1)
        self.emit("udp_interface", f"publishMoveGoal X : {x:.6f}, Y : {y:.6f}")
        self.emit("state_manager", "resetTryMoveTargetCount")
        self.emit("state_manager", f"move_target_callback({x:.2f},{y:.2f},{yaw:.2f}(DEG))")
        self.goal_t = self.now
        if undock:
            self.state("ONSTATION", "UNDOCKING", soc="START_NAVIGATION")
            self.emit("state_manager", "[setOnstationStatus] Off-Station docking-Status hex[0e]", dt=0.3)
            self.sensors_on()
            self.x, self.y, self.yaw = STATION
            self.go([(0.0, 0.0), (0.6, 0.0)], speed=0.15)
            self.state("UNDOCKING", "NAVIGATION", robot="UNDOCKING_DONE_START_NAVIGATION")
        self.send_goal(goal)

    def arrive(self, goal):
        x, y, yaw = goal
        self.yaw = yaw
        self.emit("controller_server", "Reached the goal!", dt=0.4)
        self.emit("bt_navigator", "Goal succeeded", dt=0.03)
        sec = (self.now - self.goal_t).total_seconds()
        self.emit("state_manager", f"[goalResultCallback] Goal reached successfully. robot({self.x:.2f}, {self.y:.2f}, "
                                   f"{self.yaw:.2f}(DEG)), goal({x:.2f}, {y:.2f}, {yaw:.2f}(DEG)) moving time[{sec:.2f}]")
        self.navi("ARRIVED_GOAL")
        self.emit("udp_interface", "setSocMovingState Status prev[MOVE_GOAL]-->new[ARRIVED_GOAL]", dt=0.25)
        self.emit("udp_interface", "SocMoving State: Send to Soc MovingState(2) fail_reason(0), On-demand ", dt=0.1)
        self.pose()

    def abort(self, goal, count, over=False):
        x, y, yaw = goal
        self.emit("controller_server", "Failed to make progress", "ERROR", dt=0.3)
        self.emit("controller_server", "[follow_path] [ActionServer] Aborting handle.", "WARN")
        self.emit("bt_navigator", "[navigate_to_pose] [ActionServer] Aborting handle.", "WARN", dt=0.02)
        self.emit("bt_navigator", "Goal failed", "ERROR")
        sec = (self.now - self.goal_t).total_seconds()
        self.emit("state_manager", f"[goalResultCallback] Goal aborted. robot({self.x:.2f}, {self.y:.2f}, {self.yaw:.2f}"
                                   f"(DEG)), goal({x:.2f}, {y:.2f}, {yaw:.2f}(DEG)) moving time[{sec:.2f}]", "WARN")
        if not over:
            self.emit("state_manager", f"[checkRetryMoveGoal] retry move goal reason[GOAL_ABORT], count[{count}]")
            return
        self.emit("state_manager", f"[checkRetryMoveGoal] retry over Fail reason[GOAL_ABORT], count[{count}]")
        self.navi("FAIL")
        self.emit("state_manager", "SET NAVI_FAIL_REASON : GOAL_ABORT")
        self.emit("state_manager", "resetTryMoveTargetCount")
        self.emit("udp_interface", "setSocMovingState Status prev[MOVE_GOAL]-->new[FAIL]", dt=0.04)
        self.emit("udp_interface", "setNaviFailReason prev[VOID]-->new[GOAL_ABORT]")
        self.emit("state_manager", "publishMoveFailError", dt=0.06)
        self.emit("state_manager", "SET ROBOT_STATUS STATUS : FAIL")
        self.emit("udp_interface", "SocMoving State: Send to Soc MovingState(4) fail_reason(3), On-demand ", dt=0.05)
        self.emit("airbot_error_manager", "Error: [S05 (OCCURED): Unable to reach target area] ", dt=0.7)
        self.mark("s05_on")
        self.emit("udp_interface", "[errorListCallback] occurred error_code = S05")
        self.emit("udp_interface", "[setSocError] add: 1 (1:occured, 2:released), error_code = S05")

    def step_detect(self, sensor="L[3]", far=262, base=210):
        """문턱 단차: 바닥 ToF far 발화 → 속도 제한 → 낮은 장애물로 표시."""
        ratio = far / base
        self.emit("A1_maneuver", f"[DROP IR PROTECT] 단차 far 발화: {sensor} {far}mm (baseline {base}mm, x{ratio:.2f})",
                  "WARN", dt=0.1)
        self.emit("A1_maneuver", f"[DROP IR PROTECT] 바닥 ToF 위험 1개(void 0, far 1) {sensor}={far}/{base}mm"
                                 f"(x{ratio:.2f}) 감지 → 전진 최고속도 0.25 m/s 로 제한", "WARN", dt=0.04)
        self.emit("A1_perception", f"low level obstacle. robot_xy({self.x:.3f}, {self.y:.3f}), "
                                   f"Obs(x:{DOOR_STEP[0] + 0.02:.3f}, y:{DOOR_STEP[1]:.3f}, Dist diff: 0.052)", dt=0.2)
        self.emit("A1_maneuver", f"[Costmap check] Robots({self.x:.2f}, {self.y:.2f}) cost(253), static(0)", dt=0.4)


def make_log_lines():
    L = HomeLog(datetime(2026, 11, 3, 13, 58, 0))
    L.emit("demo_notice", "※ " + NOTICE)
    L.boot()
    L.emit("udp_interface", "soc-cmd received : reqSetTimeData year : 2026, month : 11, day : 3, hour : 13, "
                            "minute : 58, second : 14", dt=10)            # 페이로드 = SoC 시각 (SoC 시계가 약 2초 빠름)
    L.mark("sync")
    L.soc_connected()
    L.emit("state_manager", "SET ROBOT_STATE STATE : ONSTATION", dt=1)
    L.emit("state_manager", "[setOnstationStatus] OnStation docking-Status hex[16]")
    L.emit("scan_monitor", "[checkScanHz] scan state is front[OFF], back[OFF]")
    L.battery(charging=True)
    L.idle(10, charging=True)

    # ① 주방 (주행 시작)
    L.now += timedelta(seconds=38)
    L.move_cmd(P1, undock=True)
    L.go([(0.6, 0.0), (1.6, 1.3), (3.2, 2.6), (P1[0], P1[1])])
    L.arrive(P1)
    L.batt -= 1
    L.battery()

    # ② 작은방2: 식탁 옆 바닥의 물건을 카메라가 감지 → 아래쪽으로 돌아감
    L.now += timedelta(seconds=95)
    L.move_cmd(P2)

    def camera(log, k):
        if k == 1:                                                   # (3.1, 2.7) → 다음 구간에서 물건 발견
            for j in range(3):
                log.emit("airbot_sensor_to_pointcloud", "================ [UPDATE] ================", dt=0.4)
                log.emit("airbot_sensor_to_pointcloud",
                         f"[ID]: 7, [Position (X, Y): ({CAM_OBS[0] + 0.01 * j:.3f}, {CAM_OBS[1] - 0.01 * j:.3f})], "
                         "[Size (W, H): (0.420, 0.300)]")
            log.emit("controller_server", "Passing new path to controller.", dt=0.2)
    L.go([(P1[0], P1[1]), (3.1, 2.7), (2.4, 1.5), (1.2, 1.3), (0.4, 1.6), (-0.6, 1.6), (-1.3, 1.65), (-1.3, 2.6),
          (P2[0], P2[1])], on_reach=camera)
    L.arrive(P2)

    # ③ 안방: 거실 통로의 낮은 상자를 1D ToF 가 감지 → 감속·정지 후 옆으로 돌아감
    L.now += timedelta(seconds=80)
    L.move_cmd(P3)

    def tof(log, k):
        if k == 4:                                                   # (1.6, 1.6) 에서 앞쪽 0.3 m 감지
            for d in (0.36, 0.31):
                log.emit("A1_perception", f"1D ToF detected. robot_xy({log.x:.3f}, {log.y:.3f}), "
                                          f"1D(x:{TOF_OBS[0]:.3f}, y:{TOF_OBS[1]:.3f}, Dist:{d:.3f})", dt=0.5)
            log.emit("A1_maneuver", f"[Costmap check] Robots({log.x:.2f}, {log.y:.2f}) cost(128), static(0)", dt=0.3)
            log.emit("controller_server", "Passing new path to controller.", dt=0.4)
    L.go([(P2[0], P2[1]), (-1.3, 2.6), (-1.3, 1.65), (-0.6, 1.6), (1.6, 1.6), (1.9, 1.05), (2.7, 1.0), (3.2, 1.6),
          (4.6, 1.6), (P3[0], P3[1])], on_reach=tof)
    L.arrive(P3)
    L.batt -= 1
    L.battery()

    # ④ 작은방1: 문턱 단차 감지 → 재시도 2회 → 도착 실패(S05)
    L.now += timedelta(seconds=70)
    L.move_cmd(P4)
    L.go([(P3[0], P3[1]), (4.6, 1.6), (0.4, 1.5), (-0.6, 1.6), (-1.2, 1.55), (-1.2, 1.12)])
    L.step_detect()
    L.abort(P4, 1)
    L.go([(-1.2, 1.12), (-1.2, 1.35)], speed=0.1, yaw=-90.0)                 # 조금 물러났다가 다시 시도
    L.send_goal(P4, retry=1)
    L.go([(-1.2, 1.35), (-1.2, 1.12)], speed=0.15)
    L.step_detect(far=265, base=211)
    for _ in range(2):
        L.emit("planner_server", "Could not generate path between the given poses", "ERROR", dt=1.0)
    L.abort(P4, 2)
    L.send_goal(P4, retry=2)
    L.step_detect(far=263, base=210)
    L.emit("planner_server", "Could not generate path between the given poses", "ERROR", dt=1.0)
    L.abort(P4, 3, over=True)
    L.batt -= 1
    L.battery()

    # 충전 복귀 (복귀 경로는 뚫려 있음)
    L.now += timedelta(seconds=40)
    L.emit("udp_interface", "soc-cmd received : ReturnToCharger")
    L.mark("return")
    L.emit("udp_interface", "StateManager Start ReturnToCharger")
    L.emit("airbot_error_manager", "Error: [S05 (RELEASED): Unable to reach target area] ", dt=0.1)
    L.mark("s05_off")
    L.state("NAVIGATION", "RETURN_CHARGER", soc="START_RETURN_CHARGER")
    L.goal_t = L.now
    L.send_goal((0.7, 0.0, 0.0))
    L.go([(-1.2, 1.12), (-1.2, 1.5)], speed=0.15)
    L.emit("A1_maneuver", "[DROP IR PROTECT] 바닥 ToF 위험 해제 → 속도 제한 해제 (cap 0.35 m/s)", dt=0.2)
    L.go([(-1.2, 1.5), (-0.6, 1.6), (0.5, 1.2), (0.7, 0.0)])
    L.arrive((0.7, 0.0, 0.0))
    L.state("RETURN_CHARGER", "DOCKING", robot="DONE_RETURN_CHARGER")
    L.emit("state_manager", "[Docking] publish Start-Docking", dt=0.1)
    L.go([(0.7, 0.0), (0.0, 0.0)], speed=0.08, yaw=0.0)                    # 후진 도킹
    L.emit("state_manager", "[Docking] detected on Docking Station!!!", dt=0.3)
    L.emit("state_manager", "[setOnstationStatus] OnStation docking-Status hex[16]", dt=0.1)
    L.state("DOCKING", "ONSTATION", robot="DONE_DOCKING")
    L.mark("docked")
    L.lidar_off()
    L.battery(charging=True)
    L.idle(30, charging=True)
    return L.lines, L.marks, L.now


# ---------------------------------------------------------------- 다른 로그 (otalog · SoC logcat)
SOC_AHEAD = 2.0


def make_ota_lines(start):
    out = []
    for t, target, current in OTA_HISTORY:
        out.append(f"[{t:%Y-%m-%d %H:%M:%S}] [INFO] OTA Version : {target}")
        out.append(f"[{t:%Y-%m-%d %H:%M:%S}] [INFO] Current Version : {current}")
        out.append(f"[{t + timedelta(seconds=379):%Y-%m-%d %H:%M:%S}] [INFO] OTA process completed successfully.")
    out.append(f"[{start:%Y-%m-%d %H:%M:%S}] [INFO] Start OTA Process")
    out.append(f"[{start + timedelta(seconds=8):%Y-%m-%d %H:%M:%S}] [INFO] [NET] eth0 UP [L1:carrier=1] [L2:arp=ok] "
               "[L3:ip=yes default=yes] [L4:listen=yes estab=1 port=9000]")
    return out


def make_soc_lines(marks, end):
    rows = []

    def soc(t, pid, tid, lvl, tag, msg):
        s = t + timedelta(seconds=SOC_AHEAD)
        rows.append((s, f"{s:%m-%d %H:%M:%S}.{s.microsecond // 1000:03d} {pid:5d} {tid:5d} {lvl} {tag}: {msg}"))

    t = marks["sync"] - timedelta(seconds=10)
    while t < end:
        soc(t, 3652, 3672, "D", "CaptureSession", "Issuing request for session.")
        t += timedelta(seconds=20)
    t = marks["sync"]
    while t < end:
        soc(t, 1880, 1902, "I", "EthernetAgent", "Ping to 10.10.0.2: time=0.40 ms")
        t += timedelta(seconds=60)
    s = marks["sync"]
    soc(s - timedelta(seconds=0.4), 1880, 1890, "I", "AMRAgent",
        f"sent SyncTime : A1DateTime: {{year={s.year}, month={s.month}, day={s.day}, hour={s.hour}, "
        f"minute={s.minute}, second={(s + timedelta(seconds=SOC_AHEAD)).second}}}")
    for key, t in marks.items():
        if key.startswith("move:"):
            soc(t - timedelta(seconds=0.05), 1880, 1890, "I", "AMRAgent", f"sent: Move to Target ({key[5:]})")
    if "return" in marks:
        soc(marks["return"] - timedelta(seconds=0.05), 1880, 1890, "I", "AMRAgent", "sent: ReturnToCharger")
    for key, st in (("s05_on", 1), ("s05_off", 2)):
        if key in marks:
            soc(marks[key] + timedelta(seconds=0.3), 1880, 1895, "I", "DeviceAgent",
                f"[null:96] : handleError From AMR: code = S05, priority: 5, status : {st}")
    rows.sort(key=lambda r: r[0])
    return [r[1] for r in rows]


def _with_notice(text: str, first_line: str) -> bytes:
    """회전 파일마다 맨 앞에 안내 줄 (시각은 그 파일 첫 줄과 같게)."""
    ts = first_line[:first_line.index("]") + 1]
    return (f"{ts} [demo_notice] [INFO] ※ {NOTICE}\n" + text).encode("utf-8")


def split_rotation(lines, parts=3):
    """airbot_log.2.txt(오래됨) → .1 → airbot_log.txt(최신). 시각 있는 줄에서만 나누고, 파일마다 맨 앞에 AI 생성 안내 줄."""
    body = [ln for ln in lines if "[demo_notice]" not in ln]
    cuts = [0]
    for k in range(1, parts):
        i = len(body) * k // parts
        while i < len(body) and not body[i].startswith("["):
            i += 1
        cuts.append(i)
    cuts.append(len(body))
    chunks = [body[a:b] for a, b in zip(cuts, cuts[1:]) if b > a]
    names = [f"airbot_log.{i}.txt" for i in range(len(chunks) - 1, 0, -1)] + ["airbot_log.txt"]
    return {name: _with_notice("\n".join(chunk) + "\n", chunk[0]) for name, chunk in zip(names, chunks)}


def make_demo(out_dir=DEFAULT_OUT) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    lines, marks, end = make_log_lines()
    files = {f"log/{k}": v for k, v in map_files().items()}
    files.update({f"log/{k}": v for k, v in split_rotation(lines).items()})
    files["log/airbot_ota/otalog.log"] = ("\n".join(make_ota_lines(datetime(2026, 11, 3, 13, 58, 5))) + "\n").encode()
    files["log/NOTICE.txt"] = (NOTICE + "\n").encode("utf-8")
    tar_buf = io.BytesIO()                                   # 다시 만들어도 같은 바이트 (시각·시간대 고정)
    with gzip.GzipFile(fileobj=tar_buf, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as tf:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = calendar.timegm((2026, 11, 3, 6, 0, 0))
            tf.addfile(info, io.BytesIO(data))
    out = out_dir / ZIP_NAME
    soc = make_soc_lines(marks, end)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in (("NOTICE.txt", (NOTICE + "\n").encode("utf-8")), ("amr/AMR_LOG.tar.gz", tar_buf.getvalue()),
                           ("soc/logs/logcat", ("\n".join(soc) + "\n").encode("utf-8"))):
            info = zipfile.ZipInfo(name, date_time=(2026, 11, 3, 15, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return out


if __name__ == "__main__":
    print(make_demo(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUT))

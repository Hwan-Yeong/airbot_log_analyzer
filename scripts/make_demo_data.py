#!/usr/bin/env python3
"""데모 로그 생성: 실제 로봇 로그 형식을 따른 합성 데이터 (실제 고객/현장 정보 없음).

    python scripts/make_demo_data.py [출력 폴더]
    → <출력 폴더>/20261101153000_DEMOBOTSN000000001.zip  (amr/AMR_LOG.tar.gz 포함, NAS 수집본과 같은 구조)
"""
import io
import json
import math
import random
import sys
import tarfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

RES = 0.05
ORIGIN = (-12.0, -8.0)
W, H = 480, 320            # 24m x 16m
ZIP_NAME = "20261101153000_DEMOBOTSN000000001.zip"
STATION = (-0.3, 0.0, 0.0)
KEEPOUTS = [[(9.0, 5.0), (10.8, 5.0), (10.8, 6.8), (9.0, 6.8)],
            [(-10.8, -6.8), (-9.0, -6.8), (-9.0, -5.0), (-10.8, -5.0)]]


# ---------------------------------------------------------------- 맵
def make_map_pgm() -> bytes:
    free, occ, unknown = 254, 0, 205
    img = [[unknown] * W for _ in range(H)]

    def px(x, y):
        return int((x - ORIGIN[0]) / RES), H - 1 - int((y - ORIGIN[1]) / RES)

    def fill(x0, y0, x1, y1, v):
        (a, b), (c, d) = px(x0, y0), px(x1, y1)
        for r in range(max(min(b, d), 0), min(max(b, d), H - 1) + 1):
            for col in range(max(min(a, c), 0), min(max(a, c), W - 1) + 1):
                img[r][col] = v

    fill(-11, -7, 11, 7, free)
    t = 0.1
    for x0, y0, x1, y1 in [(-11, -7, 11, -7 + t), (-11, 7 - t, 11, 7), (-11, -7, -11 + t, 7), (11 - t, -7, 11, 7),
                           (-3, -7, -3 + t, -1), (-3, 0.5, -3 + t, 7),          # 거실 | 주방 벽 (문 -1~0.5)
                           (4, -7, 4 + t, 2), (4, 3.2, 4 + t, 7),               # 거실 | 침실 벽 (문 2~3.2)
                           (4, 1, 6.5, 1 + t), (7.7, 1, 11, 1 + t)]:            # 침실 | 서재 벽 (문 6.5~7.7)
        fill(x0, y0, x1, y1, occ)
    for x0, y0, x1, y1 in [(-8, 2, -6, 3.5), (0, -6.5, 2.5, -5.5), (6, 3, 9.5, 6), (8.5, -6.5, 10.5, -4)]:
        fill(x0, y0, x1, y1, occ)                                               # 가구
    rng = random.Random(7)
    for _ in range(60):                                                         # 약간의 스캔 잡음
        x, y = rng.uniform(-10.8, 10.8), rng.uniform(-6.8, 6.8)
        c, r = px(x, y)
        if img[r][c] == free:
            img[r][c] = occ
    return f"P5\n{W} {H}\n255\n".encode() + bytes(v for row in img for v in row)


def map_files() -> dict:
    area = {"keepout_zones": [{"id": f"demo-zone-{i + 1}", "coordinates": [{"x": x, "y": y} for x, y in z]}
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
class Log:
    def __init__(self, start: datetime):
        self.now = start
        self.lines = []
        self.x, self.y, self.yaw = 0.0, 0.0, 0.0
        self.batt = 92
        self.rng = random.Random(42)

    def emit(self, node, msg, level="INFO", dt=0.0):
        self.now += timedelta(seconds=dt)
        ts = self.now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        self.lines.append(f"[{ts}] [{node}] [{level}] {msg}")

    def raw(self, text):
        self.lines.append(text)

    def pose(self):
        self.emit("udp_interface", f"Current RobotPose [1]({self.x:.3f}, {self.y:.3f}, {self.yaw:.1f}(deg)) , "
                                   f"odom({self.x + 0.4:.3f}, {self.y - 0.1:.3f}, {self.yaw:.1f}(deg)) , "
                                   f"original_odom({self.x:.3f}, {self.y:.3f}, {self.yaw:.1f}(deg))")

    def battery(self, charging=False):
        cur = "1000.0" if charging else f"-{self.rng.randint(900, 1300)}"
        self.emit("airbot_error_monitor", "[ChargingErrorMonitor] Docking status: " + ("0x30" if charging else "0x00"))
        self.raw(f"Battery Manufacturer:[2] / Remaining capacity:[{self.batt * 87} mAh] / Fullcharge capacity:"
                 f"[8700 mAh] / Percentage:[{self.batt} %] / Current:[{cur} mA] / Voltage:[20{300 + self.batt} mV]"
                 f" / Temp1:[33 °C] / Temp2:[32 °C]")
        self.raw("Battery Cell Voltage:[1]: 4069, [2]: 4067, [3]: 4067, [4]: 4069, [5]: 4069")

    def idle(self, minutes, charging=False):
        """대기: 10분마다 메모리, 배터리 로그."""
        for _ in range(int(minutes // 10)):
            self.emit("airbot_error_monitor", f"Memory Usage: {self.rng.uniform(23, 27):.2f}% (970976 / 3998588 kB)"
                      "-------------", dt=600)
            if charging and self.batt < 100:
                self.batt = min(100, self.batt + 3)
            self.battery(charging)

    def state(self, a, b, soc="VOID", robot="VOID"):
        self.emit("state_manager", f"[StateManager] EXIT STATE: [{a}] -> Transition by SOC:[{soc}]|ROBOT:[{robot}]"
                                   f" -> RUN STATE: [{b}]", dt=0.05)
        self.emit("state_manager", f"SET ROBOT_STATE STATE : {b}")

    def navi(self, s):
        self.emit("state_manager", f"SET NAVI_STATE STATE : {s}", dt=0.01)

    def drive(self, path, speed=0.35, on_step=None):
        """경유점을 따라 이동하며 2.5초마다 RobotPose, 1초마다 Costmap check."""
        step = 0
        for (x0, y0), (x1, y1) in zip(path, path[1:]):
            dist = math.hypot(x1 - x0, y1 - y0)
            self.yaw = math.degrees(math.atan2(y1 - y0, x1 - x0))
            n = max(int(dist / speed), 1)
            for i in range(1, n + 1):
                self.x, self.y = x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n
                self.emit("A1_maneuver", f"[Costmap check] Robots({self.x:.2f}, {self.y:.2f}) cost(0), static(0)", dt=1.0)
                step += 1
                if step % 3 == 0:
                    self.pose()
                if on_step:
                    on_step(self, step)
        self.pose()

    def goal(self, x, y, yaw):
        self.emit("state_manager", f"move_target_callback({x:.2f},{y:.2f},{yaw:.2f}(DEG))", dt=0.2)
        self.navi("MOVE_GOAL")
        self.emit("state_manager", f"[moveToTarget] robot ({self.x:.2f}, {self.y:.2f}, {self.yaw:.2f}(DEG)) "
                                   f"send goal({x:.2f}, {y:.2f}, {yaw:.2f}(DEG))", dt=0.1)

    def boot(self, version="6.40"):
        self.emit("heartbeat_check", "Heartbeat updated: 1")
        self.emit("heartbeat_check", f"📄 Expected software version: {version}")
        self.emit("airbot_logging", "node initialized", dt=1.5)
        self.emit("airbot_error_manager", "node initialized", dt=0.1)
        self.emit("airbot_node_manager", "node initialized", dt=0.1)
        self.emit("airbot_node_manager", "NAVI BRINGUP NODE, Process started successfully with PID: 1399", dt=2.0)


def make_log_lines():
    L = Log(datetime(2024, 10, 14, 7, 53, 55))          # 부팅 직후 RTC 초기값 (실제 로그와 같은 현상)
    L.boot()
    L.now = datetime(2026, 11, 1, 8, 59, 50)            # SoC 시간 동기화
    L.emit("udp_interface", "soc-cmd received : reqSetTimeData year : 2026, month : 11, day : 1, hour : 8, "
                            "minute : 59, second : 50")
    L.emit("state_manager", "SET ROBOT_STATE STATE : ONSTATION", dt=1)
    L.emit("state_manager", "[setOnstationStatus] OnStation docking-Status hex[16]")
    L.emit("scan_monitor", "[checkScanHz] scan state is front[OFF], back[OFF]")
    L.battery(charging=True)
    L.idle(20, charging=True)

    # 1) 침실로 이동 (장애물 감지)
    L.emit("udp_interface", "soc-cmd received : Move to Target (5.0, 5.0, 90.0(deg))", dt=30)
    L.state("ONSTATION", "UNDOCKING", soc="START_NAVIGATION")
    L.emit("state_manager", "[setOnstationStatus] Off-Station docking-Status hex[0e]", dt=0.5)
    L.emit("scan_monitor", "[checkScanHz] scan state is front[ON], back[ON]", dt=0.5)
    L.x, L.y, L.yaw = 0.0, 0.0, 0.0
    L.drive([(0.0, 0.0), (0.6, 0.0)])
    L.state("UNDOCKING", "NAVIGATION", robot="UNDOCKING_DONE_START_NAVIGATION")
    L.goal(5.0, 5.0, 90.0)

    def obstacles(log, step):
        if step == 12:
            log.emit("A1_perception", f"1D ToF detected. robot_xy({log.x:.3f}, {log.y:.3f}), "
                                      f"1D(x:{log.x + 0.25:.3f}, y:{log.y + 0.2:.3f}, Dist:0.210)")
        if step == 15:
            log.emit("A1_perception", f"low level obstacle. robot_xy({log.x:.3f}, {log.y:.3f}), "
                                      f"Obs(x:{log.x + 0.3:.3f}, y:{log.y:.3f}, Dist diff: 0.030)")
        if step == 18:
            log.emit("airbot_sensor_to_pointcloud", "[ID]: 0, [Position (X, Y): (4.60, 3.30)]")
            log.emit("airbot_sensor_to_pointcloud", "[ID]: 14, [Position (X, Y): (5.40, 4.20)]")

    L.drive([(0.6, 0.0), (3.0, 2.6), (5.0, 2.6), (5.0, 5.0)], on_step=obstacles)
    L.emit("state_manager", "[goalResultCallback] Goal reached successfully. robot(5.00, 5.00, 90.00(DEG)), "
                            "goal(5.00, 5.00, 90.00(DEG)) moving time[31.00]", dt=0.5)
    L.navi("ARRIVED_GOAL")
    L.batt -= 2
    L.battery()
    L.idle(10)

    # 2) 주방으로 이동 중 낙하 감지 → 후진 → 목표 실패(S05)
    L.emit("udp_interface", "soc-cmd received : Move to Target (-7.0, -4.0, 180.0(deg))", dt=20)
    L.goal(-7.0, -4.0, 180.0)
    L.drive([(5.0, 5.0), (5.0, 2.6), (3.0, 2.6), (0.0, -0.3), (-3.0, -0.3), (-6.4, -3.4)])
    L.emit("A1_maneuver", "[DROP IR] ff detected.", "WARN", dt=0.2)
    L.emit("airbot_error_monitor", f"[CliffDetectionErrorMonitor] Initial check => Cliff IR ff[1] : true, "
                                   f"pre_position (X, Y): ({L.x:.3f}, {L.y:.3f})", dt=0.01)
    L.emit("A1_maneuver", "[DROP IR] FREEZE→BACKUP (straight forward 0.20m @ 0.10m/s)", "WARN", dt=0.3)
    L.emit("airbot_error_manager", "Error: [S07-2 (OCCURED): Cliff IR Detected] ", dt=0.4)
    L.emit("actuation", "Error-list stop active: forcing cmd_vel to zero", "WARN", dt=0.1)
    L.drive([(L.x, L.y), (-6.1, -3.1)], speed=0.1)
    L.emit("A1_maneuver", "[DROP IR] BACKUP done traveled=0.20m (reached=1 watchdog=0 path_hazard=0)", "WARN")
    L.emit("A1_maneuver", "[DROP IR] released (IR clear + wall confirmed global+local) → emergency=false", dt=1.5)
    L.emit("airbot_error_manager", "Error: [S07-2 (RELEASED): Cliff IR Detected] ", dt=0.2)
    for _ in range(3):
        L.emit("planner_server", "Could not generate path between the given poses", "ERROR", dt=2.0)
    L.emit("bt_navigator", "Goal failed", "ERROR", dt=0.5)
    L.emit("state_manager", f"[goalResultCallback] Goal aborted. robot({L.x:.2f}, {L.y:.2f}, {L.yaw:.2f}(DEG)), "
                            "goal(-7.00, -4.00, 180.00(DEG)) moving time[52.00]", "WARN", dt=0.1)
    L.emit("airbot_error_manager", "Error: [S05 (OCCURED): Unable to reach target area] ", dt=0.3)
    L.emit("odom_monitor", "[odom_monitor] Unexpected odometry change detected! Actual linear distance: 0.412 "
                           "(Tolerance: 0.300), Angular displacement: 0.120", "WARN", dt=1.0)
    L.idle(10)

    # 3) 충전 복귀
    L.emit("udp_interface", "soc-cmd received : ReturnToCharger", dt=5)
    L.emit("airbot_error_manager", "Error: [S05 (RELEASED): Unable to reach target area] ", dt=0.1)
    L.state("NAVIGATION", "RETURN_CHARGER", soc="START_RETURN_CHARGER")
    L.goal(0.0, 0.0, 0.0)
    L.drive([(L.x, L.y), (-3.0, -0.3), (-0.3, -0.1), (0.0, 0.0)])
    L.emit("state_manager", "[goalResultCallback] Goal reached successfully. robot(0.00, 0.00, 0.00(DEG)), "
                            "goal(0.00, 0.00, 0.00(DEG)) moving time[40.00]", dt=0.4)
    L.state("RETURN_CHARGER", "DOCKING", robot="DONE_RETURN_CHARGER")
    L.emit("state_manager", "[Docking] publish Start-Docking", dt=0.1)
    L.yaw = 180.0
    L.drive([(0.0, 0.0), (-0.25, 0.0)], speed=0.08)
    L.emit("state_manager", "[Docking] detected on Docking Station!!!", dt=0.5)
    L.emit("state_manager", "[setOnstationStatus] OnStation docking-Status hex[16]", dt=0.1)
    L.state("DOCKING", "ONSTATION", robot="DONE_DOCKING")
    L.emit("scan_monitor", "[checkScanHz] scan state is front[OFF], back[OFF]", dt=1.0)
    L.batt -= 3
    L.battery(charging=True)
    L.idle(60, charging=True)

    # 4) 재부팅 (RTC 초기값으로 시간 역행 → 시간 동기화)
    L.emit("udp_interface", "soc-cmd received : Reboot", dt=10)
    L.emit("udp_interface", "[systemRebootCommand] start-Reboot", dt=3)
    L.now = datetime(2024, 10, 14, 7, 53, 55)
    L.boot()
    for node in ("/bt_navigator", "/controller_server"):
        L.emit("airbot_node_manager", f"[node_monitor] [NAVI] Required node [{node}] is NOT running.", "WARN", dt=1.0)
    L.now = datetime(2026, 11, 1, 11, 32, 10)
    L.emit("udp_interface", "soc-cmd received : reqSetTimeData year : 2026, month : 11, day : 1, hour : 11, "
                            "minute : 32, second : 10")
    L.emit("state_manager", "SET ROBOT_STATE STATE : ONSTATION", dt=1)
    L.battery(charging=True)
    L.idle(30, charging=True)

    # 5) 오후: 서재 이동 후 복귀
    L.emit("udp_interface", "soc-cmd received : Move to Target (8.0, -2.0, 0.0(deg))", dt=40)
    L.state("ONSTATION", "UNDOCKING", soc="START_NAVIGATION")
    L.emit("state_manager", "[setOnstationStatus] Off-Station docking-Status hex[0e]", dt=0.5)
    L.x, L.y, L.yaw = 0.0, 0.0, 0.0
    L.drive([(0.0, 0.0), (0.6, 0.0)])
    L.state("UNDOCKING", "NAVIGATION", robot="UNDOCKING_DONE_START_NAVIGATION")
    L.goal(8.0, -2.0, 0.0)
    L.drive([(0.6, 0.0), (3.0, 2.6), (7.1, 2.6), (7.1, -2.0), (8.0, -2.0)])
    L.emit("state_manager", "[goalResultCallback] Goal reached successfully. robot(8.00, -2.00, 0.00(DEG)), "
                            "goal(8.00, -2.00, 0.00(DEG)) moving time[45.00]", dt=0.5)
    L.emit("odom_monitor", "[IMU_monitor]IMU anomaly detected! Unexpected yaw change. Actual Yaw Change: 0.041, "
                           "Expected Yaw Change: 0.000 (Tolerance: 0.030)", "WARN", dt=3)
    L.idle(10)
    L.emit("udp_interface", "soc-cmd received : ReturnToCharger", dt=5)
    L.state("NAVIGATION", "RETURN_CHARGER", soc="START_RETURN_CHARGER")
    L.goal(0.0, 0.0, 0.0)
    L.drive([(8.0, -2.0), (7.1, -2.0), (7.1, 2.6), (3.0, 2.6), (0.0, 0.0)])
    L.emit("state_manager", "[goalResultCallback] Goal reached successfully. robot(0.00, 0.00, 0.00(DEG)), "
                            "goal(0.00, 0.00, 0.00(DEG)) moving time[48.00]", dt=0.4)
    L.state("RETURN_CHARGER", "DOCKING", robot="DONE_RETURN_CHARGER")
    L.emit("state_manager", "[Docking] publish Start-Docking", dt=0.1)
    L.emit("state_manager", "[Docking] detected on Docking Station!!!", dt=8)
    L.emit("state_manager", "[setOnstationStatus] OnStation docking-Status hex[16]", dt=0.1)
    L.state("DOCKING", "ONSTATION", robot="DONE_DOCKING")
    L.batt -= 4
    L.battery(charging=True)
    L.idle(40, charging=True)
    return L.lines


def split_rotation(lines, parts=3):
    """airbot_log.2.txt(오래됨) → .1 → airbot_log.txt(최신)."""
    size = len(lines) // parts + 1
    chunks = [lines[i:i + size] for i in range(0, len(lines), size)]
    names = [f"airbot_log.{i}.txt" for i in range(len(chunks) - 1, 0, -1)] + ["airbot_log.txt"]
    return {name: ("\n".join(chunk) + "\n").encode("utf-8") for name, chunk in zip(names, chunks)}


def make_demo(out_dir) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {f"log/{k}": v for k, v in map_files().items()}
    files.update({f"log/{k}": v for k, v in split_rotation(make_log_lines()).items()})
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w:gz") as tf:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = int(datetime(2026, 11, 1, 15, 30).timestamp())
            tf.addfile(info, io.BytesIO(data))
    out = out_dir / ZIP_NAME
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("amr/AMR_LOG.tar.gz", tar_buf.getvalue())
        zf.writestr("soc/logs/logcat", "demo soc log (분석에 사용하지 않음)\n")
    return out


if __name__ == "__main__":
    print(make_demo(sys.argv[1] if len(sys.argv) > 1 else "demo"))

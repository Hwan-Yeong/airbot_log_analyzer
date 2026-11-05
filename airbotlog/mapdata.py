"""맵 폴더 로드 (airbot_map_00.yaml/pgm, area.json, wall.json, station_pose.json)."""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class MapData:
    dir: Path
    image_path: Path
    resolution: float
    origin: tuple
    width: int = 0
    height: int = 0
    keepouts: list = field(default_factory=list)   # [(id, [(x, y), ...]), ...]
    walls: list = field(default_factory=list)      # [[(x, y), ...], ...]
    station: tuple = None                          # (x, y, theta[rad])

    def world_to_px(self, x: float, y: float):
        """맵 좌표(m) → 이미지 픽셀 (y 아래 방향)."""
        return ((x - self.origin[0]) / self.resolution,
                self.height - (y - self.origin[1]) / self.resolution)

    def px_to_world(self, px: float, py: float):
        return (px * self.resolution + self.origin[0],
                (self.height - py) * self.resolution + self.origin[1])


def _pgm_size(path: Path):
    """PGM 헤더에서 크기 읽기 (P2/P5)."""
    try:
        with open(path, "rb") as f:
            head = f.read(512)
    except OSError:
        return 0, 0
    if not head.startswith((b"P5", b"P2")):
        return 0, 0
    text = re.sub(rb"#[^\n]*\n", b"\n", head[2:])
    nums = re.findall(rb"\d+", text)
    if len(nums) >= 2:
        return int(nums[0]), int(nums[1])
    return 0, 0


def _points(raw):
    pts = []
    for p in raw or []:
        if isinstance(p, dict) and "x" in p and "y" in p:
            pts.append((float(p["x"]), float(p["y"])))
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            pts.append((float(p[0]), float(p[1])))
    return pts


def _read_json(path: Path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def load_map(map_dir) -> MapData:
    map_dir = Path(map_dir)
    yaml_path = None
    info = {}
    for y in sorted(map_dir.glob("*.yaml")):
        try:
            with open(y, "r", encoding="utf-8") as f:
                d = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(d, dict) and "image" in d:
            yaml_path, info = y, d
            break
    if yaml_path is None:
        raise FileNotFoundError(f"맵 yaml 이 없습니다: {map_dir}")
    image_path = map_dir / str(info["image"])
    origin = info.get("origin") or [0.0, 0.0, 0.0]
    m = MapData(dir=map_dir, image_path=image_path,
                resolution=float(info.get("resolution", 0.05)),
                origin=(float(origin[0]), float(origin[1]), float(origin[2]) if len(origin) > 2 else 0.0))
    m.width, m.height = _pgm_size(image_path)

    area = _read_json(map_dir / "area.json")
    if isinstance(area, dict):
        for z in area.get("keepout_zones", []) or []:
            pts = _points(z.get("coordinates"))
            if len(pts) >= 3:
                m.keepouts.append((str(z.get("id", "")), pts))

    wall = _read_json(map_dir / "wall.json")
    items = []
    if isinstance(wall, dict):
        for key in ("walls", "virtual_walls", "keepout_zones", "lines"):
            items += wall.get(key, []) or []
    elif isinstance(wall, list):
        items = wall
    for w in items:
        raw = w.get("coordinates", w.get("points")) if isinstance(w, dict) else w
        pts = _points(raw)
        if len(pts) >= 2:
            m.walls.append(pts)

    st = _read_json(map_dir / "station_pose.json")
    if isinstance(st, dict):
        st = st.get("station_pose", st)
        try:
            m.station = (float(st["x"]), float(st["y"]), float(st.get("theta", 0.0)))
        except (KeyError, TypeError, ValueError):
            m.station = None
    return m

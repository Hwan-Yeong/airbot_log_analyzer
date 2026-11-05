"""config/rules.yaml 로드 및 컴파일."""
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .settings import CONFIG_DIR

FLOAT_RE = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
LEVELS = ("info", "warn", "error")


class RuleError(Exception):
    pass


@dataclass
class Category:
    id: str
    name: str
    color: str = "#455a64"
    visible: bool = True


@dataclass
class Layer:
    id: str
    name: str
    shape: str = "circle"
    color: str = "#1565c0"
    size: float = 8.0
    visible: bool = True
    label: str = ""


@dataclass
class Rule:
    id: str
    regex: re.Pattern
    kind: str = "event"
    category: str = "system"
    node: str = ""
    levels: frozenset = frozenset()
    contains: tuple = ()
    text: str = ""
    level: str = "info"
    layer: str = ""
    at_robot: bool = False
    track: str = ""
    value: str = ""
    list: bool = True
    list_on_change: bool = False
    continuation: bool = False
    dedupe_sec: float = 0.0
    dedupe_by: str = "text"
    dedupe_pos: bool = False

    def format_text(self, groups: dict, default: str) -> str:
        if not self.text:
            return default
        return self.text.format_map(_Fmt(groups))


class _Fmt(dict):
    def __missing__(self, key):
        return "?"

    def __getitem__(self, key):
        v = super().get(key, "?")
        return "" if v is None else v


def _compile(rid: str, pattern: str) -> re.Pattern:
    try:
        return re.compile(pattern.replace("%F", FLOAT_RE))
    except re.error as e:
        raise RuleError(f"규칙 '{rid}' 정규식 오류: {e}")


def _rule_from(d: dict, kind_default="event") -> Rule:
    rid = str(d.get("id") or kind_default)
    if "regex" not in d:
        raise RuleError(f"규칙 '{rid}' 에 regex 가 없습니다.")
    contains = d.get("contains") or ()
    if isinstance(contains, str):
        contains = (contains,)
    level = str(d.get("level", "info")).lower()
    return Rule(
        id=rid,
        regex=_compile(rid, str(d["regex"])),
        kind=str(d.get("kind", kind_default)),
        category=str(d.get("category", "system")),
        node=str(d.get("node", "") or ""),
        levels=frozenset(str(x).upper() for x in (d.get("levels") or ())),
        contains=tuple(str(c) for c in contains),
        text=str(d.get("text", "") or ""),
        level=level if level in LEVELS else "info",
        layer=str(d.get("layer", "") or ""),
        at_robot=bool(d.get("at_robot", False)),
        track=str(d.get("track", "") or ""),
        value=str(d.get("value", "") or ""),
        list=bool(d.get("list", True)),
        list_on_change=bool(d.get("list_on_change", False)),
        continuation=bool(d.get("continuation", False)),
        dedupe_sec=float(d.get("dedupe_sec", 0) or 0),
        dedupe_by="rule" if str(d.get("dedupe_by", "text")) == "rule" else "text",
        dedupe_pos=bool(d.get("dedupe_pos", False)),
    )


@dataclass
class RuleSet:
    path: Path = None
    pose: Rule = None
    categories: list = field(default_factory=list)
    layers: list = field(default_factory=list)
    rules: list = field(default_factory=list)

    def __post_init__(self):
        self.cat_by_id = {c.id: c for c in self.categories}
        self.layer_by_id = {l.id: l for l in self.layers}
        self.continuation = [r for r in self.rules if r.continuation]
        line_rules = ([self.pose] if self.pose else []) + [r for r in self.rules if not r.continuation]
        self._by_node = {}
        self._generic = [r for r in line_rules if not r.node]
        for r in line_rules:
            if r.node:
                self._by_node.setdefault(r.node, []).append(r)
        self._cache = {}

    def rules_for(self, node: str) -> list:
        """해당 노드 줄에서 검사할 규칙 목록 (캐시)."""
        got = self._cache.get(node)
        if got is None:
            got = self._by_node.get(node, []) + self._generic
            self._cache[node] = got
        return got

    @classmethod
    def load(cls, path=None) -> "RuleSet":
        path = Path(path) if path else CONFIG_DIR / "rules.yaml"
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        pose = _rule_from({**data["pose"], "id": "pose"}, "pose") if data.get("pose") else None
        if pose:
            pose.kind, pose.list = "pose", False
        cats = [Category(**{k: v for k, v in c.items() if k in Category.__dataclass_fields__})
                for c in data.get("categories", [])]
        layers = [Layer(**{k: v for k, v in l.items() if k in Layer.__dataclass_fields__})
                  for l in data.get("layers", [])]
        rules = [_rule_from(r) for r in data.get("rules", [])]
        ids = [r.id for r in rules]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise RuleError("규칙 id 중복: " + ", ".join(sorted(dup)))
        return cls(path=path, pose=pose, categories=cats, layers=layers, rules=rules)

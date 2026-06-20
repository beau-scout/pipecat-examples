"""Turn EyePop predictions into people, adult/child labels, and proximity groups."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import AgeSettings, GroupingSettings

_INT_RE = re.compile(r"\d+")
_ADULT_WORDS = ("adult", "senior", "elderly", "old")


@dataclass
class Person:
    x: float
    y: float
    width: float
    height: float
    confidence: float
    age_label: str | None = None          # raw EyePop label, e.g. "10-17"
    age_estimate: float | None = None      # midpoint of the range, if numeric
    category: str = "unknown"              # "adult" | "child" | "unknown"
    group_id: int | None = None            # set only when part of a group
    is_alone: bool = True

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2.0, self.y + self.height / 2.0

    def to_dict(self) -> dict:
        return {
            "x": self.x, "y": self.y, "width": self.width, "height": self.height,
            "confidence": round(self.confidence, 3),
            "age_label": self.age_label,
            "age_estimate": self.age_estimate,
            "category": self.category,
            "group_id": self.group_id,
            "is_alone": self.is_alone,
        }


@dataclass
class PeopleAnalysis:
    persons: list[Person] = field(default_factory=list)
    source_width: int = 0
    source_height: int = 0

    @property
    def total(self) -> int:
        return len(self.persons)

    @property
    def adults(self) -> int:
        return sum(1 for p in self.persons if p.category == "adult")

    @property
    def children(self) -> int:
        return sum(1 for p in self.persons if p.category == "child")

    @property
    def groups(self) -> int:
        return len({p.group_id for p in self.persons if p.group_id is not None})

    @property
    def alone(self) -> int:
        return sum(1 for p in self.persons if p.is_alone)


# --- age ----------------------------------------------------------------
def classify_age(label: str | None, age: AgeSettings) -> tuple[str, float | None]:
    """Map a raw age label to ("adult"|"child"|"unknown", estimated_age)."""
    if not label:
        return "unknown", None
    norm = label.strip().lower()

    if norm in (c.lower() for c in age.child_labels):
        return "child", None
    if any(w in norm for w in _ADULT_WORDS):
        return "adult", None

    nums = [int(n) for n in _INT_RE.findall(norm)]
    if not nums:
        return "unknown", None

    lo, hi = min(nums), max(nums)
    mid = (lo + hi) / 2.0
    if hi < age.adult_min_age:
        return "child", mid
    if lo >= age.adult_min_age:
        return "adult", mid
    # Range straddles the boundary (e.g. "16-20"): decide by midpoint.
    return ("child" if mid < age.adult_min_age else "adult"), mid


def _find_age_label(obj: dict, category: str) -> str | None:
    """Search an object's classes (and nested objects) for the age category,
    returning the highest-confidence matching label."""
    best_label, best_conf = None, -1.0

    def walk(node: dict) -> None:
        nonlocal best_label, best_conf
        for cls in node.get("classes", []) or []:
            if str(cls.get("category", "")).lower() == category.lower():
                conf = float(cls.get("confidence", 0.0))
                if conf > best_conf:
                    best_conf, best_label = conf, cls.get("classLabel")
        # Some Pops also encode a single classification directly on the object.
        if str(node.get("category", "")).lower() == category.lower():
            conf = float(node.get("confidence", 0.0))
            if conf > best_conf:
                best_conf, best_label = conf, node.get("classLabel")
        for child in node.get("objects", []) or []:
            walk(child)

    walk(obj)
    return best_label


def _is_person(obj: dict) -> bool:
    label = str(obj.get("classLabel", "")).lower()
    category = str(obj.get("category", "")).lower()
    return label == "person" or category == "person" or label == "people"


def parse_people(result: dict, age: AgeSettings, min_confidence: float) -> PeopleAnalysis:
    persons: list[Person] = []
    for obj in result.get("objects", []) or []:
        if not _is_person(obj):
            continue
        conf = float(obj.get("confidence", 1.0))
        if conf < min_confidence:
            continue
        age_label = _find_age_label(obj, age.category)
        category, estimate = classify_age(age_label, age)
        persons.append(
            Person(
                x=float(obj.get("x", 0.0)),
                y=float(obj.get("y", 0.0)),
                width=float(obj.get("width", 0.0)),
                height=float(obj.get("height", 0.0)),
                confidence=conf,
                age_label=age_label,
                age_estimate=estimate,
                category=category,
            )
        )
    return PeopleAnalysis(
        persons=persons,
        source_width=int(result.get("source_width", 0)),
        source_height=int(result.get("source_height", 0)),
    )


# --- grouping (proximity clustering via union-find) ---------------------
class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def assign_groups(analysis: PeopleAnalysis, cfg: GroupingSettings) -> None:
    """Cluster nearby people and annotate each Person with group/alone state."""
    persons = analysis.persons
    n = len(persons)
    for p in persons:
        p.group_id, p.is_alone = None, True
    if n < 2:
        return

    uf = _UnionFind(n)
    for i in range(n):
        cx_i, cy_i = persons[i].center
        for j in range(i + 1, n):
            cx_j, cy_j = persons[j].center
            dist = ((cx_i - cx_j) ** 2 + (cy_i - cy_j) ** 2) ** 0.5
            # Threshold scales with apparent size so it works near and far.
            mean_h = (persons[i].height + persons[j].height) / 2.0 or 1.0
            if dist <= cfg.proximity_factor * mean_h:
                uf.union(i, j)

    clusters: dict[int, list[int]] = {}
    for i in range(n):
        clusters.setdefault(uf.find(i), []).append(i)

    next_group_id = 1
    for members in clusters.values():
        if len(members) >= cfg.min_group_size:
            gid = next_group_id
            next_group_id += 1
            for i in members:
                persons[i].group_id = gid
                persons[i].is_alone = False
        else:
            for i in members:
                persons[i].is_alone = True

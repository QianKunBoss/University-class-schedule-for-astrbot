"""数据模型：节次、课程、课表。

设计要点：
* 一门课可能跨 2~4 节，也可能只在单/双周上，因此用 ``start_period``/``end_period``
  + ``weeks`` 集合表达，而不是存一句原始文本；
* 强智课表同一格里可能塞多门课（不同周次共用同一时段），解析时会拆成多条 Course，
  并保留 ``raw`` 原文便于排障。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from .const import WEEKDAY_CN


def _hm_to_minutes(hm: str) -> int:
    """``"08:30"`` -> 510；格式非法返回 -1。"""
    try:
        h, m = str(hm).split(":", 1)
        return int(h) * 60 + int(m)
    except Exception:
        return -1


@dataclass
class Period:
    """一节课的作息时间。"""

    index: int
    start: str  # HH:MM
    end: str  # HH:MM

    @property
    def start_minute(self) -> int:
        return _hm_to_minutes(self.start)

    @property
    def end_minute(self) -> int:
        return _hm_to_minutes(self.end)

    def to_dict(self) -> dict:
        return {"index": self.index, "start": self.start, "end": self.end}

    @classmethod
    def from_dict(cls, d: dict) -> Period:
        return cls(
            index=int(d.get("index", 0)),
            start=str(d.get("start", "00:00")),
            end=str(d.get("end", "00:00")),
        )


@dataclass
class Course:
    """一门课在某个固定时段上的安排。"""

    name: str
    weekday: int  # 1=周一 ... 7=周日
    start_period: int  # 起始节次（1 起）
    end_period: int  # 结束节次（含）
    weeks: list[int] = field(default_factory=list)  # 上课周次集合
    teacher: str = ""
    location: str = ""
    raw: str = ""  # 单元格原文，排障用
    weeks_guessed: bool = False  # 周次是否靠猜（解析不出时的兜底标记）
    manual: bool = False  # True = 用户在面板里手动添加/编辑的自定义课程
    entry_id: str = ""  # 自定义课程的唯一 id（fetched 课程为空）
    mark: str = ""  # 教务调课标记字母（琼台红标 O/P 代表不同调课方式），仅展示用

    @property
    def uid(self) -> str:
        """稳定标识：只由「课名+星期+节次区间」决定，用于变更检测与去重。"""
        key = f"{self.name}|{self.weekday}|{self.start_period}|{self.end_period}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]

    @property
    def fingerprint(self) -> str:
        """内容指纹：含周次与教室，用于「隐藏某条教务课程」的标记。"""
        key = (
            f"{self.name}|{self.weekday}|{self.start_period}|{self.end_period}"
            f"|{','.join(str(w) for w in self.weeks)}|{self.location}"
        )
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]

    @property
    def weekday_cn(self) -> str:
        if 1 <= self.weekday <= 7:
            return WEEKDAY_CN[self.weekday - 1]
        return f"周{self.weekday}"

    def active_in(self, week: int) -> bool:
        """这门课在第 ``week`` 周是否要上。周次为空时视为每周都上。"""
        if not self.weeks:
            return True
        return week in self.weeks

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "weekday": self.weekday,
            "start_period": self.start_period,
            "end_period": self.end_period,
            "weeks": list(self.weeks),
            "teacher": self.teacher,
            "location": self.location,
            "raw": self.raw,
            "weeks_guessed": self.weeks_guessed,
            "manual": self.manual,
            "entry_id": self.entry_id,
            "mark": self.mark,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Course:
        return cls(
            name=str(d.get("name", "")),
            weekday=int(d.get("weekday", 0) or 0),
            start_period=int(d.get("start_period", 0) or 0),
            end_period=int(d.get("end_period", 0) or 0),
            weeks=[int(x) for x in (d.get("weeks") or [])],
            teacher=str(d.get("teacher", "") or ""),
            location=str(d.get("location", "") or ""),
            raw=str(d.get("raw", "") or ""),
            weeks_guessed=bool(d.get("weeks_guessed", False)),
            manual=bool(d.get("manual", False)),
            entry_id=str(d.get("entry_id", "") or ""),
            mark=str(d.get("mark", "") or ""),
        )


@dataclass
class Timetable:
    """一个学期的完整课表。"""

    term: str = ""
    term_name: str = ""
    courses: list[Course] = field(default_factory=list)
    # 教务抓到的原始课程（未应用隐藏过滤、未合并自定义课）。
    # 自定义课程/隐藏过滤都从这份列表出发重建 courses，保证操作可逆。
    fetched_courses: list[Course] = field(default_factory=list)
    fetched_at: float = 0.0
    week1_monday: str = ""  # 第 1 周周一的日期，ISO 格式 YYYY-MM-DD
    week1_source: str = ""  # jw / config / guessed / manual
    snapshot: str = ""  # 原始 HTML 快照文件名

    def courses_on(self, week: int, weekday: int) -> list[Course]:
        """返回某周某天的课程，按开始节次排序。"""
        items = [c for c in self.courses if c.weekday == weekday and c.active_in(week)]
        return sorted(items, key=lambda c: (c.start_period, c.name))

    def to_dict(self) -> dict:
        return {
            "term": self.term,
            "term_name": self.term_name,
            "courses": [c.to_dict() for c in self.courses],
            "fetched_courses": [c.to_dict() for c in self.fetched_courses],
            "fetched_at": self.fetched_at,
            "week1_monday": self.week1_monday,
            "week1_source": self.week1_source,
            "snapshot": self.snapshot,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Timetable:
        courses = [Course.from_dict(x) for x in (d.get("courses") or [])]
        fetched = [Course.from_dict(x) for x in (d.get("fetched_courses") or [])]
        if not fetched:
            # 旧版状态文件没有 fetched_courses：从 courses 里推导（排除自定义课）
            fetched = [c for c in courses if not c.manual]
        return cls(
            term=str(d.get("term", "") or ""),
            term_name=str(d.get("term_name", "") or ""),
            courses=courses,
            fetched_courses=fetched,
            fetched_at=float(d.get("fetched_at", 0) or 0),
            week1_monday=str(d.get("week1_monday", "") or ""),
            week1_source=str(d.get("week1_source", "") or ""),
            snapshot=str(d.get("snapshot", "") or ""),
        )

    @property
    def is_empty(self) -> bool:
        return not self.courses


@dataclass
class TermOption:
    """教务系统学期下拉框的一项。"""

    id: str  # 如 2026-2027-1
    name: str  # 如 2026-2027学年第一学期
    selected: bool = False

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "selected": self.selected}

    @classmethod
    def from_dict(cls, d: dict) -> TermOption:
        return cls(id=str(d.get("id", "")), name=str(d.get("name", "")), selected=bool(d.get("selected")))


def as_int(value: Any, default: int = 0) -> int:
    """宽松取整，忽略 ``"3"``/``3``/``None`` 的差异。"""
    try:
        return int(str(value).strip())
    except Exception:
        return default

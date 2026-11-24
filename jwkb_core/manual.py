"""自定义课程存储：面板里手动增删改的课程，以及「隐藏某条教务课程」的标记。

为什么单独存文件而不是写进配置：
* 教务刷新会整体重建课表，自定义内容混在里面容易被覆盖；
* 独立的 ``manual_courses.json`` / ``hidden_courses.json`` 与刷新解耦，
  用户在面板里的编辑永久生效，直到自己删掉。

合并模型（``apply``）：
* ``Timetable.fetched_courses`` 保存教务原始课程（不动的基准）；
* ``courses`` = fetched − 隐藏指纹 + 自定义课程，每次操作都从基准重建，
  因此「隐藏/恢复/删除」都可逆，重复应用也不会叠加。
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path

from .models import Course
from .storage import read_json, write_json


class ManualStore:
    """自定义课程 + 隐藏标记的持久化与合并。"""

    def __init__(self, data_dir: str | Path, log=None):
        self.dir = Path(data_dir)
        self.courses_path = self.dir / "manual_courses.json"
        self.hidden_path = self.dir / "hidden_courses.json"
        self.log = log
        self.entries: list[dict] = []  # [{"id": str, "course": Course}]
        self.hidden: list[str] = []  # fingerprint 列表
        self._load()

    # ---------- 持久化 ----------
    def _load(self) -> None:
        self.entries = []
        data = read_json(self.courses_path, None)
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict) or not isinstance(item.get("course"), dict):
                    continue
                try:
                    course = Course.from_dict(item["course"])
                except Exception as exc:
                    self._warn("[kb] 跳过损坏的自定义课程条目：%s", exc)
                    continue
                self.entries.append(
                    {"id": str(item.get("id") or course.entry_id or uuid.uuid4().hex[:8]), "course": course}
                )
        self.hidden = [str(x) for x in (read_json(self.hidden_path, None) or []) if x]

    def save(self) -> None:
        try:
            write_json(
                self.courses_path,
                [{"id": e["id"], "course": e["course"].to_dict()} for e in self.entries],
            )
            write_json(self.hidden_path, self.hidden)
        except Exception as exc:
            self._warn("[kb] 保存自定义课程失败：%s", exc)

    # ---------- CRUD ----------
    def add(self, course: Course) -> Course:
        entry_id = uuid.uuid4().hex[:8]
        stored = replace(course, manual=True, entry_id=entry_id)
        self.entries.append({"id": entry_id, "course": stored})
        self.save()
        return stored

    def update(self, entry_id: str, course: Course) -> Course | None:
        for e in self.entries:
            if e["id"] == entry_id:
                stored = replace(course, manual=True, entry_id=entry_id)
                e["course"] = stored
                self.save()
                return stored
        return None

    def remove(self, entry_id: str) -> bool:
        before = len(self.entries)
        self.entries = [e for e in self.entries if e["id"] != entry_id]
        if len(self.entries) != before:
            self.save()
            return True
        return False

    def hide(self, fingerprint: str) -> None:
        if fingerprint and fingerprint not in self.hidden:
            self.hidden.append(fingerprint)
            self.save()

    def unhide(self, fingerprint: str) -> None:
        if fingerprint in self.hidden:
            self.hidden = [x for x in self.hidden if x != fingerprint]
            self.save()

    # ---------- 合并 ----------
    def apply(self, timetable) -> None:
        """把隐藏过滤 + 自定义课程合并进课表（幂等，可反复调用）。"""
        if timetable is None:
            return
        fetched = timetable.fetched_courses or [c for c in timetable.courses if not c.manual]
        timetable.fetched_courses = list(fetched)

        visible = [c for c in fetched if c.fingerprint not in self.hidden]
        manual = [replace(e["course"], manual=True, entry_id=e["id"]) for e in self.entries]
        merged = sorted(visible + manual, key=lambda c: (c.weekday, c.start_period, c.name))
        timetable.courses = merged

    # ---------- 工具 ----------
    def _warn(self, msg: str, *args) -> None:
        if self.log:
            self.log.warning(msg, *args)

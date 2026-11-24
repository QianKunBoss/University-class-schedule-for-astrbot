"""自定义课程存储测试。

面板编辑的核心约束：apply 必须幂等（重复应用不叠加）、隐藏可逆（unhide 后教务课程
要能回来）、教务刷新不影响自定义内容。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jwkb_core.manual import ManualStore  # noqa: E402
from jwkb_core.models import Course, Timetable  # noqa: E402


def _fetched() -> list[Course]:
    return [
        Course(
            name="高等数学",
            weekday=1,
            start_period=1,
            end_period=2,
            weeks=[1, 2, 3],
            teacher="张三",
            location="教101",
        ),
        Course(
            name="大学英语",
            weekday=5,
            start_period=3,
            end_period=4,
            weeks=[2],
            teacher="李四",
            location="外语楼202",
        ),
    ]


def _timetable() -> Timetable:
    return Timetable(term="2026-2027-1", courses=_fetched(), fetched_courses=list(_fetched()))


def _store(tmp_path) -> ManualStore:
    return ManualStore(tmp_path)


def test_add_and_apply_merges_manual_course(tmp_path):
    store = _store(tmp_path)
    added = store.add(Course(name="班会", weekday=3, start_period=7, end_period=8, weeks=[2]))
    assert added.manual and added.entry_id

    tt = _timetable()
    store.apply(tt)
    names = {(c.name, c.weekday) for c in tt.courses}
    assert ("班会", 3) in names and ("高等数学", 1) in names
    # 原始 fetched 列表不动
    assert len(tt.fetched_courses) == 2


def test_apply_is_idempotent(tmp_path):
    store = _store(tmp_path)
    store.add(Course(name="班会", weekday=3, start_period=7, end_period=8))
    tt = _timetable()
    store.apply(tt)
    store.apply(tt)
    store.apply(tt)
    assert len([c for c in tt.courses if c.name == "班会"]) == 1


def test_hide_and_unhide_is_reversible(tmp_path):
    store = _store(tmp_path)
    tt = _timetable()
    fp = tt.fetched_courses[0].fingerprint  # 高等数学

    store.hide(fp)
    store.apply(tt)
    assert all(c.name != "高等数学" for c in tt.courses)
    # 隐藏的课程仍在 fetched 基准里，可恢复
    assert any(c.name == "高等数学" for c in tt.fetched_courses)

    store.unhide(fp)
    store.apply(tt)
    assert any(c.name == "高等数学" for c in tt.courses)


def test_update_and_remove(tmp_path):
    store = _store(tmp_path)
    added = store.add(Course(name="班会", weekday=3, start_period=7, end_period=8))
    updated = store.update(added.entry_id, Course(name="班会（改）", weekday=4, start_period=7, end_period=8))
    assert updated.name == "班会（改）"
    assert len(store.entries) == 1

    assert store.remove(added.entry_id)
    assert not store.entries


def test_persistence_across_instances(tmp_path):
    fp = _timetable().fetched_courses[0].fingerprint  # 高等数学的真实指纹
    store = _store(tmp_path)
    store.add(Course(name="班会", weekday=3, start_period=7, end_period=8))
    store.hide(fp, "高等数学")

    store2 = ManualStore(tmp_path)
    assert len(store2.entries) == 1
    assert store2.hidden == [{"fp": fp, "name": "高等数学"}]
    tt = _timetable()
    store2.apply(tt)
    assert any(c.name == "班会" for c in tt.courses)
    assert all(c.name != "高等数学" for c in tt.courses)


def test_course_fingerprint_stable():
    c = Course(name="高等数学", weekday=1, start_period=1, end_period=2, weeks=[1, 2], location="教101")
    c2 = Course.from_dict(c.to_dict())
    assert c.fingerprint == c2.fingerprint
    # 周次不同 → 指纹不同（隐藏按内容而不是按课名）
    c3 = Course.from_dict({**c.to_dict(), "weeks": [4]})
    assert c3.fingerprint != c.fingerprint

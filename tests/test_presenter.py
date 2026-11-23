"""面板展示层测试：周网格排布与识别完整度统计。

面板上「第几节课在第几列」一旦错位，用户看到的就是假课表，
所以网格布局必须有回归测试。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jwkb_core.config import Config  # noqa: E402
from jwkb_core.models import Course, Timetable  # noqa: E402
from jwkb_core.presenter import (  # noqa: E402
    course_card,
    recognition_stats,
    week_bounds,
    week_grid,
)

SCHEDULE = Config.from_dict({}).schedule  # 用内置 12 节作息


def _tt(courses) -> Timetable:
    return Timetable(
        term="2026-2027-1",
        term_name="2026-2027学年第一学期",
        courses=courses,
        week1_monday="2026-09-07",  # 第 1 周周一
    )


def test_course_card_fields():
    c = Course(name="高等数学", weekday=1, start_period=1, end_period=2, teacher="张三", location="教101")
    card = course_card(c, SCHEDULE)
    assert card["name"] == "高等数学"
    assert card["teacher"] == "张三"
    assert card["location"] == "教101"
    assert card["time"] == "08:00-09:40"
    assert card["incomplete"] is False


def test_course_card_marks_incomplete():
    c = Course(name="体育", weekday=2, start_period=3, end_period=4)
    assert course_card(c, SCHEDULE)["incomplete"] is True


def test_week_grid_places_course_in_right_cell():
    tt = _tt(
        [
            Course(name="高等数学", weekday=1, start_period=1, end_period=2, weeks=[12], location="教101"),
            Course(name="大学英语", weekday=3, start_period=3, end_period=4, weeks=[12], teacher="李四"),
        ]
    )
    grid = week_grid(tt, SCHEDULE, 12, today=date(2026, 11, 23))

    assert grid["week"] == 12
    assert len(grid["days"]) == 7
    # 第 1 行是 1-2 节，第 2 行是 3-4 节
    first_row = grid["rows"][0]
    assert first_row["label"] == "1-2"
    assert first_row["cells"][0][0]["name"] == "高等数学"  # 周一
    assert all(not c for c in first_row["cells"][1:])  # 其余列为空

    second_row = grid["rows"][1]
    assert second_row["cells"][2][0]["name"] == "大学英语"  # 周三

    assert grid["days"][0]["today"] is True  # 2026-11-23 是周一
    assert grid["days"][1]["today"] is False


def test_week_grid_filters_by_week():
    tt = _tt([Course(name="单周课", weekday=1, start_period=1, end_period=2, weeks=[11])])
    assert week_grid(tt, SCHEDULE, 12)["total"] == 0
    assert week_grid(tt, SCHEDULE, 11)["total"] == 1


def test_week_grid_cross_period_course_appears_in_both_rows():
    """跨节次的课（1-4 节）应在 1-2 与 3-4 两行都出现，否则视觉上会「断成两截」。"""
    tt = _tt([Course(name="实验课", weekday=2, start_period=1, end_period=4, weeks=[5])])
    grid = week_grid(tt, SCHEDULE, 5)
    assert grid["rows"][0]["cells"][1][0]["name"] == "实验课"
    assert grid["rows"][1]["cells"][1][0]["name"] == "实验课"


def test_recognition_stats():
    courses = [
        Course(name="A", weekday=1, start_period=1, end_period=2, teacher="张三", location="教101"),
        Course(name="B", weekday=2, start_period=1, end_period=2, teacher="李四"),
        Course(name="C", weekday=3, start_period=1, end_period=2, location="教202"),
        Course(name="D", weekday=4, start_period=1, end_period=2, weeks_guessed=True),
    ]
    st = recognition_stats(courses)
    assert st["total"] == 4
    assert st["no_teacher"] == 2  # C、D
    assert st["no_location"] == 2  # B、D
    assert st["weeks_guessed"] == 1
    # 只有 A 同时具备教师和地点
    assert st["complete"] == 1


def test_week_bounds():
    tt = _tt([])  # week1 = 2026-09-07
    start, end = week_bounds(tt, 12)
    assert start == "11/23"
    assert end == "11/29"

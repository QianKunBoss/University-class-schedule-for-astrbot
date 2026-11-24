"""展示层：把 Timetable 转成前端直接可渲染的结构。

放在独立模块里而不是塞进 webapi，是因为「周网格」和「识别统计」与 HTTP 无关，
可以单独做单元测试，也方便以后复用到图片渲染。
"""

from __future__ import annotations

from datetime import date, timedelta

from .const import WEEKDAY_CN
from .models import Course, Timetable
from .term import week_dates


def course_card(course: Course, schedule) -> dict:
    """单元格里一张课程卡片需要的字段。"""
    start = schedule.period_start(course.start_period)
    end = schedule.period_end(course.end_period)
    return {
        "name": course.name,
        "teacher": course.teacher,
        "location": course.location,
        "start_period": course.start_period,
        "end_period": course.end_period,
        "time": f"{start}-{end}" if start and end else "",
        "weeks": list(course.weeks),
        "weeks_guessed": course.weeks_guessed,
        # 缺字段的会在面板里标黄，提示识别不完整
        "incomplete": not (course.teacher and course.location),
        # 面板编辑：自定义课程可改/删；教务课程可隐藏
        "manual": course.manual,
        "entry_id": course.entry_id,
        "fingerprint": course.fingerprint,
        # 教务调课标记（琼台红标 O/P 代表不同调课方式），面板显示为徽章
        "mark": getattr(course, "mark", ""),
    }


def week_grid(timetable: Timetable, schedule, week: int, today: date | None = None) -> dict:
    """生成「节次 × 星期」网格。

    Args:
        timetable: 课表。
        schedule: 作息配置（提供节次时间与 big_period_size）。
        week: 教学周。
        today: 用于标记「今天」那一列。
    """
    courses = [c for c in timetable.courses if c.active_in(week)]
    max_period = max((c.end_period for c in courses), default=schedule.big_period_size * 4)
    step = max(1, schedule.big_period_size)

    rows: list[dict] = []
    for start in range(1, max_period + 1, step):
        end = min(start + step - 1, max_period)
        cells: list[list[dict]] = []
        for wd in range(1, 8):
            day_courses = [
                c for c in courses if c.weekday == wd and c.start_period <= end and c.end_period >= start
            ]
            cells.append(
                [course_card(c, schedule) for c in sorted(day_courses, key=lambda x: x.start_period)]
            )
        rows.append(
            {
                "label": f"{start}-{end}" if end != start else str(start),
                "start": start,
                "end": end,
                "time": f"{schedule.period_start(start)}-{schedule.period_end(end)}",
                "cells": cells,
            }
        )

    days = []
    dates = week_dates(_week1(timetable), week)
    for i in range(7):
        d = dates[i] if i < len(dates) else None
        days.append(
            {
                "weekday": i + 1,
                "label": WEEKDAY_CN[i],
                "date": d.isoformat() if d else "",
                "short": f"{d.month}/{d.day}" if d else "",
                "today": bool(d and today and d == today),
            }
        )

    return {
        "week": week,
        "term": timetable.term,
        "term_name": timetable.term_name,
        "rows": rows,
        "days": days,
        "total": len(courses),
    }


def _week1(timetable: Timetable) -> date | None:
    try:
        y, m, d = (int(x) for x in timetable.week1_monday.split("-"))
        return date(y, m, d)
    except Exception:
        return None


def recognition_stats(courses: list[Course]) -> dict:
    """识别完整度统计，面板和 /kb status 都用它提示「有老师/教室没认出来」。"""
    total = len(courses)
    no_teacher = [c for c in courses if not c.teacher]
    no_location = [c for c in courses if not c.location]
    guessed = [c for c in courses if c.weeks_guessed]
    return {
        "total": total,
        "no_teacher": len(no_teacher),
        "no_location": len(no_location),
        "weeks_guessed": len(guessed),
        "complete": total - len({id(c) for c in no_teacher} | {id(c) for c in no_location}),
        "samples": [
            {"name": c.name, "teacher": c.teacher, "location": c.location, "raw": c.raw[:120]}
            for c in (no_teacher + no_location)[:5]
        ],
    }


def upcoming_day_courses(
    timetable: Timetable, schedule, week: int, day: date, now_minutes: int | None = None
) -> list[dict]:
    """某天剩余的课（面板「今天」小卡片用）。"""
    out = []
    for c in timetable.courses_on(week, day.weekday() + 1):
        start = schedule.period_start(c.start_period)
        hh, mm = (start.split(":") + ["0"])[:2] if start else ("", "")
        start_min = int(hh) * 60 + int(mm) if hh.isdigit() and mm.isdigit() else None
        if now_minutes is not None and start_min is not None and start_min < now_minutes:
            continue
        out.append(course_card(c, schedule))
    return out


def week_bounds(timetable: Timetable, week: int) -> tuple[str, str]:
    """第 N 周的起止日期（面板上显示「11/23 - 11/29」）。"""
    dates = week_dates(_week1(timetable), week)
    if not dates:
        return "", ""
    return dates[0].strftime("%m/%d"), (dates[0] + timedelta(days=6)).strftime("%m/%d")

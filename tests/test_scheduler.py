"""调度与去重测试。

这里的用例全部注入固定的 ``now``，不依赖真实时间，
所以无论在哪个时区、什么时间跑 CI，结果都稳定。
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jwkb_core.config import Config  # noqa: E402
from jwkb_core.const import TZ  # noqa: E402
from jwkb_core.models import Course, Timetable  # noqa: E402
from jwkb_core.scheduler import (  # noqa: E402
    NotifiedStore,
    brief_alert,
    diff_timetables,
    due_class_alerts,
)


def _config(**notify) -> Config:
    return Config.from_dict({"notify": notify})


def _timetable(courses) -> Timetable:
    return Timetable(
        term="2026-2027-1",
        term_name="2026-2027学年第一学期",
        courses=courses,
        week1_monday="2026-09-07",
    )


MONDAY = date(2026, 11, 23)  # 周一，第 12 周


def _at(hh: int, mm: int) -> datetime:
    return datetime(MONDAY.year, MONDAY.month, MONDAY.day, hh, mm, tzinfo=TZ)


def test_due_when_inside_window(tmp_path):
    cfg = _config(ahead_minutes=20, daily_brief_time="")
    tt = _timetable([Course(name="高等数学", weekday=1, start_period=1, end_period=2, weeks=[12])])
    store = NotifiedStore(tmp_path / "n.json")

    # 第 1 节 08:00，07:41 -> 还有 19 分钟
    alerts = due_class_alerts(tt, cfg, _at(7, 41), 12, store)
    assert len(alerts) == 1
    assert "19 分钟后" in alerts[0].text
    assert "高等数学" in alerts[0].text


def test_not_due_outside_window(tmp_path):
    cfg = _config(ahead_minutes=20, daily_brief_time="")
    tt = _timetable([Course(name="高等数学", weekday=1, start_period=1, end_period=2, weeks=[12])])
    store = NotifiedStore(tmp_path / "n.json")

    assert due_class_alerts(tt, cfg, _at(7, 35), 12, store) == []  # 还有 25 分钟
    assert due_class_alerts(tt, cfg, _at(8, 5), 12, store) == []  # 已上课 5 分钟


def test_no_duplicate_after_mark(tmp_path):
    cfg = _config(ahead_minutes=20, daily_brief_time="")
    tt = _timetable([Course(name="高等数学", weekday=1, start_period=1, end_period=2, weeks=[12])])
    store = NotifiedStore(tmp_path / "n.json")

    alerts = due_class_alerts(tt, cfg, _at(7, 41), 12, store)
    assert len(alerts) == 1
    store.mark(alerts[0].key)
    assert due_class_alerts(tt, cfg, _at(7, 42), 12, store) == []


def test_second_reminder_level(tmp_path):
    """20 分钟档发过后，5 分钟档应该还能再发一次（不同档位不同 key）。"""
    cfg = _config(ahead_minutes=20, second_ahead_minutes=5, daily_brief_time="")
    tt = _timetable([Course(name="高等数学", weekday=1, start_period=1, end_period=2, weeks=[12])])
    store = NotifiedStore(tmp_path / "n.json")

    first = due_class_alerts(tt, cfg, _at(7, 42), 12, store)
    assert len(first) == 1
    store.mark(first[0].key)

    assert due_class_alerts(tt, cfg, _at(7, 50), 12, store) == []  # 还有 10 分钟，20 档已发
    second = due_class_alerts(tt, cfg, _at(7, 56), 12, store)  # 还有 4 分钟，落入 5 档
    assert len(second) == 1
    assert second[0].key != first[0].key


def test_course_not_in_this_week(tmp_path):
    cfg = _config(ahead_minutes=20, daily_brief_time="")
    tt = _timetable([Course(name="单周课", weekday=1, start_period=1, end_period=2, weeks=[11])])
    store = NotifiedStore(tmp_path / "n.json")
    assert due_class_alerts(tt, cfg, _at(7, 41), 12, store) == []


def test_brief_once_per_day(tmp_path):
    cfg = _config(ahead_minutes=20, daily_brief_time="07:00", brief_skip_empty=True)
    tt = _timetable([Course(name="高等数学", weekday=1, start_period=1, end_period=2, weeks=[12])])
    store = NotifiedStore(tmp_path / "n.json")

    assert brief_alert(tt, cfg, _at(6, 30), 12, store) is None  # 未到点
    alert = brief_alert(tt, cfg, _at(7, 0), 12, store)
    assert alert is not None and "早上好" in alert.text
    store.mark(alert.key)
    assert brief_alert(tt, cfg, _at(7, 30), 12, store) is None


def test_brief_skipped_when_no_class(tmp_path):
    cfg = _config(ahead_minutes=20, daily_brief_time="07:00", brief_skip_empty=True)
    tt = _timetable([])
    store = NotifiedStore(tmp_path / "n.json")
    assert brief_alert(tt, cfg, _at(7, 0), 12, store) is None
    # 静默标记，避免每分钟重复判断
    assert store.seen(f"brief|{MONDAY.isoformat()}")


def test_diff_detects_location_change():
    old = _timetable(
        [Course(name="高等数学", weekday=1, start_period=1, end_period=2, weeks=[12], location="教101")]
    )
    new = _timetable(
        [Course(name="高等数学", weekday=1, start_period=1, end_period=2, weeks=[12], location="教202")]
    )
    changes = diff_timetables(old, new)
    assert len(changes) == 1 and "教202" in changes[0]


def test_diff_detects_add_and_remove():
    old = _timetable([Course(name="A", weekday=1, start_period=1, end_period=2, weeks=[12])])
    new = _timetable([Course(name="B", weekday=2, start_period=3, end_period=4, weeks=[12])])
    changes = diff_timetables(old, new)
    assert any("新增" in c for c in changes)
    assert any("取消" in c for c in changes)


def test_scheduler_accepts_provider_getter_kwarg(tmp_path):
    """main.py 会用 inspect 检查这个参数，用来判断安装目录是否新旧混杂。"""
    import inspect

    from jwkb_core.scheduler import Scheduler

    params = inspect.signature(Scheduler.__init__).parameters
    assert "provider_getter" in params


def test_provider_getter_can_be_injected_after_init(tmp_path):
    """兜底路径：不给构造参数、改用属性注入也要能生效（老版本核心包不认识该参数）。"""
    from jwkb_core.scheduler import Scheduler

    async def getter():
        return None

    sch = Scheduler(
        cfg_provider=lambda: _config(ahead_minutes=20),
        jw_provider=lambda: None,
        notifier=None,
        data_dir=tmp_path,
        log=None,
    )
    assert sch.provider_getter is None
    sch.provider_getter = getter
    assert sch.provider_getter is getter

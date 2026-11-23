"""周次与学期计算测试。

第 1 周算错一周，提醒就整体错位一周，所以这块必须有回归测试。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jwkb_core.config import ScheduleConf  # noqa: E402
from jwkb_core.term import (  # noqa: E402
    current_terms,
    current_week,
    estimate_week1,
    monday_of,
    parse_term_id,
    resolve_week1,
    term_display_name,
    week1_from_html,
    week_dates,
)


def test_parse_term_id():
    assert parse_term_id("2026-2027-1") == (2026, 1)
    assert parse_term_id("2026-2027-2") == (2026, 2)
    assert parse_term_id("bad") is None


def test_term_display_name():
    assert term_display_name("2026-2027-1") == "2026-2027学年第一学期"
    assert term_display_name("2026-2027-2") == "2026-2027学年第二学期"


def test_monday_of():
    assert monday_of(date(2026, 11, 23)) == date(2026, 11, 23)  # 周一
    assert monday_of(date(2026, 11, 26)) == date(2026, 11, 23)  # 周四


def test_current_week():
    week1 = date(2026, 9, 7)
    assert current_week(week1, date(2026, 9, 7)) == 1
    assert current_week(week1, date(2026, 9, 13)) == 1  # 周日仍算第 1 周
    assert current_week(week1, date(2026, 9, 14)) == 2
    assert current_week(week1, date(2026, 11, 23)) == 12
    assert current_week(week1, date(2026, 9, 1)) is None  # 开学前


def test_week_dates():
    days = week_dates(date(2026, 9, 7), 2)
    assert days[0] == date(2026, 9, 14)
    assert len(days) == 7


def test_resolve_week1_prefers_config():
    sch = ScheduleConf(term="2026-2027-1", week1_monday="2026-09-07")
    day, source = resolve_week1("<html>第5周</html>", sch, date(2026, 11, 23))
    assert day == date(2026, 9, 7)
    assert source == "config"


def test_resolve_week1_from_jw():
    sch = ScheduleConf(term="auto", week1_monday="")
    day, source = resolve_week1("<script>var nowWeek = 12;</script>", sch, date(2026, 11, 23))
    assert day == date(2026, 9, 7)  # 11-23 往前 11 周
    assert source == "jw"


def test_resolve_week1_guessed():
    """没有已知校历的学期 → 估算（来源 guessed）。"""
    sch = ScheduleConf(term="2029-2030-1", week1_monday="")
    day, source = resolve_week1("<html>没有周次信息</html>", sch, date(2029, 11, 23))
    assert source == "guessed"
    assert day == estimate_week1("2029-2030-1")


def test_current_terms():
    assert current_terms(date(2026, 11, 23))[0] == "2026-2027-1"
    assert current_terms(date(2027, 3, 1))[0] == "2026-2027-2"


@pytest.mark.parametrize("term", ["2026-2027-1", "2026-2027-2"])
def test_estimate_week1_is_monday(term):
    d = estimate_week1(term)
    assert d is not None and d.weekday() == 0


# ---------- week1_from_html 回归：教务页下拉框坑 ----------

TODAY = date(2026, 11, 23)  # 周一
W1 = date(2026, 9, 7)  # 第 12 周对应的第 1 周周一


def test_week1_from_selected_option():
    """周次下拉框里带 selected 的选项 = 教务认定的当前周，最可靠。"""
    html = """
    <select id="xq">
      <option value="11">第11周</option>
      <option value="12" selected="selected">第12周</option>
      <option value="13">第13周</option>
    </select>"""
    assert week1_from_html(html, TODAY) == W1


def test_week1_from_nowweek_var():
    assert week1_from_html("var nowWeek = 12;", TODAY) == W1


def test_week1_from_phrase():
    assert week1_from_html("您好，本周是第12周", TODAY) == W1


def test_week1_from_plain_week_when_unique():
    """下拉框外的唯一周次也可以采信。"""
    html = "<div>当前教学周：第12周</div>"
    assert week1_from_html(html, TODAY) == W1


def test_week1_dropdown_without_selected_returns_none():
    """回归：下拉框里第1周..第20周全是候选，绝不能命中第1周
    （旧 bug：会把第1周周一算成本周一，整个课表错位一个学期）。"""
    options = "".join(f"<option>{i}周</option>" for i in range(1, 21))
    html = f"<select>{options}</select>"
    assert week1_from_html(html, TODAY) is None


def test_week1_multiple_ambiguous_weeks_returns_none():
    """下拉框外出现多个不同周次且无 current 标记 → 不猜。"""
    html = "<div>第3周考试</div><div>第12周上课</div>"
    assert week1_from_html(html, TODAY) is None


def test_estimate_week1_qtnu_fall_2026():
    """琼台 2026-2027-1：9月1-4日准备周，第一周周一 = 2026-09-07。
    旧算法 monday_of(9月1日) = 08-31，整体错一周。"""
    assert estimate_week1("2026-2027-1") == date(2026, 9, 7)


def test_estimate_week1_qtnu_spring_2027():
    assert estimate_week1("2026-2027-2") == date(2027, 3, 1)


def test_resolve_week1_uses_preset_over_guess():
    from jwkb_core.term import KNOWN_WEEK1, resolve_week1

    sch = ScheduleConf(term="2026-2027-1")
    week1, source = resolve_week1("<option>第1周</option>", sch, date(2026, 11, 23))
    assert week1 == KNOWN_WEEK1["2026-2027-1"] == date(2026, 9, 7)
    assert source == "preset"


def test_resolve_week1_config_beats_preset():
    sch = ScheduleConf(term="2026-2027-1", week1_monday="2026-09-07")
    from jwkb_core.term import resolve_week1

    week1, source = resolve_week1("", sch)
    assert source == "config"

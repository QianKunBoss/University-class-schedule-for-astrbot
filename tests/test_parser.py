"""解析器单元测试。

样本 ``fixtures/xskb_sample.html`` 是脱敏的结构样本，覆盖四个最容易出错的点：
1. ``<font title>`` 结构化字段；
2. 同一格里两门课（``kbcontent1`` / ``kbcontent2``）；
3. 单周（``1-16周(单)``）；
4. ``rowspan`` 合并导致的跨大节课程。

真实教务页面拿到后，应把快照另存一份到这里做回归测试（记得先脱敏）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jwkb_core.errors import ParseError  # noqa: E402
from jwkb_core.parser import _classify_lines, parse_timetable, parse_weeks  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "xskb_sample.html"


@pytest.fixture(scope="module")
def courses():
    html = FIXTURE.read_text(encoding="utf-8")
    return parse_timetable(html, max_week=20, big_period_size=2)


def _find(courses, name):
    matches = [c for c in courses if c.name == name]
    assert matches, f"没解析出课程 {name}，实际有：{[c.name for c in courses]}"
    return matches[0]


def test_font_fields(courses):
    c = _find(courses, "高等数学")
    assert c.weekday == 1
    assert (c.start_period, c.end_period) == (1, 2)
    assert c.teacher == "张三"
    assert c.location == "教101"
    assert c.weeks == list(range(1, 17))
    assert c.weeks_guessed is False


def test_two_courses_in_one_cell(courses):
    en = _find(courses, "大学英语")
    pe = _find(courses, "体育")
    assert en.weeks == list(range(1, 9))
    assert pe.weeks == list(range(9, 17))
    assert (en.start_period, en.end_period) == (3, 4)
    assert (pe.start_period, pe.end_period) == (3, 4)


def test_odd_weeks_and_heuristic_fields(courses):
    c = _find(courses, "数据结构")
    assert c.weeks == [1, 3, 5, 7, 9, 11, 13, 15]
    assert c.teacher == "赵六"
    assert c.location == "机房A"
    assert (c.start_period, c.end_period) == (5, 6)


def test_rowspan_cross_periods(courses):
    c = _find(courses, "大学物理")
    assert c.weekday == 2
    # 第四、第五大节合并，big_period_size=2 -> 第 7~10 节
    assert (c.start_period, c.end_period) == (7, 10)
    assert c.weeks == list(range(1, 16))


def test_sorted_and_unique(courses):
    keys = [(c.weekday, c.start_period) for c in courses]
    assert keys == sorted(keys)
    assert len(courses) == 5


@pytest.mark.parametrize(
    "text,expected,guessed",
    [
        ("1-16周", list(range(1, 17)), False),
        ("1-8,10-16周", list(range(1, 9)) + list(range(10, 17)), False),
        ("1-16周(单)", [1, 3, 5, 7, 9, 11, 13, 15], False),
        ("1-16周(双)", [2, 4, 6, 8, 10, 12, 14, 16], False),
        ("第3周", [3], False),
        ("", [], True),
        ("未安排", [], True),
    ],
)
def test_parse_weeks(text, expected, guessed):
    weeks, is_guessed = parse_weeks(text, max_week=20)
    assert weeks == expected
    assert is_guessed is guessed


def test_empty_html_raises():
    with pytest.raises(ParseError):
        parse_timetable("<html></html>")


def test_no_table_raises():
    with pytest.raises(ParseError):
        parse_timetable("<html><body><p>登录超时</p></body></html>")


# ---------- 混排行/碎片清理（琼台真实页面踩坑） ----------


def test_classify_location_and_weeks_on_same_line():
    """「世贸楼301（第1-16周）(周次)」这种混排行：教室和周次都要拿到。"""
    fields = _classify_lines(["世贸楼301（第1-16周）(周次)", "薛朝秋", "大学英语(英文C)"], {})
    assert fields["location"] == "世贸楼301"
    weeks, guessed = parse_weeks(fields["weeks"])
    assert weeks == list(range(1, 17))
    assert not guessed


def test_classify_name_keeps_parentheses():
    """课程名里的 (英文C) 不能被当成碎片剥掉。"""
    fields = _classify_lines(["大学英语(英文C)", "薛朝秋"], {})
    assert fields["name"] == "大学英语(英文C)"


def test_classify_name_strip_week_paren():
    """名字尾部粘连的（周次）碎片要清掉。"""
    fields = _classify_lines(["大学心理健康教育（周次）", "张三"], {})
    assert fields["name"] == "大学心理健康教育"


def test_classify_location_with_paren_week_tail():
    """教室后面粘着不含数字的 (周) 尾巴也要剥掉。"""
    fields = _classify_lines(["厚德楼（周次）", "周勇"], {})
    assert fields["location"] == "厚德楼"
    assert fields["name"] == "周勇"  # 只剩一行，最长者当课程名


def test_parse_weeks_multi_ranges_like_real_page():
    """真实页面里出现的复合周次：8-10,9-10,11-13周 / 5-11,16-18周。"""
    assert parse_weeks("8-10,9-10,11-13周")[0] == list(range(8, 14))
    assert parse_weeks("5-11,16-18周")[0] == list(range(5, 12)) + list(range(16, 19))
    assert parse_weeks("2-6,13周")[0] == list(range(2, 7)) + [13]


# ---------- 续块省略课程名（琼台页面真实结构） ----------


def test_parse_cell_continuation_blocks_inherit_name():
    """同格续块只写 教师/周次/教室，课程名沿用上一块；
    分隔线不能泄漏成课程名，教室行不能被当成课程名。"""
    headers = "".join(f"<th>星期{d}</th>" for d in ["一", "二", "三", "四", "五", "六", "日"])
    blanks = "<td></td>" * 6
    html = (
        "<table>"
        f"<tr><th>节次</th>{headers}</tr>"
        "<tr><td>1-2节</td>"
        '<td><div class="kbcontent">'
        "程序设计基础<br/>宁营<br/>2周[01-02节]<br/>报国楼（原理A）401（本部）<br/>"
        "--------------------<br/>于营<br/>4周[01-02节]<br/>报国楼（原理A）401（本部）"
        "</div></td>"
        f"{blanks}</tr></table>"
    )
    courses = parse_timetable(html, big_period_size=2)
    assert len(courses) == 2
    assert {c.name for c in courses} == {"程序设计基础"}
    assert {c.teacher for c in courses} == {"宁营", "于营"}
    assert sorted(tuple(c.weeks) for c in courses) == [(2,), (4,)]
    assert all(c.location == "报国楼（原理A）401（本部）" for c in courses)
    assert all(not SEPARATOR_LIKE(c.name) for c in courses)


def SEPARATOR_LIKE(text):
    import re as _re

    return bool(_re.fullmatch(r"[\s\-—–_=＝]{4,}", text or ""))


def test_classify_teacher_only_block_with_default_name():
    fields = _classify_lines(
        ["于营", "4周[01-02节]", "报国楼（原理A）401（本部）"], {}, default_name="程序设计基础"
    )
    assert fields["name"] == "程序设计基础"
    assert fields["teacher"] == "于营"
    assert fields["location"] == "报国楼（原理A）401（本部）"
    weeks, _ = parse_weeks(fields["weeks"])
    assert weeks == [4]


def test_classify_dash_separator_never_becomes_name():
    fields = _classify_lines(
        ["------------------", "报国楼（原理A）401（本部）"], {}, default_name="程序设计基础"
    )
    assert fields["name"] == "程序设计基础"
    assert fields["location"] == "报国楼（原理A）401（本部）"


def test_classify_first_block_teacher_like_name_still_works():
    """没有 default_name 时，两字课程名（如「体育」）仍能正常识别。"""
    fields = _classify_lines(["体育", "冯功辉"], {})
    assert fields["name"] == "体育"
    assert fields["teacher"] == "冯功辉"


# ---------- 琼台真实页面结构（kbcontent1/kbcontent 双 div + 调课红标） ----------

REAL_CELL = (
    "<table>"
    "<tr><th>节次</th><th>星期一</th><th>星期二</th><th>星期三</th>"
    "<th>星期四</th><th>星期五</th><th>星期六</th><th>星期日</th></tr>"
    "<tr><td>01-02节</td><td></td><td></td><td></td><td></td>"
    "<td>"
    '<input type="hidden" name="jx0415zbdiv_1" value="X-5-1">'
    '<div id="X-5-1" class="kbcontent1">大学英语（一）<br/>'
    "<font title='周次(节次)'>2(周)</font><br/>"
    "<font title='教室'>乐教楼（原实D）505（桂）</font><br/>"
    "----------------------<br>大学英语（一）<br/>"
    "<font title='周次(节次)'>3(周)</font><br/>"
    "<font title='教室'>乐教楼（原实D）505（桂）</font></div>"
    '<div id="X-5-2" style="display: none;" class="kbcontent">大学英语（一）<br/>'
    "<font title='老师'>毛燕</font><br/>"
    "<font title='周次(节次)'>2(周)[01-02节]</font><br/>"
    "<font title='教室'>乐教楼（原实D）505（桂）</font><br/>"
    "---------------------<br>大学英语（一）<br/>"
    "<font title='老师'>毛燕</font><br/>"
    "<font title='周次(节次)'>3(周)[01-02节]</font><br/>"
    "<font title='教室'>乐教楼（原实D）505（桂）</font><br/>"
    "----------------------<br>大学英语（一）<br/>"
    "<font title='老师'>毛燕</font><br/>"
    "<font title='周次(节次)'>7-16(周)</font><br/>"
    "<font title='教室'>乐教楼（原实D）505（桂）</font></div>"
    "</td><td></td><td></td><td></td></tr></table>"
)


def test_real_qtnu_cell_structure():
    """完整版 kbcontent 优先、调课红标不进名字、每段周次独立。"""
    courses = parse_timetable(REAL_CELL, max_week=20, big_period_size=2)
    # 只解析隐藏的完整版，不会因可见简版产生重复
    assert len(courses) == 3
    assert all(c.name == "大学英语（一）" for c in courses)
    assert all(c.teacher == "毛燕" for c in courses)
    assert all(c.location == "乐教楼（原实D）505（桂）" for c in courses)
    assert all((c.start_period, c.end_period) == (1, 2) for c in courses)
    assert sorted(tuple(c.weeks) for c in courses) == [(2,), (3,), tuple(range(7, 17))]
    assert not any("&nbsp" in c.name for c in courses)


def test_real_qtnu_cell_with_red_marker():
    """调课红标 <font color='red'>&nbspO</font> 不能混进课程名。"""
    cell_html = (
        "<table>"
        "<tr><th>节次</th><th>星期一</th><th>星期二</th><th>星期三</th>"
        "<th>星期四</th><th>星期五</th><th>星期六</th><th>星期日</th></tr>"
        "<tr><td>05-06节</td>"
        '<td><div class="kbcontent">程序设计基础'
        "<span><font color='red'>&nbspO</font></span>于营<br/>"
        "<font title='周次(节次)'>3(周)[05-06节]</font><br/>"
        "<font title='教室'>报国楼（原理A）304（桂）</font></div></td>"
        "<td></td><td></td><td></td><td></td><td></td><td></td></tr></table>"
    )
    courses = parse_timetable(cell_html, max_week=20, big_period_size=2)
    assert len(courses) == 1
    assert courses[0].name == "程序设计基础"
    assert courses[0].teacher == "于营"
    assert courses[0].weeks == [3]
    assert (courses[0].start_period, courses[0].end_period) == (5, 6)


def test_real_page_snapshot_if_present():
    """有真实抓取样本时做整页回归（本地文件，不入库）。"""
    real = Path(__file__).resolve().parent.parent / "debug_qtnu_xskb.html"
    if not real.exists():
        pytest.skip("本地无真实页面样本")
    courses = parse_timetable(
        real.read_text(encoding="utf-8", errors="replace"), max_week=20, big_period_size=2
    )
    assert len(courses) >= 60
    assert not any("&nbsp" in c.name for c in courses)
    assert not any(c.name.startswith("-") for c in courses)
    # 绝大多数课程应识别出教师
    assert len([c for c in courses if c.teacher]) >= len(courses) * 0.9

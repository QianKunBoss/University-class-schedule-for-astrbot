"""周次计算：第 1 周周一、当前第几周、学期 id 解析。

第 1 周周一（``week1_monday``）是整套提醒功能的地基 —— 算错一周，
提醒就会整体错位一周。因此这里提供三级来源，并在状态里显示实际用了哪一级：

1. 配置里填的值（用户显式指定，优先级最高，``/kb week1`` 会写这里）；
2. 教务系统页面里自带的「当前周」反推（首次运行无需配置即可用）；
3. 按学期 id 估算（标记为 ``guessed``，状态里会提示用户核对）。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from .const import TZ

TERM_RE = re.compile(r"^(\d{4})-(\d{4})-(\d)$")


def monday_of(d: date) -> date:
    """返回某天所在自然周的周一。"""
    return d - timedelta(days=d.weekday())


def today_cn() -> date:
    """北京时间下的今天（不依赖容器 TZ）。"""
    return datetime.now(TZ).date()


def now_cn() -> datetime:
    return datetime.now(TZ)


def parse_term_id(term: str) -> tuple[int, int] | None:
    """``2026-2027-1`` -> (2026, 1)；格式非法返回 None。"""
    m = TERM_RE.match((term or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(3))


def term_display_name(term: str) -> str:
    """``2026-2027-1`` -> ``2026-2027学年第一学期``。"""
    m = TERM_RE.match((term or "").strip())
    if not m:
        return term or ""
    cn = {"1": "第一学期", "2": "第二学期", "3": "第三学期"}.get(m.group(3), f"第{m.group(3)}学期")
    return f"{m.group(1)}-{m.group(2)}学年{cn}"


def current_terms(today: date | None = None) -> list[str]:
    """按时间推算「可能的当前学期」，最近的在最前。

    9 月~次年 1 月是秋季学期，2 月~7 月是春季学期，8 月算作秋季学期前的假期。
    """
    d = today or today_cn()
    y = d.year
    if d.month >= 8:
        return [f"{y}-{y + 1}-1", f"{y - 1}-{y}-2", f"{y}-{y + 1}-2"]
    if d.month == 1:
        return [f"{y - 1}-{y}-1", f"{y - 1}-{y}-2", f"{y}-{y + 1}-1"]
    return [f"{y - 1}-{y}-2", f"{y - 1}-{y}-1", f"{y}-{y + 1}-1"]


def next_monday_on_or_after(d: date) -> date:
    """返回 d 当天（若已是周一）或之后最近的周一。"""
    return d + timedelta(days=(7 - d.weekday()) % 7)


def estimate_week1(term: str) -> date | None:
    """按学期 id 估算第 1 周周一（仅作最后兜底）。

    教务页面不含当前周信息时只能估算。琼台的校历模式（官方 2026-2027 学年）：
    9 月 1-4 日为准备周，老生周末报到后**下一个周一**开始第一周；
    第二学期同理（2 月底报到，3 月初上课）。因此取「报到日之后」的第一个周一，
    而不是 9 月 1 日所在周的周一 —— 后者会把准备周当成第一周（真实踩过的坑）。
    """
    parsed = parse_term_id(term)
    if not parsed:
        return None
    year, no = parsed
    if no == 1:
        # 第一个 ≥9月5日 的周一（2026 年：9月5日周六 → 9月7日周一）
        return next_monday_on_or_after(date(year, 9, 5))
    if no == 2:
        # 第一个 ≥2月26日 的周一（2027 年：2月27日周六 → 3月1日周一）
        return next_monday_on_or_after(date(year + 1, 2, 26))
    return monday_of(date(year + 1, 7, 1))


# 已知校历（官方发布的第一学期第一周周一）。教务页面不含当前周，
# 这里的值优先于估算；用户在面板「第1周周一」里填的日期优先级最高，会覆盖这里。
KNOWN_WEEK1 = {
    "2026-2027-1": date(2026, 9, 7),
}


# 「当前是第几周」的可靠来源，按优先级排列。
# 注意：教务页的周次下拉框里「第1周..第20周」全都是普通 <option>，
# 直接搜「第N周」会命中第1周，把第1周周一算成本周（真实踩过的 bug）。
_WEEK_OPTION_SELECTED_RE = re.compile(r"<option[^>]*selected[^>]*>[^<]*?第\s*(\d{1,2})\s*周", re.I)
_NOW_PATTERNS = (
    re.compile(r"nowWeek\s*[:=]\s*['\"]?(\d{1,2})", re.I),
    re.compile(r"zhouci\s*[:=]\s*['\"]?(\d{1,2})", re.I),
    re.compile(r"(?:本周|当前|今天)(?:是|为|处在)?\s*第?\s*(\d{1,2})\s*周"),
    re.compile(r"周次\s*[：:]\s*第?\s*(\d{1,2})\s*周"),
)
_WEEK_OPTION_RE = re.compile(r"<option[^>]*>\s*第\s*(\d{1,2})\s*周", re.I)


def _pick_week(week: int) -> int | None:
    return week if 1 <= week <= 30 else None


def week1_from_html(html: str, today: date | None = None) -> date | None:
    """从教务页面里读「当前第几周」，反推第 1 周周一。

    按可靠性排序：
    1. 周次下拉框里带 ``selected`` 的选项（教务把当前周标记在这里，最可靠）；
    2. 页面 JS 变量 ``nowWeek`` / ``zhouci``；
    3. 「本周是第N周」「周次：第N周」这类文案；
    4. 以上都没有时，若「第N周」只出现了一种周次（且不在下拉框里），才采信。
    """
    d = today or today_cn()
    src = html or ""

    m = _WEEK_OPTION_SELECTED_RE.search(src)
    if m:
        week = _pick_week(int(m.group(1)))
        if week:
            return monday_of(d) - timedelta(days=7 * (week - 1))

    for pattern in _NOW_PATTERNS:
        m = pattern.search(src)
        if m:
            week = _pick_week(int(m.group(1)))
            if week:
                return monday_of(d) - timedelta(days=7 * (week - 1))

    # 去掉下拉框候选后，只有唯一周次时才采信（多个说明都是候选，无法判断当前周）
    stripped = _WEEK_OPTION_RE.sub(" ", src)
    weeks = {int(m.group(1)) for m in re.finditer(r"第\s*(\d{1,2})\s*周", stripped)}
    weeks = {w for w in weeks if 1 <= w <= 30}
    if len(weeks) == 1:
        week = weeks.pop()
        return monday_of(d) - timedelta(days=7 * (week - 1))
    return None


def resolve_week1(html: str, schedule, today: date | None = None) -> tuple[date | None, str]:
    """确定第 1 周周一。

    Returns:
        (日期, 来源)。来源取值 ``config`` / ``jw`` / ``guessed`` / ``none``。
    """
    configured = getattr(schedule, "week1_date", None)
    if configured:
        return configured, "config"

    # 已知校历（官方发布值），比估算可靠
    term = getattr(schedule, "term", "") or ""
    known = KNOWN_WEEK1.get(term)
    if known:
        return known, "preset"

    from_jw = week1_from_html(html, today)
    if from_jw:
        return from_jw, "jw"

    term = getattr(schedule, "term", "") or ""
    guessed = estimate_week1(term)
    if guessed:
        return guessed, "guessed"
    return None, "none"


def current_week(week1: date | None, today: date | None = None) -> int | None:
    """今天属于第几周；开学前返回 None。"""
    if not week1:
        return None
    d = today or today_cn()
    days = (d - week1).days
    if days < 0:
        return None
    return days // 7 + 1


def week_dates(week1: date | None, week: int) -> list[date]:
    """返回某周 7 天的日期列表（周一到周日）。"""
    if not week1 or week < 1:
        return []
    start = week1 + timedelta(days=7 * (week - 1))
    return [start + timedelta(days=i) for i in range(7)]


def weekday_of(d: date) -> int:
    """1=周一 ... 7=周日。"""
    return d.weekday() + 1

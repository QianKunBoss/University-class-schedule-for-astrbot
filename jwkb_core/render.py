"""文本渲染：把课表渲染成聊天里好看的文本，以及给文转图用的 HTML 模板。

渲染与逻辑分离：``scheduler`` 只负责判断「该不该发」，这里只负责「长什么样」。
这样改文案不会碰到提醒逻辑，单测也更容易写。
"""

from __future__ import annotations

from datetime import date

from .const import WEEKDAY_CN
from .models import Course, Timetable

DIVIDER = "─" * 14


def _period_range(course: Course, schedule) -> str:
    """``08:00-09:40``；作息表里没有对应节次时只显示节号。"""
    start = schedule.period_start(course.start_period)
    end = schedule.period_end(course.end_period)
    if start and end:
        return f"{start}-{end}"
    return ""


def _course_line(course: Course, schedule, index: int | None = None) -> str:
    span = _period_range(course, schedule)
    head = f"{span} " if span else ""
    parts = [f"{head}{course.name}"]
    if course.location:
        parts.append(f"@{course.location}")
    line = " ".join(parts)
    if index is not None:
        return f"{index}. {line}"
    return f"• {line}"


def day_text(
    timetable: Timetable,
    schedule,
    week: int,
    day: date,
    title_prefix: str = "",
) -> str:
    """渲染某一天的课表。"""
    weekday = day.weekday() + 1
    courses = timetable.courses_on(week, weekday)
    head = f"{title_prefix}{day.month}月{day.day}日 {WEEKDAY_CN[weekday - 1]} · 第{week}周"
    if not courses:
        return f"📅 {head}\n{DIVIDER}\n今天没有课，好好休息 ☕"

    lines = [f"📅 {head}", DIVIDER]
    for i, c in enumerate(courses, start=1):
        lines.append(_course_line(c, schedule, i))
        extra = " ".join(x for x in [c.teacher, f"第{c.start_period}-{c.end_period}节"] if x)
        if extra:
            lines.append(f"    {extra}")
    lines.append(DIVIDER)
    lines.append(f"共 {len(courses)} 节课")
    return "\n".join(lines)


def week_text(timetable: Timetable, schedule, week: int) -> str:
    """渲染整周课表（按天分段，群里刷屏也不至于太长）。"""
    lines = [f"🗓 第{week}周课表（{timetable.term_name or timetable.term}）"]
    total = 0
    for wd in range(1, 8):
        courses = timetable.courses_on(week, wd)
        if not courses:
            continue
        total += len(courses)
        lines.append("")
        lines.append(f"{WEEKDAY_CN[wd - 1]}")
        for c in courses:
            span = _period_range(c, schedule)
            loc = f" @{c.location}" if c.location else ""
            lines.append(f"  {span} {c.name}{loc}")
    if total == 0:
        return f"🗓 第{week}周没有课"
    lines.append("")
    lines.append(f"共 {total} 节")
    return "\n".join(lines)


def alert_text(course: Course, schedule, minutes_left: int, start_hm: str) -> str:
    """上课提醒。"""
    if minutes_left <= 0:
        title = "🔔 上课时间到"
    else:
        title = f"⏰ {minutes_left} 分钟后上课"
    end_hm = schedule.period_end(course.end_period)
    span = f"{start_hm}-{end_hm}" if end_hm else start_hm
    lines = [
        title,
        "",
        f"📘 {course.name}",
        f"🕐 {span}（第{course.start_period}-{course.end_period}节）",
    ]
    if course.location:
        lines.append(f"📍 {course.location}")
    if course.teacher:
        lines.append(f"👤 {course.teacher}")
    return "\n".join(lines)


def brief_text(timetable: Timetable, schedule, week: int, day: date) -> str:
    """每日早报：今天上什么课。"""
    weekday = day.weekday() + 1
    courses = timetable.courses_on(week, weekday)
    head = f"☀️ 早上好！今天是 {day.month}月{day.day}日 {WEEKDAY_CN[weekday - 1]}（第{week}周）"
    if not courses:
        return f"{head}\n\n今天没有课 🎉"
    lines = [head, "", f"今天有 {len(courses)} 节课："]
    for c in courses:
        span = _period_range(c, schedule)
        loc = f" @{c.location}" if c.location else ""
        lines.append(f"  {span} {c.name}{loc}")
    first = schedule.period_start(courses[0].start_period)
    if first:
        lines.append("")
        lines.append(f"第一节课 {first} 开始，别迟到～")
    return "\n".join(lines)


def status_text(data: dict) -> str:
    """``/kb status`` 的输出。"""
    lines = [
        "📊 课表插件状态",
        DIVIDER,
        f"认证方式：{data.get('auth_mode', '-')}",
        f"账号：{data.get('account', '-')}",
        f"教务地址：{data.get('base_url', '-')}",
        f"SSO 回调：{data.get('service_url', '-')}",
        f"学期：{data.get('term_name') or data.get('term', '-')}",
        f"当前周：{data.get('week', '-')}（第1周周一 {data.get('week1', '-')}，来源 {data.get('week1_source', '-')}）",
        f"课表缓存：{data.get('cache', '-')}",
        f"课程数：{data.get('course_count', 0)}",
        DIVIDER,
        f"提醒：{'开' if data.get('notify_enabled') else '关'}，提前 {data.get('ahead', 0)} 分钟",
        f"早报：{data.get('brief', '关闭')}",
        f"推送目标：{data.get('targets', 0)} 个",
        f"验证码识别：{data.get('ocr', '-')}",
    ]
    note = data.get("note")
    if note:
        lines.append("")
        lines.append(f"⚠️ {note}")
    err = data.get("last_error")
    if err:
        lines.append("")
        lines.append(f"❌ 最近错误：{err}")
    return "\n".join(lines)


HELP_TEXT = """📖 课表插件用法

/kb today — 今天上什么
/kb tomorrow — 明天上什么
/kb week [n] — 本周/第 n 周/下周(next)
/kb day [周几] — 指定某天，如 /kb day 周三
/kb refresh — 立即重新抓取课表
/kb status — 查看登录状态与配置
/kb umo — 显示当前会话标识

管理员：/kb push here — 把本会话设为推送目标
/kb ahead 20 — 提前 20 分钟提醒
/kb brief 07:00 — 每日早报时间（off 关闭）
/kb notify on|off — 总开关
/kb term [学期] — 查看/切换学期
/kb week1 2026-09-07 — 校正第 1 周周一
/kb period show — 查看作息时间表"""


WEEK_HTML_TMPL = """<!doctype html>
<html><head><meta charset="utf-8"><style>
  body{margin:0;padding:20px;background:#fff;font-family:"Microsoft YaHei",sans-serif;color:#1f2937;width:760px}
  h1{font-size:22px;margin:0 0 4px}
  .sub{color:#6b7280;font-size:14px;margin-bottom:14px}
  table{border-collapse:collapse;width:100%;table-layout:fixed}
  th,td{border:1px solid #e5e7eb;padding:6px;font-size:13px;vertical-align:top}
  th{background:#f3f4f6;font-weight:600}
  td.p{background:#fafafa;text-align:center;color:#6b7280;width:64px}
  .c{background:#eef4ff;border-left:3px solid #3b6fd4;border-radius:4px;padding:5px;margin:3px 0}
  .n{font-weight:600;color:#1e3a8a}
  .m{color:#6b7280;font-size:12px}
</style></head><body>
<h1>{{ title }}</h1>
<div class="sub">{{ subtitle }}</div>
<table>
  <tr><th class="p">节次</th>{% for d in days %}<th>{{ d }}</th>{% endfor %}</tr>
  {% for row in rows %}
  <tr>
    <td class="p">{{ row.label }}</td>
    {% for cell in row.cells %}
    <td>{% for c in cell %}<div class="c"><div class="n">{{ c.name }}</div><div class="m">{{ c.location }}</div></div>{% endfor %}</td>
    {% endfor %}
  </tr>
  {% endfor %}
</table>
</body></html>"""

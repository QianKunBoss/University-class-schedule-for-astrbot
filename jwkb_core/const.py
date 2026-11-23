"""全局常量。

注意：本文件不得出现任何具体学校的域名或名称 —— 插件面向市场发布，
所有学校相关的值都必须来自用户配置（见 _conf_schema.json 的 jw 分组）。
"""

from __future__ import annotations

PLUGIN_NAME = "astrbot_plugin_jw_timetable"
PLUGIN_DISPLAY_NAME = "教务课表"
PLUGIN_VERSION = "0.3.8"
PLUGIN_AUTHOR = "QianKunBoss"
PLUGIN_DESC = "连接强智/SSO 教务系统实时抓取课表，上课前自动提醒"

# 全部时间计算固定使用北京时间，不依赖容器 TZ 环境变量。
# Windows 或精简镜像可能没有 tzdata，此时退回固定 +08:00 时区，
# 中国全境无夏令时，两者行为完全一致，不会影响提醒准确性。
try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - 仅在没有 tzdata 的环境触发
    from datetime import timedelta, timezone

    TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
WEEKDAY_ALIASES = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "日": 7,
    "天": 7,
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "7": 7,
}

# 内置作息时间表：仅在用户未配置 schedule.periods 时兜底使用。
# 各校作息差异很大，README 与 /kb status 都会提示用户核对。
DEFAULT_PERIODS: list[dict] = [
    {"index": 1, "start": "08:00", "end": "08:45"},
    {"index": 2, "start": "08:55", "end": "09:40"},
    {"index": 3, "start": "10:00", "end": "10:45"},
    {"index": 4, "start": "10:55", "end": "11:40"},
    {"index": 5, "start": "14:30", "end": "15:15"},
    {"index": 6, "start": "15:25", "end": "16:10"},
    {"index": 7, "start": "16:20", "end": "17:05"},
    {"index": 8, "start": "17:15", "end": "18:00"},
    {"index": 9, "start": "19:30", "end": "20:15"},
    {"index": 10, "start": "20:25", "end": "21:10"},
    {"index": 11, "start": "21:20", "end": "22:05"},
    {"index": 12, "start": "22:15", "end": "23:00"},
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# 单次推送去重记录的保留天数
NOTIFIED_KEEP_DAYS = 3
# 快照保留份数上限
SNAPSHOT_KEEP = 10

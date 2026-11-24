"""定时调度：用「每分钟 tick + 计算」的方式决定该不该发提醒。

为什么不用「每节课挂一个 APScheduler job」：
* 课表会变（调课、换教室、停课），挂死的 job 立刻失配；
* 插件重启后 job 要全部重建，容易漏；
* tick 是幂等的 —— 重启、改表、系统时间跳变都能自愈，
  真正的去重交给落盘的 ``notified.json``。

所有判断都写成纯函数（``due_class_alerts`` / ``diff_timetables``），
方便用注入 ``now`` 的方式做单元测试。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from datetime import datetime
from datetime import time as dtime
from pathlib import Path

from . import render, term
from .config import Config
from .const import TZ
from .errors import AuthError, JwError
from .models import Timetable
from .parser import parse_timetable
from .storage import read_json, write_json

# 登录失败后的退避时间（秒）。
# 不加退避的话，配置错误时插件会每分钟登录一次，既刷日志也容易被学校判为异常访问。
BACKOFF_AUTH = 1800  # 账号/配置问题：不会自愈，等久一点
BACKOFF_OTHER = 300  # 网络或解析问题：5 分钟后重试


@dataclass
class Alert:
    kind: str  # class / brief / change
    key: str
    text: str


class NotifiedStore:
    """已推送记录，跨重启去重。键形如 ``2026-11-23|<课程uid>|20``。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict[str, float] = read_json(self.path, {}) or {}

    def seen(self, key: str) -> bool:
        return key in self.data

    def mark(self, key: str) -> None:
        self.data[key] = time.time()
        self.flush()

    def flush(self) -> None:
        try:
            write_json(self.path, self.data)
        except Exception:
            pass

    def prune(self, keep_days: int = 3) -> None:
        cutoff = time.time() - keep_days * 86400
        pruned = {k: v for k, v in self.data.items() if float(v) >= cutoff}
        if len(pruned) != len(self.data):
            self.data = pruned
            self.flush()


# ---------------- 纯函数：该不该发 ----------------
def due_class_alerts(
    timetable: Timetable,
    cfg: Config,
    now: datetime,
    week: int,
    store: NotifiedStore,
) -> list[Alert]:
    """返回本分钟应当推送的上课提醒。

    档位选择规则：取「能覆盖当前剩余分钟数的最小档位」。
    例如 20/5 两档，剩 19 分钟命中 20 档、剩 4 分钟命中 5 档，
    这样两档不会互相顶掉，也不会同一节课反复触发。
    """
    leads = sorted({x for x in (cfg.notify.ahead_minutes, cfg.notify.second_ahead_minutes) if x > 0})
    if not leads:
        return []

    today = now.date()
    weekday = today.weekday() + 1
    alerts: list[Alert] = []

    for course in timetable.courses_on(week, weekday):
        start_hm = cfg.schedule.period_start(course.start_period)
        if not start_hm:
            continue
        try:
            hh, mm = (int(x) for x in start_hm.split(":"))
        except ValueError:
            continue
        start_dt = datetime.combine(today, dtime(hh, mm), tzinfo=TZ)
        minutes_left = (start_dt - now).total_seconds() / 60.0

        # 已开课超过 1 分钟就不再提醒（宁可漏，不要课后才提醒）
        if minutes_left < -1:
            continue

        matched = next((lead for lead in leads if minutes_left <= lead), None)
        if matched is None:
            continue

        key = f"{today.isoformat()}|{course.uid}|{matched}"
        if store.seen(key):
            continue

        alerts.append(
            Alert(
                kind="class",
                key=key,
                text=render.alert_text(course, cfg.schedule, int(round(minutes_left)), start_hm),
            )
        )
    return alerts


def brief_alert(
    timetable: Timetable,
    cfg: Config,
    now: datetime,
    week: int,
    store: NotifiedStore,
) -> Alert | None:
    """每日早报：到点且当天未发过才返回。"""
    hm = cfg.notify.daily_brief_time
    if not hm:
        return None
    try:
        hh, mm = (int(x) for x in hm.split(":"))
    except ValueError:
        return None
    if (now.hour, now.minute) < (hh, mm):
        return None

    key = f"brief|{now.date().isoformat()}"
    if store.seen(key):
        return None

    today = now.date()
    has_class = bool(timetable.courses_on(week, today.weekday() + 1))
    if not has_class and cfg.notify.brief_skip_empty:
        store.mark(key)  # 没课就不打扰，但记一下避免每分钟重复判断
        return None
    return Alert(kind="brief", key=key, text=render.brief_text(timetable, cfg.schedule, week, today))


def diff_timetables(old: Timetable, new: Timetable) -> list[str]:
    """对比两次课表，返回人类可读的变更描述。"""
    old_map = {c.uid: c for c in old.courses}
    new_map = {c.uid: c for c in new.courses}
    lines: list[str] = []

    for uid in new_map.keys() - old_map.keys():
        c = new_map[uid]
        lines.append(f"➕ 新增 {c.name} {c.weekday_cn} 第{c.start_period}-{c.end_period}节")
    for uid in old_map.keys() - new_map.keys():
        c = old_map[uid]
        lines.append(f"➖ 取消 {c.name} {c.weekday_cn} 第{c.start_period}-{c.end_period}节")
    for uid in new_map.keys() & old_map.keys():
        a, b = old_map[uid], new_map[uid]
        if a.location != b.location:
            lines.append(f"🔁 换教室 {b.name}：{a.location or '未知'} → {b.location or '未知'}")
        if a.teacher != b.teacher:
            lines.append(f"👤 换教师 {b.name}：{a.teacher or '未知'} → {b.teacher or '未知'}")
        if a.weeks != b.weeks:
            lines.append(f"📅 周次变动 {b.name}")
    return lines


# ---------------- 调度器 ----------------
class Scheduler:
    """持有课表缓存、负责刷新与提醒派发。"""

    def __init__(self, cfg_provider, jw_provider, notifier, data_dir, log=None, provider_getter=None):
        self.cfg_provider = cfg_provider
        self.jw_provider = jw_provider
        self.notifier = notifier
        self.log = log
        # 可选的 LLM provider 获取器（AI 补全识别用）
        self.provider_getter = provider_getter
        self.dir = Path(data_dir)
        self.timetable: Timetable | None = None
        self.last_refresh = 0.0
        self.last_error = ""
        self.week1_source = ""
        self.store = NotifiedStore(self.dir / "notified.json")
        self._no_target_warned = False
        self._retry_after = 0.0  # 失败退避截止时间戳
        self._load_state()

    # ---------- 状态持久化 ----------
    def _state_path(self) -> Path:
        return self.dir / "timetable.json"

    def _load_state(self) -> None:
        data = read_json(self._state_path(), None)
        if not data:
            return
        try:
            tt = Timetable.from_dict(data)
            if tt.courses:
                self.timetable = tt
                self.last_refresh = tt.fetched_at or 0.0
                self.week1_source = tt.week1_source
                self._info("[kb] 已从本地缓存加载课表：%d 门课", len(tt.courses))
        except Exception:
            self.timetable = None

    def save_state(self) -> None:
        if not self.timetable:
            return
        try:
            write_json(self._state_path(), self.timetable.to_dict())
        except Exception as exc:
            self._warn("[kb] 保存课表缓存失败：%s", exc)

    # ---------- 工具 ----------
    def _info(self, msg: str, *args) -> None:
        if self.log:
            self.log.info(msg, *args)

    def _warn(self, msg: str, *args) -> None:
        if self.log:
            self.log.warning(msg, *args)

    def week1_date(self):
        cfg = self.cfg_provider()
        if cfg.schedule.week1_date:
            return cfg.schedule.week1_date
        if self.timetable and self.timetable.week1_monday:
            try:
                y, m, d = (int(x) for x in self.timetable.week1_monday.split("-"))
                from datetime import date as _date

                return _date(y, m, d)
            except Exception:
                return None
        return None

    def current_week(self) -> int | None:
        return term.current_week(self.week1_date(), term.today_cn())

    # ---------- 刷新 ----------
    async def refresh(self, force: bool = False) -> Timetable:
        """登录并重新抓取课表，返回新的 Timetable。"""
        cfg = self.cfg_provider()
        jw = self.jw_provider()
        terms = await jw.fetch_terms()

        term_id = cfg.schedule.term
        if not term_id or term_id.lower() == "auto":
            term_id = jw._default_term(terms) or ""
        if not term_id:
            raise JwError("无法确定学期，请用 /kb term 指定（形如 2026-2027-1）")

        html, snap = await jw.fetch_timetable_html(term_id)
        courses = parse_timetable(
            html,
            max_week=cfg.schedule.max_week,
            big_period_size=cfg.schedule.big_period_size,
        )

        # 可选：让模型把没识别出来的教师/教室补上。
        # 修改的是同一批 Course 对象，因此面板与推送用的都是补全后的数据。
        if cfg.schedule.llm_enhance:
            await self._enhance(courses)

        sch = replace(cfg.schedule, term=term_id)
        week1, source = term.resolve_week1(html, sch, term.today_cn())
        # 第1周算错一周，提醒就整体错位一周，务必把依据打进日志
        self._info("[kb] 第1周周一=%s（来源 %s）", week1, source)

        tt = Timetable(
            term=term_id,
            term_name=term.term_display_name(term_id),
            courses=courses,
            fetched_at=time.time(),
            week1_monday=week1.isoformat() if week1 else "",
            week1_source=source,
            snapshot=snap,
        )
        tt.fetched_courses = list(courses)

        # 合并面板里的自定义课程 / 隐藏标记（manual_provider 由 main.py 注入，
        # 用属性注入而非构造参数，兼容旧版核心包的混合目录场景）
        provider = getattr(self, "manual_provider", None)
        if callable(provider):
            try:
                store = provider()
                if store is not None:
                    store.apply(tt)
            except Exception as exc:
                self._warn("[kb] 合并自定义课程失败：%s", exc)

        old = self.timetable
        self.timetable = tt
        self.last_refresh = tt.fetched_at
        self.week1_source = source
        self.save_state()
        # 成功即清除失败退避与错误记忆
        self._retry_after = 0.0
        self.last_error = ""

        self._info(
            "[kb] 课表刷新完成：%d 门课，学期 %s，第1周 %s（%s）",
            len(courses),
            term_id,
            tt.week1_monday or "-",
            source,
        )

        # 变更检测：首次抓取不推，避免一上来就刷屏
        if old and cfg.notify.notify_on_change:
            changes = diff_timetables(old, tt)
            if changes:
                text = "🔔 课表有变动\n" + "\n".join(changes[:12])
                if len(changes) > 12:
                    text += f"\n… 另有 {len(changes) - 12} 处变动"
                await self.notifier.broadcast(text)
        return tt

    async def _enhance(self, courses) -> None:
        """用 AstrBot 已配置的模型补全识别不出来的字段（失败即跳过）。"""
        if not self.provider_getter:
            return
        try:
            provider = await self.provider_getter()
        except Exception as exc:
            self._warn("[kb] 获取模型 provider 失败，跳过 AI 补全：%s", exc)
            return
        if provider is None:
            self._warn("[kb] 已开启 AI 补全，但 AstrBot 里没有可用的对话模型")
            return
        try:
            from .enhance import enhance_courses

            await enhance_courses(
                courses,
                provider,
                self.dir / "enhance_cache.json",
                self.log,
            )
        except Exception as exc:
            self._warn("[kb] AI 补全异常（已忽略，使用规则解析结果）：%s", exc)

    async def refresh_if_stale(self) -> None:
        cfg = self.cfg_provider()
        ttl = max(1, cfg.schedule.refresh_hours) * 3600
        if self.timetable and (time.time() - self.last_refresh) < ttl:
            return
        await self.refresh()

    # ---------- 每分钟 tick ----------
    async def tick(self) -> None:
        try:
            await self._tick()
        except JwError as exc:
            wait = BACKOFF_AUTH if isinstance(exc, AuthError) else BACKOFF_OTHER
            self._retry_after = time.time() + wait
            # 同一个错误只记一次，避免每分钟刷屏
            if self.last_error != exc.user_msg:
                self._warn(
                    "[kb] 定时任务异常：%s（%d 分钟内不再重试）",
                    exc.user_msg,
                    wait // 60,
                )
                self.last_error = exc.user_msg
        except Exception as exc:
            self._retry_after = time.time() + BACKOFF_OTHER
            self._warn("[kb] 定时任务未预期异常：%s: %s", exc.__class__.__name__, exc)

    async def _tick(self) -> None:
        cfg = self.cfg_provider()
        if not cfg.notify.enabled and not cfg.notify.daily_brief_time:
            return

        # 失败退避期内直接跳过，别去打扰学校服务器
        if time.time() < self._retry_after:
            return

        await self.refresh_if_stale()
        if not self.timetable:
            return

        now = term.now_cn()
        week = term.current_week(self.week1_date(), now.date())
        if week is None:
            return  # 未开学或学期已结束

        self.store.prune()

        if not cfg.push.targets():
            if not self._no_target_warned:
                self._warn("[kb] 尚未配置推送目标，提醒不会发出。可在目标会话发 /kb push here")
                self._no_target_warned = True
            return
        self._no_target_warned = False

        # 1) 每日早报
        if cfg.notify.enabled:
            alert = brief_alert(self.timetable, cfg, now, week, self.store)
            if alert:
                sent = await self.notifier.broadcast(alert.text)
                if sent:
                    self.store.mark(alert.key)

        # 2) 上课提醒
        if cfg.notify.enabled:
            for alert in due_class_alerts(self.timetable, cfg, now, week, self.store):
                sent = await self.notifier.broadcast(alert.text)
                if sent:
                    self.store.mark(alert.key)

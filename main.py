"""教务课表插件入口。

结构说明：
* 本文件只做「装配 + 指令 + 定时器」；
* 登录/抓取在 ``jwkb_core/jw_client.py``，解析在 ``jwkb_core/parser.py``，
  调度判断在 ``jwkb_core/scheduler.py``，文案在 ``jwkb_core/render.py``。

导入约定：把插件根目录插入 ``sys.path`` 后统一用 ``from jwkb_core.xxx import``，
避免 AstrBot 以包形式加载时同一模块被加载两次（会导致单例与锁失效）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

# 热更新防陈旧（重要）：AstrBot 重载/更新插件时只会移除 main.py 的模块缓存，
# **不会清理插件自带的 jwkb_core.* 子包** —— 旧模块会一直留在 sys.modules，
# 新 main.py import 到的仍是内存里的旧代码，造成「界面是新版、后端还是旧逻辑」
# 的混合状态（terms 报未找到路由、解析修复反复不生效的根因，真实踩过）。
# 所以每次 main.py 被重新加载时，先清掉 jwkb_core 的全部缓存再导入。
for _m in [m for m in list(sys.modules) if m == "jwkb_core" or m.startswith("jwkb_core.")]:
    del sys.modules[_m]

from astrbot.api import AstrBotConfig, logger  # noqa: E402
from astrbot.api.event import AstrMessageEvent, filter  # noqa: E402
from astrbot.api.star import Context, Star, register  # noqa: E402

from jwkb_core import captcha, render, term  # noqa: E402
from jwkb_core.config import Config  # noqa: E402
from jwkb_core.const import (  # noqa: E402
    PLUGIN_AUTHOR,
    PLUGIN_DESC,
    PLUGIN_DISPLAY_NAME,
    PLUGIN_NAME,
    PLUGIN_VERSION,
    WEEKDAY_ALIASES,
    WEEKDAY_CN,
)
from jwkb_core.errors import AuthError, ConfigError, JwError  # noqa: E402
from jwkb_core.jw_client import JwClient  # noqa: E402
from jwkb_core.notifier import Notifier  # noqa: E402
from jwkb_core.scheduler import Scheduler  # noqa: E402
from jwkb_core.storage import resolve_data_dir  # noqa: E402

try:  # APScheduler 一般随 AstrBot 一起装好，缺失时退回 asyncio 循环
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
except Exception:  # pragma: no cover
    AsyncIOScheduler = None  # type: ignore[assignment]


@register(PLUGIN_NAME, PLUGIN_AUTHOR, PLUGIN_DESC, PLUGIN_VERSION)
class JwTimetablePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        # 个别加载路径下 config 可能为 None，兜成空字典避免指令里 setdefault 崩
        self.config: AstrBotConfig = config if config is not None else {}  # type: ignore[assignment]
        self.jw: JwClient | None = None
        self.notifier: Notifier | None = None
        self.scheduler: Scheduler | None = None
        self._aps = None
        self._task: asyncio.Task | None = None
        self._data_dir = None
        # 配置文件 mtime，用于热加载 WebUI 里的改动
        self._cfg_mtime: int | None = None

    # ---------------- 生命周期 ----------------
    async def initialize(self) -> None:
        """插件启用时调用：准备数据目录、装配依赖、启动每分钟 tick。"""
        self._data_dir = resolve_data_dir(PLUGIN_NAME)
        cfg = self.cfg()
        self.jw = JwClient(cfg, self._data_dir, log=logger, cfg_provider=self.cfg)
        self.notifier = Notifier(self.context, self.cfg, log=logger)
        self.scheduler = Scheduler(
            cfg_provider=self.cfg,
            jw_provider=lambda: self.jw,
            notifier=self.notifier,
            data_dir=self._data_dir,
            log=logger,
        )
        # 用属性注入而不是构造参数：万一安装目录里 jwkb_core 还是旧版
        # （AstrBot 更新失败会从备份回滚，可能留下 main.py 新、jwkb_core 旧的混合状态），
        # 旧版 Scheduler 不认识这个参数会直接 TypeError 让整个插件加载失败。
        # 这里改为属性赋值，最坏情况只是 AI 补全不可用，插件其余功能照常工作。
        self._check_core_version()
        self.scheduler.provider_getter = (
            getattr(self.scheduler, "provider_getter", None) or self._llm_provider
        )

        self._start_ticker()

        # WebUI 面板：v4.28+ 支持插件独立页面。注册失败不能影响课表主功能，
        # 因此这里单独兜住异常（老版本没有 register_web_api 时会走到这里）。
        self.panel = None
        try:
            from jwkb_core.webapi import WebPanel

            self.panel = WebPanel(self)
            paths = self.panel.register(self.context)
            logger.info("[kb] WebUI 面板已注册 %d 个接口", len(paths))
        except Exception as exc:
            logger.warning("[kb] WebUI 面板注册失败（课表功能不受影响）：%s", exc)
        logger.info(
            "[kb] %s v%s 已启动，数据目录 %s",
            PLUGIN_DISPLAY_NAME,
            PLUGIN_VERSION,
            self._data_dir,
        )
        # 把「读的是哪个配置文件、文件里和内存里的面板开关各是什么」直接打进日志，
        # 这类配置不一致的问题看一行日志就能定位。
        try:
            diag = self.panel_diag()
            logger.info(
                "[kb] 配置文件：%s（文件 web.enabled=%s，内存=%s）",
                diag.get("config_path"),
                diag.get("file_web_enabled"),
                diag.get("mem_web_enabled"),
            )
        except Exception:
            pass
        if not cfg.jw.base_url:
            logger.warning("[kb] 尚未配置教务地址，请在 WebUI 插件配置里填写 jw 分组")
        if not cfg.push.targets():
            logger.warning("[kb] 尚未配置推送目标，可在目标会话发送 /kb push here")

    def _start_ticker(self) -> None:
        """优先用 APScheduler；没有就退回 asyncio 循环，保证提醒一定跑起来。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover
            loop = asyncio.get_event_loop()

        if AsyncIOScheduler is not None:
            try:
                self._aps = AsyncIOScheduler(event_loop=loop, timezone="Asia/Shanghai")
            except TypeError:  # APScheduler 4.x 去掉了 event_loop 参数
                self._aps = AsyncIOScheduler(timezone="Asia/Shanghai")
            self._aps.add_job(
                self._safe_tick,
                "interval",
                seconds=60,
                id="kb_tick",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=30,
            )
            self._aps.start()
            return

        self._task = asyncio.create_task(self._loop_forever())

    async def _loop_forever(self) -> None:
        while True:
            await self._safe_tick()
            await asyncio.sleep(60)

    async def _safe_tick(self) -> None:
        """tick 的兜底包装：任何异常都不能让定时任务死掉。"""
        if not self.scheduler:
            return
        await self.scheduler.tick()

    async def terminate(self) -> None:
        """插件停用/重载时清理资源。"""
        if self._aps:
            try:
                self._aps.shutdown(wait=False)
            except Exception:
                pass
            self._aps = None
        if self._task:
            self._task.cancel()
            self._task = None
        if self.jw:
            await self.jw.close()
        logger.info("[kb] 插件已停止")

    # ---------------- 内部工具 ----------------
    def cfg(self) -> Config:
        """每次调用都重新解析配置，并检测配置文件是否被 WebUI 改过。"""
        self._reload_config_if_changed()
        try:
            raw = dict(self.config) if self.config else {}
        except Exception:
            raw = {}
        return Config.from_dict(raw)

    def _config_path(self) -> str:
        path = getattr(self.config, "config_path", None)
        return path if isinstance(path, str) else ""

    def _candidate_config_paths(self) -> list[str]:
        """可能的插件配置文件路径，按优先级排列。

        ``self.config.config_path`` 理论上最权威，但不同版本/加载方式下它可能指向
        全局配置，所以再兜一个标准位置 ``data/config/<插件名>_config.json``。
        """
        paths: list[str] = []
        p = self._config_path()
        if p:
            paths.append(p)
        try:
            from jwkb_core.storage import config_dir, plugin_config_path

            paths.append(str(plugin_config_path(PLUGIN_NAME)))
            # 再退一步：把 data/config 下的 json 全扫一遍。
            # 不能只认 *_config.json —— 实际部署里文件名可能是 <插件名>.json，
            # 甚至放在子目录里，光靠命名约定会漏掉。
            cdir = config_dir()
            if cdir.is_dir():
                paths.extend(str(f) for f in sorted(cdir.glob("*.json")))
                paths.extend(str(f) for f in sorted(cdir.rglob("*.json")))
        except Exception:
            pass

        seen: set[str] = set()
        uniq: list[str] = []
        for x in paths:
            if x and x not in seen:
                seen.add(x)
                uniq.append(x)
        return uniq

    def _read_cfg_file(self) -> tuple[str, dict] | None:
        """找到「确实是本插件配置」的那个文件，返回 (路径, 内容)。

        判定条件用 ``jw`` 分组（本插件独有），比「任意键命中」更难误匹配。
        """
        for path in self._candidate_config_paths():
            try:
                with open(path, encoding="utf-8-sig") as f:
                    data = json.load(f)
            except Exception:
                continue
            if isinstance(data, dict) and isinstance(data.get("jw"), dict):
                return path, data
        return None

    def _reload_config_if_changed(self) -> None:
        """让内存里的配置跟上磁盘。

        启动时先同步一次（``_cfg_mtime`` 初始为 None），之后按 mtime 增量同步。
        这样 WebUI 里改的面板开关、账号密码都能即时生效。
        """
        found = self._read_cfg_file()
        if not found:
            return
        path, data = found
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            mtime = None
        if mtime is not None and mtime == self._cfg_mtime:
            return
        self._cfg_mtime = mtime
        try:
            self.config.clear()
            self.config.update(data)
        except Exception as exc:
            logger.warning("[kb] 应用新配置失败：%s", exc)
            return
        logger.info("[kb] 已加载配置：%s", path)

    def panel_diag(self) -> dict:
        """配置诊断信息：读的是哪个文件、里面有没有本插件的分组。

        面板已不再有「启用/关闭」开关（0.3.3 起面板始终可用），
        这里只保留对排障有用的配置文件信息。
        """
        found = self._read_cfg_file()
        path, data = found if found else ("", {})
        return {
            "version": PLUGIN_VERSION,
            "config_path": path or (self._config_path() or "未找到配置文件"),
            "config_found": bool(found),
            "groups": sorted(data.keys()) if isinstance(data, dict) else [],
            "candidates": self._candidate_config_paths(),
        }

    def _check_core_version(self) -> None:
        """检测安装目录里核心包是否与 main.py 同版本。

        AstrBot 更新插件失败时会从备份回滚，可能留下「main.py 已更新、jwkb_core 还是旧版」
        的混合目录。这种情况下插件仍能加载，但新功能会静默缺失，所以这里显式告警。
        """
        try:
            import inspect

            params = inspect.signature(Scheduler.__init__).parameters
            if "provider_getter" not in params:
                logger.error(
                    "[kb] 检测到安装目录文件版本不一致（jwkb_core 是旧版）。"
                    "请删除插件目录后重新安装：rm -rf data/plugins/astrbot_plugin_jw_timetable"
                )
        except Exception:
            pass

    async def _llm_provider(self):
        """取 AstrBot 当前默认对话模型，供 AI 补全识别使用。

        拿不到就返回 None —— 调用方会跳过补全，退回规则解析结果。
        """
        try:
            if not self.cfg().schedule.llm_enhance:
                return None
        except Exception:
            return None
        try:
            return await self.context.get_using_provider_async()
        except Exception:
            pass
        try:
            return self.context.get_using_provider()
        except Exception:
            return None

    def _save_config(self) -> bool:
        """把配置改动写回磁盘。

        优先用 AstrBotConfig 自带的 ``save_config``；如果拿到的只是普通 dict
        （部分加载路径会这样），就直接把 JSON 写到找到的那个配置文件里，
        否则面板里的开关一重启就丢。
        """
        try:
            if hasattr(self.config, "save_config"):
                self.config.save_config()
                self._cfg_mtime = None
                return True
        except Exception as exc:
            logger.warning("[kb] 框架保存配置失败，尝试直接写文件：%s", exc)

        found = self._read_cfg_file()
        candidates = self._candidate_config_paths()
        path = found[0] if found else (candidates[0] if candidates else "")
        if not path:
            logger.warning("[kb] 找不到配置文件，配置改动无法持久化")
            return False
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(dict(self.config), f, ensure_ascii=False, indent=4)
            self._cfg_mtime = None  # 强制下次重新读取
            logger.info("[kb] 配置已写入 %s", path)
            return True
        except Exception as exc:
            logger.warning("[kb] 写配置文件失败：%s", exc)
            return False

    async def _ensure_timetable(self, force: bool = False):
        """拿到课表，没有或强制时先刷新。"""
        cfg = self.cfg()
        cfg.jw.validate()
        if self.scheduler is None:
            raise JwError("插件尚未初始化完成，请稍后重试")
        if force or self.scheduler.timetable is None:
            await self.scheduler.refresh(force=force)
        return self.scheduler.timetable

    async def _send_week_image(self, event: AstrMessageEvent, week: int):
        """尝试把整周课表渲染成图片，失败则回退文本。"""
        tt = self.scheduler.timetable
        cfg = self.cfg()
        if tt is None or not hasattr(self, "html_render"):
            return None
        rows = []
        per = cfg.schedule.big_period_size
        max_period = max((c.end_period for c in tt.courses), default=per * 5)
        for start in range(1, max_period + 1, per):
            end = min(start + per - 1, max_period)
            cells = []
            for wd in range(1, 8):
                items = [
                    c for c in tt.courses_on(week, wd) if c.start_period <= end and c.end_period >= start
                ]
                cells.append([{"name": c.name, "location": c.location} for c in items])
            label = f"{start}-{end}节" if end != start else f"{start}节"
            rows.append({"label": label, "cells": cells})
        data = {
            "title": f"第{week}周课表",
            "subtitle": f"{tt.term_name or tt.term} · {cfg.jw.display_account or ''}",
            "days": WEEKDAY_CN,
            "rows": rows,
        }
        try:
            url = await self.html_render(render.WEEK_HTML_TMPL, data)
            return event.image_result(url)
        except Exception as exc:
            logger.warning("[kb] 课表图渲染失败，回退文本：%s", exc)
            return None

    # ---------------- 指令 ----------------
    @filter.command_group("kb")
    def kb(self):
        """课表相关指令集合。"""

    @kb.command("help")
    async def kb_help(self, event: AstrMessageEvent):
        """查看用法。"""
        yield event.plain_result(render.HELP_TEXT)

    @kb.command("today")
    async def kb_today(self, event: AstrMessageEvent):
        """查看今天的课表。"""
        try:
            tt = await self._ensure_timetable()
            week = self.scheduler.current_week()
            if week is None:
                yield event.plain_result("还没开学（或已放假），当前不在任何教学周内。")
                return
            today = term.today_cn()
            yield event.plain_result(render.day_text(tt, self.cfg().schedule, week, today))
        except JwError as exc:
            yield event.plain_result(f"❌ {exc.user_msg}")

    @kb.command("tomorrow")
    async def kb_tomorrow(self, event: AstrMessageEvent):
        """查看明天的课表。"""
        try:
            tt = await self._ensure_timetable()
            week = self.scheduler.current_week()
            if week is None:
                yield event.plain_result("还没开学（或已放假），当前不在任何教学周内。")
                return
            day = term.today_cn() + timedelta(days=1)
            target_week = term.current_week(self.scheduler.week1_date(), day) or week
            yield event.plain_result(render.day_text(tt, self.cfg().schedule, target_week, day, "明天 "))
        except JwError as exc:
            yield event.plain_result(f"❌ {exc.user_msg}")

    @kb.command("week")
    async def kb_week(self, event: AstrMessageEvent, arg: str = ""):
        """查看本周或指定周课表，如 /kb week 3。"""
        try:
            tt = await self._ensure_timetable()
            cur = self.scheduler.current_week()
            if cur is None:
                yield event.plain_result("还没开学（或已放假），当前不在任何教学周内。")
                return
            week = cur
            a = (arg or "").strip().lower()
            if a in {"next", "下", "下周"}:
                week = cur + 1
            elif a in {"prev", "上", "上周"}:
                week = cur - 1
            elif a.isdigit():
                week = int(a)
            if week < 1:
                yield event.plain_result("周次必须大于 0")
                return
            img = await self._send_week_image(event, week)
            if img is not None:
                yield img
            else:
                yield event.plain_result(render.week_text(tt, self.cfg().schedule, week))
        except JwError as exc:
            yield event.plain_result(f"❌ {exc.user_msg}")

    @kb.command("day")
    async def kb_day(self, event: AstrMessageEvent, arg: str = ""):
        """查看指定星期的课表，如 /kb day 周三。"""
        try:
            a = (arg or "").strip()
            if not a:
                yield event.plain_result("用法：/kb day 周三（或 3）")
                return
            key = a.replace("星期", "").replace("周", "").strip()
            weekday = WEEKDAY_ALIASES.get(key)
            if weekday is None:
                yield event.plain_result("没看懂这个星期几，试试 /kb day 周三 或 /kb day 3")
                return
            tt = await self._ensure_timetable()
            week = self.scheduler.current_week()
            if week is None:
                yield event.plain_result("还没开学（或已放假），当前不在任何教学周内。")
                return
            courses = tt.courses_on(week, weekday)
            head = f"📅 第{week}周 {WEEKDAY_CN[weekday - 1]}"
            if not courses:
                yield event.plain_result(f"{head}\n没有课")
                return
            cfg = self.cfg()
            lines = [head, render.DIVIDER]
            for i, c in enumerate(courses, start=1):
                span = f"{cfg.schedule.period_start(c.start_period)}-{cfg.schedule.period_end(c.end_period)}"
                loc = f" @{c.location}" if c.location else ""
                lines.append(f"{i}. {span} {c.name}{loc}")
            yield event.plain_result("\n".join(lines))
        except JwError as exc:
            yield event.plain_result(f"❌ {exc.user_msg}")

    @kb.command("refresh")
    async def kb_refresh(self, event: AstrMessageEvent):
        """立即重新登录并抓取课表。"""
        try:
            await self._ensure_timetable(force=True)
            tt = self.scheduler.timetable
            yield event.plain_result(f"✅ 已刷新：{tt.term_name or tt.term} 共 {len(tt.courses)} 个课程安排")
        except JwError as exc:
            yield event.plain_result(f"❌ {exc.user_msg}")

    @kb.command("status")
    async def kb_status(self, event: AstrMessageEvent):
        """查看运行状态。"""
        cfg = self.cfg()
        tt = self.scheduler.timetable if self.scheduler else None
        week1 = self.scheduler.week1_date() if self.scheduler else None
        ocr_ok, ocr_reason = captcha.available()
        data = {
            "auth_mode": (self.jw.auth_mode_used if self.jw else "") or cfg.jw.auth_mode,
            "account": cfg.jw.display_account or "未配置",
            "base_url": cfg.jw.base or "未配置",
            "service_url": cfg.jw.service_url or "未配置",
            "term": cfg.schedule.term,
            "term_name": tt.term_name if tt else "",
            "week": self.scheduler.current_week() if self.scheduler else "-",
            "week1": week1.isoformat() if week1 else "-",
            "week1_source": (tt.week1_source if tt else "") or "-",
            "cache": (
                time.strftime("%m-%d %H:%M", time.localtime(tt.fetched_at))
                if tt and tt.fetched_at
                else "无缓存"
            ),
            "course_count": len(tt.courses) if tt else 0,
            "notify_enabled": cfg.notify.enabled,
            "ahead": cfg.notify.ahead_minutes,
            "brief": cfg.notify.daily_brief_time or "关闭",
            "targets": len(cfg.push.targets()),
            "ocr": "可用" if ocr_ok else f"不可用（{ocr_reason[:40]}）",
            "last_error": self.scheduler.last_error if self.scheduler else "",
            "note": "",
        }
        notes = []
        if not cfg.schedule.periods_ok:
            notes.append("作息时间表解析失败，已回退默认值，请检查 schedule.periods")
        if not cfg.push.targets():
            notes.append("未配置推送目标，提醒不会发出（在目标会话发 /kb push here）")
        if tt and tt.week1_source == "guessed":
            notes.append("第 1 周周一是估算的，建议用 /kb week1 YYYY-MM-DD 校正")
        data["note"] = "；".join(notes)
        yield event.plain_result(render.status_text(data))

    @kb.command("umo")
    async def kb_umo(self, event: AstrMessageEvent):
        """显示当前会话标识（排查推送问题用）。"""
        yield event.plain_result(
            f"当前会话标识：\n{event.unified_msg_origin}\n\n在管理员可用 /kb push here 直接把它设为推送目标。"
        )

    @kb.command("push")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def kb_push(self, event: AstrMessageEvent, arg: str = ""):
        """设置推送目标：/kb push here 或 /kb push list。"""
        cfg = self.cfg()
        a = (arg or "").strip().lower()
        if a in {"", "list"}:
            lines = ["📮 当前推送目标："]
            lines.append(
                f"私聊（{'开' if cfg.push.private_enabled else '关'}）：{len(cfg.push.private_umos)} 个"
            )
            for u in cfg.push.private_umos:
                lines.append(f"  {u}")
            lines.append(f"群聊（{'开' if cfg.push.group_enabled else '关'}）：{len(cfg.push.group_umos)} 个")
            for u in cfg.push.group_umos:
                lines.append(f"  {u}")
            yield event.plain_result("\n".join(lines))
            return

        if a != "here":
            yield event.plain_result("用法：/kb push here 或 /kb push list")
            return

        umo = event.unified_msg_origin
        is_group = "Group" in umo
        key = "group_umos" if is_group else "private_umos"
        enable_key = "group_enabled" if is_group else "private_enabled"
        try:
            push_cfg = self.config.setdefault("push", {})
            items = list(push_cfg.get(key) or [])
            if umo in items:
                yield event.plain_result("这个会话已经在推送列表里了。")
                return
            items.append(umo)
            push_cfg[key] = items
            push_cfg[enable_key] = True
            self._save_config()
            yield event.plain_result(f"✅ 已把当前{'群' if is_group else '私聊'}加入推送目标：\n{umo}")
        except Exception as exc:
            yield event.plain_result(f"❌ 写入配置失败：{exc}")

    @kb.command("ahead")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def kb_ahead(self, event: AstrMessageEvent, arg: str = ""):
        """设置提前提醒分钟数：/kb ahead 20。"""
        a = (arg or "").strip()
        if not a.isdigit():
            cur = self.cfg().notify.ahead_minutes
            yield event.plain_result(f"当前提前 {cur} 分钟。用法：/kb ahead 20")
            return
        value = max(0, min(int(a), 240))
        try:
            self.config.setdefault("notify", {})["ahead_minutes"] = value
            self._save_config()
            yield event.plain_result(f"✅ 已设置为提前 {value} 分钟提醒")
        except Exception as exc:
            yield event.plain_result(f"❌ 写入配置失败：{exc}")

    @kb.command("brief")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def kb_brief(self, event: AstrMessageEvent, arg: str = ""):
        """设置每日早报时间：/kb brief 07:00 或 /kb brief off。"""
        a = (arg or "").strip()
        if not a:
            cur = self.cfg().notify.daily_brief_time or "关闭"
            yield event.plain_result(f"当前早报时间：{cur}\n用法：/kb brief 07:00 或 /kb brief off")
            return
        if a.lower() in {"off", "关闭", "0"}:
            value = ""
        else:
            hh, _, mm = a.replace("：", ":").partition(":")
            if not (hh.isdigit() and mm.isdigit()):
                yield event.plain_result("格式不对，示例：/kb brief 07:00")
                return
            value = f"{int(hh):02d}:{int(mm):02d}"
        try:
            self.config.setdefault("notify", {})["daily_brief_time"] = value
            self._save_config()
            yield event.plain_result(f"✅ 早报时间已设为：{value or '关闭'}")
        except Exception as exc:
            yield event.plain_result(f"❌ 写入配置失败：{exc}")

    @kb.command("notify")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def kb_notify(self, event: AstrMessageEvent, arg: str = ""):
        """总开关：/kb notify on|off。"""
        a = (arg or "").strip().lower()
        if a not in {"on", "off"}:
            cur = "on" if self.cfg().notify.enabled else "off"
            yield event.plain_result(f"当前提醒：{cur}。用法：/kb notify on|off")
            return
        try:
            self.config.setdefault("notify", {})["enabled"] = a == "on"
            self._save_config()
            yield event.plain_result(f"✅ 提醒已{'开启' if a == 'on' else '关闭'}")
        except Exception as exc:
            yield event.plain_result(f"❌ 写入配置失败：{exc}")

    @kb.command("week1")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def kb_week1(self, event: AstrMessageEvent, arg: str = ""):
        """校正第 1 周周一：/kb week1 2026-09-07。"""
        a = (arg or "").strip()
        if not a:
            w1 = self.scheduler.week1_date() if self.scheduler else None
            yield event.plain_result(
                f"当前第 1 周周一：{w1.isoformat() if w1 else '未知'}\n用法：/kb week1 2026-09-07"
            )
            return
        try:
            from datetime import date as _date

            y, m, d = (int(x) for x in a.replace("/", "-").split("-"))
            value = _date(y, m, d)
        except Exception:
            yield event.plain_result("日期格式不对，示例：/kb week1 2026-09-07")
            return
        try:
            self.config.setdefault("schedule", {})["week1_monday"] = value.isoformat()
            self._save_config()
            if self.scheduler:
                self.scheduler.week1_source = "config"
            week = term.current_week(value, term.today_cn())
            yield event.plain_result(
                f"✅ 第 1 周周一已设为 {value.isoformat()}，今天算第 {week} 周"
                if week
                else f"✅ 已设为 {value.isoformat()}（今天在该周之前）"
            )
        except Exception as exc:
            yield event.plain_result(f"❌ 写入配置失败：{exc}")

    @kb.command("period")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def kb_period(self, event: AstrMessageEvent, arg: str = ""):
        """查看/修改作息：/kb period show 或 /kb period set 3 10:00-10:45。"""
        parts = (arg or "").split()
        cfg = self.cfg()
        if not parts or parts[0] == "show":
            lines = ["⏰ 当前作息时间表", render.DIVIDER]
            for p in cfg.schedule.periods:
                lines.append(f"第{p.index}节  {p.start}-{p.end}")
            lines.append("")
            lines.append("改单节：/kb period set 3 10:00-10:45")
            yield event.plain_result("\n".join(lines))
            return

        if parts[0] != "set":
            yield event.plain_result("用法：/kb period show | /kb period set 3 10:00-10:45")
            return
        if len(parts) < 3 or not parts[1].isdigit() or "-" not in parts[2]:
            yield event.plain_result("用法：/kb period set 3 10:00-10:45")
            return

        idx = int(parts[1])
        a, _, b = parts[2].partition("-")
        from jwkb_core.config import _parse_hm  # 复用同一套时间校验

        start, end = _parse_hm(a), _parse_hm(b)
        if not start or not end:
            yield event.plain_result("时间格式不对，示例：10:00-10:45")
            return

        items = [p.to_dict() for p in cfg.schedule.periods]
        for item in items:
            if item["index"] == idx:
                item["start"], item["end"] = start, end
                break
        else:
            items.append({"index": idx, "start": start, "end": end})
            items.sort(key=lambda x: x["index"])
        try:
            import json

            self.config.setdefault("schedule", {})["periods"] = json.dumps(items, ensure_ascii=False)
            self._save_config()
            yield event.plain_result(f"✅ 第{idx}节已设为 {start}-{end}")
        except Exception as exc:
            yield event.plain_result(f"❌ 写入配置失败：{exc}")

    @kb.command("term")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def kb_term(self, event: AstrMessageEvent, arg: str = ""):
        """查看/切换学期：/kb term 2026-2027-1。"""
        a = (arg or "").strip()
        if not a:
            cfg = self.cfg()
            tt = self.scheduler.timetable if self.scheduler else None
            yield event.plain_result(
                f"当前学期：{cfg.schedule.term}\n"
                f"实际课表：{tt.term_name if tt else '未加载'}\n"
                "切换：/kb term 2026-2027-1"
            )
            return
        try:
            self.config.setdefault("schedule", {})["term"] = a
            self._save_config()
        except Exception as exc:
            yield event.plain_result(f"❌ 写入配置失败：{exc}")
            return
        try:
            await self._ensure_timetable(force=True)
            tt = self.scheduler.timetable
            yield event.plain_result(f"✅ 已切换到 {a}，共 {len(tt.courses)} 个课程安排")
        except JwError as exc:
            yield event.plain_result(f"❌ 切换失败：{exc.user_msg}")

    @kb.command("login")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def kb_login(self, event: AstrMessageEvent):
        """强制重新登录教务系统。"""
        try:
            cfg = self.cfg()
            cfg.jw.validate()
            await self.jw.ensure_login(force=True)
            yield event.plain_result(f"✅ 登录成功（方式：{self.jw.auth_mode_used}）")
        except (JwError, ConfigError) as exc:
            yield event.plain_result(f"❌ {exc.user_msg}")

    @kb.command("logout")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def kb_logout(self, event: AstrMessageEvent):
        """清除本地保存的会话。"""
        try:
            if self.jw:
                self.jw.clear_session()
            yield event.plain_result("✅ 已清除本地会话，下次使用会重新登录")
        except Exception as exc:
            yield event.plain_result(f"❌ {exc}")

    # 顶层中文指令：/课表 等价于 /kb today
    @filter.command("课表")
    async def timetable_cn(self, event: AstrMessageEvent, arg: str = ""):
        """中文快捷指令：/课表 [今天|明天|周]"""
        a = (arg or "").strip()
        try:
            tt = await self._ensure_timetable()
            week = self.scheduler.current_week()
            if week is None:
                yield event.plain_result("还没开学（或已放假），当前不在任何教学周内。")
                return
            cfg = self.cfg()
            if a in {"明天", "明日"}:
                day = term.today_cn() + timedelta(days=1)
                w = term.current_week(self.scheduler.week1_date(), day) or week
                yield event.plain_result(render.day_text(tt, cfg.schedule, w, day, "明天 "))
            elif a in {"周", "本周", "全周"}:
                yield event.plain_result(render.week_text(tt, cfg.schedule, week))
            else:
                yield event.plain_result(render.day_text(tt, cfg.schedule, week, term.today_cn()))
        except (JwError, ConfigError, AuthError) as exc:
            yield event.plain_result(f"❌ {exc.user_msg}")

    # ---------------- LLM 工具（function calling） ----------------
    # 注册为大模型可调用的工具：QQ 里直接问「今天有什么课」「下节课是什么」，
    # LLM 会自动调用下面的工具取数据，再组织语言回答。
    @staticmethod
    def _parse_day_arg(arg: str):
        """把 LLM 给的日期参数转成 date：支持 今天/明天/后天/昨天 与 YYYY-MM-DD。"""
        a = (arg or "").strip().lower() or "今天"
        today = term.today_cn()
        named = {"今天": 0, "今日": 0, "明天": 1, "明日": 1, "后天": 2, "昨天": -1, "昨日": -1}
        if a in named:
            return today + timedelta(days=named[a])
        m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", a)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
        return today

    @filter.llm_tool(name="kb_query_day_schedule")
    async def kb_query_day_schedule(self, event: AstrMessageEvent, day: str = "今天"):
        """查询学生的当日课程安排（琼台师范学院教务课表，包含课程名、节次、时间、地点、教师）。

        Args:
            day(string): 要查询哪一天。支持"今天"、"明天"、"后天"、"昨天"，或 YYYY-MM-DD 格式的具体日期，默认"今天"。
        """
        try:
            tt = await self._ensure_timetable()
            cfg = self.cfg()
            d = self._parse_day_arg(day)
            week = term.current_week(self.scheduler.week1_date(), d)
            if week is None:
                yield event.plain_result(f"{d.year}年{d.month}月{d.day}日 不在教学周内（放假中）。")
                return
            yield event.plain_result(render.day_text(tt, cfg.schedule, week, d))
        except (JwError, ConfigError, AuthError) as exc:
            yield event.plain_result(f"课表查询失败：{exc.user_msg}")

    @filter.llm_tool(name="kb_query_week_timetable")
    async def kb_query_week_timetable(self, event: AstrMessageEvent, week: str = ""):
        """查询学生整周课程表（周一到周日）。

        Args:
            week(string): 教学周次，数字字符串（如 "12"）。留空表示当前教学周。
        """
        try:
            tt = await self._ensure_timetable()
            cfg = self.cfg()
            a = (week or "").strip()
            w = int(a) if a.isdigit() else (self.scheduler.current_week() or 0)
            if not 1 <= w <= 30:
                yield event.plain_result(f"周次无效：{week!r}（当前可能不在教学周内）。")
                return
            yield event.plain_result(render.week_text(tt, cfg.schedule, w))
        except (JwError, ConfigError, AuthError) as exc:
            yield event.plain_result(f"课表查询失败：{exc.user_msg}")

    @filter.llm_tool(name="kb_query_next_class")
    async def kb_query_next_class(self, event: AstrMessageEvent):
        """查询学生下一节课是什么时候上、上什么（今天还没开始的课，今天没有了就看明天）。"""
        try:
            tt = await self._ensure_timetable()
            cfg = self.cfg()
            now = term.now_cn()
            week1 = self.scheduler.week1_date()
            for offset in range(0, 4):
                d = now.date() + timedelta(days=offset)
                w = term.current_week(week1, d)
                if w is None:
                    continue
                for c in sorted(tt.courses_on(w, d.weekday() + 1), key=lambda x: x.start_period):
                    start_hm = cfg.schedule.period_start(c.start_period)
                    if not start_hm:
                        continue
                    hh, mm = (int(x) for x in start_hm.split(":"))
                    start_dt = datetime(d.year, d.month, d.day, hh, mm)
                    if offset == 0 and start_dt <= now:
                        continue
                    when = f"今天 {start_hm}" if offset == 0 else f"{d.month}月{d.day}日 {start_hm}"
                    lines = [
                        f"下一节课：{when}（第{w}周）",
                        f"📘 {c.name}",
                        f"🕐 第{c.start_period}-{c.end_period}节 {start_hm} 开始",
                    ]
                    if c.location:
                        lines.append(f"📍 {c.location}")
                    if c.teacher:
                        lines.append(f"👤 {c.teacher}")
                    yield event.plain_result("\n".join(lines))
                    return
            yield event.plain_result("近几天没有找到课程安排（可能在放假）。")
        except (JwError, ConfigError, AuthError) as exc:
            yield event.plain_result(f"查询失败：{exc.user_msg}")

    @filter.llm_tool(name="kb_query_status")
    async def kb_query_status(self, event: AstrMessageEvent):
        """查询课表插件状态：当前学期、第1周周一日期、当前教学周、课程总数、教务登录方式。"""
        try:
            tt = await self._ensure_timetable()
            week1 = self.scheduler.week1_date()
            w = self.scheduler.current_week()
            info = [
                f"当前学期：{tt.term_name or tt.term}",
                f"第 1 周周一：{week1.isoformat() if week1 else '未确定'}",
                f"当前教学周：{'第' + str(w) + '周' if w else '不在教学周内'}",
                f"课程安排总数：{len(tt.courses)}",
                f"教务登录方式：{self.jw.auth_mode_used if self.jw else '未知'}",
            ]
            yield event.plain_result("\n".join(info))
        except (JwError, ConfigError, AuthError) as exc:
            yield event.plain_result(f"查询失败：{exc.user_msg}")

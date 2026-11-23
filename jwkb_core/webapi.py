"""WebUI 面板的后端接口。

路由统一以插件名为前缀（``/astrbot_plugin_jw_timetable/xxx``），前端通过
``window.AstrBotPluginView.apiGet/apiPost`` 调用，endpoint 不带前缀。

兼容策略：优先用官方 ``astrbot.api.web``（v4.28+ 已提供）；老版本缺少该模块时
退回 Quart 的 ``jsonify``，保证插件在旧版 AstrBot 上也能加载、不至于 import 失败。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import presenter, term
from .const import PLUGIN_NAME, PLUGIN_VERSION
from .errors import JwError
from .storage import mask_account

try:  # 官方推荐入口（AstrBot v4.28+）
    from astrbot.api.web import (  # type: ignore
        error_response,
        file_response,
        json_response,
        request,
    )

    WEB_NATIVE = True
except Exception:  # pragma: no cover - 仅老版本触发
    WEB_NATIVE = False
    try:
        from quart import jsonify  # type: ignore
        from quart import request as _quart_request
        from quart import send_file as _quart_send_file

        request = _quart_request

        def json_response(obj: Any):
            return jsonify(obj)

        def error_response(message: str, status_code: int = 400):
            return jsonify({"status": "error", "message": message}), status_code

    except Exception:  # 最后兜底：连 quart 都没有时返回纯 dict
        request = None

        def json_response(obj: Any):
            return obj

        def error_response(message: str, status_code: int = 400):
            return {"status": "error", "message": message}, status_code


def _file_response(path: str, filename: str):
    """下载文件：官方 file_response 优先，老版本退回 quart.send_file。"""
    if WEB_NATIVE:
        return file_response(path, filename=filename, content_type="text/html")
    try:
        return _quart_send_file(path, as_attachment=True, attachment_filename=filename)
    except Exception:
        return error_response("当前 AstrBot 版本不支持文件下载，请直接到服务器上取快照文件", status_code=501)


def _ok(**kw) -> Any:
    kw.setdefault("ok", True)
    return json_response(kw)


def _fail(message: str, **kw) -> Any:
    kw.update({"ok": False, "message": message})
    return json_response(kw)


class WebPanel:
    """把插件实例包装成一组 Web API。"""

    def __init__(self, plugin):
        self.plugin = plugin
        self.log = getattr(plugin, "logger", None)

    # ---------------- 注册 ----------------
    # 名称 -> (处理器方法名, 允许的 HTTP 方法)。_dispatch 兜底路由用。
    _API_TABLE: dict[str, tuple[str, list[str]]] = {
        "status": ("status", ["GET"]),
        "timetable": ("timetable", ["GET"]),
        "config": ("config", ["GET", "POST"]),
        "terms": ("terms", ["GET"]),
        "login": ("login", ["POST"]),
        "refresh": ("refresh", ["POST"]),
        "toggle": ("toggle", ["POST"]),
        "debug/snapshot": ("debug_snapshot", ["GET"]),
    }

    def register(self, context) -> list[str]:
        """注册全部路由，返回注册成功的路径（便于启动日志核对）。"""
        routes = [
            ("status", self.status, ["GET"], "课表插件状态"),
            ("timetable", self.timetable, ["GET"], "按周获取课表"),
            ("config", self.config, ["GET", "POST"], "读取或保存插件配置"),
            ("terms", self.terms, ["GET"], "登录后获取可选学期列表"),
            ("login", self.login, ["POST"], "测试登录教务系统"),
            ("refresh", self.refresh, ["POST"], "刷新课表"),
            ("toggle", self.toggle, ["POST"], "开关提醒"),
            ("debug/snapshot", self.debug_snapshot, ["GET"], "导出最近一次课表页面原始 HTML"),
        ]
        done: list[str] = []
        for path, handler, methods, desc in routes:
            full = f"/{PLUGIN_NAME}/{path}"
            try:
                context.register_web_api(full, handler, methods, desc)
                done.append(full)
            except Exception as exc:
                self._warn("[kb] 注册 Web API 失败 %s：%s", full, exc)

        # 兜底：再注册一条 <path:rest> 通配路由，内部按表分发。
        # 具体路由先注册所以优先匹配；万一某个 AstrBot 版本/前端组合匹配不上
        # 具体路由（出现过 terms 报「未找到该路由」），这条保证接口一定可达。
        try:
            context.register_web_api(
                f"/{PLUGIN_NAME}/<path:rest>",
                self._dispatch,
                ["GET", "POST"],
                "课表插件 API 兜底路由",
            )
            done.append(f"/{PLUGIN_NAME}/<path:rest>")
        except Exception as exc:
            self._warn("[kb] 注册兜底路由失败：%s", exc)
        return done

    async def _dispatch(self, rest: str = ""):
        """兜底分发：按路径与方法找到具体处理器。"""
        name = (rest or "").strip("/")
        method = "GET"
        try:
            method = (getattr(request, "method", "GET") or "GET").upper()
        except Exception:
            pass
        entry = self._API_TABLE.get(name)
        if not entry or method not in entry[1]:
            return error_response(
                f"未找到该路由：{method} /{name}",
                status_code=404,
            )
        return await getattr(self, entry[0])()

    # ---------------- 工具 ----------------
    def _warn(self, msg: str, *args) -> None:
        if self.log:
            self.log.warning(msg, *args)

    def _info(self, msg: str, *args) -> None:
        if self.log:
            self.log.info(msg, *args)

    def _cfg(self):
        return self.plugin.cfg()

    def _panel_diag(self) -> dict:
        diag = {}
        getter = getattr(self.plugin, "panel_diag", None)
        if callable(getter):
            try:
                diag = dict(getter())
            except Exception:
                diag = {}
        diag["version"] = PLUGIN_VERSION
        return diag

    # ---------------- GET status ----------------
    async def status(self):
        cfg = self._cfg()
        sch = self.plugin.scheduler
        tt = sch.timetable if sch else None
        week1 = sch.week1_date() if sch else None
        try:
            from . import captcha

            ocr_ok, _ = captcha.available()
        except Exception:
            ocr_ok = False

        courses = tt.courses if tt else []
        return _ok(
            version=PLUGIN_VERSION,
            config_path=(getattr(self.plugin, "_config_path", lambda: "")() or ""),
            auth_mode=(self.plugin.jw.auth_mode_used if self.plugin.jw else "") or cfg.jw.auth_mode,
            account=cfg.jw.display_account,
            masked_account=mask_account(cfg.jw.username),
            base_url=cfg.jw.base,
            cas_url=cfg.jw.cas_url,
            service_url=cfg.jw.service_url,
            has_password=bool(cfg.jw.password),
            username=cfg.jw.username,
            term=cfg.schedule.term,
            term_name=(tt.term_name if tt else "") or "",
            week=sch.current_week() if sch else None,
            week1=week1.isoformat() if week1 else "",
            week1_source=(tt.week1_source if tt else "") or "",
            week1_configured=bool(cfg.schedule.week1_monday),
            course_count=len(courses),
            fetched_at=(
                time.strftime("%m-%d %H:%M", time.localtime(tt.fetched_at)) if tt and tt.fetched_at else ""
            ),
            last_error=(sch.last_error if sch else "") or "",
            notify_enabled=cfg.notify.enabled,
            ahead=cfg.notify.ahead_minutes,
            brief=cfg.notify.daily_brief_time or "",
            targets=len(cfg.push.targets()),
            ocr=ocr_ok,
            llm_enhance=bool(getattr(cfg.schedule, "llm_enhance", False)),
            stats=presenter.recognition_stats(courses),
            web_native=WEB_NATIVE,
            diag=self._panel_diag(),
        )

    # ---------------- GET timetable ----------------
    async def timetable(self):
        cfg = self._cfg()
        sch = self.plugin.scheduler
        tt = sch.timetable if sch else None
        if tt is None or not tt.courses:
            return _fail("还没有课表数据，先点「刷新课表」或到设置里填好账号后登录")

        cur = sch.current_week()
        raw_week = request.query.get("week", None, type=int) if request else None
        week = raw_week or cur or 1
        week = max(1, min(int(week), cfg.schedule.max_week))

        grid = presenter.week_grid(tt, cfg.schedule, week, term.today_cn())
        grid["current_week"] = cur
        grid["max_week"] = cfg.schedule.max_week
        start, end = presenter.week_bounds(tt, week)
        grid["bounds"] = f"{start} - {end}" if start else ""
        if cur and week == cur:
            now = term.now_cn()
            grid["today"] = presenter.upcoming_day_courses(
                tt, cfg.schedule, week, now.date(), now.hour * 60 + now.minute
            )
        else:
            grid["today"] = []
        grid["ok"] = True
        return json_response(grid)

    # ---------------- GET debug/snapshot ----------------
    async def debug_snapshot(self):
        """导出最近一次抓到的课表页面原始 HTML，用于离线适配解析器。"""
        data_dir = str(getattr(self.plugin, "_data_dir", "") or "")
        if not data_dir:
            return error_response("插件数据目录未知", status_code=500)
        folder = Path(data_dir) / "snapshots"
        files = sorted(folder.glob("xskb_*.html")) if folder.is_dir() else []
        if not files:
            return error_response(
                "还没有课表页面快照（先点「刷新课表」成功抓取一次才会有）",
                status_code=404,
            )
        latest = files[-1]
        return _file_response(str(latest), latest.name)

    # ---------------- GET terms ----------------
    async def terms(self):
        """登录教务系统后拉取学期下拉列表，供面板选择。

        依赖会话：未登录时 ensure_login 会自动登录（可能要几秒）。
        """
        cfg = self._cfg()
        try:
            cfg.jw.validate()
        except JwError as exc:
            return _fail(exc.user_msg, hint="先在设置里填好教务地址与账号密码并保存")
        if not self.plugin.jw:
            return _fail("插件尚未初始化完成，请稍后重试")

        try:
            terms = await self.plugin.jw.fetch_terms()
        except JwError as exc:
            return _fail(exc.user_msg, hint=exc.hint)
        except Exception as exc:
            self._warn("[kb] 获取学期列表异常：%s", exc)
            return _fail(f"获取学期列表异常：{exc}")

        current = cfg.schedule.term
        return _ok(
            terms=[t.to_dict() for t in terms],
            current=current,
            default=(self.plugin.jw._default_term(terms) if terms else ""),
            logged_in=await self.plugin.jw.is_logged_in(),
        )

    # ---------------- GET/POST config ----------------
    async def config(self):
        if request is not None and getattr(request, "method", "GET").upper() == "POST":
            return await self._save_config()
        return self._read_config()

    def _read_config(self):
        cfg = self._cfg()
        return _ok(
            jw={
                "base_url": cfg.jw.base_url,
                "auth_mode": cfg.jw.auth_mode,
                "cas_url": cfg.jw.cas_url,
                "service_url": cfg.jw.service_url,
                "username": cfg.jw.username,
                "has_password": bool(cfg.jw.password),
                "ssl_verify": cfg.jw.ssl_verify,
                "captcha_ocr": cfg.jw.captcha_ocr,
            },
            notify={
                "enabled": cfg.notify.enabled,
                "ahead_minutes": cfg.notify.ahead_minutes,
                "second_ahead_minutes": cfg.notify.second_ahead_minutes,
                "daily_brief_time": cfg.notify.daily_brief_time,
                "brief_skip_empty": cfg.notify.brief_skip_empty,
            },
            schedule={
                "term": cfg.schedule.term,
                "week1_monday": cfg.schedule.week1_monday,
                "refresh_hours": cfg.schedule.refresh_hours,
                "llm_enhance": bool(getattr(cfg.schedule, "llm_enhance", False)),
            },
        )

    async def _save_config(self):
        payload = await request.json(default={}) if request else {}
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)

        # 先触发一次热加载，确保基于磁盘上的最新配置做增量修改，
        # 否则可能用内存里的旧字典覆盖掉 WebUI 刚保存的其它项。
        self._cfg()
        root = self.plugin.config
        changed: list[str] = []

        def put(section: str, key: str, value, cast=None):
            if value is None:
                return
            if cast is not None:
                try:
                    value = cast(value)
                except (TypeError, ValueError):
                    return
            bucket = root.setdefault(section, {})
            if bucket.get(key) != value:
                bucket[key] = value
                changed.append(f"{section}.{key}")

        jw = payload.get("jw") or {}
        put("jw", "base_url", jw.get("base_url"), str)
        put("jw", "auth_mode", jw.get("auth_mode"), str)
        put("jw", "cas_url", jw.get("cas_url"), str)
        put("jw", "service_url", jw.get("service_url"), str)
        put("jw", "username", jw.get("username"), str)
        put("jw", "ssl_verify", jw.get("ssl_verify"), bool)
        put("jw", "captcha_ocr", jw.get("captcha_ocr"), bool)
        # 密码留空表示「不修改」，避免把掩码星号写回去
        pwd = (jw.get("password") or "").strip()
        if pwd:
            put("jw", "password", pwd, str)

        notify = payload.get("notify") or {}
        put("notify", "enabled", notify.get("enabled"), bool)
        put("notify", "ahead_minutes", notify.get("ahead_minutes"), int)
        put("notify", "second_ahead_minutes", notify.get("second_ahead_minutes"), int)
        put("notify", "daily_brief_time", notify.get("daily_brief_time"), str)
        put("notify", "brief_skip_empty", notify.get("brief_skip_empty"), bool)

        schedule = payload.get("schedule") or {}
        put("schedule", "term", schedule.get("term"), str)
        put("schedule", "week1_monday", schedule.get("week1_monday"), str)
        put("schedule", "refresh_hours", schedule.get("refresh_hours"), int)
        put("schedule", "llm_enhance", schedule.get("llm_enhance"), bool)

        if not changed:
            return _ok(changed=[], message="没有改动")

        saved = self.plugin._save_config()
        # 账号或地址变动后，旧会话可能属于别的账号，直接作废
        if any(k.startswith("jw.") for k in changed) and self.plugin.jw:
            try:
                self.plugin.jw.clear_session()
            except Exception:
                pass
        self._info("[kb] 面板保存配置：%s", ", ".join(changed))

        cfg = self._cfg()
        warning = ""
        try:
            cfg.jw.validate()
        except JwError as exc:
            warning = exc.user_msg

        return _ok(changed=changed, saved=saved, warning=warning, service_url=cfg.jw.service_url)

    # ---------------- POST login ----------------
    async def login(self):
        cfg = self._cfg()
        try:
            cfg.jw.validate()
        except JwError as exc:
            return _fail(exc.user_msg)

        if not self.plugin.jw:
            return _fail("插件尚未初始化完成，请稍后重试")
        try:
            await self.plugin.jw.ensure_login(force=True)
            ok = await self.plugin.jw.is_logged_in()
            return _ok(mode=self.plugin.jw.auth_mode_used, logged_in=ok)
        except JwError as exc:
            return _fail(exc.user_msg, detail=exc.detail, hint=exc.hint)
        except Exception as exc:
            self._warn("[kb] 面板登录异常：%s", exc)
            return _fail(f"登录异常：{exc}")

    # ---------------- POST refresh ----------------
    async def refresh(self):
        sch = self.plugin.scheduler
        if sch is None:
            return _fail("插件尚未初始化完成，请稍后重试")
        try:
            tt = await sch.refresh(force=True)
            stats = presenter.recognition_stats(tt.courses)
            return _ok(
                count=len(tt.courses),
                term=tt.term,
                term_name=tt.term_name,
                week1=tt.week1_monday,
                week1_source=tt.week1_source,
                stats=stats,
            )
        except JwError as exc:
            return _fail(exc.user_msg, detail=exc.detail, hint=exc.hint)
        except Exception as exc:
            self._warn("[kb] 面板刷新异常：%s", exc)
            return _fail(f"刷新异常：{exc}")

    # ---------------- POST toggle ----------------
    async def toggle(self):
        payload = await request.json(default={}) if request else {}
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        self._cfg()  # 先热加载再加锁式修改
        root = self.plugin.config
        changed = []
        if "notify_enabled" in payload:
            bucket = root.setdefault("notify", {})
            value = bool(payload.get("notify_enabled"))
            if bucket.get("enabled") != value:
                bucket["enabled"] = value
                changed.append("notify.enabled")
        if changed:
            self.plugin._save_config()
            self._info("[kb] 开关变更：%s", ", ".join(changed))
        return _ok(changed=changed)

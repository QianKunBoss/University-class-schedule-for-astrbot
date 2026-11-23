"""强智教务系统客户端。

职责：
1. **认证**：CAS 统一认证（默认）与强智直连登录（兜底），支持会话（cookie）落盘复用；
2. **抓取**：学期列表、课表页 HTML，并把原始页面存成快照便于排障；
3. **解码**：强智页面编码不统一（UTF-8 / GBK 混用），统一走 ``decode_html``。

为什么不用 ``requests``：AstrBot 插件市场审核明确要求使用异步 HTTP 库。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import time
from pathlib import Path
from typing import Any

import httpx

from . import captcha
from .config import Config
from .errors import (
    AuthError,
    CaptchaRequiredError,
    JwError,
    NetworkError,
    SessionExpiredError,
    http_error_to_jw,
)
from .models import TermOption
from .sso_client import SsoClient
from .storage import mask_account, prune_files, read_json, write_json, write_text
from .textutil import decode_bytes

OPTION_RE = re.compile(r"""<option[^>]*value=["']([^"']*)["'][^>]*>([^<]*)</option>""", re.I)
SELECT_RE = re.compile(r"""<select[^>]*name=["']xnxq01id["'][^>]*>([\s\S]*?)</select>""", re.I)
NOW_WEEK_RE = re.compile(r"""nowWeek\s*[:=]\s*['"]?(\d{1,2})""", re.I)
WEEK_TEXT_RE = re.compile(r"第\s*(\d{1,2})\s*周")

# 判定是否落回登录页
LOGIN_PAGE_WORDS = ("LoginToXk", "用户登录", "请输入账号", "登录超时")
# 判定是否已登录
LOGGED_IN_WORDS = ("xsMain", "退出", "学号", "我的课表")


def decode_html(resp: httpx.Response) -> str:
    """按「响应头 → meta → utf-8 → gb18030」的顺序解码，返回可读文本。

    强智部分页面是 GBK，httpx 默认按 UTF-8 解码会整页乱码，
    所以这里一律拿原始 bytes 自己解。
    """
    return decode_bytes(resp.content, resp.headers.get("content-type", ""))


class JwClient:
    """强智教务系统客户端（一个实例持有一个 httpx 客户端与 cookie jar）。"""

    def __init__(self, cfg: Config, data_dir: str | Path, log=None, cfg_provider=None):
        self._cfg_provider = cfg_provider
        self.cfg = cfg
        self.jw = cfg.jw
        self.dir = Path(data_dir)
        self.log = log
        self.client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self.auth_mode_used = ""  # sso / direct / session
        self.last_login_ts = 0.0

    def _sync_cfg(self) -> None:
        """从 provider 重新取配置。

        WebUI 里改了账号/密码后，插件实例不会重建，这里每次操作前刷新一次，
        避免用户改了密码却还被旧凭据带着连续登录失败。
        """
        if not self._cfg_provider:
            return
        try:
            cfg = self._cfg_provider()
            self.cfg = cfg
            self.jw = cfg.jw
        except Exception:
            pass

    # ---------------- 基础 ----------------
    def _log_info(self, msg: str, *args) -> None:
        if self.log:
            self.log.info(msg, *args)

    def _log_warn(self, msg: str, *args) -> None:
        if self.log:
            self.log.warning(msg, *args)

    @property
    def root(self) -> str:
        """``http://host/jsxsd``"""
        return self.jw.base + "/jsxsd"

    @property
    def probe_url(self) -> str:
        return self.root + "/framework/xsMain.jsp"

    @property
    def login_url(self) -> str:
        return self.root + "/xk/LoginToXk"

    @property
    def verify_url(self) -> str:
        return self.root + "/verifycode.servlet"

    @property
    def term_page_url(self) -> str:
        return self.root + "/xskbcx/xskbcx_cxXskbcxIndex.html"

    @property
    def timetable_url(self) -> str:
        return self.root + "/xskb/xskb_list.do"

    # ---------------- 生命周期 ----------------
    async def start(self) -> None:
        """创建 HTTP 客户端并尝试恢复上次的会话 cookie。"""
        self._sync_cfg()
        if self.client is not None:
            return
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.jw.timeout, connect=min(10, self.jw.timeout)),
            follow_redirects=True,
            verify=self.jw.ssl_verify,
            trust_env=False,  # 避免宿主机公司代理干扰
            headers={
                "User-Agent": self.jw.user_agent,
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )
        self._restore_session()

    async def close(self) -> None:
        if self.client is not None:
            try:
                await self.client.aclose()
            except Exception:
                pass
            self.client = None

    # ---------------- 会话持久化 ----------------
    def _session_path(self) -> Path:
        return self.dir / "session.json"

    def _fingerprint(self) -> str:
        raw = f"{self.jw.base}|{self.jw.username}".lower()
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]

    def _restore_session(self) -> bool:
        data = read_json(self._session_path(), {}) or {}
        if not data or data.get("fp") != self._fingerprint():
            return False
        restored = 0
        for item in data.get("cookies") or []:
            try:
                self.client.cookies.set(
                    item["name"], item["value"], domain=item.get("domain", ""), path=item.get("path", "/")
                )
                restored += 1
            except Exception:
                continue
        if restored:
            self._log_info("[jw] 已恢复 %d 条会话 cookie（免登录）", restored)
        return restored > 0

    def persist_session(self) -> None:
        """保存 cookie 到磁盘；不保存任何密码。"""
        if self.client is None:
            return
        cookies: list[dict[str, Any]] = []
        for c in self.client.cookies.jar:
            cookies.append(
                {
                    "name": c.name,
                    "value": c.value,
                    "domain": c.domain or "",
                    "path": c.path or "/",
                }
            )
        write_json(
            self._session_path(),
            {
                "fp": self._fingerprint(),
                "ts": int(time.time()),
                "auth_mode": self.auth_mode_used,
                "cookies": cookies,
            },
        )

    def clear_session(self) -> None:
        try:
            self._session_path().unlink()
        except OSError:
            pass

    # ---------------- 登录 ----------------
    async def ensure_login(self, force: bool = False) -> None:
        """确保会话可用；必要时自动登录。并发调用只会真正登录一次。"""
        self._sync_cfg()
        await self.start()
        if not force and await self.is_logged_in():
            self.auth_mode_used = self.auth_mode_used or "session"
            return

        async with self._lock:
            # 双重检查：等锁期间可能别的协程已经登录好了
            if not force and await self.is_logged_in():
                return

            cfg = self.jw
            mode = cfg.auth_mode
            self._log_info("[jw] 开始登录：%s（账号 %s）", mode, mask_account(cfg.username))

            if mode in ("auto", "sso") and cfg.cas_url:
                try:
                    await self._login_sso()
                    self.auth_mode_used = "sso"
                    self.persist_session()
                    return
                except AuthError:
                    raise  # 凭据错误直接抛出，绝不重试
                except JwError as exc:
                    if mode == "sso":
                        raise
                    self._log_warn("[jw] SSO 登录失败，回退直连登录：%s", exc.user_msg)

            if mode in ("auto", "direct"):
                await self._login_direct()
                self.auth_mode_used = "direct"
                self.persist_session()
                return

            raise JwError("没有可用的登录方式", "请检查 jw.auth_mode 与 jw.cas_url 配置")

    async def _login_sso(self) -> None:
        if not self.jw.cas_url:
            raise JwError("未配置统一认证地址（jw.cas_url）")
        sso = SsoClient(self.client, self.jw, log=self.log)
        ticket = await sso.login()
        body = await sso.open_service(ticket)
        if not await self.is_logged_in():
            raise AuthError(
                "统一认证通过但教务会话未建立",
                "可能是教务侧 SSO 配置变更；请把日志中 [sso] 开头的行反馈给作者",
            )
        self._log_info("[jw] SSO 登录完成（页面 %d 字节）", len(body))

    async def _login_direct(self) -> None:
        if not self.jw.captcha_ocr:
            raise CaptchaRequiredError(
                "该校直连登录需要图形验证码，但未启用 OCR",
                "请在配置中打开 jw.captcha_ocr 并安装 ddddocr",
            )
        ok, reason = captcha.available()
        if not ok:
            raise CaptchaRequiredError(f"验证码识别库不可用：{reason}", captcha.install_hint())

        attempts = max(1, self.jw.login_max_retry)
        last_error = ""
        for i in range(1, attempts + 1):
            try:
                img = await self.client.get(f"{self.verify_url}?t={time.time():.3f}")
            except Exception as exc:
                raise http_error_to_jw(exc) from exc
            code = await captcha.recognize(img.content)
            self._log_info("[jw] 第 %d/%d 次尝试，验证码识别为 %s", i, attempts, code)
            if not code:
                last_error = "验证码识别为空"
                continue

            b64_u = base64.b64encode(self.jw.username.encode("utf-8")).decode()
            b64_p = base64.b64encode(self.jw.password.encode("utf-8")).decode()
            data = {
                "userAccount": self.jw.username,
                "userPassword": "",
                "RANDOMCODE": code,
                "encoded": f"{b64_u}%%%{b64_p}",
            }
            try:
                resp = await self.client.post(self.login_url, data=data)
            except Exception as exc:
                raise http_error_to_jw(exc) from exc
            html = decode_html(resp)

            if "密码错误" in html or "用户名或密码" in html:
                raise AuthError("教务系统提示账号或密码错误")
            if await self.is_logged_in():
                self._log_info("[jw] 直连登录成功")
                return
            last_error = "验证码错误" if "验证码" in html else "登录后仍未进入主页"
            self._log_warn("[jw] 登录未成功（%s），换一张验证码重试", last_error)

        raise AuthError(
            f"直连登录连续 {attempts} 次失败：{last_error}",
            "若多次失败请确认账号密码正确，或改用 sso 认证方式",
        )

    async def is_logged_in(self) -> bool:
        """以教务主页内容判定会话是否有效（状态码不可靠，重定向后仍是 200）。"""
        if self.client is None:
            return False
        try:
            resp = await self.client.get(self.probe_url)
        except Exception:
            return False
        html = decode_html(resp)
        if any(w in html for w in LOGIN_PAGE_WORDS):
            return False
        return any(w in html for w in LOGGED_IN_WORDS)

    # ---------------- 抓取 ----------------
    async def fetch_terms(self) -> list[TermOption]:
        """抓取学期下拉框。"""
        await self.ensure_login()
        try:
            resp = await self.client.get(self.term_page_url)
        except Exception as exc:
            raise http_error_to_jw(exc) from exc
        html = decode_html(resp)
        if any(w in html for w in LOGIN_PAGE_WORDS):
            raise SessionExpiredError("打开课表查询页时被要求重新登录")

        terms = self._parse_terms(html)
        if not terms:
            # 有些版本把下拉框放在课表页本身
            try:
                resp2 = await self.client.post(self.timetable_url, data={"xnxq01id": "", "zc": ""})
                terms = self._parse_terms(decode_html(resp2))
            except Exception:
                terms = []
        if terms:
            self._log_info("[jw] 发现 %d 个学期，默认 %s", len(terms), self._default_term(terms))
        return terms

    @staticmethod
    def _parse_terms(html: str) -> list[TermOption]:
        block = SELECT_RE.search(html)
        scope = block.group(1) if block else html
        out: list[TermOption] = []
        for value, label in OPTION_RE.findall(scope):
            value, label = value.strip(), label.strip()
            # 学期 id 形如 2026-2027-1
            if not re.fullmatch(r"\d{4}-\d{4}-\d{1,2}", value):
                continue
            selected = f'value="{value}" selected' in html or f"value='{value}' selected" in html
            out.append(TermOption(id=value, name=label or value, selected=selected))
        return out

    @staticmethod
    def _default_term(terms: list[TermOption]) -> str:
        # 1) 页面显式标记 selected 的学期
        for t in terms:
            if t.selected:
                return t.id
        # 2) 按当前日期推算（11 月不可能选「下学期」，真实踩过的坑：
        #    琼台学期下拉第一项是 2026-2027-2，导致 auto 抓了个空课表）
        from .term import current_terms

        ids = {t.id for t in terms}
        for pid in current_terms():
            if pid in ids:
                return pid
        # 3) 兜底取第一项
        return terms[0].id if terms else ""

    async def fetch_timetable_html(self, term_id: str) -> tuple[str, str]:
        """抓取课表页 HTML，返回 (html, 快照文件名)。"""
        await self.ensure_login()
        html = ""
        try:
            resp = await self.client.post(self.timetable_url, data={"xnxq01id": term_id, "zc": ""})
            html = decode_html(resp)
        except Exception as exc:
            raise http_error_to_jw(exc) from exc

        if any(w in html for w in LOGIN_PAGE_WORDS):
            # 会话在抓取瞬间失效，重登一次后重试
            self._log_warn("[jw] 抓课表时发现会话失效，重新登录后重试")
            await self.ensure_login(force=True)
            resp = await self.client.post(self.timetable_url, data={"xnxq01id": term_id, "zc": ""})
            html = decode_html(resp)
            if any(w in html for w in LOGIN_PAGE_WORDS):
                raise SessionExpiredError("重新登录后仍无法访问课表页")

        if "kbcontent" not in html and "课表" not in html:
            # 少数版本只支持 GET
            try:
                resp = await self.client.get(self.timetable_url, params={"xnxq01id": term_id})
                alt = decode_html(resp)
                if "kbcontent" in alt or len(alt) > len(html):
                    html = alt
            except Exception:
                pass

        snap_name = f"xskb_{term_id.replace('-', '')}_{int(time.time())}.html"
        self._save_snapshot(snap_name, html)
        return html, snap_name

    def _save_snapshot(self, name: str, html: str) -> Path:
        folder = self.dir / "snapshots"
        path = folder / name
        try:
            write_text(path, html)
            prune_files(folder, "xskb_*.html", self.cfg.schedule.snapshot_keep or 10)
        except Exception as exc:
            self._log_warn("[jw] 保存页面快照失败：%s", exc)
        return path

    @staticmethod
    def parse_server_week(html: str) -> int | None:
        """从页面里读出教务认定的「当前第几周」，用于反推第 1 周周一。"""
        m = NOW_WEEK_RE.search(html)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
        m2 = WEEK_TEXT_RE.search(html)
        if m2:
            try:
                return int(m2.group(1))
            except ValueError:
                pass
        return None


__all__ = ["JwClient", "decode_html", "decode_bytes", "NetworkError"]

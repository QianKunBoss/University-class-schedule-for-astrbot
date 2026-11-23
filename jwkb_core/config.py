"""配置解析：把 AstrBot 传进来的 dict 转成带默认值、带校验的强类型对象。

为什么要这一层：
* ``_conf_schema.json`` 新增字段后，旧配置文件里缺项时 AstrBot 会补默认值，
  但用户在 WebUI 里把数字填成字符串、把 periods 贴成非法 JSON 的情况很常见，
  这里统一兜住，避免运行期到处 ``int()`` 崩。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date

from .const import DEFAULT_PERIODS, PLUGIN_NAME, USER_AGENT
from .errors import ConfigError
from .models import Period
from .storage import mask_account


def _s(value, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _i(value, default: int) -> int:
    try:
        if isinstance(value, bool):
            return default
        return int(str(value).strip())
    except Exception:
        return default


def _b(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "on", "是"}:
        return True
    if s in {"0", "false", "no", "off", "否"}:
        return False
    return default


def _str_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        # 兼容用户手动输入的逗号/换行分隔
        parts = value.replace("，", ",").replace("\n", ",").split(",")
        return [p.strip() for p in parts if p.strip()]
    return []


def _ensure_scheme(url: str, default_scheme: str) -> str:
    """补全 URL 的协议头。

    用户从浏览器地址栏复制时经常丢掉 ``http://``，或者只复制了
    ``//jw.example.edu.cn/...``。CAS 对 ``service`` 参数是**逐字符严格匹配**的，
    少一个 ``http://`` 就会返回「未认证授权的服务」，所以这里统一补齐。

    Args:
        url: 原始地址。
        default_scheme: 缺省协议（教务用 http，统一认证用 https）。
    """
    u = (url or "").strip()
    if not u:
        return ""
    low = u.lower()
    if low.startswith(("http://", "https://")):
        return u
    if u.startswith("//"):
        return f"{default_scheme}:{u}"
    return f"{default_scheme}://{u.lstrip('/')}"


def _scheme_of(url: str) -> str:
    return "https" if (url or "").lower().startswith("https://") else "http"


def _parse_hm(text: str) -> str | None:
    """把 ``8:00`` / ``08:00`` / ``08：00`` 归一成 ``HH:MM``，非法返回 None。"""
    t = str(text).strip().replace("：", ":").replace(".", ":")
    if not t:
        return None
    parts = t.split(":")
    if len(parts) != 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return f"{h:02d}:{m:02d}"


def build_periods(raw: str | list | None) -> tuple[list[Period], bool]:
    """解析作息时间表。

    支持两种写法：
    * JSON 数组：``[{"index":1,"start":"08:00","end":"08:45"}, ...]``
    * 简写数组：``["08:00-08:45", "08:55-09:40"]``（按顺序编号，从 1 开始）

    Returns:
        (节次列表, 是否解析成功)。失败时回退内置默认值并把第二项置为 False，
        由调用方提示用户核对。
    """
    default = [Period(**p) for p in DEFAULT_PERIODS]
    if not raw:
        return default, True

    data = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return default, True
        try:
            data = json.loads(text)
        except Exception:
            return default, False

    if not isinstance(data, list) or not data:
        return default, False

    periods: list[Period] = []
    for i, item in enumerate(data, start=1):
        if isinstance(item, dict):
            start = _parse_hm(item.get("start", ""))
            end = _parse_hm(item.get("end", ""))
            idx = _i(item.get("index", i), i)
        elif isinstance(item, str) and "-" in item:
            a, b = item.split("-", 1)
            start, end = _parse_hm(a), _parse_hm(b)
            idx = i
        else:
            continue
        if start and end:
            periods.append(Period(index=idx, start=start, end=end))

    if not periods:
        return default, False
    periods.sort(key=lambda p: p.index)
    return periods, True


@dataclass
class JWConf:
    base_url: str = ""
    cas_url: str = ""
    service_url: str = ""
    username: str = ""
    password: str = ""
    auth_mode: str = "auto"  # auto / sso / direct
    ssl_verify: bool = True
    timeout: int = 20
    login_max_retry: int = 3
    captcha_ocr: bool = False
    user_agent: str = USER_AGENT

    @property
    def base(self) -> str:
        return self.base_url.rstrip("/")

    @property
    def display_account(self) -> str:
        return mask_account(self.username)

    def validate(self) -> None:
        if not self.base:
            raise ConfigError("未配置教务系统地址（jw.base_url）")
        if not self.base.lower().startswith(("http://", "https://")):
            raise ConfigError(
                f"教务地址格式不对：{self.base_url!r}",
                "必须以 http:// 或 https:// 开头，例如 http://jw.example.edu.cn",
            )
        if not self.username or not self.password:
            raise ConfigError("未配置教务账号或密码（jw.username / jw.password）")
        if self.auth_mode == "sso" and not self.cas_url:
            raise ConfigError("认证方式为 sso 但未配置统一认证地址（jw.cas_url）")
        if self.cas_url and not self.cas_url.lower().startswith(("http://", "https://")):
            raise ConfigError(
                f"统一认证地址格式不对：{self.cas_url!r}",
                "必须以 https:// 开头，例如 https://cas.example.edu.cn",
            )
        if self.cas_url and not self.service_url:
            raise ConfigError(
                "走 SSO 但无法确定回调地址（jw.service_url）",
                "请填写教务系统 SSO 回调地址，或补全 jw.base_url 让它自动推导",
            )


@dataclass
class ScheduleConf:
    term: str = "auto"
    week1_monday: str = ""
    max_week: int = 20
    periods: list[Period] = field(default_factory=list)
    periods_ok: bool = True
    big_period_size: int = 2
    refresh_hours: int = 6
    snapshot_keep: int = 10
    # 用 AstrBot 已配置的模型补全识别不出来的教师/教室
    llm_enhance: bool = False

    def period_start(self, index: int) -> str:
        for p in self.periods:
            if p.index == index:
                return p.start
        return ""

    def period_end(self, index: int) -> str:
        for p in self.periods:
            if p.index == index:
                return p.end
        return ""

    @property
    def week1_date(self) -> date | None:
        try:
            y, m, d = (int(x) for x in self.week1_monday.split("-"))
            return date(y, m, d)
        except Exception:
            return None


@dataclass
class NotifyConf:
    enabled: bool = True
    ahead_minutes: int = 20
    second_ahead_minutes: int = 0
    daily_brief_time: str = "07:00"
    brief_skip_empty: bool = True
    notify_on_change: bool = True
    max_sends_per_minute: int = 3


@dataclass
class PushConf:
    private_enabled: bool = True
    group_enabled: bool = False
    private_umos: list[str] = field(default_factory=list)
    group_umos: list[str] = field(default_factory=list)

    def targets(self) -> list[str]:
        """按开关合并出实际要推送的会话列表（去重、保序）。"""
        out: list[str] = []
        if self.private_enabled:
            out.extend(self.private_umos)
        if self.group_enabled:
            out.extend(self.group_umos)
        seen, uniq = set(), []
        for u in out:
            if u and u not in seen:
                seen.add(u)
                uniq.append(u)
        return uniq


@dataclass
class Config:
    jw: JWConf
    schedule: ScheduleConf
    notify: NotifyConf
    push: PushConf

    @classmethod
    def from_dict(cls, raw: dict | None) -> Config:
        raw = raw or {}
        jw_raw = raw.get("jw") or {}
        sch_raw = raw.get("schedule") or {}
        nt_raw = raw.get("notify") or {}
        ps_raw = raw.get("push") or {}

        # URL 一律补全协议头：CAS 对 service 参数是严格匹配的，
        # 少写 http:// 或者把 http 写成 https，都会直接得到「未认证授权的服务」。
        base_url = _ensure_scheme(_s(jw_raw.get("base_url")), "http")
        # 用户常把教务根路径一起填进来（http://host/jsxsd），这里剥掉，
        # 否则后面拼 /jsxsd/xxx 会变成 /jsxsd/jsxsd/xxx
        if base_url.lower().rstrip("/").endswith("/jsxsd"):
            base_url = base_url.rstrip("/")[: -len("/jsxsd")]
        cas_url = _ensure_scheme(_s(jw_raw.get("cas_url")), "https").rstrip("/")
        service_url = _s(jw_raw.get("service_url"))
        if service_url:
            # 回调地址属于教务侧，缺协议头时沿用教务的协议（通常 http）
            service_url = _ensure_scheme(service_url, _scheme_of(base_url))
        elif base_url:
            service_url = base_url.rstrip("/") + "/jsxsd/sso.jsp"

        auth_mode = _s(jw_raw.get("auth_mode"), "auto").lower()
        if auth_mode not in {"auto", "sso", "direct"}:
            auth_mode = "auto"

        jw = JWConf(
            base_url=base_url,
            cas_url=cas_url,
            service_url=service_url,
            username=_s(jw_raw.get("username")),
            password=_s(jw_raw.get("password")),
            auth_mode=auth_mode,
            ssl_verify=_b(jw_raw.get("ssl_verify"), True),
            timeout=max(5, _i(jw_raw.get("timeout"), 20)),
            login_max_retry=max(0, min(_i(jw_raw.get("login_max_retry"), 3), 10)),
            captcha_ocr=_b(jw_raw.get("captcha_ocr"), False),
            user_agent=_s(jw_raw.get("user_agent")) or USER_AGENT,
        )

        periods, periods_ok = build_periods(sch_raw.get("periods"))
        schedule = ScheduleConf(
            term=_s(sch_raw.get("term"), "auto") or "auto",
            week1_monday=_s(sch_raw.get("week1_monday")),
            max_week=max(1, min(_i(sch_raw.get("max_week"), 20), 40)),
            periods=periods,
            periods_ok=periods_ok,
            big_period_size=max(1, min(_i(sch_raw.get("big_period_size"), 2), 4)),
            refresh_hours=max(1, _i(sch_raw.get("refresh_hours"), 6)),
            snapshot_keep=max(0, _i(sch_raw.get("snapshot_keep"), 10)),
            llm_enhance=_b(sch_raw.get("llm_enhance"), False),
        )

        brief = _s(nt_raw.get("daily_brief_time"), "07:00")
        if brief not in {"", "off", "关闭"} and _parse_hm(brief) is None:
            brief = "07:00"
        notify = NotifyConf(
            enabled=_b(nt_raw.get("enabled"), True),
            ahead_minutes=max(0, min(_i(nt_raw.get("ahead_minutes"), 20), 240)),
            second_ahead_minutes=max(0, min(_i(nt_raw.get("second_ahead_minutes"), 0), 120)),
            daily_brief_time=_parse_hm(brief) or "",
            brief_skip_empty=_b(nt_raw.get("brief_skip_empty"), True),
            notify_on_change=_b(nt_raw.get("notify_on_change"), True),
            max_sends_per_minute=max(1, min(_i(nt_raw.get("max_sends_per_minute"), 3), 20)),
        )

        push = PushConf(
            private_enabled=_b(ps_raw.get("private_enabled"), True),
            group_enabled=_b(ps_raw.get("group_enabled"), False),
            private_umos=_str_list(ps_raw.get("private_umos")),
            group_umos=_str_list(ps_raw.get("group_umos")),
        )

        # 注：0.3.3 起面板不再有启用/关闭开关，配置里的历史 web 分组会被忽略
        return cls(jw=jw, schedule=schedule, notify=notify, push=push)

    def summary(self) -> dict:
        """用于 /kb status 的安全摘要（不含密码）。"""
        return {
            "base_url": self.jw.base,
            "auth_mode": self.jw.auth_mode,
            "account": self.jw.display_account,
            "term": self.schedule.term,
            "ahead": self.notify.ahead_minutes,
            "targets": len(self.push.targets()),
        }


__all__ = ["Config", "JWConf", "ScheduleConf", "NotifyConf", "PushConf", "build_periods", "PLUGIN_NAME"]

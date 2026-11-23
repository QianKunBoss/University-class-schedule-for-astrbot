"""CAS 统一身份认证客户端（适配金智 tpass 体系）。

流程（每一步都对应真机抓到的前端行为，见 ``/tpass/comm/se/js/login_se.js``）：

1. ``GET  {cas}/tpass/login?service=<教务回调地址>``
   → 页面里带隐藏域 ``lt``（一次性登录票据）与 ``execution``
2. ``POST {cas}/tpass/rsa``
   → ``{"publicKey": "MFww..."}``，这是 **Base64 编码的 DER**（SubjectPublicKeyInfo），
     不是 PEM。用 ``load_der_public_key`` 解析，用错会一直报解密失败。
3. RSA PKCS#1 v1.5 分别加密用户名、密码，Base64 后填入 ``ul`` / ``pl``
4. ``POST {cas}/tpass/login?service=...``，表单带
   ``rsa(空) / ul / pl / lt / execution / choosenumber / _eventId=submit``
5. 成功返回 302，``Location`` 里带 ``?ticket=ST-...``
6. ``GET`` 该 Location，教务系统用它向 CAS 校验票据并建立自己的会话

注意：教师/学生账号在多身份时会返回「选择账号」页（需 ``choosenumber`` + 3DES），
本模块检测到就抛 ``MultiIdentityError``，不做静默猜测。
"""

from __future__ import annotations

import base64
import re
from urllib.parse import quote, urljoin, urlsplit

import httpx

from .errors import (
    AuthError,
    CaptchaRequiredError,
    MultiIdentityError,
    NetworkError,
    http_error_to_jw,
)
from .textutil import decode_bytes, extract_error, strip_tags

LT_RE = re.compile(r"""id=["']lt["'][^>]*value=["']([^"']+)""", re.I)
EXEC_RE = re.compile(r"""name=["']execution["'][^>]*value=["']([^"']*)""", re.I)
TICKET_RE = re.compile(r"[?&]ticket=(ST-[^&\s\"'<>]+)", re.I)

# CAS 返回登录页且带这些词，说明是凭据问题（不可重试，避免触发锁号）
_AUTH_FAIL_WORDS = (
    "密码错误",
    "密码不正确",
    "用户名或密码",
    "账号或密码",
    "用户名不存在",
    "账号不存在",
    "用户不存在",
    "认证失败",
    "登录失败",
    "密码已过期",
    "账号已被锁定",
    "账号被锁定",
    "用户已被禁用",
)
_CAPTCHA_WORDS = ("验证码错误", "验证码不正确", "请输入验证码")
_MULTI_WORDS = ("请选择你要登录的账号", "确认登录账号", "选择账号", "numberlist")
# CAS 拒绝 service 参数时返回的错误页（关键：这种页面**没有 lt**，
# 如果当成「已有会话」继续走，会在换票据时报出完全不相干的错误）
_SERVICE_ERROR_WORDS = (
    "未认证授权",
    "未授权的服务",
    "不允许使用CAS",
    "不允许使用cas",
    "invalid service",
    "未注册的服务",
)

# 判断教务侧「已登录」的特征
_LOGIN_PAGE_WORDS = ("LoginToXk", "用户登录", "请输入账号")
_LOGGED_IN_WORDS = ("xsMain", "学号", "退出", "课表")


def _same_host(url, cas_url: str) -> bool:
    """判断最终落地地址是否还在统一认证域名上。"""
    try:
        a = urlsplit(str(url)).netloc.lower()
        b = urlsplit(cas_url).netloc.lower()
        return bool(a) and a == b
    except Exception:
        return True


def _body(resp: httpx.Response) -> str:
    """统一按原始 bytes 解码，避免各校 CAS 编码不一致导致中文乱码。"""
    return decode_bytes(resp.content, resp.headers.get("content-type", ""))


def rsa_encrypt_b64(public_key_b64: str, text: str) -> str:
    """用服务端下发的 DER 公钥做 RSA PKCS#1 v1.5 加密，返回 Base64 字符串。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    raw = base64.b64decode(public_key_b64)
    key = serialization.load_der_public_key(raw)
    max_len = key.key_size // 8 - 11  # PKCS#1 v1.5 填充占 11 字节
    data = text.encode("utf-8")
    if len(data) > max_len:
        raise AuthError(
            f"账号或密码过长（{len(data)} 字节，该公钥上限 {max_len} 字节）",
            "请联系管理员确认统一认证使用的密钥长度",
        )
    return base64.b64encode(key.encrypt(data, padding.PKCS1v15())).decode("ascii")


def build_login_url(cas_url: str, service_url: str) -> str:
    """拼接 CAS 登录地址，``service`` 必须完整 URL 编码（含 ://）。"""
    base = cas_url.rstrip("/")
    if not base.endswith("/tpass"):
        base = base + "/tpass"
    return f"{base}/login?service={quote(service_url, safe='')}"


class SsoClient:
    """CAS 登录器。一个实例复用同一个 httpx 客户端，保证 cookie jar 连续。"""

    def __init__(self, client: httpx.AsyncClient, cfg, log=None):
        self.client = client
        self.cfg = cfg
        self.cas_url = str(getattr(cfg, "cas_url", "") or "").rstrip("/")
        self.service_url = str(getattr(cfg, "service_url", "") or "")
        self.log = log

    # ---------- 内部工具 ----------
    def _warn(self, msg: str, *args) -> None:
        if self.log:
            self.log.warning(msg, *args)

    def _info(self, msg: str, *args) -> None:
        if self.log:
            self.log.info(msg, *args)

    @property
    def _tpass_base(self) -> str:
        base = self.cas_url
        return base if base.endswith("/tpass") else base + "/tpass"

    # ---------- 主流程 ----------
    async def login(self) -> str:
        """执行完整 CAS 登录，返回 ``ST-`` 票据。

        Raises:
            AuthError: 账号/密码错误（不可重试）。
            CaptchaRequiredError: 服务端要求验证码。
            MultiIdentityError: 账号多身份需人工选择。
            NetworkError: 网络类错误（可重试）。
        """
        login_url = build_login_url(self.cas_url, self.service_url)
        self._info("[sso] 请求统一认证登录页")

        # 0) 预热教务会话：部分学校（如琼台）的 sso.jsp 要求**会话已存在**才接受票据，
        #    否则校验失败并强制登出（落地页 var flag='1' → Logon.do?method=logout）。
        #    浏览器里用户总是先打开教务页再跳登录，这里补上同样的前置请求。
        if self.service_url:
            try:
                await self.client.get(self.service_url, follow_redirects=False)
            except Exception as exc:
                self._warn("[sso] 预热教务会话失败（继续登录）：%s", exc)

        try:
            resp = await self.client.get(login_url, follow_redirects=True)
        except Exception as exc:  # httpx 的异常族
            raise http_error_to_jw(exc) from exc

        html = _body(resp)
        lt = LT_RE.search(html)
        if not lt:
            # 情况一：CAS 直接把请求重定向到了业务系统 —— 说明 CASTGC 会话仍然有效
            if not _same_host(resp.url, self.cas_url):
                self._info("[sso] 已被直接重定向到业务地址，说明 CAS 会话仍有效：%s", str(resp.url)[:120])
                return ""

            # 情况二：CAS 拒绝了 service 参数（页面里没有 lt，只有「系统提示」）
            text = strip_tags(html)
            for word in _SERVICE_ERROR_WORDS:
                if word in text:
                    raise AuthError(
                        f"统一认证拒绝了回调地址：{text[:60]}",
                        f"当前 service_url = {self.service_url}。该参数必须与浏览器登录时"
                        "跳转地址逐字符一致（含 http:// 或 https://、端口）；"
                        "不少学校教务只接受 http:// 而不是 https://",
                    )

            # 情况三：其他异常页，把诊断信息写清楚，便于远程排障
            raise AuthError(
                f"统一认证登录页结构异常（HTTP {resp.status_code}，{len(html)} 字节，无 lt 字段）",
                f"service_url = {self.service_url}；"
                f"落地地址 = {str(resp.url)[:100]}。若确认账号密码无误，请把本行日志反馈给作者",
            )

        lt_value = lt.group(1)
        execution = EXEC_RE.search(html)
        execution_value = execution.group(1) if execution else "e1s1"
        self._info("[sso] 取得 lt=%s execution=%s", lt_value[:16] + "...", execution_value)

        # 2) 取公钥
        rsa_url = urljoin(self._tpass_base + "/", "rsa")
        try:
            rsa_resp = await self.client.post(rsa_url)
            rsa_resp.raise_for_status()
            public_key = (rsa_resp.json() or {}).get("publicKey", "")
        except Exception as exc:
            raise NetworkError(f"无法获取统一认证公钥：{exc}") from exc
        if not public_key:
            raise NetworkError("统一认证公钥接口未返回 publicKey，接口可能已改版")

        # 3) 加密凭据
        cfg = self.cfg
        ul = rsa_encrypt_b64(public_key, cfg.username)
        pl = rsa_encrypt_b64(public_key, cfg.password)

        # 4) 提交登录
        form = {
            "rsa": "",
            "ul": ul,
            "pl": pl,
            "lt": lt_value,
            "execution": execution_value,
            "choosenumber": "",
            "_eventId": "submit",
        }
        try:
            res = await self.client.post(login_url, data=form, follow_redirects=False)
        except Exception as exc:
            raise http_error_to_jw(exc) from exc

        kind, payload = self._classify(res)
        if kind == "ticket":
            self._info("[sso] 登录成功，取得服务票据")
            return payload
        if kind == "auth_failed":
            raise AuthError(f"统一认证拒绝登录：{payload}")
        if kind == "captcha":
            raise CaptchaRequiredError()
        if kind == "multi":
            raise MultiIdentityError()
        if kind == "redirect":
            # 302 但没带 ticket：多半是被踢回登录页（凭据错误或会话异常）
            raise AuthError(f"统一认证未返回票据（跳转至 {payload[:80]}）")
        raise AuthError(f"统一认证返回了无法识别的结果：{payload[:120]}")

    def _classify(self, resp: httpx.Response) -> tuple[str, str]:
        """判定 CAS 的响应类型。

        优先级：302 带票据 > 显式错误容器 > 关键词扫描 > 未知。
        显式错误容器（如 ``id="errormsghide"``）最重要 —— 它在 text/html 模板里，
        必须先按 id 精确取，再谈全文扫描。
        """
        loc = resp.headers.get("location", "") or ""
        if resp.status_code in (301, 302, 303, 307, 308):
            m = TICKET_RE.search(loc)
            if m:
                return "ticket", m.group(1)
            return "redirect", loc

        html = _body(resp)
        err = extract_error(html)
        text = strip_tags(html)

        probe = f"{err} {text}"
        for w in _AUTH_FAIL_WORDS:
            if w in probe:
                return "auth_failed", err or w
        for w in _CAPTCHA_WORDS:
            if w in probe:
                return "captcha", w
        if any(w in probe for w in _MULTI_WORDS):
            return "multi", "选择账号"
        # 页面里重新出现验证码输入框
        if re.search(r"""id=["']code["']""", html):
            return "captcha", "需要验证码"
        return "unknown", (err or text)[:200]

    async def open_service(self, ticket: str) -> str:
        """拿票据去教务系统换会话，返回最终落地页 HTML。

        会对多个回调地址变体依次尝试 —— 不同学校强智的 SSO 接收端路径不一样。
        """
        candidates: list[str] = []
        if ticket:
            sep = "&" if "?" in self.service_url else "?"
            candidates.append(f"{self.service_url}{sep}ticket={ticket}")
        candidates.append(self.service_url)

        last_err = ""
        for url in candidates:
            try:
                resp = await self.client.get(
                    url,
                    follow_redirects=True,
                    headers={"Referer": build_login_url(self.cas_url, self.service_url)},
                )
            except Exception as exc:
                last_err = str(exc)
                continue
            body = _body(resp)
            if resp.status_code < 400 and not any(w in body for w in _LOGIN_PAGE_WORDS):
                self._info(
                    "[sso] 教务会话建立成功（HTTP %s，落地 %s）", resp.status_code, str(resp.url)[:120]
                )
                return body
            last_err = f"HTTP {resp.status_code}"
            self._warn("[sso] 回调 %s 失败：%s", url[:120], last_err)

        if "unknown url type" in last_err or "missing an 'http" in last_err:
            hint = f"回调地址不是完整 URL：{self.service_url!r}。请填写以 http:// 或 https:// 开头的完整地址"
        else:
            hint = "可能是教务侧 SSO 配置变更，请把本次日志反馈给插件作者"
        raise AuthError(
            f"统一认证已通过，但教务系统未接受票据（{last_err}）",
            hint,
        )

    async def verify_session(self, probe_url: str) -> bool:
        """访问教务首页判断会话是否真的可用（以页面内容为准，不看状态码）。"""
        try:
            resp = await self.client.get(probe_url, follow_redirects=True)
        except Exception:
            return False
        body = _body(resp)
        if any(w in body for w in _LOGIN_PAGE_WORDS):
            return False
        return any(w in body for w in _LOGGED_IN_WORDS)

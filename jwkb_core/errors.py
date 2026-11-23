"""异常体系。

设计原则：
1. 每个异常都带 ``user_msg``（可直接回给用户的中文），避免把堆栈抛到聊天窗口；
2. 区分「可重试」与「不可重试」—— 账号密码错误绝不能重试，否则会触发学校锁号。
"""

from __future__ import annotations


class JwError(Exception):
    """所有教务相关异常的基类。"""

    retryable = False

    def __init__(self, detail: str = "", hint: str = ""):
        self.detail = detail
        self.hint = hint
        super().__init__(detail or self.__class__.__name__)

    @property
    def user_msg(self) -> str:
        parts = [self.detail or "未知错误"]
        if self.hint:
            parts.append(self.hint)
        return "；".join(parts)


class ConfigError(JwError):
    """配置缺失或非法。"""

    def __init__(self, detail: str = "", hint: str = ""):
        super().__init__(detail, hint or "请在 WebUI 插件配置中补全后重试")


class NetworkError(JwError):
    """网络层错误（超时、DNS、连接被拒）。"""

    retryable = True

    def __init__(self, detail: str = "", hint: str = ""):
        super().__init__(detail, hint or "请检查 AstrBot 所在机器能否访问教务系统")


class AuthError(JwError):
    """账号或密码错误 —— 不可重试。"""

    retryable = False

    def __init__(self, detail: str = "", hint: str = ""):
        super().__init__(detail, hint or "请核对账号密码；连续错误可能导致账号被临时锁定")


class CaptchaRequiredError(JwError):
    """服务端要求验证码，但未启用 OCR。"""

    retryable = False

    def __init__(self, detail: str = "", hint: str = ""):
        super().__init__(
            detail or "登录需要验证码",
            hint or "请安装 ddddocr（pip install ddddocr）并在配置中打开 captcha_ocr",
        )


class MultiIdentityError(JwError):
    """一个账号绑定了多个身份（如学生 + 教工），CAS 要求选择身份。"""

    retryable = False

    def __init__(self, detail: str = "", hint: str = ""):
        super().__init__(
            detail or "该账号在统一认证下绑定了多个身份，需要人工选择",
            hint or "请先用浏览器登录统一认证，确认默认身份后把账号提供给管理员排查",
        )


class SessionExpiredError(JwError):
    """会话失效，需要重新登录。"""

    retryable = True

    def __init__(self, detail: str = "教务会话已失效", hint: str = ""):
        super().__init__(detail, hint)


class ParseError(JwError):
    """页面结构无法解析（通常是教务系统改版）。"""

    retryable = False

    def __init__(self, detail: str = "", hint: str = ""):
        super().__init__(
            detail or "无法解析教务系统返回的课表页面",
            hint or "已保存页面快照，请把 snapshots 目录下的文件反馈给插件作者",
        )


class NoTimetableError(JwError):
    """登录成功但当前学期没有课表数据。"""

    retryable = False

    def __init__(self, detail: str = "当前学期未查询到课表", hint: str = ""):
        super().__init__(detail, hint or "请用 /kb term 切换到正确学期后重试")


def http_error_to_jw(exc: Exception) -> JwError:
    """把 httpx 异常翻译成带中文提示的 NetworkError。"""
    name = exc.__class__.__name__
    if name in {"ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "TimeoutException"}:
        return NetworkError(f"连接教务系统超时（{name}）")
    if name in {"ConnectError", "ConnectError"}:
        return NetworkError(f"无法连接教务系统（{exc}）")
    if name in {"TooManyRedirects", "RemoteProtocolError", "HTTPError"}:
        return NetworkError(f"教务系统响应异常（{name}）")
    return NetworkError(f"请求教务系统失败：{exc}")

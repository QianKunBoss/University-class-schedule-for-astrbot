"""配置归一化与校验测试。

这块的教训来自真实故障：用户在 WebUI 里把教务地址填成 ``jw.example.edu.cn``（少了 http://），
插件推导出的 SSO 回调地址就变成 ``jw.example.edu.cn/jsxsd/sso.jsp``；
CAS 对 ``service`` 参数是逐字符严格匹配的，于是返回「未认证授权的服务」错误页 —— 该页面**没有 lt 字段**，
被旧代码误判成「已有有效会话」，最终报出 ``unknown url type`` 这种完全误导人的错误。

所以 URL 归一化必须有测试兜住。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jwkb_core.config import Config, _ensure_scheme  # noqa: E402
from jwkb_core.errors import ConfigError  # noqa: E402


def _cfg(**jw) -> Config:
    return Config.from_dict({"jw": jw})


@pytest.mark.parametrize(
    "raw,default,expected",
    [
        ("jw.example.edu.cn", "http", "http://jw.example.edu.cn"),
        ("jw.example.edu.cn/jsxsd/sso.jsp", "http", "http://jw.example.edu.cn/jsxsd/sso.jsp"),
        ("/jw.example.edu.cn/jsxsd/sso.jsp", "http", "http://jw.example.edu.cn/jsxsd/sso.jsp"),
        ("//jw.example.edu.cn/jsxsd/sso.jsp", "http", "http://jw.example.edu.cn/jsxsd/sso.jsp"),
        ("https://jw.example.edu.cn", "http", "https://jw.example.edu.cn"),
        ("", "http", ""),
    ],
)
def test_ensure_scheme(raw, default, expected):
    assert _ensure_scheme(raw, default) == expected


def test_base_url_without_scheme_gets_http():
    cfg = _cfg(base_url="jw.example.edu.cn", username="1", password="2")
    assert cfg.jw.base == "http://jw.example.edu.cn"
    assert cfg.jw.service_url == "http://jw.example.edu.cn/jsxsd/sso.jsp"


def test_service_url_relative_is_fixed():
    """用户把回调地址填成 /jw.xxx/... 或 //jw.xxx/... 时也要救回来。"""
    for raw in ("/jw.example.edu.cn/jsxsd/sso.jsp", "//jw.example.edu.cn/jsxsd/sso.jsp"):
        cfg = _cfg(base_url="http://jw.example.edu.cn", service_url=raw, username="1", password="2")
        assert cfg.jw.service_url == "http://jw.example.edu.cn/jsxsd/sso.jsp"


def test_explicit_https_service_kept():
    """显式写了 https 就尊重用户选择，不擅自改成 http。"""
    cfg = _cfg(
        base_url="http://jw.example.edu.cn",
        service_url="https://jw.example.edu.cn/jsxsd/sso.jsp",
        username="1",
        password="2",
    )
    assert cfg.jw.service_url.startswith("https://")


def test_base_url_with_jsxsd_suffix_is_stripped():
    """把 /jsxsd 一起填进来时不能拼出 /jsxsd/jsxsd。"""
    cfg = _cfg(base_url="http://jw.example.edu.cn/jsxsd", username="1", password="2")
    assert cfg.jw.base == "http://jw.example.edu.cn"
    assert cfg.jw.service_url == "http://jw.example.edu.cn/jsxsd/sso.jsp"


def test_cas_url_defaults_to_https():
    cfg = _cfg(cas_url="cas.example.edu.cn", username="1", password="2")
    assert cfg.jw.cas_url == "https://cas.example.edu.cn"


def test_validate_ok():
    cfg = _cfg(
        base_url="http://jw.example.edu.cn",
        cas_url="https://cas.example.edu.cn",
        auth_mode="sso",
        username="2026001",
        password="pwd",
    )
    cfg.jw.validate()  # 不应抛异常


def test_validate_requires_base_url():
    with pytest.raises(ConfigError):
        _cfg(username="1", password="2").jw.validate()


def test_validate_requires_credentials():
    with pytest.raises(ConfigError):
        _cfg(base_url="http://jw.example.edu.cn").jw.validate()


def test_validate_sso_requires_cas():
    with pytest.raises(ConfigError):
        _cfg(base_url="http://jw.example.edu.cn", auth_mode="sso", username="1", password="2").jw.validate()


def test_password_not_in_summary():
    cfg = _cfg(base_url="http://jw.example.edu.cn", username="2026001", password="secret-pwd")
    text = str(cfg.summary())
    assert "secret-pwd" not in text

"""Web API 分发层测试。

terms 路由在线上出现过「未找到该路由」，根因是具体路由在某些环境下匹配不上；
兜底方案是 `<path:rest>` 通配路由 + 内部按表分发。这里验证分发逻辑本身。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jwkb_core import webapi  # noqa: E402


class FakeJw:
    auth_mode_used = "sso"

    async def fetch_terms(self):
        return []

    async def is_logged_in(self):
        return True

    def _default_term(self, terms):
        return ""


class FakePlugin:
    logger = None
    config = {}
    scheduler = None
    jw = FakeJw()

    def cfg(self):
        from jwkb_core.config import Config

        return Config.from_dict(
            {
                "jw": {
                    "base_url": "http://jw.example.edu.cn",
                    "cas_url": "https://cas.example.edu.cn",
                    "auth_mode": "sso",
                    "username": "2026001",
                    "password": "pwd",
                }
            }
        )


def _panel() -> webapi.WebPanel:
    return webapi.WebPanel(FakePlugin())


def test_dispatch_terms_via_fallback():
    panel = _panel()
    r = asyncio.run(panel._dispatch("terms"))
    assert r["ok"] is True
    assert r["terms"] == []
    assert r["logged_in"] is True


def test_dispatch_status_via_fallback():
    panel = _panel()
    r = asyncio.run(panel._dispatch("status"))
    assert r["ok"] is True
    assert r["version"]


def test_dispatch_unknown_path_is_404():
    panel = _panel()
    r = asyncio.run(panel._dispatch("nope"))
    # 兜底环境（无 astrbot/quart）返回 (dict, status) 二元组
    body, code = r
    assert code == 404
    assert "nope" in body["message"]


def test_dispatch_strips_slashes():
    panel = _panel()
    r = asyncio.run(panel._dispatch("/terms/"))
    assert r["ok"] is True


def test_register_includes_fallback_route():
    """注册函数应把通配兜底路由也登记进去。"""

    class Ctx:
        def __init__(self):
            self.registered = []

        def register_web_api(self, route, handler, methods, desc):
            self.registered.append(route)

    ctx = Ctx()
    done = _panel().register(ctx)
    assert any("<path:rest>" in p for p in done)
    assert ctx.registered == done

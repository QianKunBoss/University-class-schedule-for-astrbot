"""消息推送：把文本发到配置好的私聊/群会话。

设计要点：
* 主动消息依赖 ``unified_msg_origin``（umo），插件通过 ``/kb push here`` 采集；
* 发送有限频，避免同一分钟连发多条被 QQ 判定异常；
* 失败只记一次警告并退避，不热重试（群发被风控时越重试越糟）。
"""

from __future__ import annotations

import asyncio
import time

from .config import Config


class Notifier:
    def __init__(self, context, cfg_provider, log=None):
        self.context = context
        self.cfg_provider = cfg_provider
        self.log = log
        self._lock = asyncio.Lock()
        self._last_send = 0.0
        self._fail_streak = 0

    def _warn(self, msg: str, *args) -> None:
        if self.log:
            self.log.warning(msg, *args)

    def _info(self, msg: str, *args) -> None:
        if self.log:
            self.log.info(msg, *args)

    # ---------------- 发送 ----------------
    async def _send_one(self, umo: str, text: str) -> bool:
        """发一条纯文本，做两级兼容：MessageChain → MessageEventResult。"""
        try:
            from astrbot.api.event import MessageChain

            await self.context.send_message(umo, MessageChain().message(text))
            return True
        except Exception as exc:
            self._warn("[kb] MessageChain 发送失败（%s），尝试回退方式", exc)

        try:
            from astrbot.api.event import MessageEventResult

            await self.context.send_message(umo, MessageEventResult().message(text))
            return True
        except Exception as exc:
            self._warn("[kb] 推送失败：%s", exc)
            return False

    async def broadcast(self, text: str) -> int:
        """按配置推送到所有目标，返回成功条数。"""
        cfg: Config = self.cfg_provider()
        targets = cfg.push.targets()
        if not targets:
            self._warn(
                "[kb] 没有推送目标，消息未发送。请在被推送的会话里发 /kb push here，"
                "或在 WebUI 配置里选择会话。"
            )
            return 0

        sent = 0
        gap = 60.0 / max(1, cfg.notify.max_sends_per_minute)
        async with self._lock:
            for umo in targets:
                # 限频：两条之间至少间隔 gap 秒
                wait = gap - (time.monotonic() - self._last_send)
                if wait > 0:
                    await asyncio.sleep(min(wait, 5))
                ok = await self._send_one(umo, text)
                self._last_send = time.monotonic()
                if ok:
                    sent += 1
                    self._fail_streak = 0
                else:
                    self._fail_streak += 1
                    if self._fail_streak >= 3:
                        self._warn("[kb] 已连续 %d 次推送失败，本轮停止", self._fail_streak)
                        break
        if sent:
            self._info("[kb] 已推送 %d/%d 个会话", sent, len(targets))
        return sent

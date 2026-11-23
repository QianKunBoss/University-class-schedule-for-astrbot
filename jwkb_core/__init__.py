"""教务课表插件核心模块。

统一使用绝对导入 ``from jwkb_core.xxx import ...``，由 ``main.py`` 把插件根目录
插入 ``sys.path``。这样避免相对导入在 AstrBot 以 ``data.plugins.<name>``
形式加载时产生「同一个类被加载两次」的问题（会导致 asyncio.Lock 与单例失效）。

包名特意不叫 ``core``：AstrBot 同时加载多个插件时，若别的插件也叫 ``core``，
``sys.modules['core']`` 已被占用，本插件会 import 到别人的模块，
报出「core.scheduler 没有 due_class_alerts」这类完全摸不着头脑的错误。
"""

from __future__ import annotations

__all__ = [
    "const",
    "errors",
    "storage",
    "models",
    "config",
]

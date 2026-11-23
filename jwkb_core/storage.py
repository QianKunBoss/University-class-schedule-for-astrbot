"""持久化工具：数据目录解析与 JSON 原子读写。

规范要求（插件市场上架审核明确检查）：
* 所有持久化数据必须落在 AstrBot 的 ``data`` 目录下，不能写插件自身目录，
  否则插件更新/重装时数据会丢失；
* 优先使用框架接口 ``StarTools.get_data_dir()``，失败时按同一规范手工反推。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def resolve_data_dir(plugin_name: str, override: str | None = None) -> Path:
    """返回本插件的数据目录，并确保其存在。

    Args:
        plugin_name: 插件名（须显式传入，见下方说明）。
        override: 显式指定的目录（探针脚本、单元测试使用）。

    Note:
        ``StarTools.get_data_dir()`` 依赖 ``inspect`` 取调用者模块名去插件注册表
        查元数据，在子模块里调用会探测失败。因此这里必须显式传插件名，
        而不是依赖它的自动探测。
    """
    if override:
        path = Path(override)
        path.mkdir(parents=True, exist_ok=True)
        return path

    try:  # 优先走框架接口
        from astrbot.api.star import StarTools

        p = StarTools.get_data_dir(plugin_name)
        if p:
            path = Path(str(p))
            path.mkdir(parents=True, exist_ok=True)
            return path
    except Exception:
        # 框架接口缺失或版本差异，走下面的回退逻辑，不向上抛
        pass

    # 回退：本文件位于 <data>/plugins/<plugin_name>/jwkb_core/storage.py
    plugin_dir = Path(__file__).resolve().parent.parent
    path = data_root_from_plugin_dir(plugin_dir) / "plugin_data" / plugin_name
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_root_from_plugin_dir(plugin_dir: str | Path) -> Path:
    """由插件目录反推 data 根：``<data>/plugins/<name>`` -> ``<data>``。

    单独抽出来是因为这里最容易算错层级（踩过：多退一层得到 ``<data>/plugins``，
    结果配置文件路径全找不到，面板开关永远读的是内存旧值）。
    """
    return Path(plugin_dir).resolve().parent.parent


def data_root() -> Path:
    """AstrBot 的 data 根目录（``.../data``），由本文件位置反推。"""
    # 本文件位于 <data>/plugins/<plugin_name>/jwkb_core/storage.py
    return data_root_from_plugin_dir(Path(__file__).resolve().parent.parent)


def plugin_config_path(plugin_name: str) -> Path:
    """插件配置文件的标准位置：``<data>/config/<plugin_name>_config.json``。"""
    return data_root() / "config" / f"{plugin_name}_config.json"


def config_dir() -> Path:
    return data_root() / "config"


def read_json(path: str | Path, default: Any = None) -> Any:
    """读取 JSON；文件不存在或内容损坏时返回 ``default``（不抛异常）。"""
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: str | Path, data: Any) -> None:
    """原子写 JSON：先写临时文件再 ``os.replace``，避免断电/崩溃留下半截文件。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except Exception:
        # 清理临时文件后继续抛出，由调用方决定如何记录
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_text(path: str | Path, text: str) -> None:
    """写入文本文件（快照用），同样保证父目录存在。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def prune_files(folder: str | Path, pattern: str, keep: int) -> None:
    """只保留最新的 ``keep`` 个匹配文件，用于快照目录滚动清理。"""
    if keep <= 0:
        return
    d = Path(folder)
    if not d.exists():
        return
    files = sorted(d.glob(pattern), key=lambda x: x.stat().st_mtime, reverse=True)
    for f in files[keep:]:
        try:
            f.unlink()
        except OSError:
            pass


def mask_account(account: str) -> str:
    """账号脱敏：``12345678`` -> ``1234****78``，用于日志与状态展示。"""
    s = (account or "").strip()
    if len(s) <= 4:
        return "*" * len(s) if s else ""
    if len(s) <= 8:
        return s[:2] + "****"
    return f"{s[:4]}****{s[-2:]}"

"""路径反推测试。

这里出过一次真实的线上故障：``data_root()`` 多退了一层目录，
得到 ``<data>/plugins`` 而不是 ``<data>``，导致插件永远找不到自己的配置文件，
面板开关只能读到内存里的旧值 —— 表现就是「配置里明明是 true，界面一直显示已关闭」。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jwkb_core.storage import (  # noqa: E402
    data_root_from_plugin_dir,
    mask_account,
    plugin_config_path,
)


def test_data_root_from_plugin_dir(tmp_path):
    plugin_dir = tmp_path / "data" / "plugins" / "astrbot_plugin_jw_timetable"
    plugin_dir.mkdir(parents=True)
    assert data_root_from_plugin_dir(plugin_dir) == (tmp_path / "data").resolve()


def test_data_root_should_be_data_not_plugins(tmp_path):
    """回归：早期版本返回了 <data>/plugins，这里显式钉住必须是 <data>。"""
    plugin_dir = tmp_path / "data" / "plugins" / "p1"
    plugin_dir.mkdir(parents=True)
    root = data_root_from_plugin_dir(plugin_dir)
    assert root.name == "data"
    assert root.parent == tmp_path.resolve()


def test_plugin_config_path_shape():
    p = plugin_config_path("astrbot_plugin_jw_timetable")
    assert p.name == "astrbot_plugin_jw_timetable_config.json"
    assert p.parent.name == "config"


def test_mask_account():
    assert mask_account("2026123456") == "2026****56"
    assert mask_account("123") == "***"
    assert mask_account("") == ""

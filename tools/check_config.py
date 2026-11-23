#!/usr/bin/env python3
"""配置诊断：这台机器上插件到底读哪个配置文件、里面有什么。

不依赖 AstrBot，直接用系统 python3 跑：

    python3 /root/data/plugins/astrbot_plugin_jw_timetable/tools/check_config.py

0.3.3 起面板不再有「启用/关闭」开关（始终可用），本脚本只负责：
1. 确认插件能否找到自己的配置文件（含 ``jw`` 分组的 json）；
2. 列出 data/config 下每个 json 的顶层分组；
3. 检查前端资源是不是新版（区分「后端已更新」和「页面还是旧的」）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jwkb_core.const import PLUGIN_NAME, PLUGIN_VERSION  # noqa: E402
from jwkb_core.storage import config_dir, data_root, plugin_config_path  # noqa: E402

LINE = "=" * 60


def find_plugin_config() -> tuple[Path | None, dict]:
    """与插件运行时一致：标准路径 → 配置目录所有 json → 子目录，只认含 jw 对象的。"""
    candidates: list[Path] = [plugin_config_path(PLUGIN_NAME)]
    cdir = config_dir()
    if cdir.is_dir():
        candidates.extend(sorted(cdir.glob("*.json")))
        candidates.extend(sorted(cdir.rglob("*.json")))
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if isinstance(data, dict) and isinstance(data.get("jw"), dict):
            return path, data
    return None, {}


def main() -> int:
    print(LINE)
    print(f"插件目录    : {ROOT}")
    print(f"插件版本    : {PLUGIN_VERSION}")
    print(f"data 根目录 : {data_root()}")
    print(f"期望配置文件: {plugin_config_path(PLUGIN_NAME)}")
    if data_root().name != "data":
        print("提示        : 插件不在 <data>/plugins 下，路径仅供参考（开发环境）")

    print(LINE)
    cdir = config_dir()
    found_path, found_data = find_plugin_config()
    if cdir.is_dir():
        all_json = sorted(cdir.rglob("*.json"))
        print(f"配置目录    : {cdir}  共 {len(all_json)} 个 json")
        for f in all_json:
            try:
                keys = list(json.loads(f.read_text(encoding="utf-8-sig")).keys())[:6]
            except Exception:
                keys = ["<解析失败>"]
            hit = found_path and f.resolve() == found_path.resolve()
            print(f"  - {f.relative_to(cdir)}  keys={keys}{'  <== 本插件配置' if hit else ''}")
    else:
        print(f"配置目录    : {cdir}  不存在")

    print(LINE)
    if not found_path:
        print("结论        : ❌ 没找到本插件的配置文件（含 jw 分组的 json）")
        print("              → 账号密码等设置无法持久化，请检查 data/config 目录")
        print("面板状态     : 面板始终可用（0.3.3 起不再有启用/关闭开关）")
        return 1

    legacy = found_data.get("web")
    print(f"实际读取文件: {found_path}")
    print(f"顶层分组    : {sorted(found_data.keys())}")
    if isinstance(legacy, dict):
        print(f"历史遗留    : web={legacy}（0.3.3 起不再参与判定，可整段删除）")
    print("面板状态     : ✅ 面板始终可用（无启用/关闭开关）")
    return 0


def check_views() -> None:
    """检查前端资源是不是新版：区分「后端已更新」和「页面还是旧的」。"""
    print(LINE)
    print("前端资源检查：")
    checks = [
        ("views/panel/index.html", "termSelect", "学期下拉选择（0.3.2+）"),
        ("views/panel/app.js", "renderTermOptions", "学期渲染函数（0.3.2+）"),
        ("views/panel/index.html", "app.js?v=030", "资源版本号（0.3.0+）"),
        ("views/panel/index.html", 'id="banner"', "旧版关闭提示条已移除（0.3.3+）"),
    ]
    for rel, marker, desc in checks:
        p = ROOT / rel
        try:
            present = marker in p.read_text(encoding="utf-8")
        except Exception:
            present = False
        # 第 4 项是「应该不存在」的反向检查
        expect_absent = "已移除" in desc
        ok = (not present) if expect_absent else present
        ok = ok and p.exists()
        print(f"  {'✅' if ok else '❌'} {rel}  {desc}")
    print(LINE)


if __name__ == "__main__":
    code = main()
    check_views()
    raise SystemExit(code)

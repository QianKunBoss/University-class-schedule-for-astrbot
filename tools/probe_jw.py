#!/usr/bin/env python3
"""P0 探针：不依赖 AstrBot，独立验证「登录 → 抓课表 → 解析」整条链路。

用法::

    # 方式一：用配置文件（与插件配置同结构）
    python tools/probe_jw.py --config probe_config.json

    # 方式二：命令行参数（密码也可用环境变量 JW_PASSWORD 传入，避免留在 shell 历史）
    python tools/probe_jw.py \
        --base-url http://jw.example.edu.cn \
        --cas-url https://cas.example.edu.cn \
        --username 2026xxxx --password '******'

产物写在 ``probe_out/``（已加入 .gitignore）：

* ``login_page.html``  —— CAS 登录页
* ``xskb_*.html``      —— 课表原始页面（**排障关键**）
* ``terms.json``       —— 学期下拉框
* ``timetable.json``   —— 解析后的结构化课表
* ``probe.log``        —— 运行日志

退出码：0 成功 / 2 配置问题 / 3 网络问题 / 4 认证失败 / 5 解析失败 / 1 其他。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from jwkb_core.config import Config  # noqa: E402
from jwkb_core.errors import (  # noqa: E402
    AuthError,
    CaptchaRequiredError,
    ConfigError,
    JwError,
    NetworkError,
    ParseError,
)
from jwkb_core.jw_client import JwClient  # noqa: E402
from jwkb_core.parser import parse_timetable  # noqa: E402
from jwkb_core.storage import mask_account, write_json, write_text  # noqa: E402
from jwkb_core.term import resolve_week1  # noqa: E402


class ConsoleLog:
    """极简日志器：探针不依赖 AstrBot，也不使用 logging 模块。"""

    def __init__(self, logfile: Path):
        self.logfile = logfile
        self.lines: list[str] = []

    def _emit(self, tag: str, msg: str, args: tuple) -> None:
        try:
            text = msg % args if args else msg
        except Exception:
            text = f"{msg} {args}"
        line = f"{time.strftime('%H:%M:%S')} {tag} {text}"
        print(line, flush=True)
        self.lines.append(line)
        try:
            self.logfile.write_text("\n".join(self.lines), encoding="utf-8")
        except Exception:
            pass

    def info(self, msg: str, *args) -> None:
        self._emit("INFO ", msg, args)

    def warning(self, msg: str, *args) -> None:
        self._emit("WARN ", msg, args)

    def error(self, msg: str, *args) -> None:
        self._emit("ERROR", msg, args)


def build_config(args) -> Config:
    """从 --config 文件或命令行参数组装 Config。"""
    if args.config:
        raw = json.loads(Path(args.config).read_text(encoding="utf-8"))
        return Config.from_dict(raw)

    password = args.password or os.environ.get("JW_PASSWORD", "")
    raw = {
        "jw": {
            "base_url": args.base_url or "",
            "cas_url": args.cas_url or "",
            "service_url": args.service_url or "",
            "username": args.username or "",
            "password": password,
            "auth_mode": args.auth_mode,
            "ssl_verify": not args.insecure,
            "captcha_ocr": args.captcha_ocr,
            "timeout": args.timeout,
        },
        "schedule": {"term": args.term or "auto"},
    }
    return Config.from_dict(raw)


async def run(args) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    log = ConsoleLog(out_dir / "probe.log")

    try:
        cfg = build_config(args)
        cfg.jw.validate()
    except ConfigError as exc:
        log.error("配置不完整：%s", exc.user_msg)
        return 2

    log.info("目标教务：%s", cfg.jw.base)
    log.info("认证方式：%s（CAS: %s）", cfg.jw.auth_mode, cfg.jw.cas_url or "未配置")
    log.info("账号：%s", mask_account(cfg.jw.username))

    client = JwClient(cfg, out_dir, log=log)
    try:
        await client.start()

        # ---------- 1. 登录 ----------
        t0 = time.time()
        try:
            await client.ensure_login()
        except CaptchaRequiredError as exc:
            log.error("需要验证码：%s", exc.user_msg)
            return 4
        except AuthError as exc:
            log.error("认证失败：%s", exc.user_msg)
            return 4
        except NetworkError as exc:
            log.error("网络异常：%s", exc.user_msg)
            return 3
        log.info("登录完成，用时 %.1fs，认证方式=%s", time.time() - t0, client.auth_mode_used)

        # 顺手存一份主页，便于确认会话真的有效
        try:
            resp = await client.client.get(client.probe_url)
            write_text(out_dir / "xsMain.html", resp.text)
        except Exception as exc:
            log.warning("保存主页失败：%s", exc)

        # ---------- 2. 学期 ----------
        terms = await client.fetch_terms()
        if terms:
            write_json(out_dir / "terms.json", [t.to_dict() for t in terms])
            log.info("学期下拉共 %d 项：", len(terms))
            for t in terms[:8]:
                log.info("   %s %s%s", t.id, t.name, " ← 默认" if t.selected else "")
        else:
            log.warning("未解析到学期下拉框，将使用配置里的学期或自动推断")

        term_id = args.term or JwClient._default_term(terms) or ""
        if not term_id:
            log.error("无法确定学期 id，请用 --term 指定（形如 2026-2027-1）")
            return 2
        log.info("使用学期：%s", term_id)

        # ---------- 3. 抓课表 ----------
        html, snap = await client.fetch_timetable_html(term_id)
        log.info("课表页面 %d 字节，快照 %s", len(html), snap)
        if "kbcontent" not in html:
            log.warning("页面里没有 kbcontent 标记，可能不是课表页；请检查 %s", snap)

        # ---------- 4. 解析 ----------
        week1, source = resolve_week1(html, cfg.schedule)
        log.info("第 1 周周一：%s（来源 %s）", week1, source)
        courses = parse_timetable(
            html,
            max_week=cfg.schedule.max_week,
            big_period_size=cfg.schedule.big_period_size,
        )
        log.info("解析出 %d 条课程安排", len(courses))
        if not courses:
            log.error("解析结果为空，原始页面已保存在 %s", out_dir / snap)
            return 5

        write_json(
            out_dir / "timetable.json",
            {
                "term": term_id,
                "week1_monday": week1.isoformat() if week1 else "",
                "week1_source": source,
                "courses": [c.to_dict() for c in courses],
            },
        )

        # ---------- 5. 抽样打印 ----------
        names = sorted({c.name for c in courses})
        log.info("课程门数：%d", len(names))
        for name in names[:15]:
            sample = next(c for c in courses if c.name == name)
            weeks = sample.weeks
            span = f"{weeks[0]}-{weeks[-1]}周" if weeks else "未识别周次"
            log.info(
                "   %-22s %s 第%d-%d节 %s %s",
                name[:22],
                sample.weekday_cn,
                sample.start_period,
                sample.end_period,
                span,
                sample.location or "-",
            )
        if len(names) > 15:
            log.info("   …… 其余 %d 门见 timetable.json", len(names) - 15)

        log.info("✅ 全链路打通，产物目录：%s", out_dir.resolve())
        return 0

    except ParseError as exc:
        log.error("解析失败：%s", exc.user_msg)
        return 5
    except AuthError as exc:
        log.error("认证失败：%s", exc.user_msg)
        return 4
    except NetworkError as exc:
        log.error("网络异常：%s", exc.user_msg)
        return 3
    except JwError as exc:
        log.error("业务异常：%s", exc.user_msg)
        return 1
    except Exception as exc:  # 兜底，避免探针直接抛栈吓人
        log.error("未预期异常：%s: %s", exc.__class__.__name__, exc)
        if args.verbose:
            import traceback

            traceback.print_exc()
        return 1
    finally:
        await client.close()


def main() -> int:
    p = argparse.ArgumentParser(description="强智/SSO 教务课表探针")
    p.add_argument("--config", help="配置文件路径（与插件配置同结构）")
    p.add_argument("--base-url", help="教务系统根地址，如 http://jw.example.edu.cn")
    p.add_argument("--cas-url", help="统一认证根地址，如 https://cas.example.edu.cn")
    p.add_argument("--service-url", help="CAS 回调地址（留空则自动用 base_url + /jsxsd/sso.jsp）")
    p.add_argument("--username", help="学号/账号")
    p.add_argument("--password", help="密码（建议改用环境变量 JW_PASSWORD）")
    p.add_argument("--auth-mode", default="auto", choices=["auto", "sso", "direct"])
    p.add_argument("--term", help="学期 id，如 2026-2027-1")
    p.add_argument("--out", default="probe_out", help="产物目录，默认 probe_out")
    p.add_argument("--timeout", type=int, default=20)
    p.add_argument("--captcha-ocr", action="store_true", help="直连模式启用验证码 OCR")
    p.add_argument("--insecure", action="store_true", help="跳过 HTTPS 证书校验")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())

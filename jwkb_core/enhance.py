"""可选的「智能识别」：用 AstrBot 已配置的大模型补全课程名/教师/教室。

为什么需要它：强智不同学校模板差异很大，有的课表单元格里教师和教室没有
``<font title="老师">`` 这类结构化标签，只靠启发式规则会漏。这里把**原始文本**交给模型
做一次结构化抽取，补上缺失字段。识别结果写回 ``Course``，因此**推送文案和面板表格用的是
同一份数据**，不会出现「面板对、推送错」的情况。

设计约束：
* 默认关闭（``schedule.llm_enhance``），避免无谓的 token 消耗；
* 只处理缺教师或缺地点的卡片，已经完整的不会重复花钱；
* 结果按「原文哈希」缓存，同一份课表刷新多次也只调用一次；
* 任何失败都静默降级为「用规则解析的结果」，绝不让刷新流程失败。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .models import Course
from .storage import read_json, write_json

PROMPT_HEADER = """你是课程表信息抽取器。下面每行是从教务系统课表单元格里切出来的原始文本，
格式为「编号<TAB>原文」。请抽取每条的 课程名、任课教师、上课地点。

要求：
1. 只输出 JSON 数组，不要任何解释、不要 markdown 代码块；
2. 格式：[{"i":0,"name":"高等数学","teacher":"张三","location":"教101"}]
3. 抽取不到的字段填空字符串 ""，严禁编造；
4. 原文里的周次（如 1-16周）、节次（如 第1-2节）不属于任何字段，丢弃。"""

_CODE_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


def _key(raw: str) -> str:
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _build_prompt(courses: list[Course]) -> str:
    lines = [f"{i}\t{c.raw}" for i, c in enumerate(courses)]
    return PROMPT_HEADER + "\n\n" + "\n".join(lines)


def _parse_reply(text: str) -> list[dict[str, Any]]:
    """从模型回复里抠出 JSON 数组，容忍代码块与前后废话。"""
    t = _CODE_FENCE.sub("", (text or "").strip())
    start, end = t.find("["), t.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(t[start : end + 1])
    except Exception:
        return []
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


async def enhance_courses(
    courses: list[Course],
    provider,
    cache_path,
    log=None,
    batch_size: int = 30,
) -> dict:
    """补全课程字段。

    Args:
        courses: 解析结果，会**原地修改** Course 的 name/teacher/location。
        provider: AstrBot 的 LLM Provider（可为 None）。
        cache_path: 缓存文件路径。
        log: 日志器。
        batch_size: 单次请求最多送多少条。

    Returns:
        统计字典：{"applied": n, "cached": n, "failed": n, "skipped": n}
    """
    stats = {"applied": 0, "cached": 0, "failed": 0, "skipped": 0}
    if provider is None:
        stats["skipped"] = len(courses)
        return stats

    cache: dict[str, dict] = read_json(cache_path, {}) or {}
    dirty = False

    # 先用缓存把能补的直接补上
    pending: list[Course] = []
    for c in courses:
        if c.teacher and c.location:
            continue
        if not c.raw:
            continue
        hit = cache.get(_key(c.raw))
        if hit:
            _apply(c, hit)
            stats["cached"] += 1
        else:
            pending.append(c)

    if not pending:
        if stats["cached"]:
            _info(log, "[kb] 智能识别：命中缓存补全 %d 条", stats["cached"])
        return stats

    for i in range(0, len(pending), batch_size):
        chunk = pending[i : i + batch_size]
        try:
            reply = await provider.text_chat(
                prompt=_build_prompt(chunk),
                context=[],
                system_prompt="你只输出合法 JSON，不做解释。",
            )
            text = getattr(reply, "completion_text", "") or ""
        except Exception as exc:
            stats["failed"] += len(chunk)
            _warn(log, "[kb] 智能识别调用失败（跳过这一批）：%s", exc)
            continue

        rows = _parse_reply(text)
        if not rows:
            stats["failed"] += len(chunk)
            _warn(log, "[kb] 智能识别返回内容无法解析，已跳过这一批")
            continue

        for row in rows:
            try:
                idx = int(row.get("i", -1))
            except (TypeError, ValueError):
                continue
            if not (0 <= idx < len(chunk)):
                continue
            course = chunk[idx]
            data = {
                "name": _clean(row.get("name")),
                "teacher": _clean(row.get("teacher")),
                "location": _clean(row.get("location")),
            }
            cache[_key(course.raw)] = data
            dirty = True
            if _apply(course, data):
                stats["applied"] += 1

    if dirty:
        try:
            write_json(cache_path, cache)
        except Exception as exc:
            _warn(log, "[kb] 智能识别缓存写入失败：%s", exc)

    _info(
        log,
        "[kb] 智能识别完成：补全 %d 条（缓存 %d、失败 %d）",
        stats["applied"],
        stats["cached"],
        stats["failed"],
    )
    return stats


def _apply(course: Course, data: dict) -> bool:
    """把模型给出的字段写回；只填空缺，不覆盖已经解析出来的可信值。"""
    changed = False
    name = _clean(data.get("name"))
    teacher = _clean(data.get("teacher"))
    location = _clean(data.get("location"))
    if not course.teacher and teacher:
        course.teacher = teacher
        changed = True
    if not course.location and location:
        course.location = location
        changed = True
    # 课程名只有明显是「周次/教师」误判时才替换
    if name and _looks_wrong_name(course.name) and name != course.name:
        course.name = name
        changed = True
    return changed


def _looks_wrong_name(name: str) -> bool:
    if not name:
        return True
    if re.fullmatch(r"[\d\s\-~—–周节第()（）]+", name):
        return True
    return len(name) > 24


def _clean(value) -> str:
    if value is None:
        return ""
    text = str(value).strip().strip("：:，,、")
    if text.lower() in {"null", "none", "无", "未知", "-", "—"}:
        return ""
    return text[:60]


def _info(log, msg: str, *args) -> None:
    if log:
        log.info(msg, *args)


def _warn(log, msg: str, *args) -> None:
    if log:
        log.warning(msg, *args)

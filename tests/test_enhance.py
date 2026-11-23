"""AI 补全识别测试。

重点验证三件事：
1. 模型返回带 markdown 代码块 / 前后废话时仍能抠出 JSON；
2. 只补空缺字段，不覆盖规则解析出的可信值；
3. 第二次调用命中缓存，不会重复花钱。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jwkb_core.enhance import _apply, _parse_reply, enhance_courses  # noqa: E402
from jwkb_core.models import Course  # noqa: E402


class FakeResponse:
    def __init__(self, text: str):
        self.completion_text = text


class FakeProvider:
    """记录调用次数，返回固定 JSON。"""

    def __init__(self, text: str):
        self.text = text
        self.calls = 0

    async def text_chat(self, prompt, context=None, system_prompt=None):
        self.calls += 1
        return FakeResponse(self.text)


def test_parse_reply_plain_json():
    rows = _parse_reply('[{"i":0,"name":"高数","teacher":"张三","location":"101"}]')
    assert rows[0]["name"] == "高数"


def test_parse_reply_with_code_fence_and_chatter():
    text = '好的，结果如下：\n```json\n[{"i":1,"name":"英语"}]\n```\n希望有帮助'
    rows = _parse_reply(text)
    assert len(rows) == 1 and rows[0]["i"] == 1


def test_parse_reply_garbage_returns_empty():
    assert _parse_reply("抱歉我无法回答") == []


def test_apply_only_fills_missing_fields():
    c = Course(name="高等数学", weekday=1, start_period=1, end_period=2, teacher="张三", location="")
    changed = _apply(c, {"name": "别的名字", "teacher": "李四", "location": "教101"})
    assert changed is True
    assert c.teacher == "张三"  # 已有值不被覆盖
    assert c.location == "教101"  # 空缺被补上
    assert c.name == "高等数学"


def test_apply_ignores_placeholder_values():
    c = Course(name="体育", weekday=2, start_period=1, end_period=2)
    _apply(c, {"name": "无", "teacher": "null", "location": "-"})
    assert c.teacher == "" and c.location == ""


def test_enhance_fills_and_caches(tmp_path):
    courses = [
        Course(name="高等数学", weekday=1, start_period=1, end_period=2, raw="高等数学 张三 1-16周"),
        Course(name="体育", weekday=2, start_period=3, end_period=4, raw="体育 王五 1-16周"),
    ]
    provider = FakeProvider(
        '[{"i":0,"name":"高等数学","teacher":"张三","location":"教101"},'
        '{"i":1,"name":"体育","teacher":"王五","location":"操场"}]'
    )
    cache = tmp_path / "cache.json"

    stats = asyncio.run(enhance_courses(courses, provider, cache))
    assert stats["applied"] == 2
    assert provider.calls == 1
    assert courses[0].location == "教101"
    assert courses[1].teacher == "王五"
    assert cache.exists()

    # 第二次：字段已完整，不再调用模型
    provider2 = FakeProvider("[]")
    stats2 = asyncio.run(enhance_courses(courses, provider2, cache))
    assert provider2.calls == 0
    assert stats2["applied"] == 0


def test_enhance_without_provider_is_noop(tmp_path):
    courses = [Course(name="A", weekday=1, start_period=1, end_period=2, raw="A")]
    stats = asyncio.run(enhance_courses(courses, None, tmp_path / "c.json"))
    assert stats["skipped"] == 1
    assert courses[0].teacher == ""


def test_enhance_survives_provider_error(tmp_path):
    class Broken:
        async def text_chat(self, *a, **kw):
            raise RuntimeError("额度用完了")

    courses = [Course(name="A", weekday=1, start_period=1, end_period=2, raw="A")]
    stats = asyncio.run(enhance_courses(courses, Broken(), tmp_path / "c.json"))
    assert stats["failed"] == 1  # 失败只统计，不抛出

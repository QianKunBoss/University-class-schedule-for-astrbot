"""可选验证码识别。

ddddocr 依赖 onnxruntime + opencv，体积大且在精简版 Docker 镜像里常缺 libGL，
所以**不放进必装依赖**：只有用户在配置里打开 ``captcha_ocr`` 且确实遇到验证码时
才会尝试导入，导入失败给出可操作的提示，而不是让整个插件装挂。
"""

from __future__ import annotations

import asyncio

_ocr = None
_init_error = ""


def available() -> tuple[bool, str]:
    """探测 OCR 是否可用，返回 (可用, 原因)。结果会缓存，避免重复初始化。"""
    global _ocr, _init_error
    if _ocr is not None:
        return True, ""
    if _init_error:
        return False, _init_error
    try:
        import ddddocr  # type: ignore

        _ocr = ddddocr.DdddOcr(show_ad=False)
        return True, ""
    except Exception as exc:  # ImportError 或缺少 libGL 等运行期错误
        _init_error = str(exc)
        return False, _init_error


def recognize_sync(image: bytes) -> str:
    """同步识别（CPU 密集，调用方需放到线程里跑）。"""
    ok, reason = available()
    if not ok:
        raise RuntimeError(f"验证码识别不可用：{reason}")
    return (_ocr.classification(image) or "").strip()


async def recognize(image: bytes) -> str:
    """异步封装：把同步 OCR 丢到线程池，避免阻塞事件循环。"""
    return await asyncio.to_thread(recognize_sync, image)


def install_hint() -> str:
    """给用户的安装提示（Docker 环境需要额外装系统库）。"""
    return (
        "如需自动识别验证码，请执行 pip install ddddocr；"
        "Docker 部署还需 apt-get install -y libgl1 libglib2.0-0"
    )

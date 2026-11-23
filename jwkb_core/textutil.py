"""HTML/文本工具：编码判定与标签剥离。

踩过的坑（务必保留这段注释，避免以后改回去）：

* 强智教务页面编码不统一（UTF-8 / GBK 混用），必须拿原始 bytes 自己解，
  直接读 ``resp.text`` 会整页乱码；
* CAS 登录页把**整个登录表单放在 ``<script type="text/html" id="login_template">``
  里**，错误提示 ``<span id="errormsghide">用户名或密码错误</span>`` 也在这个模板中。
  如果按惯例把 ``<script>...</script>`` 全部删掉再提文字，错误信息会一起消失，
  最终只能给出「未知错误」。所以这里对 ``type="text/html"`` 的模板要保留。
"""

from __future__ import annotations

import re

CHARSET_RE = re.compile(rb"""charset=["']?\s*([\w-]{2,20})""", re.I)
# 只匹配真正的脚本块，跳过 text/html 模板
_SCRIPT_RE = re.compile(r"""(?is)<script(?![^>]*type=["']text/html)[\s\S]*?</script>""")
_STYLE_RE = re.compile(r"(?is)<style[\s\S]*?</style>")
_TAG_RE = re.compile(r"(?s)<[^>]+>")

# 各校 CAS 的错误提示容器名，命中即取其中的文字
ERROR_BLOCK_RE = re.compile(
    r"""id=["'](?:errormsghide|errormsg|errorMsg|loginError|error_msg)["'][^>]*>([\s\S]{0,300}?)</""",
    re.I,
)


def decode_bytes(raw: bytes, content_type: str = "") -> str:
    """按「响应头 → meta → utf-8 → gb18030」顺序解码。"""
    if not raw:
        return ""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace")

    candidates: list[str] = []
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    if m:
        candidates.append(m.group(1).lower())
    m2 = CHARSET_RE.search(raw[:4096])
    if m2:
        candidates.append(m2.group(1).decode("ascii", "ignore").lower())
    candidates.extend(["utf-8", "gb18030"])

    for enc in candidates:
        if enc in {"gb2312", "gbk"}:
            enc = "gb18030"  # 超集，容错更好
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def strip_tags(html: str) -> str:
    """剥离标签取纯文字，**保留 ``text/html`` 模板内容**。"""
    text = _SCRIPT_RE.sub(" ", html or "")
    text = _STYLE_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_error(html: str) -> str:
    """提取页面里显式的错误提示文字（没有则返回空串）。"""
    m = ERROR_BLOCK_RE.search(html or "")
    if not m:
        return ""
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", m.group(1))).strip()

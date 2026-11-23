#!/usr/bin/env python3
"""生成插件 logo.png（512×512 方图）。

只依赖 Pillow，画法用 4 倍超采样再缩放，边缘更干净。
需要重新生成时执行：

    python tools/make_logo.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 512
SS = 4  # 超采样倍数
W = SIZE * SS

BG = (44, 90, 160, 255)  # 钢蓝
BG_DARK = (31, 64, 118, 255)
CARD = (255, 255, 255, 255)
LINE = (198, 216, 238, 255)
ACCENT = (242, 166, 59, 255)  # 暖橙


def rounded(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def main() -> None:
    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 底板（纯色，不用渐变）
    rounded(d, (0, 0, W, W), int(W * 0.22), BG)

    # 日历卡纸
    left, top, right, bottom = int(W * 0.16), int(W * 0.23), int(W * 0.84), int(W * 0.80)
    radius = int(W * 0.055)
    rounded(d, (left, top, right, bottom), radius, CARD)

    # 顶部深色标题条（下缘平直，与卡纸圆角对齐）
    header_h = int(W * 0.115)
    rounded(d, (left, top, right, top + header_h + radius), radius, BG_DARK)
    d.rectangle((left, top + header_h, right, top + header_h + radius), fill=BG_DARK)

    # 两个挂环
    for cx in (int(W * 0.34), int(W * 0.66)):
        d.rounded_rectangle(
            (cx - int(W * 0.022), int(W * 0.175), cx + int(W * 0.022), int(W * 0.255)),
            radius=int(W * 0.022),
            fill=BG_DARK,
        )

    # 表格网格：4 列 × 3 行，线端点都落在网格区内，不会「悬空」
    grid_left = left + int(W * 0.055)
    grid_right = right - int(W * 0.055)
    grid_top = top + header_h + int(W * 0.045)
    grid_bottom = bottom - int(W * 0.055)
    cols, rows = 4, 3
    cw = (grid_right - grid_left) / cols
    ch = (grid_bottom - grid_top) / rows
    lw = max(2, int(W * 0.007))

    for i in range(1, cols):
        x = grid_left + cw * i
        d.line((x, grid_top, x, grid_bottom), fill=LINE, width=lw)
    for j in range(1, rows):
        y = grid_top + ch * j
        d.line((grid_left, y, grid_right, y), fill=LINE, width=lw)

    # 高亮一格：代表「即将开始的这节课」
    pad = int(W * 0.011)
    x0 = grid_left + cw * 2 + pad
    y0 = grid_top + ch * 1 + pad
    rounded(
        d,
        (x0, y0, grid_left + cw * 3 - pad, grid_top + ch * 2 - pad),
        int(W * 0.022),
        ACCENT,
    )

    # 另一格打对勾：代表「已提醒」
    check_w = max(4, int(W * 0.013))
    bx, by = grid_left + cw * 0.28, grid_top + ch * 2.45
    d.line((bx, by, bx + cw * 0.16, by + ch * 0.26), fill=BG, width=check_w)
    d.line(
        (bx + cw * 0.16, by + ch * 0.26, bx + cw * 0.46, by - ch * 0.26),
        fill=BG,
        width=check_w,
    )

    out = img.resize((SIZE, SIZE), Image.LANCZOS)
    path = Path(__file__).resolve().parent.parent / "logo.png"
    out.save(path, "PNG", optimize=True)
    print(f"已生成 {path}（{path.stat().st_size} 字节）")


if __name__ == "__main__":
    main()

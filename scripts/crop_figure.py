#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从整页幻灯片 PNG 中裁出图例区域，并做墨迹边界校验；不合格自动回退整页。

为什么需要这个脚本：PPT 里的组合图形、带校徽角标的页面，肉眼看边界和算法算边界
经常差几个像素，裁多裁少都会把图切残。所以这里把"裁"和"验"绑在一起——
裁完立刻检查四条边是不是干净背景，不干净就扩边重试，重试仍不过就退回整页。

用法：
  python crop_figure.py p012.png -o ../notes/img/x-图1.1.png
  python crop_figure.py p012.png -o out.png --box 120,90,860,610      # 手工指定框
  python crop_figure.py p012.png -o out.png --erase 0,0,200,60        # 先抹掉角标
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def dominant_border_color(img: Image.Image) -> tuple[int, int, int]:
    """取四条边上出现最多的颜色作为背景色（课件背景常是 242,242,242 而非纯白）。"""
    w, h = img.size
    strips = [
        img.crop((0, 0, w, 2)),
        img.crop((0, h - 2, w, h)),
        img.crop((0, 0, 2, h)),
        img.crop((w - 2, 0, w, h)),
    ]
    counts: dict[tuple[int, int, int], int] = {}
    for s in strips:
        for cnt, col in (s.convert("RGB").getcolors(maxcolors=1 << 20) or []):
            counts[col] = counts.get(col, 0) + cnt
    if not counts:
        return (255, 255, 255)
    return max(counts.items(), key=lambda kv: kv[1])[0]


def ink_mask(img: Image.Image, bg: tuple[int, int, int], tol: int) -> Image.Image:
    """与背景色的差异超过 tol 的像素视为墨迹。"""
    flat = Image.new("RGB", img.size, bg)
    diff = ImageChops.difference(img.convert("RGB"), flat).convert("L")
    return diff.point(lambda v: 255 if v > tol else 0)


def ink_ratio_of_band(mask: Image.Image, band: int) -> float:
    """四条边带的墨迹占比，用来判断裁剪有没有切到内容。"""
    w, h = mask.size
    if w <= 2 * band or h <= 2 * band:
        return 1.0
    boxes = [
        (0, 0, w, band),
        (0, h - band, w, h),
        (0, 0, band, h),
        (w - band, 0, w, h),
    ]
    total_ink = 0
    total_px = 0
    for b in boxes:
        sub = mask.crop(b)
        hist = sub.histogram()
        total_ink += hist[255]
        total_px += sub.size[0] * sub.size[1]
    return total_ink / max(1, total_px)


def expand(box: tuple[int, int, int, int], scale: float, w: int, h: int):
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    hw, hh = (x1 - x0) * scale / 2, (y1 - y0) * scale / 2
    return (
        max(0, int(cx - hw)),
        max(0, int(cy - hh)),
        min(w, int(cx + hw)),
        min(h, int(cy + hh)),
    )


def crop_one(
    page_png: Path,
    out_png: Path,
    mode: str = "auto",
    box: tuple[int, int, int, int] | None = None,
    erase: list[tuple[int, int, int, int]] | None = None,
    pad: int = 18,
    tol: int = 18,
    border_band: int = 3,
    max_area_ratio: float = 0.80,
    min_ink_ratio: float = 0.002,
) -> dict:
    img = Image.open(page_png).convert("RGB")
    W, H = img.size

    work = img.copy()
    if erase:
        d = ImageDraw.Draw(work)
        bg0 = dominant_border_color(img)
        for e in erase:
            d.rectangle(e, fill=bg0)

    bg = dominant_border_color(work)
    mask = ink_mask(work, bg, tol)
    result = {
        "page_png": str(page_png),
        "out": str(out_png),
        "mode": mode,
        "bg": list(bg),
        "fallback": False,
        "reason": "",
        "bbox": None,
    }

    if mode == "box" and box:
        # 几何框来自 PPT 形状坐标，是权威信息：直接裁，不做边框校验。
        # （图的边缘本身就是内容，按"边带必须干净"去验必然失败，验了反而会错误地扩边）
        x0 = max(0, box[0] - pad)
        y0 = max(0, box[1] - pad)
        x1 = min(W, box[2] + pad)
        y1 = min(H, box[3] + pad)
        if x1 - x0 < 20 or y1 - y0 < 20:
            result.update(fallback=True, reason="几何框过小（%dx%d）" % (x1 - x0, y1 - y0))
            out_png.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(page_png, out_png)
            return result
        sub = img.crop((x0, y0, x1, y1))
        out_png.parent.mkdir(parents=True, exist_ok=True)
        sub.save(out_png, "PNG", optimize=True)
        result.update(bbox=[x0, y0, x1, y1], border_clean=None,
                      note="几何框，未做边框校验", size=[sub.size[0], sub.size[1]])
        return result

    full = mask.getbbox()
    if full is None:
        result.update(fallback=True, reason="整页无墨迹（空白页）")
        out_png.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(page_png, out_png)
        return result

    area_ratio = (full[2] - full[0]) * (full[3] - full[1]) / float(W * H)
    if area_ratio > max_area_ratio:
        result.update(fallback=True, reason="内容覆盖整页 %.0f%%，裁剪无意义" % (area_ratio * 100))
        out_png.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(page_png, out_png)
        return result

    candidates = [expand(full, s, W, H) for s in (1.0, 1.06, 1.14, 1.25)]

    last_ratio = 1.0
    for cand in candidates:
        x0, y0, x1, y1 = cand
        x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
        x1, y1 = min(W, x1 + pad), min(H, y1 + pad)
        if x1 - x0 < 40 or y1 - y0 < 40:
            continue
        sub = img.crop((x0, y0, x1, y1))
        sub_bg = dominant_border_color(sub)
        sub_mask = ink_mask(sub, sub_bg, tol)
        ratio = ink_ratio_of_band(sub_mask, border_band)
        last_ratio = ratio
        if ratio <= 0.0005:
            hist = sub_mask.histogram()
            ink_share = hist[255] / float(sub.size[0] * sub.size[1])
            if ink_share < min_ink_ratio:
                continue
            out_png.parent.mkdir(parents=True, exist_ok=True)
            sub.save(out_png, "PNG", optimize=True)
            result.update(
                bbox=[x0, y0, x1, y1],
                border_clean=True,
                border_ink_ratio=round(ratio, 6),
                ink_share=round(ink_share, 5),
                size=[sub.size[0], sub.size[1]],
            )
            return result

    result.update(
        fallback=True,
        reason="四轮扩边后边框仍含内容（边带墨迹 %.2f%%），回退整页" % (last_ratio * 100),
    )
    out_png.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(page_png, out_png)
    return result


def _rect(s: str) -> tuple[int, int, int, int]:
    parts = [int(v) for v in s.replace(" ", "").split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("应为 x0,y0,x1,y1")
    return tuple(parts)  # type: ignore[return-value]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("page_png")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--box", type=_rect, default=None, help="手工裁剪框 x0,y0,x1,y1")
    ap.add_argument("--erase", type=_rect, action="append", default=None,
                    help="计算前抹掉的区域（如校徽、页脚），可重复")
    ap.add_argument("--pad", type=int, default=18)
    ap.add_argument("--tol", type=int, default=18, help="墨迹判定阈值，背景复杂时调大")
    ap.add_argument("--json", action="store_true", help="只输出 JSON 结果")
    args = ap.parse_args()

    page = Path(args.page_png).resolve()
    out = Path(args.out).resolve()
    if not page.exists():
        print("ERROR 找不到 %s" % page, file=sys.stderr)
        return 2

    res = crop_one(
        page,
        out,
        mode="box" if args.box else "auto",
        box=args.box,
        erase=args.erase,
        pad=args.pad,
        tol=args.tol,
    )

    if args.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        flag = "FALLBACK-整页" if res["fallback"] else "OK-已裁剪"
        print("%s  %s  %s" % (flag, Path(out).name, res.get("reason", "")))
        if res.get("bbox"):
            print("      bbox=%s size=%s" % (res["bbox"], res.get("size")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

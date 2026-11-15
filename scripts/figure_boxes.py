#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""定位某一页上「图」的真实位置，输出像素裁剪框。

为什么不能靠墨迹边界：一页图文页里标题和正文也是墨迹，自动框会把它们一起框进去。
PPT 内部保存了每个形状的精确坐标，直接换算成像素就得到准确的图框——这是确定性信息，
比任何图像算法都可靠。墨迹算法只在拿不到形状坐标时（少数 PDF）才用。

用法：
  python figure_boxes.py 课件.pptx --page 4 --width 1600
  python figure_boxes.py 课件.pdf  --page 4 --dpi 150
输出 JSON，candidates 按面积从大到小排列。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def _bbox_of_shape(sh) -> tuple[int, int, int, int] | None:
    try:
        if sh.left is None or sh.top is None or sh.width is None or sh.height is None:
            return None
        x0, y0 = int(sh.left), int(sh.top)
        return (x0, y0, x0 + int(sh.width), y0 + int(sh.height))
    except Exception:
        return None


def boxes_pptx(path: Path, page: int, width_px: int) -> dict:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    prs = Presentation(str(path))
    slides = list(prs.slides)
    if not (1 <= page <= len(slides)):
        raise SystemExit("页码超出范围：1-%d" % len(slides))
    slide = slides[page - 1]
    sw = int(prs.slide_width)
    sh_ = int(prs.slide_height)
    scale = width_px / float(sw)
    height_px = int(round(sh_ * scale))

    title_id = None
    try:
        if slide.shapes.title is not None:
            title_id = slide.shapes.title.shape_id
    except Exception:
        pass

    fig_kinds = {
        MSO_SHAPE_TYPE.PICTURE: "picture",
        MSO_SHAPE_TYPE.CHART: "chart",
        MSO_SHAPE_TYPE.TABLE: "table",
        MSO_SHAPE_TYPE.GROUP: "group",
        MSO_SHAPE_TYPE.MEDIA: "media",
        MSO_SHAPE_TYPE.EMBEDDED_OLE_OBJECT: "ole",
    }

    cands = []
    for sh in slide.shapes:
        if sh.shape_id == title_id:
            continue
        bbox = _bbox_of_shape(sh)
        if bbox is None:
            continue
        kind = None
        try:
            kind = fig_kinds.get(sh.shape_type)
        except Exception:
            kind = None
        if kind is None:
            try:
                if getattr(sh, "has_table", False) and sh.has_table:
                    kind = "table"
            except Exception:
                pass
        has_text = False
        try:
            has_text = bool(sh.has_text_frame and sh.text_frame.text.strip())
        except Exception:
            pass
        if kind is None:
            if has_text or (sh.is_placeholder if hasattr(sh, "is_placeholder") else False):
                continue
            kind = "shape"
        if kind in ("table",) and not has_text:
            pass
        x0, y0, x1, y1 = bbox
        px = [
            int(round(x0 * scale)), int(round(y0 * scale)),
            int(round(x1 * scale)), int(round(y1 * scale)),
        ]
        area = (x1 - x0) * (y1 - y0) / float(sw * sh_)
        if area < 0.01:
            continue
        cands.append({
            "kind": kind,
            "name": sh.name,
            "text": (sh.text_frame.text.strip()[:60] if has_text else ""),
            "bbox": px,
            "size": [px[2] - px[0], px[3] - px[1]],
            "area_ratio": round(area, 3),
        })

    cands.sort(key=lambda c: c["area_ratio"], reverse=True)
    return {
        "source": str(path), "kind": "pptx", "page": page,
        "render_px": [width_px, height_px], "candidates": cands,
    }


def boxes_pdf(path: Path, page: int, dpi: int) -> dict:
    import pymupdf

    doc = pymupdf.open(str(path))
    if not (1 <= page <= len(doc)):
        raise SystemExit("页码超出范围：1-%d" % len(doc))
    pg = doc[page - 1]
    s = dpi / 72.0
    cands = []

    for i, info in enumerate(pg.get_image_info(xrefs=True) or []):
        r = info.get("bbox")
        if not r:
            continue
        px = [int(r[0] * s), int(r[1] * s), int(r[2] * s), int(r[3] * s)]
        cands.append({"kind": "image", "name": "img%d" % i, "text": "",
                      "bbox": px, "size": [px[2] - px[0], px[3] - px[1]],
                      "area_ratio": round((r[2] - r[0]) * (r[3] - r[1]) /
                                          (pg.rect.width * pg.rect.height), 3)})

    drawings = pg.get_drawings() or []
    if drawings:
        xs, ys = [], []
        for d in drawings:
            r = d.get("rect")
            if not r:
                continue
            if r.width * r.height > pg.rect.width * pg.rect.height * 0.92:
                continue  # 整页背景块，排除
            xs += [r.x0, r.x1]
            ys += [r.y0, r.y1]
        if xs:
            px = [int(min(xs) * s), int(min(ys) * s), int(max(xs) * s), int(max(ys) * s)]
            cands.append({"kind": "vector", "name": "drawings", "text": "",
                          "bbox": px, "size": [px[2] - px[0], px[3] - px[1]],
                          "area_ratio": round(((px[2] - px[0]) * (px[3] - px[1])) /
                                              (pg.rect.width * pg.rect.height * s * s), 3)})

    cands.sort(key=lambda c: c["area_ratio"], reverse=True)
    return {
        "source": str(path), "kind": "pdf", "page": page,
        "render_px": [int(pg.rect.width * s), int(pg.rect.height * s)],
        "candidates": cands,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--page", type=int, required=True)
    ap.add_argument("--width", type=int, default=1600, help="与 render_pages.py 的 --width 一致")
    ap.add_argument("--dpi", type=int, default=150, help="与 render_pages.py 的 --dpi 一致")
    args = ap.parse_args()

    src = Path(args.source).expanduser().resolve()
    if not src.exists():
        print("ERROR 找不到 %s" % src, file=sys.stderr)
        return 2

    if src.suffix.lower() == ".pdf":
        res = boxes_pdf(src, args.page, args.dpi)
    elif src.suffix.lower() in (".pptx", ".pptm", ".ppt"):
        if src.suffix.lower() == ".ppt":
            print("ERROR .ppt 请先用 extract.py 转出的 .converted.pptx 定位", file=sys.stderr)
            return 2
        res = boxes_pptx(src, args.page, args.width)
    else:
        print("ERROR 只支持 .pptx/.pdf", file=sys.stderr)
        return 2

    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

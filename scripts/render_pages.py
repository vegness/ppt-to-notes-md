#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把每页幻灯片渲染成 PNG，供后续看图、裁图。

.pptx / .ppt -> 本机 PowerPoint COM 导出（保真度最高，母版/字体/形状都与放映一致）
.pdf         -> pymupdf 渲染

用法：
  python render_pages.py <课件路径> -o _ppt2notes_tmp/pages [--width 1920] [--dpi 150]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def render_via_powerpoint(src: Path, outdir: Path, width: int) -> int:
    import win32com.client as win32

    app = win32.Dispatch("PowerPoint.Application")
    pres = app.Presentations.Open(
        str(src.resolve()), ReadOnly=True, Untitled=False, WithWindow=False
    )
    try:
        sw = float(pres.PageSetup.SlideWidth)   # 单位：点
        sh = float(pres.PageSetup.SlideHeight)
        height = max(1, int(round(width * sh / sw)))
        count = int(pres.Slides.Count)
        for i in range(1, count + 1):
            out = outdir / ("p%03d.png" % i)
            pres.Slides(i).Export(str(out.resolve()), "PNG", width, height)
        return count
    finally:
        pres.Close()


def render_via_pymupdf(src: Path, outdir: Path, dpi: int) -> int:
    import pymupdf

    doc = pymupdf.open(str(src))
    for i, page in enumerate(doc, start=1):
        pix = page.get_pixmap(dpi=dpi)
        pix.save(str(outdir / ("p%03d.png" % i)))
    return len(doc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("-o", "--outdir", required=True)
    ap.add_argument("--width", type=int, default=1920, help="幻灯片导出宽度（像素）")
    ap.add_argument("--dpi", type=int, default=150, help="PDF 渲染 DPI")
    args = ap.parse_args()

    src = Path(args.source).expanduser().resolve()
    if not src.exists():
        print("ERROR 找不到文件：%s" % src, file=sys.stderr)
        return 2

    outdir = Path(args.outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    suffix = src.suffix.lower()
    if suffix in (".pptx", ".pptm", ".ppt"):
        try:
            n = render_via_powerpoint(src, outdir, args.width)
            how = "PowerPoint COM"
        except Exception as exc:
            print("ERROR PowerPoint 导出失败：%s" % exc, file=sys.stderr)
            print("      请确认本机装了 PowerPoint，且没有弹窗占用（如\"恢复未保存\"对话框）。",
                  file=sys.stderr)
            return 3
    elif suffix == ".pdf":
        n = render_via_pymupdf(src, outdir, args.dpi)
        how = "pymupdf"
    else:
        print("ERROR 只支持 .pptx/.ppt/.pdf", file=sys.stderr)
        return 2

    missing = [i for i in range(1, n + 1) if not (outdir / ("p%03d.png" % i)).exists()]
    print("OK  %d 页已渲染到 %s（%s）" % (n, outdir, how))
    if missing:
        print("WARN 缺失页面：%s" % missing[:10], file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""课件结构化抽取：.pptx / .ppt / .pdf  ->  slides.json

输出每页的：标题、正文块（含缩进层级）、表格、图片数量、演讲者备注、字符数。
后续写笔记与长度校验都以这份 JSON 为准，避免模型凭记忆复述课件内容。

用法：
  python extract.py <课件路径> -o slides.json [--workdir _ppt2notes_tmp]
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


# ---------------------------------------------------------------- .ppt 预处理
def convert_ppt_to_pptx(src: Path, workdir: Path) -> Path:
    """把 97-2003 的 .ppt 用本机 PowerPoint 另存为 .pptx（python-pptx 不支持 .ppt）。"""
    import win32com.client as win32

    workdir.mkdir(parents=True, exist_ok=True)
    dst = workdir / (src.stem + ".converted.pptx")
    if dst.exists() and dst.stat().st_size > 0:
        return dst

    app = win32.Dispatch("PowerPoint.Application")
    pres = app.Presentations.Open(
        str(src.resolve()), ReadOnly=True, Untitled=False, WithWindow=False
    )
    try:
        pres.SaveAs(str(dst.resolve()), 24)  # 24 = ppSaveAsOpenXMLPresentation
    finally:
        pres.Close()
    if not dst.exists():
        raise RuntimeError("PowerPoint 另存 .pptx 失败：%s" % dst)
    return dst


# -------------------------------------------------------------------- .pptx
def extract_pptx(path: Path) -> dict:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    prs = Presentation(str(path))
    slides: list[dict] = []
    total_chars = 0

    for idx, slide in enumerate(prs.slides, start=1):
        title = ""
        try:
            if slide.shapes.title is not None:
                title = (slide.shapes.title.text or "").strip()
        except Exception:
            title = ""

        blocks: list[dict] = []
        tables: list[list[list[str]]] = []
        pictures: list[dict] = []

        for sh in slide.shapes:
            try:
                if sh.has_text_frame:
                    for p in sh.text_frame.paragraphs:
                        text = "".join(r.text for r in p.runs).strip()
                        if text:
                            blocks.append({"level": int(p.level or 0), "text": text})
            except Exception:
                pass
            try:
                if getattr(sh, "has_table", False) and sh.has_table:
                    rows = [[c.text.strip() for c in r.cells] for r in sh.table.rows]
                    tables.append(rows)
            except Exception:
                pass
            try:
                if sh.shape_type == MSO_SHAPE_TYPE.PICTURE:
                    pictures.append(
                        {"name": sh.name, "w": int(sh.width or 0), "h": int(sh.height or 0)}
                    )
            except Exception:
                pass

        notes = ""
        try:
            if slide.has_notes_slide:
                notes = (slide.notes_slide.notes_text_frame.text or "").strip()
        except Exception:
            notes = ""

        chars = sum(len(b["text"]) for b in blocks)
        chars += sum(len(c) for t in tables for r in t for c in r)
        total_chars += chars

        slides.append(
            {
                "page": idx,
                "title": title,
                "blocks": blocks,
                "tables": tables,
                "pictures": pictures,
                "notes": notes,
                "char_count": chars,
            }
        )

    return {
        "source": str(path),
        "kind": "pptx",
        "pages": len(slides),
        "slide_w_emu": int(prs.slide_width or 0),
        "slide_h_emu": int(prs.slide_height or 0),
        "total_chars": total_chars,
        "slides": slides,
    }


# --------------------------------------------------------------------- .pdf
def extract_pdf(path: Path) -> dict:
    import pymupdf

    doc = pymupdf.open(str(path))
    slides: list[dict] = []
    total_chars = 0

    for pno, page in enumerate(doc, start=1):
        data = page.get_text("dict")
        lines: list[dict] = []
        for blk in data.get("blocks", []):
            for ln in blk.get("lines", []):
                text = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
                if not text:
                    continue
                size = max((float(s.get("size", 0)) for s in ln.get("spans", [])), default=0.0)
                lines.append({"text": text, "size": round(size, 1)})

        sizes = sorted(l["size"] for l in lines if l["size"] > 0)
        median = sizes[len(sizes) // 2] if sizes else 0.0

        blocks: list[dict] = []
        title = ""
        if lines:
            title = max(lines, key=lambda l: l["size"])["text"]
        for ln in lines:
            s = ln["size"]
            if median and s >= median * 1.35:
                level = 0
            elif median and s >= median * 1.10:
                level = 1
            else:
                level = 2
            blocks.append({"level": level, "text": ln["text"]})

        chars = sum(len(b["text"]) for b in blocks)
        total_chars += chars

        slides.append(
            {
                "page": pno,
                "title": title,
                "blocks": blocks,
                "tables": [],
                "pictures": [{"name": f"img{i}"} for i in range(len(page.get_images()))],
                "notes": "",
                "char_count": chars,
            }
        )

    return {
        "source": str(path),
        "kind": "pdf",
        "pages": len(slides),
        "slide_w_emu": 0,
        "slide_h_emu": 0,
        "total_chars": total_chars,
        "slides": slides,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--workdir", default="")
    args = ap.parse_args()

    src = Path(args.source).expanduser().resolve()
    if not src.exists():
        print("ERROR 找不到文件：%s" % src, file=sys.stderr)
        return 2

    workdir = Path(args.workdir).resolve() if args.workdir else src.parent / "_ppt2notes_tmp"
    suffix = src.suffix.lower()

    if suffix == ".pdf":
        result = extract_pdf(src)
    elif suffix in (".pptx", ".pptm"):
        result = extract_pptx(src)
    elif suffix == ".ppt":
        result = extract_pptx(convert_ppt_to_pptx(src, workdir))
        result["source"] = str(src)
    else:
        print("ERROR 只支持 .pptx/.ppt/.pdf，收到 %s" % suffix, file=sys.stderr)
        return 2

    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    no_text = [s["page"] for s in result["slides"] if s["char_count"] < 5]
    print("OK  %s -> %s" % (out.name, out))
    print("    页数=%d  总字符=%d  无文本页=%s"
          % (result["pages"], result["total_chars"], no_text[:20] or "无"))
    if no_text and len(no_text) == result["pages"]:
        print("WARN 全部页面无文本层，疑似扫描件/纯图片课件，笔记无法生成。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""笔记自检：把 9 条写作约束里能机器判定的部分逐条过一遍。

为什么要有它：约束靠提示词守着，跑几次就会漂；交给脚本判，漂了能立刻抓回来。
不能机器判的（详略是否得当、解释是否到位）留给模型自评，这里只报 WARN 供人看。

用法：
  python verify_notes.py notes/xxx-notes.md --slides _ppt2notes_tmp/slides.json
退出码：0 全部通过（可有 WARN）；1 存在 FAIL。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

FAIL, WARN, OK = "FAIL", "WARN", "OK"

BOX_CHARS = set("─│┌┐└┘├┤┬┴┼━┃┏┓┗┛┣┫┳┻╋═║╔╗╚╝▀▄█▌▐░▒▓")
MOJIBAKE = [
    "\ufffd",
    "锟斤拷",
    "ï¿½",
    "â€",
    "ã€",
    "\u00c3\u00a4",
    "\u00c3\u00a5",
    "\u00c3\u00a7",
]
PAGE_CITE = re.compile(r"（PPT\s*P(\d+)(?:\s*-\s*P(\d+))?）")
PAGE_CITE_HALF = re.compile(r"\(PPT\s*P(\d+)")
IMG_REF = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")
POINTER = re.compile(
    r"详见|参见|见第\s*\d+\s*页|翻到|翻至|参考课件|详见课件|此处省略|如上页所示|"
    r"同页|本页|下一页|上一页|右图|左图|上图|下图|图中所示|该图所示|"
    r"(画面|页面)(的)?(右侧|左侧)|(右侧|左侧|右边|左边)的(图|示意|表格|流程)"
)
HEADING = re.compile(r"^(#{1,6})\s+(.*)$")

# ---- 内容判据用的正则
DIVERGE = re.compile(r"顺便说|顺便提|另外|值得一提的是|值得注意的还有|扩展一|引申|题外话|"
                     r"补充一点|与此相关|顺带|再说一句|进一步说|换个角度")
METAPHOR = re.compile(r"就像|好比|类似于|相当于|打个比方|可以想象|不妨想|如同|如同是")
SENT_SPLIT = re.compile(r"[。！？；!?;]")
TERM = re.compile(r"[A-Za-z][A-Za-z0-9+#\-]{2,}")
STOP_TERMS = {"ppt", "pptx", "pdf", "typora", "markdown", "png", "jpg", "http", "https", "www"}


def is_meta_block(text: str) -> bool:
    """文首元信息块也是引用块，但它不是解释。"""
    head = text.strip()
    return bool(re.match(r"^(源文件|总页数|页码基准|笔记语言|生成时间|语言)", head)) or \
        ("源文件" in head and "|" in head)


def collect_quote_blocks(lines: list[str]) -> list[tuple[int, str, str]]:
    """把连续的 > 行收成一条解释块，并记住它前面那一行是什么类型。

    prev_kind 是"这条解释有没有锚点"的证据：只有紧跟在原文段落（或图、表、图注）
    之后，才算它在解释那段内容；跟在标题或另一条解释后面，多半已经跑题。
    """
    blocks: list[tuple[int, str, str]] = []
    prev_kind = "none"
    in_fence = False
    i = 0
    n = len(lines)
    while i < n:
        s = lines[i].strip()
        if s.startswith("```") or s.startswith("~~~"):
            in_fence = not in_fence
            prev_kind = "code"
            i += 1
            continue
        if in_fence:
            i += 1
            continue
        if not s:
            i += 1          # 空行不改变 prev_kind，解释与原文之间允许隔空行
            continue
        if s.startswith(">"):
            start = i
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            blocks.append((start + 1, "\n".join(buf), prev_kind))
            prev_kind = "quote"
            continue
        if HEADING.match(lines[i]):
            prev_kind = "heading"
        elif s.startswith("!["):
            prev_kind = "image"
        elif s.startswith("|"):
            prev_kind = "table"
        elif re.match(r"^[图表]\s*\d+[\.\-]\d+", s):
            prev_kind = "caption"
        else:
            prev_kind = "text"
        i += 1
    return blocks


def build_section_map(headings: list[tuple[int, str, int]]):
    """给行号找它所属的二级小节，用于"一节至多一处比喻"。"""
    marks = [(ln, idx) for idx, (lvl, _t, ln) in enumerate(headings) if lvl <= 2]

    def section_of(line_no: int) -> int:
        cur = -1
        for ln, idx in marks:
            if ln <= line_no:
                cur = idx
            else:
                break
        return cur

    return section_of


def strip_inline_math(line: str) -> str:
    return re.sub(r"\$\$.*?\$\$|\$[^$\n]*\$", "", line)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("notes_md")
    ap.add_argument("--slides", default="", help="extract.py 产出的 slides.json，用于溯源外部概念")
    ap.add_argument("--img-dir", default="")
    args = ap.parse_args()

    md_path = Path(args.notes_md).resolve()
    if not md_path.exists():
        print("ERROR 找不到 %s" % md_path, file=sys.stderr)
        return 2
    text = md_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    issues: list[tuple[str, str, str]] = []   # (level, where, message)
    def add(level, where, msg):
        issues.append((level, where, msg))

    in_fence = False
    fence_lang = ""
    paren_count = {"open_quote": False}
    headings: list[tuple[int, str, int]] = []
    quote_chars = 0      # 解释（灰底）
    body_chars = 0       # 原文与其它正文（白底）
    header_chars = 0     # 元信息块，不是解释
    image_lines: list[tuple[int, str]] = []
    cite_count = 0

    for i, raw in enumerate(lines, start=1):
        line = raw.rstrip()
        stripped = line.strip()

        if stripped.startswith("```") or stripped.startswith("~~~"):
            if not in_fence:
                in_fence = True
                fence_lang = stripped.lstrip("`~").strip().lower()
                if fence_lang in ("mermaid", "dot", "graphviz", "plantuml"):
                    add(FAIL, "L%d" % i, "禁止在笔记里用 %s 画图（约束4：不得用符号绘制流程图）" % fence_lang)
            else:
                in_fence = False
            continue

        if in_fence:
            if any(ch in BOX_CHARS for ch in line):
                add(FAIL, "L%d" % i, "代码块内出现制表符/框线字符，疑似字符画流程图")
            if re.search(r"\+\s*[-=]{2,}\s*\+", line) or re.search(r"(-->|==>|<--|<==)", line):
                add(FAIL, "L%d" % i, "代码块内出现 ASCII 箭头图，疑似字符画流程图")
            continue

        # ---- 高亮配平
        if line.count("==") % 2 == 1:
            add(FAIL, "L%d" % i, "== 高亮未闭合（本行出现 %d 次）" % line.count("=="))
        # ---- 数学环境
        if line.count("$") % 2 == 1:
            add(FAIL, "L%d" % i, "行内数学 $ 未闭合（本行出现 %d 个）" % line.count("$"))
        outside = strip_inline_math(line)
        bad_cmd = re.findall(r"\\[A-Za-z]{2,}", outside)
        if bad_cmd:
            add(FAIL, "L%d" % i, "LaTeX 命令 %s 落在数学环境外，Typora 会原样显示，属乱码" % bad_cmd[:3])
        # ---- 乱码
        for pat in MOJIBAKE:
            if pat in line:
                add(FAIL, "L%d" % i, "检测到乱码片段 %r" % pat)
                break
        # ---- 字符画
        if any(ch in BOX_CHARS for ch in line):
            add(FAIL, "L%d" % i, "出现框线字符，违反「不得用符号画图」")
        if re.search(r"(-->|==>|<--|<==|→→|↓↓)", line):
            add(WARN, "L%d" % i, "疑似箭头流程表示，确认不是用符号画流程")
        # ---- 空指引（含以课件版面为参照的隐性指路）
        is_caption = stripped.startswith("![") or bool(re.match(r"^图\s*\d+[\.\-]\d+", stripped))
        if POINTER.search(line) and not PAGE_CITE.search(line) and not is_caption:
            add(WARN, "L%d" % i, "疑似对着课件说话/只指路不给内容：%s" % stripped[:40])

        # ---- 引用块里塞表格（Typora 不渲染，整张表会退化成一堆竖线）
        if stripped.startswith(">"):
            inner = stripped.lstrip(">").strip()
            nxt_raw = lines[i].strip() if i < len(lines) else ""
            nxt_inner = nxt_raw.lstrip(">").strip() if nxt_raw.startswith(">") else ""
            if inner.count("|") >= 2 and "-" in nxt_inner and \
                    re.match(r"^\|?[\s\-:|]+\|[\s\-:|]*$", nxt_inner or "x"):
                add(FAIL, "L%d" % i, "表格被放进引用块：Typora 不渲染，会退化成一堆竖线")

        # ---- 表格题注
        if re.match(r"^\|.*\|$", stripped) and i < len(lines):
            nxt = lines[i].strip()
            if re.match(r"^\|[\s\-:|]+\|$", nxt) and "-" in nxt:
                prev3 = "\n".join(lines[max(0, i - 4):i - 1])
                if not re.search(r"表\s*\d+[\.\-]\d+", prev3):
                    add(WARN, "L%d" % i, "表格上方没有题注（形如「表1.1 表名（PPT P4）」），读者无法判断来源")
        # ---- 页码格式
        for m in PAGE_CITE.finditer(line):
            cite_count += 1
            a = int(m.group(1))
            b = m.group(2)
            if a < 1 or (b and int(b) < a):
                add(FAIL, "L%d" % i, "页码不合理：%s" % m.group(0))
        if PAGE_CITE_HALF.search(line):
            add(WARN, "L%d" % i, "页码用了半角括号，统一改为（PPT P12）")
        # ---- 结构统计
        m = HEADING.match(line)
        if m:
            headings.append((len(m.group(1)), m.group(2), i))
        if stripped.startswith(">"):
            inner = stripped.lstrip(">").strip()
            is_header = bool(re.match(r"^(源文件|总页数|页码基准|笔记语言|生成时间|语言)", inner)) or \
                ("源文件" in inner and "|" in inner)
            if is_header:
                header_chars += len(inner)
            else:
                quote_chars += len(inner)
                paren_count["open_quote"] = True
        elif stripped:
            body_chars += len(stripped)
        # ---- 图片
        for im in IMG_REF.finditer(line):
            image_lines.append((i, im.group(1)))

    # ================= 全文级检查 =================
    if not paren_count["open_quote"]:
        add(WARN, "-", "除元信息外没有任何解释段落：确认这一章真的不需要作注（解释用 > 引用块承载）")

    head_text = "\n".join(lines[:20])
    for key in ("源文件", "总页数", "页码基准"):
        if key not in head_text:
            add(FAIL, "-", "文首缺元信息字段「%s」（笔记须独立可读，需交代来源）" % key)

    if not headings:
        add(FAIL, "-", "没有任何标题层级")

    prev = 0
    for lvl, txt, ln in headings:
        if prev and lvl > prev + 1:
            add(WARN, "L%d" % ln, "标题层级跳跃 %d->%d：%s" % (prev, lvl, txt[:30]))
        prev = lvl

    # ---- 图片存在性 + 图注
    for ln, ref in image_lines:
        p = (md_path.parent / ref).resolve()
        if not p.exists():
            add(FAIL, "L%d" % ln, "图片不存在：%s" % ref)
            continue
        nxt = ""
        for j in range(ln, min(ln + 3, len(lines))):
            if lines[j].strip():
                nxt = lines[j].strip()
                break
        if not re.search(r"图\s*\d+[\.\-]\d+", nxt):
            add(WARN, "L%d" % ln, "截图下一行没有图注（形如「图1.1 xxx」）")

    # ---- 孤儿图
    img_dir = Path(args.img_dir).resolve() if args.img_dir else (md_path.parent / "img")
    if img_dir.exists():
        used = {(md_path.parent / r).resolve() for _, r in image_lines}
        for f in img_dir.glob("*"):
            stem = md_path.stem.replace("-notes", "")
            if f.is_file() and str(stem) in f.name and f.resolve() not in used:
                add(WARN, "-", "孤儿插图（笔记未引用）：img/%s" % f.name)

    # ================= 内容判据（替代字数判据） =================
    # 加戏不是"写得多"，是"写到了不相关的地方"。所以这里判三件事：
    #   1) 解释有没有锚点（是否紧跟它解释的那段原文）
    #   2) 有没有从 A 滑向 B 的路标语
    #   3) 有没有引入课件从未给过出处的概念
    # 字数只作统计展示，不参与判定。
    slides_text = ""
    if args.slides:
        sp = Path(args.slides).resolve()
        if sp.exists():
            data = json.loads(sp.read_text(encoding="utf-8"))
            parts = []
            for s in data.get("slides", []):
                parts.append(s.get("title", ""))
                parts.extend(b.get("text", "") for b in s.get("blocks", []))
                parts.extend(c for t in s.get("tables", []) for r in t for c in r)
                parts.append(s.get("notes", ""))
            slides_text = "\n".join(parts).lower()

    blocks = collect_quote_blocks(lines)
    section_of = build_section_map(headings)
    metaphor_seen: dict[int, int] = {}

    for start_ln, text, prev_kind in blocks:
        if is_meta_block(text):
            continue
        # 1) 锚点
        if prev_kind not in ("text", "image", "caption", "table", "code"):
            add(WARN, "L%d" % start_ln,
                "解释没有锚点（前一行是%s）：解释要紧跟在它解释的原文之后"
                % {"heading": "标题", "quote": "另一条解释", "none": "文件开头", "code": "代码块"}.get(prev_kind, prev_kind))
        # 2) 发散路标语
        hit = DIVERGE.search(text)
        if hit:
            add(WARN, "L%d" % start_ln,
                "疑似从 A 滑向 B：「%s」——这类引导语后面通常已经不是本节内容" % hit.group(0))
        # 3) 比喻：一节至多一处
        hits = METAPHOR.findall(text)
        if hits:
            sec = section_of(start_ln)
            before = metaphor_seen.get(sec, 0)
            metaphor_seen[sec] = before + len(hits)
            if before + len(hits) > 1:
                add(WARN, "L%d" % start_ln,
                    "本小节共 %d 处比喻（「%s」等）：一节至多一处，多则喧宾夺主"
                    % (before + len(hits), hits[0]))
        # 4) 一条解释只讲一件事
        sent = len([x for x in SENT_SPLIT.split(text) if x.strip()])
        if sent > 3:
            add(WARN, "L%d" % start_ln,
                "这条解释 %d 句：超过三句说明你想讲的是新知识点，该进正文而不是注里" % sent)
        # 5) 外部概念：课件没给过出处
        if slides_text:
            unknown = [t for t in TERM.findall(text) if t.lower() not in slides_text]
            unknown = [t for t in unknown if t.lower() not in STOP_TERMS]
            if unknown:
                add(WARN, "L%d" % start_ln,
                    "课件全文未出现这些概念：%s。若属必要前提，请标「（前置知识）」并只写最少部分"
                    % "、".join(unknown[:4]))

    # ---- 统计（仅展示，不判定）
    stat_txt = "原文 %d 字 / 解释 %d 字" % (body_chars, quote_chars)
    if args.slides and slides_text:
        sp = Path(args.slides).resolve()
        data = json.loads(sp.read_text(encoding="utf-8"))
        src_chars = max(1, int(data.get("total_chars", 0)))
        stat_txt += " / 课件 %d 字（%.0f%%，仅供参考不判定）" % (src_chars, (body_chars + quote_chars) * 100.0 / src_chars)
    stat_txt += " / 解释 %d 条" % len([b for b in blocks if not is_meta_block(b[1])])

    # ================= 输出 =================
    fails = [x for x in issues if x[0] == FAIL]
    warns = [x for x in issues if x[0] == WARN]
    print("=" * 68)
    print("自检：%s" % md_path.name)
    print("  " + stat_txt)
    print("  页码 %d 处 / 插图 %d 张 / 标题 %d 个" % (cite_count, len(image_lines), len(headings)))
    print("=" * 68)
    if not issues:
        print("  全部通过")
    for lvl, where, msg in fails + warns:
        print("  [%s] %-6s %s" % (lvl, where, msg))
    print("-" * 68)
    print("结论：%d 项不通过，%d 项需人工确认" % (len(fails), len(warns)))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())

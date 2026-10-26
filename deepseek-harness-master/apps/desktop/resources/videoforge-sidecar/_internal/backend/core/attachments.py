# -*- coding: utf-8 -*-
"""VideoForge · 对话附件：把上传的文件读成**文本**，让 agent 真能看懂。

═══════════════════════════════════════════════════════════════════
为什么需要它（用户原话）
═══════════════════════════════════════════════════════════════════
「对话框要支持图片、文件（txt、word、pdf等格式的）支持输入，而且能读取和识别」

═══════════════════════════════════════════════════════════════════
设计取舍（每一条都是为了"别骗用户"）
═══════════════════════════════════════════════════════════════════
1. **抽成纯函数**（`extract_text`），不依赖 FastAPI/数据库 ——
   这样它能被直接测。本仓的规矩：能测的东西才会被真的验过。
2. 解析库**已经装好，而且已经在 `VideoForge.spec` 的 hiddenimports 里**
   （`pypdf` / `docx` / `fitz`）⇒ **打包版也能解析**，不是"开发能跑、exe 缺模块"。
3. **认不出来就如实说认不出来**，绝不返回空字符串冒充成功 ——
   否则用户会以为"模型读过了"，然后在错误的假设上往下走。
4. 文本有上限（默认 20000 字）：超了**截断并明确标注截了多少** ——
   既不爆上下文，也不让模型以为手里就是全文。
5. 图片：**如实告知"没有做视觉识别"**。要不要看得懂图，取决于用户配的模型
   是否多模态 —— 这里不假装。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("videoforge.attachments")

#: 单文件大小上限（超过就直接拒绝并说明，不让它悄悄吃满内存/上下文）
#: ★★ 2026-09-27 第 46 轮放宽：用户第 1 条 ——「剧本文件上传的大小字数都有限制」。
#:    原来是 50MB / 2 万字：一个 40 集的剧本（几万字）一上传就被截断，
#:    用户看到的是"读进来一半"。现在**读进来**放到 200MB / 20 万字。
MAX_BYTES = 200 * 1024 * 1024
#: 抽出来的正文上限（字符）—— 这是**存和给用户看**的上限，不是给模型的上限
MAX_CHARS = 200000
#: 真正**塞进模型提示词**的上限：模型上下文有限，太长会挤掉剧本本身。
#: 超过就带上明确说明（原文完整保存在上传目录，可随时再读）。
PROMPT_CHARS = 24000

TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log",
            ".srt", ".ass", ".yaml", ".yml", ".ini", ".py", ".js", ".html"}
DOC_EXT = {".docx", ".doc"}
PDF_EXT = {".pdf"}
SHEET_EXT = {".xlsx", ".xlsm"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}


def kind_of(name: str) -> str:
    """按扩展名判类型。认不出返回 "unknown"（**不猜成文本**）。"""
    ext = Path(str(name or "")).suffix.lower()
    if ext in TEXT_EXT:
        return "text"
    if ext in DOC_EXT:
        return "docx"
    if ext in PDF_EXT:
        return "pdf"
    if ext in SHEET_EXT:
        return "sheet"
    if ext in IMAGE_EXT:
        return "image"
    return "unknown"


def safe_name(name: str) -> str:
    """把上传的文件名收敛成安全的**纯文件名**。

    ★ 为什么必须做：multipart 的 `filename` 是客户端说了算的，
      里面可能带 `..\\..\\windows\\system32` 这种路径分隔符 ——
      直接拼进保存路径就是**路径穿越**（能在任意位置写文件）。
      这里只留最后一段、去掉分隔符与控制字符，再兜一个默认名。
    """
    n = str(name or "").strip().replace("\\", "/").split("/")[-1]
    n = re.sub(r'[<>:"|?*\x00-\x1f]', "_", n).strip(" .")
    if not n:
        n = "attachment"
    if len(n) > 120:
        stem, ext = os.path.splitext(n)
        n = stem[:110] + ext[:10]
    return n


def save_upload(data_dir: str, filename: str, content: bytes) -> Path:
    """把上传的字节落到 `<data_dir>/uploads/`，返回保存路径。

    · 文件名加时间戳前缀（避免同名互相覆盖，也便于按时间找）；
    · 只写在这个固定子目录里（配合 `safe_name` 防穿越）。
    """
    if len(content or b"") > MAX_BYTES:
        raise ValueError("文件太大（%.1f MB，上限 %d MB）"
                         % (len(content) / 1048576.0, MAX_BYTES // 1048576))
    up = Path(data_dir or ".") / "uploads"
    up.mkdir(parents=True, exist_ok=True)
    stem = "%s_%s" % (time.strftime("%Y%m%d-%H%M%S"), safe_name(filename))
    dst = up / stem
    # ★★ 2026-09-27 第一次真跑抓到的真 bug：时间戳只精确到**秒**，
    #   于是**同一秒内传两个同名文件**（连拖两张、或先拖后粘贴）会算出
    #   同一个路径 —— 后一个**静默覆盖**前一个，用户以为两个都传上去了。
    #   这里加序号兜底：存在就 _1、_2……（不引入随机数，便于按时间找文件）。
    n = 1
    while dst.exists():
        p = Path(stem)
        dst = up / ("%s_%d%s" % (p.stem, n, p.suffix))
        n += 1
    dst.write_bytes(content or b"")
    return dst


# ──────────────────────── 各类型的解析 ────────────────────────


def _read_text_file(p: Path) -> str:
    """读文本文件：UTF-8 → GBK → 宽松解码。

    ★ 为什么三步：中文 Windows 上用户存的 txt 很可能是 GBK/ANSI 的，
      直接用 UTF-8 读会抛 UnicodeDecodeError（或者读出一堆 \ufffd）。
      `errors="replace"` 是最后兜底 —— 宁可有几个问号，也别整个文件读不出来。
    """
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _read_docx(p: Path) -> str:
    """Word（.docx）：段落 + 表格里的文字都要，表格常带关键信息。"""
    import docx  # python-docx
    d = docx.Document(str(p))
    parts = [para.text for para in d.paragraphs if str(para.text or "").strip()]
    for t in d.tables:
        for row in t.rows:
            cells = [str(c.text or "").strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _read_pdf(p: Path) -> str:
    """PDF：优先 PyMuPDF(fitz)（快、版面好），失败退回 pypdf。"""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(str(p))
        try:
            return "\n".join(page.get_text() or "" for page in doc)
        finally:
            doc.close()
    except Exception as e:
        logger.warning("fitz 读 PDF 失败（改用 pypdf）：%s", e)
    import pypdf
    r = pypdf.PdfReader(str(p))
    return "\n".join((pg.extract_text() or "") for pg in r.pages)


def _read_sheet(p: Path) -> str:
    """Excel（.xlsx）：每行拼成 TSV，多个 sheet 用小标题隔开。"""
    import openpyxl
    wb = openpyxl.load_workbook(str(p), data_only=True, read_only=True)
    out = []
    try:
        for ws in wb.worksheets:
            rows = []
            for row in ws.iter_rows(values_only=True):
                cells = ["" if c is None else str(c) for c in row]
                if any(c.strip() for c in cells):
                    rows.append("\t".join(cells).rstrip())
            if rows:
                out.append("### %s\n%s" % (ws.title, "\n".join(rows)))
    finally:
        try:
            wb.close()
        except Exception:
            pass
    return "\n\n".join(out)


def _describe_image(p: Path) -> str:
    """图片：只报**能确定的事实**（尺寸/格式/大小），不假装看懂了画面。"""
    try:
        from PIL import Image
        with Image.open(str(p)) as im:
            return "图片：%s，%dx%d 像素，%s" % (p.name, im.width, im.height,
                                                (im.format or "").lower())
    except Exception as e:  # noqa: BLE001
        logger.warning("读图片信息失败：%s", e)
        return "图片：%s" % p.name


# ──────────────────────── 主入口 ────────────────────────


def extract_text(path: Any, limit: int = MAX_CHARS) -> Dict[str, Any]:
    """把文件读成文本。

    返回（**每个键都有意义，别只看 text**）：
        kind         "text"|"docx"|"pdf"|"sheet"|"image"|"unknown"
        text         抽出来的正文（可能被截断；认不出来时为空串）
        chars        正文长度
        total_chars  截断前的长度
        truncated    是否被截断
        ok           是否真的拿到了内容
        note         给用户看的一句话（成功/失败/为什么）
        error        失败原因（ok=False 时非空）
    """
    p = Path(str(path))
    kind = kind_of(p.name)
    base = {"kind": kind, "text": "", "chars": 0, "total_chars": 0,
            "truncated": False, "ok": False, "note": "", "error": ""}

    if not p.is_file():
        return {**base, "error": "文件不存在：%s" % p,
                "note": "文件不存在 —— 可能上传没成功。"}

    size = p.stat().st_size
    try:
        if kind == "text":
            raw = _read_text_file(p)
        elif kind == "docx":
            if p.suffix.lower() == ".doc":
                return {**base, "error": "旧版 .doc 不支持",
                        "note": "旧版 .doc（97-2003）读不了 —— 请另存为 .docx 再传。"}
            raw = _read_docx(p)
        elif kind == "pdf":
            raw = _read_pdf(p)
        elif kind == "sheet":
            raw = _read_sheet(p)
        elif kind == "image":
            raw = _describe_image(p)
            # ★★ 2026-09-29（用户第 1 条：「支持图片理解或者直接应用上传的图片」）
            #
            #   原来这里只回"尺寸/格式"，并说"没有做视觉识别"。
            #   实测（`tests/probe_vision.py`）：**这个模型是能看图的多模态** ——
            #   给它一张定妆照，它能准确说出「1 位男性、高髻、留胡须、
            #   灰褐粗布交领长袍、深色灯笼裤、赤脚、深色烟雾背景」。
            #   所以图片**应该直接作为图像块送给模型**，而不是只报一个尺寸。
            #
            #   `vision=True` 就是给上层（chat 接口/前端）的信号：
            #   "这个附件要按**图像内容块**发出去，别只发文字"。
            return {**base, "text": raw, "chars": len(raw),
                    "total_chars": len(raw), "ok": True, "vision": True,
                    "note": ("图片已收到（%s）。我会把这张图**原样发给当前配置的"
                             "对话模型**看 —— 多模态模型能直接看懂画面，"
                             "纯文本模型会忽略它。也可以直接说一句"
                             "「就用这张当某某的参考图」。"
                             % Path(p).name)}
        else:
            return {**base, "error": "不支持的文件类型：%s" % p.suffix,
                    "note": ("这个格式（%s）我读不了。能读：txt/md/csv/json、"
                             "docx、pdf、xlsx，以及图片的基本信息。" % (p.suffix or "无扩展名"))}
    except Exception as e:  # noqa: BLE001
        logger.warning("解析附件失败 %s：%s", p, e)
        return {**base, "error": "%s: %s" % (type(e).__name__, e),
                "note": ("解析失败（%s）—— 文件可能损坏、加密、或者是扫描件"
                         "（扫描件里没有文字层，需要 OCR，本项目没做）。"
                         % type(e).__name__)}

    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    total = len(text)
    truncated = total > limit
    if truncated:
        text = text[:limit]
    if not text.strip():
        return {**base, "total_chars": total,
                "note": ("解析出来是**空的** —— 多半是扫描件/图片型 PDF"
                         "（里面没有文字层，需要 OCR），或者文件本来就是空的。"),
                "error": "没有抽出任何文字"}
    return {**base, "text": text, "chars": len(text), "total_chars": total,
            "truncated": truncated, "ok": True,
            "note": ("已读取「%s」：%d 字%s"
                     % (Path(p).name, total,
                        "（太长，只送了前 %d 字）" % limit if truncated else "")),
            "size": size}


def as_prompt_block(name: str, info: Dict[str, Any]) -> str:
    """把附件包成一段**给模型看的**文本块（前端并进用户消息里用）。

    为什么用显式标记而不是裸拼：模型要知道"哪一段是附件内容、到哪结束"，
    否则会把文件内容当成用户说的话。

    ★★ 2026-09-27 第 46 轮（用户第 1 条）：**存下来的**正文可以很长
      （`MAX_CHARS` 已放宽到 20 万字），但塞进模型提示词的必须再截一刀
      （`PROMPT_CHARS`）——否则一份 10 万字的剧本会把模型上下文挤满，
      反而看不清用户在说什么。截断时**明确写出来**，并告诉用户原文在哪。
    """
    if not info.get("ok"):
        return "【附件：%s】⚠ %s" % (name, info.get("note") or info.get("error") or "读取失败")
    text = str(info.get("text") or "")
    total = int(info.get("total_chars") or info.get("chars") or len(text))
    cut = len(text) > PROMPT_CHARS
    if cut:
        text = text[:PROMPT_CHARS]
    head = "【附件：%s（原文 %d 字%s）】" % (
        name, total,
        "；**这里只带前 %d 字给模型**，原文完整保存在 %s，需要时让用户说"
        "「接着读后面的」或直接引用原文" % (PROMPT_CHARS, info.get("path") or "上传目录")
        if cut else ("" if not info.get("truncated") else "，已截断"))
    return "%s\n%s\n【附件结束】" % (head, text)

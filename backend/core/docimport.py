# -*- coding: utf-8 -*-
"""
VideoForge · 剧本导入（把用户的文档变成可用的剧本）

支持：.txt / .md / .json / .docx / .pdf / 直接粘贴的文字
产出：纯文本 →（可选）交给 LLM 结构化成分场剧本 → 落库

设计：
- 解析层只负责"把各种格式变成文本"，不认识的东西明确报错，不猜
- PDF 走文字层（pypdf）；扫描件没有文字层时用 PyMuPDF 再试一次，仍为空则
  明确告诉用户"这是图片型 PDF，需要先 OCR"，而不是给一个空剧本
- 结构化交给 ScriptService（复用「想法生成大纲」那套），保证下游（分镜/角色/场景）一致
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Dict, Optional, Tuple

logger = logging.getLogger("videoforge.docimport")

TEXT_EXT = {".txt", ".md", ".markdown", ".json", ".csv", ".srt", ".log"}
DOCX_EXT = {".docx"}
PDF_EXT = {".pdf"}
RTF_EXT = {".rtf"}


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _from_docx(path: str) -> str:
    """用 python-docx 读；失败则退回 XML 解析"""
    try:
        import docx  # type: ignore
        d = docx.Document(path)
        parts = [p.text for p in d.paragraphs if p and p.text and p.text.strip()]
        for t in getattr(d, "tables", []) or []:
            for row in t.rows:
                cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
        text = "\n".join(parts)
        if text.strip():
            return text
    except Exception as e:
        logger.warning("python-docx 解析失败，退回 XML：%s", e)
    import zipfile
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:br[^>]*/>", "\n", xml)
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(a, b)
    return text


def _from_pdf(path: str) -> Tuple[str, str]:
    """返回 (文本, 备注)。文字层为空时提示需要 OCR"""
    text = ""
    try:
        from pypdf import PdfReader
        r = PdfReader(path)
        pages = []
        for p in r.pages[:400]:
            try:
                pages.append(p.extract_text() or "")
            except Exception:
                pages.append("")
        text = "\n".join(pages)
    except Exception as e:
        logger.warning("pypdf 解析失败：%s", e)
    if len(text.strip()) >= 30:
        return text, ""
    # 再试 PyMuPDF（对某些 PDF 的文字层更稳）
    try:
        import fitz  # type: ignore
        doc = fitz.open(path)
        text = "\n".join(page.get_text() for page in doc)
        doc.close()
    except Exception as e:
        logger.warning("PyMuPDF 解析失败：%s", e)
    if len(text.strip()) >= 30:
        return text, ""
    return text, "该 PDF 没有可提取的文字层（很可能是扫描件/图片型 PDF）。请先用 OCR 转成文字，或直接粘贴文本。"


def _from_rtf(path: str) -> str:
    raw = open(path, "rb").read().decode("latin-1", "ignore")
    raw = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes([int(m.group(1), 16)]).decode("cp1252", "ignore"), raw)
    raw = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", raw)
    return raw.replace("{", "").replace("}", "")


def parse_document(path: str) -> Tuple[str, str]:
    """把文档解析成纯文本。返回 (text, note)。异常时抛 ValueError（带可读原因）"""
    if not path or not os.path.exists(path):
        raise ValueError("文件不存在")
    ext = os.path.splitext(path)[1].lower()
    size = os.path.getsize(path)
    if size > 60 * 1024 * 1024:
        raise ValueError(f"文件过大（{size // 1048576} MB），请拆分后再导入")

    if ext in TEXT_EXT:
        for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030", "big5", "latin-1"):
            try:
                raw = open(path, "r", encoding=enc).read()
                break
            except UnicodeDecodeError:
                continue
        else:
            raw = open(path, "rb").read().decode("utf-8", "ignore")
        if ext == ".json":
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    cand = obj.get("text") or obj.get("content") or obj.get("outline") or ""
                    if cand:
                        raw = cand if isinstance(cand, str) else json.dumps(cand, ensure_ascii=False, indent=1)
            except Exception:
                pass
        return _clean(raw), ""
    if ext in DOCX_EXT:
        return _clean(_from_docx(path)), ""
    if ext in PDF_EXT:
        t, note = _from_pdf(path)
        return _clean(t), note
    if ext in RTF_EXT:
        return _clean(_from_rtf(path)), ""
    if ext in (".doc",):
        raise ValueError("不支持旧版 .doc 格式。请在 Word 里另存为 .docx 后再导入")
    if ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"):
        raise ValueError("这是图片文件。图片版剧本请先 OCR 成文字，或直接在下方文本框粘贴文字")
    raise ValueError(f"暂不支持的格式：{ext or '(无扩展名)'}。支持 txt / md / json / docx / pdf / rtf")


def looks_like_script(text: str) -> bool:
    """粗判文本是否像剧本（含场次/对白标记）"""
    if not text or len(text) < 40:
        return False
    markers = ("第", "场", "集", "内景", "外景", "INT", "EXT", "旁白", "台词", "镜头")
    hits = sum(1 for m in markers if m in text[:4000])
    return hits >= 2 or "\n" in text

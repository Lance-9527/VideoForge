# -*- coding: utf-8 -*-
"""
电影语言技法工具（挂到 agent 工具注册表）

════════════════════════════════════════════════════════════════════
给 agent 两个动作：

  1. `find_cinematic_techniques`  按「我想要什么氛围/效果」搜候选技法
  2. `build_cinematic_prompt`    取某条技法的完整模板并填好主体

为什么不让 agent 直接把 150 条写进提示词：
  实测（2026-09-29，MiniMax image-01，14 张对照图）——
  器材品牌名（Kodak/Zeiss/ARRI）与导演名对成图**无任何视觉贡献**，
  删掉后出图一致；而摄影技法词（chiaroscuro / Dutch angle / shallow
  depth of field）**确实生效**。所以库里只保留后者。

  另一条实测结论：荷兰角单独写会让模型把**人和场景一起**倾斜（错的），
  荷兰角应是摄影机绕光轴倾斜、人物保持垂直。库里该条已内置修正描述。
════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from . import prompt_library as pl
from .agent_tools import REGISTRY, err, ok

logger = logging.getLogger("videoforge.promptlib_tools")


@REGISTRY.register(
    "find_cinematic_techniques",
    "按**氛围或视觉效果**找电影摄影技法。用户说「我想拍出不安的感觉」"
    "「这个场景要压抑一点」「怎么拍出复古的味道」时用它。\n"
    "★ 会直接返回**可用的提示词模板**，不用再调第二个工具就能出图。\n"
    "  · 传 `mood`（中文或英文均可，说法随意）必填。\n"
    "  · `cat` 可选：camera 摄影 / composition 构图 / lighting 灯光 / "
    "genres 类型 / vfx 特效。\n"
    "  · 只返回对**静态出图有效**的技法；剪辑与叙事类（时间结构）"
    "对单张图无效，已自动排除。\n"
    "  · `subject` 传了就自动把 [Subject] 填掉，省一步。",
    {"mood": {"type": "string",
              "desc": "想要的氛围或效果，中英文皆可，如「不安」「孤独 冷」「怀旧」",
              "required": True},
     "cat": {"type": "string",
             "desc": "可选，限制分类：camera/composition/lighting/genres/vfx",
             "required": False},
     "subject": {"type": "string",
                 "desc": "可选，主体描述（如「穿米色风衣的中年男人」）",
                 "required": False},
     "limit": {"type": "string",
               "desc": "可选，返回条数，默认 3，最多 6", "required": False}},
)
async def t_find_cinematic_techniques(args: Dict[str, Any]) -> Any:
    bad = _need(args, "mood")
    if bad:
        return bad
    if not pl.is_available():
        return err("电影语言提示词库没加载到（backend/data/prompt_library/"
                   "techniques.json 缺失）。先不用这个功能，直接手写提示词。")

    mood = str(args["mood"]).strip()
    cat = str(args.get("cat") or "").strip().lower()
    subject = str(args.get("subject") or "").strip()
    try:
        limit = int(args.get("limit") or 3)
    except (TypeError, ValueError):
        limit = 3
    limit = max(1, min(6, limit))

    try:
        hits = pl.search(mood, cat=cat, static_only=True, limit=limit)
    except Exception as e:  # noqa: BLE001
        logger.exception("find_cinematic_techniques failed")
        return err(f"检索技法失败：{type(e).__name__}: {e}")

    if not hits:
        return err(f"没找到匹配「{mood}」的技法。换个说法试试，"
                   f"或者用英文词（lonely / tense / noir / vintage）。")

    items: List[Dict[str, Any]] = []
    for t in hits:
        entry = {
            "slug": t["slug"],
            "title": t["title"],
            "category": t.get("cat_cn") or t.get("cat"),
            "when": str(t.get("when") or "")[:300],
        }
        entry["prompt"] = (pl.fill_subject(t["prompt"], subject) if subject
                           else t["prompt"])
        items.append(entry)

    return ok(
        query=mood,
        count=len(items),
        techniques=items,
        note=("上面每条都带完整提示词模板，可以直接拿去出图。"
              + ("已经把 [Subject] 填成你说的主体。" if subject
                 else "把 [Subject] 换成你的主体描述就能用。")),
        warning=pl.subject_language_warning(subject) if subject else "",
    )


@REGISTRY.register(
    "build_cinematic_prompt",
    "取某条电影技法的**完整提示词模板**，并自动填好主体。"
    "用法：先 find_cinematic_techniques 拿到 slug，再把 slug 传进来。",
    {"slug": {"type": "string",
              "desc": "技法 slug（如 dutch-angle / chiaroscuro / three-point-lighting）",
              "required": True},
     "subject": {"type": "string",
                 "desc": "主体描述，会替换模板里的 [Subject]", "required": True}},
)
async def t_build_cinematic_prompt(args: Dict[str, Any]) -> Any:
    bad = _need(args, "slug", "subject")
    if bad:
        return bad
    if not pl.is_available():
        return err("电影语言提示词库没加载到。")

    slug = str(args["slug"]).strip().lower()
    subject = str(args["subject"]).strip()
    t = pl.get(slug)
    if not t:
        sample = ", ".join(x["slug"] for x in pl.search("", limit=6))
        return err(f"没有 slug 叫「{slug}」的技法。先调 "
                   f"find_cinematic_techniques 查，或参考：{sample}")

    filled = pl.fill_subject(t["prompt"], subject)
    return ok(
        slug=slug,
        title=t["title"],
        category=t.get("cat_cn") or t.get("cat"),
        prompt=filled,
        when=str(t.get("when") or "")[:400],
        note=("提示词是英文的，直接喂给出图模型即可，不要再翻译成中文 —— "
              "多数模型对英文摄影术语的响应明显更好。"),
        warning=pl.subject_language_warning(subject),
    )


@REGISTRY.register(
    "list_cinematic_categories",
    "列出电影语言提示词库有哪些分类、各多少条、是否可用。"
    "用户在问「你有哪些拍摄手法」时用它。",
    {},
)
async def t_list_cinematic_categories(args: Dict[str, Any]) -> Any:
    if not pl.is_available():
        return err("电影语言提示词库没加载到。")
    per: Dict[str, int] = {}
    for t in pl._load_techniques():          # noqa: SLF001
        k = t.get("cat_cn") or t.get("cat") or "?"
        per[k] = per.get(k, 0) + 1
    n_img = sum(1 for t in pl._load_techniques()          # noqa: SLF001
                if t.get("effective") == "image")
    return ok(
        total=pl.library_size(),
        usable_for_still_image=n_img,
        categories=per,
        note=("剪辑与叙事类共 21 条对静态出图无效（它们是时间结构概念），"
              "查找工具会自动排除；做视频分镜时仍可参考。"),
    )


def _need(args: Dict[str, Any], *keys: str):
    miss = [k for k in keys if not str(args.get(k) or "").strip()]
    if miss:
        return err(f"缺少必填参数：{', '.join(miss)}")
    return None

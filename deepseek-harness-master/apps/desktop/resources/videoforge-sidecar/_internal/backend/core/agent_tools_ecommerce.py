# -*- coding: utf-8 -*-
"""VideoForge · 「电商商品视频」功能的 Agent 工具

═══════════════════════════════════════════════════════════════════
这个功能点是干什么的
═══════════════════════════════════════════════════════════════════
用户原话（2026-10-24）：「最后再研究下电商行业，电商商品视频的技巧，
然后完美搜集做好后，单独做成一个功能，回馈到 VideoForge」。

电商商品视频和剧情短剧是**两种东西**，三条硬约束短剧没有：

  ① 平台规格是硬指标（时长/比例/分辨率/首帧）—— 超了被裁切或压缩，成片白做；
  ② 结构按品类的转化漏斗走 —— 不是「起承转合」；
  ③ 合规是红线 —— 一句「全网第一」能让整条素材下架。

所以这个功能点把三条约束都做成**可查的数据 + 可执行的检查**，数据在
`skills_builtin/ecommerce-product-video/rules.json`（单一真相源）。

═══════════════════════════════════════════════════════════════════
工具清单
═══════════════════════════════════════════════════════════════════
  · `ecommerce_plan_video`   —— 商品 → 视频方案（scenes 用 VideoForge 剧本 schema，
                                 可直接进 scriptaudit / 分镜）
  · `ecommerce_audit_copy`   —— 文案合规体检（广告法绝对化用语 / 化妆品医疗暗示 /
                                 食品保健 / 三品一械 / 平台红线），BLOCK + WARN 双层
  · `ecommerce_platform_spec`—— 查平台规格 + 核对成片参数
  · `ecommerce_ai_labeling`  —— AI 内容标识法定义务清单（GB 45438-2025）

═══════════════════════════════════════════════════════════════════
纪律
═══════════════════════════════════════════════════════════════════
· 纯本地、零网络、零付费（不调任何生成模型）。
· 平台规格查不到就返回 `unknown`，**绝不编一套默认值冒充平台规则**。
· 合规词表是 BLOCK + WARN 双层：纯黑名单会误杀合法文案（商标含「最」、
  限定时空的销量事实都合法），所以 warn 层要求举证而不是直接拒。
· **不提供去 AI 标识的能力**，也不应被用于此目的（办法第十条明文禁止）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .agent_tools import REGISTRY, ToolResult, err, merged, ok

logger = logging.getLogger("videoforge.agent.tools.ecommerce")


def _svc():
    from .agent_tools import _services as s
    return s()


def _bootstrap() -> ToolResult:
    """确认内置 skill 被 skill 系统看到了（否则规则没加载）。"""
    try:
        from .skills import get_registry
        reg = get_registry(None)
        if reg.get("ecommerce-product-video") is None:
            probs = reg.errors() or []
            return err("内置 skill ecommerce-product-video 没加载上。"
                       "检查 backend/skills_builtin/ecommerce-product-video/ 是否有 "
                       "SKILL.md + rules.json。解析问题：%s" % (probs[:3] or "无"))
    except Exception as e:  # noqa: BLE001
        logger.warning("skill 自检失败（继续）：%s", e)
    return None


@REGISTRY.register(
    "ecommerce_plan_video",
    "**电商商品视频方案**：给一个商品 + 平台 + 品类，产出可直接进流水线的分镜方案。"
    "按品类的转化漏斗排段（抛需求→给方案→验信任→催下单，各品类有各自的必须/禁止项），"
    "并把时长精确切到目标秒数（总和 == 目标，能直接过 scriptaudit）。"
    "产出 scenes[] 用 VideoForge 剧本 schema，同时带合规体检结果、平台规格核对、"
    "AI 标识清单与批量投稿风险提醒。纯本地、不花钱。",
    {"product": {"type": "string", "desc": "商品名（必填）", "required": True},
     "platform": {"type": "string",
                  "desc": "平台 key：douyin_shop_main / douyin_cart / taobao_main / "
                          "jd_main / qianchuan_vertical / xiaohongshu_note …（不传则用 "
                          "ecommerce_platform_spec 列全部）"},
     "category": {"type": "string",
                  "desc": "品类 key：beauty / food / apparel / tech3c / home / baby / daily"},
     "selling_points": {"type": "array", "desc": "卖点数组（尽量给可验证的事实，别给形容词）"},
     "audience": {"type": "string", "desc": "目标人群（一句话）"},
     "duration_sec": {"type": "number",
                      "desc": "目标时长秒（不传则用平台推荐值，再没有就 30s 兜底）"},
     "price_line": {"type": "string", "desc": "价格/优惠表述（务必与挂车实际成交价一致）"},
     "brand": {"type": "string", "desc": "品牌名（可选）"},
     "ai_label": {"type": "string",
                  "desc": "AI 标识选择（**用户可选**）：off=不加 / plan=只给方案（默认）/ "
                          "apply=出片后真烧进画面"}},
    needs_project=False,
)
async def t_ecommerce_plan_video(args: Dict[str, Any]) -> ToolResult:
    bad = _bootstrap()
    if bad:
        return bad
    try:
        from . import ecommerce_video as EV
    except Exception as e:  # noqa: BLE001
        return err("电商模块没加载上：%s: %s" % (type(e).__name__, e))
    prod = str(args.get("product") or "").strip()
    if not prod:
        return err("必须给 `product`（商品名）。")
    sps = args.get("selling_points")
    if isinstance(sps, str):
        sps = [x.strip() for x in sps.replace("\n", "、").split("、") if x.strip()]
    if not isinstance(sps, list):
        sps = []
    try:
        plan = EV.plan_video(
            product=prod,
            platform=str(args.get("platform") or ""),
            category=str(args.get("category") or ""),
            selling_points=[str(x) for x in sps],
            audience=str(args.get("audience") or ""),
            duration_sec=float(args.get("duration_sec") or 0),
            price_line=str(args.get("price_line") or ""),
            brand=str(args.get("brand") or ""),
            ai_label=str(args.get("ai_label") or "plan"))
    except Exception as e:  # noqa: BLE001
        logger.exception("ecommerce_plan_video failed")
        return err("生成商品视频方案失败：%s: %s" % (type(e).__name__, e))
    if not plan.get("ok"):
        return err(str(plan.get("error") or "生成失败"))

    comp = plan.get("compliance") or {}
    return ok(
        product=plan.get("product"),
        platform=(plan.get("platform") or {}).get("name_zh"),
        category=(plan.get("category") or {}).get("name_zh"),
        target_duration=plan.get("target_duration"),
        beats=plan.get("beats"),
        scenes=plan.get("scenes"),
        compliance={"ok": comp.get("ok"), "score": comp.get("score"),
                    "errors": comp.get("errors"), "warnings": comp.get("warnings"),
                    "hits": [{"term": h.get("term"), "severity": h.get("severity"),
                              "why": h.get("why")} for h in (comp.get("hits") or [])][:12]},
        platform_check=plan.get("platform_check"),
        ai_label=plan.get("ai_label"),
        ai_labeling=plan.get("ai_labeling"),
        batch_risk=plan.get("batch_risk"),
        warnings=plan.get("warnings"),
        note=("scenes 是 VideoForge 剧本 schema，可直接进 scriptaudit / 分镜。"
              "合规 errors 必须清零才能发；warn 命中要准备举证材料。"
              "AI 标识是**可选项**（ai_label=off/plan/apply），工具不强制，但 off 有违规风险。"))


@REGISTRY.register(
    "ecommerce_audit_copy",
    "**电商文案合规体检**：查广告法绝对化用语、化妆品医疗暗示词、食品保健功效宣称、"
    "三品一械诱导语、平台红线（站外引流/限时秒杀/全网最低/原价），"
    "并区分 BLOCK（命中即拒）与 WARN（需举证）。纯本地、免费。"
    "★ 只查文案，不查画面；★ 不是法律意见，上线前让法务复核。",
    {"text": {"type": "string", "desc": "要检查的文案/口播词/字幕（必填）", "required": True},
     "category": {"type": "string",
                  "desc": "品类（可选，用于提示该品类额外红线）：beauty/food/baby/tech3c…"},
     "project_id": {"type": "string", "desc": "项目 id（可选）"}},
    needs_project=False,
)
async def t_ecommerce_audit_copy(args: Dict[str, Any]) -> ToolResult:
    bad = _bootstrap()
    if bad:
        return bad
    try:
        from . import ecommerce_video as EV
    except Exception as e:  # noqa: BLE001
        return err("电商模块没加载上：%s: %s" % (type(e).__name__, e))
    text = str(args.get("text") or "")
    if not text.strip():
        return err("必须给 `text`（要检查的文案）。")
    try:
        rep = EV.audit_copy(text)
    except Exception as e:  # noqa: BLE001
        logger.exception("ecommerce_audit_copy failed")
        return err("合规体检失败：%s: %s" % (type(e).__name__, e))
    extra: Dict[str, Any] = {}
    cat = str(args.get("category") or "").strip()
    if cat:
        cs = EV.category_structure(cat)
        if cs.get("state") == "ok":
            extra["category_red_lines"] = [
                {"beat": b.get("beat"), "must_not": b.get("must_not")}
                for b in (cs.get("beats") or []) if b.get("must_not")]
    return ok(audit=rep, category=cat or None, **extra,
              note=("errors>0 必须改；warnings 是「需举证」不是「必须删」——"
                    "商标含「最」、限定时空的销量事实都合法。"))


@REGISTRY.register(
    "ecommerce_platform_spec",
    "查电商平台的**视频规格**（时长/比例/分辨率/首帧要求/码率），并可顺带核对"
    "你打算做的成片参数是否合规。已收录抖店/抖音挂车/千川竖横/淘宝主图·详情·微详情/"
    "京东主图·商详/拼多多/微信小店/视频号/小红书。"
    "★ 查不到就如实说查不到，不编平台规则。",
    {"platform": {"type": "string",
                  "desc": "平台 key 或中文名；不传则列出全部已收录平台"},
     "duration_sec": {"type": "number", "desc": "你会做的成片时长（可选，用于核对）"},
     "width": {"type": "number", "desc": "成片宽（可选）"},
     "height": {"type": "number", "desc": "成片高（可选）"}},
    needs_project=False,
)
async def t_ecommerce_platform_spec(args: Dict[str, Any]) -> ToolResult:
    bad = _bootstrap()
    if bad:
        return bad
    try:
        from . import ecommerce_video as EV
    except Exception as e:  # noqa: BLE001
        return err("电商模块没加载上：%s: %s" % (type(e).__name__, e))
    plat = str(args.get("platform") or "").strip()
    if not plat:
        return ok(platforms=EV.list_platforms(),
                  note="传 `platform` 可查单个平台完整规格；传 duration_sec/width/height 可核对成片参数。")
    spec = EV.platform_spec(plat)
    if spec.get("state") != "ok":
        return err(str(spec.get("reason") or "未收录该平台"))
    check = None
    if args.get("duration_sec") or (args.get("width") and args.get("height")):
        check = EV.check_against_platform(
            platform=plat,
            duration_sec=float(args.get("duration_sec") or 0),
            width=int(args.get("width") or 0), height=int(args.get("height") or 0))
    return ok(spec=spec, check=check,
              note="规格来自 skills_builtin/ecommerce-product-video/rules.json（抓取 2026-10-24），上线前复核官方。")


@REGISTRY.register(
    "ecommerce_ai_labeling",
    "AI 生成内容标识：先给**法定义务清单**（《人工智能生成合成内容标识办法》+ 强制性国标 "
    "GB 45438-2025，2025-09-01 起施行）；传 `video_path` + `apply=true` 就**真的**把标识"
    "烧进画面（起始提示 + 全程角标）并写进元数据与旁挂 JSON。"
    "L4「发布时主动声明」只能用户自己做，工具会如实提醒。"
    "★ 本工具不提供去标能力，也不应被用于此目的。",
    {"video_path": {"type": "string",
                    "desc": "要打标识的视频路径（不传则只出清单/方案，不动文件）"},
     "apply": {"type": "boolean",
               "desc": "true=真烧进画面（会重新编码，耗时与片长成正比）"},
     "out_path": {"type": "string", "desc": "输出路径（默认在原文件旁 *.ailabel.mp4）"},
     "mode": {"type": "string",
              "desc": "start_and_badge（默认，起始提示+全程角标）/ start_only / badge_only"},
     "start_seconds": {"type": "number", "desc": "起始提示持续几秒（默认 3）"},
     "start_text": {"type": "string", "desc": "起始提示文字（默认「本视频由 AI 生成」）"},
     "badge_text": {"type": "string", "desc": "角标文字（默认「AI 生成」）"},
     "font_path": {"type": "string",
                   "desc": "中文字体路径（默认自动找 simhei/msyh/simsun）"}},
    needs_project=False,
)
async def t_ecommerce_ai_labeling(args: Dict[str, Any]) -> ToolResult:
    bad = _bootstrap()
    if bad:
        return bad
    try:
        from . import ecommerce_video as EV
    except Exception as e:  # noqa: BLE001
        return err("电商模块没加载上：%s: %s" % (type(e).__name__, e))

    vp = str(args.get("video_path") or "").strip()
    want_apply = bool(args.get("apply"))
    mode = str(args.get("mode") or "start_and_badge")
    start_s = float(args.get("start_seconds") or 3.0)
    stext = str(args.get("start_text") or EV.DEFAULT_START_TEXT)
    btext = str(args.get("badge_text") or EV.DEFAULT_BADGE_TEXT)

    if not vp or not want_apply:
        # 只出清单 + 方案（不动文件）
        return ok(checklist=EV.ai_labeling_checklist(),
                  plan=EV.ai_label_plan(video_path=vp, mode=mode, start_seconds=start_s,
                                        start_text=stext, badge_text=btext),
                  batch_risk=EV.batch_upload_risk(),
                  pitfalls=EV.ai_generation_pitfalls()[:6],
                  note=("这是清单与方案，**没有动文件**。要真烧传 apply=true + video_path。"
                        "L4「发布时主动声明」只能用户自己做。"))

    try:
        r = EV.apply_ai_label(vp, out_path=str(args.get("out_path") or ""),
                              mode=mode, start_seconds=start_s,
                              start_text=stext, badge_text=btext,
                              font_path=str(args.get("font_path") or ""))
    except Exception as e:  # noqa: BLE001
        logger.exception("apply_ai_label failed")
        return err("打 AI 标识失败：%s: %s" % (type(e).__name__, e))
    if not r.get("ok"):
        return err("打 AI 标识失败：%s" % (r.get("error") or "未知错误"))
    return ok(**r)


@REGISTRY.register(
    "ecommerce_rewrite_selling_points",
    "把**空泛形容词卖点**改写成可拍摄的画面或可读的数字（电商方法论核心："
    "能用镜头表达的，就不要用嘴说）。例如「质量特别好」→「拍洗 30 次后的实拍」、"
    "「效果很明显」→「使用前 vs 使用 7 天后，同机位同光位」。"
    "已含数字的不动；是「主张但没数字」的会提示补数字。纯本地、免费。",
    {"selling_points": {"type": "array", "desc": "卖点数组（必填）", "required": True},
     "project_id": {"type": "string", "desc": "项目 id（可选）"}},
    needs_project=False,
)
async def t_ecommerce_rewrite_selling_points(args: Dict[str, Any]) -> ToolResult:
    bad = _bootstrap()
    if bad:
        return bad
    try:
        from . import ecommerce_video as EV
    except Exception as e:  # noqa: BLE001
        return err("电商模块没加载上：%s: %s" % (type(e).__name__, e))
    sps = args.get("selling_points")
    if isinstance(sps, str):
        sps = [x.strip() for x in sps.replace("\n", "、").split("、") if x.strip()]
    if not isinstance(sps, list) or not sps:
        return err("必须给 `selling_points`（数组或顿号分隔的字符串）。")
    try:
        r = EV.rewrite_selling_points([str(x) for x in sps])
    except Exception as e:  # noqa: BLE001
        logger.exception("rewrite_selling_points failed")
        return err("卖点改写失败：%s: %s" % (type(e).__name__, e))
    return ok(**r,
              note=("改写是**方向建议**，落地时要换成你真实能拍/能测的内容，别照抄示例。"
                    "still_vague 里的需要人工补一个可验证的事实。"))


# -*- coding: utf-8 -*-
"""
白膜 3D 预演 agent 工具（Blender）
====================================

跟 core/previz_blender.py 是同一套引擎，这里只是把它暴露成 agent 能调的三个动作：

  · `previz_status`              Blender 在不在（不渲染、不花钱）
  · `previz_render`              把分镜渲成白膜（本地，**零费用**）
  · `previz_asset_cards`         从白膜结论导出角色卡/场景卡内容

为什么值得做成 agent 工具：
  白膜是「渲染视频前的 3D 预演」，它的价值是**在花钱之前**发现
  比例失真 / 道具漏画 / 站位打架 / 运镜不成立。agent 能自己跑一轮预演
  再决定要不要出片，就避免了在正式生成上试错。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .agent_tools import REGISTRY, err, ok
from . import previz_blender as pb

logger = logging.getLogger("videoforge.previz_blender_tools")


@REGISTRY.register(
    "previz_status",
    "查白膜 3D 预演引擎（Blender）在不在、在哪。**只读，不渲染、不花钱。**"
    "用户问「白膜能不能用」时先调它。",
    {},
)
async def t_previz_status(args: Dict[str, Any]) -> Any:
    exe = pb.blender_path()
    # ★ 2026-10-25：白膜默认通道是 openVideoForge（MCP）；这里如实报「哪条路现在能用」，
    #   免得用户按旧认知以为必须装 Blender。probe() 只读，不渲染、不动页面。
    ch = "blender"
    wb = ""
    try:
        from core import previz_mcp as _mc
        ch = _mc.channel()
        if ch != "blender":
            pr = _mc.probe()
            wb = ("✓ 白膜工作台可用（%s）" % _mc.bridge_url() if pr.get("ok")
                  else "白膜工作台当前不可用：%s" % (pr.get("reason") or "未知原因"))
    except Exception as e:  # noqa: BLE001
        wb = "白膜工作台自检异常：%s: %s" % (type(e).__name__, e)
    return ok(available=bool(exe), exe=exe, channel=ch, workbench=wb,
              note=("✓ Blender 可用，可以出白膜 3D 预演（本地渲染，零费用）" if exe
                    else "未找到 blender.exe。白膜 3D 预演需要 Blender 4.2+；"
                         "装好后设环境变量 VIDEOFORGE_BLENDER 指向 blender.exe。"))


@REGISTRY.register(
    "previz_render",
    "把项目的分镜渲成**白膜 3D 预演**（Blender 本地渲染，**零费用**）。\n"
    "白膜 = 渲染视频前的空间预演：谁在哪、多高、什么姿态、哪些道具、谁挡谁、"
    "镜头怎么动。用来在**花钱出正式片之前**先验一遍比例和站位。\n"
    "  · `shot_indices` 可选，只渲指定几镜（如 [3, 8]）；不填则按设计表全渲。\n"
    "  · 产出是每镜的抽帧 PNG，用来看构图和走位，不是成片。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True},
     "shot_indices": {"type": "string",
                      "desc": "可选，如 \"3,8\"（逗号分隔的第 N 镜）", "required": False}},
)
async def t_previz_render(args: Dict[str, Any]) -> Any:
    from .main import db  # 运行时拿，避免循环依赖
    pid = str(args.get("project_id") or "").strip()
    if not pid:
        return err("缺少 project_id")
    only = None
    raw = str(args.get("shot_indices") or "").strip()
    if raw:
        only = []
        for tok in raw.replace("，", ",").split(","):
            tok = tok.strip()
            if tok.isdigit():
                only.append(int(tok))
        if not only:
            return err(f"shot_indices 解析不出镜号：{raw}")

    if not pb.blender_available():
        return err("没找到 blender.exe，无法出白膜 3D 预演。"
                   "装 Blender 4.2+ 或设环境变量 VIDEOFORGE_BLENDER。")

    import asyncio as _a
    r = await _a.wait_for(
        _a.to_thread(pb.render_project_previz, db, pid, only=only), timeout=900)
    if not r.get("ok"):
        return err(r.get("reason") or "白膜渲染失败")
    n = sum(len(s["frames"]) for s in r["shots"])
    return ok(out_dir=r["out_dir"], shots=len(r["shots"]), frames=n,
              paths=[f for s in r["shots"] for f in s["frames"]][:40],
              note=f"白膜已渲出 {n} 张（{len(r['shots'])} 镜），零费用。"
                   f"目录：{r['out_dir']}")


@REGISTRY.register(
    "previz_asset_link",
    "把白膜里的角色/场景与**资产库**做一一对应，并输出资产锚点。\n"
    "★ 用途：这是连续剧的命门 —— 第 1 集建的「陈砚」角色卡，第 5 集拍陈砚时必须\n"
    "  还是同一张脸、同一件短褗，否则观众认不出是同一个人。\n"
    "  白膜里的「陈砚」原本只是一个灰模方块，与资产库的 id **没有任何链接**；\n"
    "  这个工具把链补上，并把每个资产压成 6~20 汉字的**短锚点**，\n"
    "  例如 `@陈砚（湿透的灰褐短褗、右眉尾旧疤）` —— 写进提示词，\n"
    "  模型就有了稳定的身份依据。\n"
    "  · 会如实报出「白膜有但资产没有」和「资产有但白膜没用」两边的不一致；\n"
    "  · **不改名、不造卡**（用户要求按剧本、不得改名）。\n"
    "  · 不花钱。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True}},
)
async def t_previz_asset_link(args: Dict[str, Any]) -> Any:
    from .main import db  # 运行时拿
    pid = str(args.get("project_id") or "").strip()
    if not pid:
        return err("缺少 project_id")
    try:
        from core import asset_link as _al
        links = _al.link(db, pid)
    except Exception as e:  # noqa: BLE001
        return err(f"建立资产对应失败：{type(e).__name__}: {e}")
    return ok(
        ready=links["ready"],
        characters=links["characters"],
        scenes=links["scenes"],
        missing=[x["name"] for x in links["missing"]],
        unused_assets=links["unused_assets"],
        no_image=links["no_image"],
        anchors=_al.anchors_block(links),
        table_md=_al.anchor_table_md(links),
        note=links["note"],
    )


@REGISTRY.register(
    "previz_asset_cards",
    "从白膜的 3D 预演结论，导出**角色卡 / 场景卡**的文字设定"
    "（真实身高、体型、姿态、常驻道具、唯一光源、场景尺寸）。\n"
    "  · 用途：把这些写进资产库，480p 出片时文字设定就和预演一致，"
    "不会体型失真。\n"
    "  · **只产出内容，不写库**（写库要走对应的创建接口）。\n"
    "  · 不花钱、不花钱、不花钱。",
    {},
)
async def t_previz_asset_cards(args: Dict[str, Any]) -> Any:
    try:
        d = pb._load_design("")  # noqa: SLF001
        chars = pb.design_to_character_cards(d)
        scenes = pb.design_to_scene_cards(d)
    except Exception as e:  # noqa: BLE001
        return err(f"导出白膜资产卡失败：{type(e).__name__}: {e}")
    return ok(characters=chars, scenes=scenes,
              n_characters=len(chars), n_scenes=len(scenes),
              note="这些是**待写入**的角色卡/场景卡内容。要落库请用"
                   "「创建角色 / 创建场景」接口把它们传进去。")

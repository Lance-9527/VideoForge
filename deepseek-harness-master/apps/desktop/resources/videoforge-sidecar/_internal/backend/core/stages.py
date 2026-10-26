# -*- coding: utf-8 -*-
"""VideoForge · 制作流水线阶段表（"一步步走完"的骨架）

## 为什么需要这个文件

改造前，agent 是一个**自由发挥的工具循环**：模型看到 27 个工具，自己决定调哪个、
调几次。用户感受到的问题是：

  1. **不知道走到哪一步了** —— 问"现在什么进度"，模型只能罗列 db 字段，
     说不出"还差哪几步"。
  2. **会一口气跑完** —— 模型可以连着调 `generate_script` → `plan_shots` →
     `generate_preview`，几轮就把钱花了，用户根本没机会看一眼。
     实测真的发生过：一轮 12 步全花在生成上。
  3. **花的钱看不见** —— 用户不知道下一步是免费的白膜还是要花钱的 480P 样片。

所以这里把"做一部片子"固化成一张**有顺序、有前置、标了花费的阶段表**，
每一步都能：

  · 用**数据库真实状态**判定"做完了没有"（`evidence`），不靠模型记忆；
  · 说清**这一步要花什么**（`cost` / `eta`）；
  · 需要用户点头的步骤**必须**先 `propose_step` 再 `confirm_step`（`gate`）。

## 与工具的对应关系

每个阶段绑定一个 agent 工具名（`tool`）。`gate=True` 的阶段，
agent 直接调用那个工具会被 `core.agent.execute_tool` **拦下来**，
要求它先走 `propose_step` —— 见 `GATED_TOOLS`。

## 判定为什么必须查库

"剧本生成了没有"如果让模型自己记，它会在多轮对话后记错，
然后重复生成、重复扣费。`evidence()` 每次都重新查一遍真实数据，
所以在任意一轮、任意一个新会话里，进度都是对的。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("videoforge.stages")


# ──────────────────────────── 花费档位 ────────────────────────────

COST_LABEL = {
    "free": "免费（本地跑，不花钱）",
    "cheap": "很少的钱（一次文字模型调用）",
    "image": "按张计费（图像模型）",
    "video": "按条计费（视频模型，明显花钱）",
    "expensive": "最贵的一步（1080P 成片）",
}


@dataclass
class Stage:
    """一个制作阶段。"""

    key: str
    title: str
    tool: str                       # 绑定的 agent 工具名（真正干活的那个）
    why: str                        # 为什么要做这一步（给用户看的一句话）
    cost: str = "free"              # COST_LABEL 的键
    eta: str = ""                   # 大致耗时，给人一个预期
    needs: List[str] = field(default_factory=list)   # 前置阶段 key
    gate: bool = True               # True = 必须先由用户确认才能执行
    # 用户在这一步最可能想改什么（前端"修改"按钮把它渲染成快捷输入）
    tweaks: List[str] = field(default_factory=list)

    @property
    def cost_label(self) -> str:
        """给人看的花费说明。

        ★ 必须是 property：`proposal_text()` 直接读 `st.cost_label`，
          而它以前只在 `as_dict()` 里拼出来 —— 于是提案卡片在生成时
          抛 AttributeError，用户看到的是"登记提案失败"。
          （实测踩到：13 个闸门用例里有 9 个是这个原因挂的。）
        """
        return COST_LABEL.get(self.cost, self.cost)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key, "title": self.title, "tool": self.tool,
            "why": self.why, "cost": self.cost,
            "cost_label": self.cost_label,
            "eta": self.eta, "needs": list(self.needs),
            "gate": self.gate, "tweaks": list(self.tweaks),
        }


# ──────────────────────────── 阶段表 ────────────────────────────
#
# ★ 顺序就是列表顺序。改顺序 = 改产品流程，请连 evidence 一起想清楚。

STAGES: List[Stage] = [
    Stage(
        "brief", "立项", "create_project",
        "先有一个项目，后面所有东西都挂在它下面。",
        cost="free", eta="瞬间",
        gate=False,
        tweaks=["改片名", "改一句话简介"],
    ),
    Stage(
        "script", "剧本", "generate_script",
        "剧本决定了故事、场次和人物，后面分镜是按场次拆的。"
        "这一步用你的想法生成大纲；你自己有剧本的话也可以直接贴进来导入。",
        cost="cheap", eta="30 秒 ~ 2 分钟",
        needs=["brief"],
        tweaks=["换个题材", "改结局", "加一个角色", "缩短到 3 场戏"],
    ),
    Stage(
        "assets", "角色 / 场景", "extract_assets",
        "从剧本里抽出有哪些角色、哪些场景，建成可复用的资产。"
        "后面每个镜头都靠引用它们来保持一致。",
        cost="cheap", eta="30 秒 ~ 1 分钟",
        needs=["script"],
        tweaks=["合并两个重复的角色", "给主角加个外号"],
    ),
    Stage(
        "looks", "定妆照 / 场景图", "make_looks",
        "每个角色和场景都要有一张参考图。**没有参考图，视频模型就锁不住长相** ——"
        "同一个角色在不同镜头会变成不同的人。这是「看起来像一部片」的地基。",
        cost="image", eta="每张 1 ~ 4 分钟",
        needs=["assets"],
        tweaks=["只做主角", "改成写实风格", "换成国风", "重画第 2 个角色"],
    ),
    Stage(
        "shots", "分镜表", "plan_shots",
        "把剧本拆成一个个镜头，并建立镜头 ↔ 角色/场景 的引用。"
        "有了分镜才能排片、渲白膜、出片。",
        cost="free", eta="瞬间",
        needs=["script"],
        tweaks=["拆细一点", "合并一些镜头", "每个镜头改成 5 秒"],
    ),
    Stage(
        "previz", "白膜预演", "render_previz",
        "★ 这是**花任何钱之前**唯一能改运镜的机会：本地渲一段白模视频，"
        "先把构图、机位、节奏定下来。免费、可以无限次重来。",
        cost="free", eta="每个镜头 1 ~ 3 分钟",
        needs=["shots"],
        tweaks=["换个机位", "改成推镜", "节奏快一点", "加一个俯拍"],
    ),
    Stage(
        "draft", "480P 小样", "generate_preview",
        "用便宜档位出一版小样，确认「脸对不对、动作对不对」。"
        "小样不满意就驳回，别急着出成片 —— 成片贵得多。",
        cost="video", eta="每条 2 ~ 8 分钟",
        needs=["previz"],
        tweaks=["再来一版", "换模型", "这一段重出"],
    ),
    Stage(
        "final", "1080P 成片", "generate_final",
        "出最终画质的镜头。**这是整个流程里最贵的一步**，"
        "所以默认要求你先看过小样、确认过了再走。",
        cost="expensive", eta="每条 5 ~ 15 分钟",
        needs=["draft"],
        tweaks=["只出这一条", "全部出成片"],
    ),
    Stage(
        "compose", "后期成片", "compose_film",
        "把出好的镜头拼成一条完整片子，带转场、可选字幕。"
        "未出片的镜头会被跳过并如实告诉你。",
        cost="free", eta="1 ~ 5 分钟",
        needs=["final"],
        tweaks=["加字幕", "转场全用溶解", "只拼前 3 个镜头", "改文件名"],
    ),
]

STAGE_BY_KEY: Dict[str, Stage] = {s.key: s for s in STAGES}
STAGE_ORDER: List[str] = [s.key for s in STAGES]

# 必须"先 propose 再 confirm"的工具（由 agent 循环强制执行，不只是提示词）
GATED_TOOLS: set = {s.tool for s in STAGES if s.gate}

# 这些工具不在这张表里，但同样属于"会花钱/会改数据"，一并纳入闸门。
EXTRA_GATED_TOOLS = {
    "generate_preview", "generate_final",   # 已在表内，留着是为了显式
    "kling_generate", "kling_white_model",  # 可灵会花灵感值
    "make_character_look", "make_scene_look",
    "refine_look",                          # 指令式改图
}
GATED_TOOLS |= EXTRA_GATED_TOOLS

#: 某一步执行时**顺带**允许调用的工具（多资产阶段会调单资产工具）。
#: 放在这里而不是 `agent.py`，是因为"工具 ↔ 阶段"的对应关系现在有**两个**用处：
#:   ① 闸门放行判断（`agent._TOOLS_OF_STAGE`）
#:   ② 被拦下时**替模型登记提案**（`tool_stage`）—— 见 2026-09-27 第 71 轮
#: 两处各写一份必然会漂，所以单一真相源放在这里。
SIDE_TOOLS: Dict[str, set] = {
    "looks": {"make_character_look", "make_scene_look", "refine_look"},
    "draft": {"generate_preview"},
    "final": {"generate_final"},
    "previz": {"render_previz"},
    "compose": {"compose_film"},
    "script": {"generate_script", "import_script"},
}


def tool_stage(name: str) -> str:
    """这个工具属于**哪一步**；不属于任何一步就返回空串。

    ★ 2026-09-27 第 71 轮加。用途只有一个但很关键：
      被闸门拦下时，要能**替模型把这一步登记成待确认提案**（见
      `agent._gate_check` / `run_agent` 的 `_auto_propose_for_blocked`）——
      那需要知道"用户其实想推进的是哪一步"。
    """
    for s in STAGES:
        if s.tool == name:
            return s.key
    for k, tools in SIDE_TOOLS.items():
        if name in tools:
            return k
    return ""


# ──────────────────────────── 真实性判定 ────────────────────────────


def _json_list(v: Any) -> List[Any]:
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip():
        try:
            x = json.loads(v)
            return x if isinstance(x, list) else []
        except Exception:
            return []
    return []


def _candidate_was_rejected(candidate: Any) -> bool:
    """Return True when a generated clip was retained only as audit evidence.

    A rejected/unverified Seedance result may remain on disk so the user can
    inspect what was paid for.  Its mere presence must never advance the
    production stage to "done".
    """
    if not isinstance(candidate, dict):
        return True
    status = str(candidate.get("validation_status") or "").strip().lower()
    if status in ("rejected", "failed", "unverified"):
        return True
    delivery = candidate.get("delivery")
    if isinstance(delivery, dict) and delivery:
        delivery_status = str(delivery.get("status") or "").strip().lower()
        if delivery.get("ok") is False or delivery_status in ("rejected", "failed", "unverified"):
            return True
    return bool(candidate.get("requires_user_review") and candidate.get("no_fallback"))


def _ev_brief(db, pid: str) -> Dict[str, Any]:
    p = db.get_project(pid)
    if not p:
        return {"done": False, "detail": "项目不存在"}
    return {"done": True, "detail": f"项目「{p.get('title')}」已建立", "title": p.get("title")}


def _ev_script(db, pid: str) -> Dict[str, Any]:
    sc = db.get_script(pid)
    if not sc:
        return {"done": False, "detail": "还没有剧本"}
    has = bool((sc.get("outline") or "").strip()) or bool(_json_list(sc.get("scenes")))
    n = len(_json_list(sc.get("scenes")))
    if not has:
        return {"done": False, "detail": "剧本记录是空的"}
    return {"done": True, "detail": f"已有剧本（{n} 场戏）" if n else "已有剧本大纲",
            "scenes": n}


def _ev_assets(db, pid: str) -> Dict[str, Any]:
    chars = db.list_characters(pid) or []
    scenes = db.list_scenes(pid) or []
    n = len(chars) + len(scenes)
    if not n:
        return {"done": False, "detail": "还没有角色/场景资产"}
    return {"done": True,
            "detail": f"{len(chars)} 个角色、{len(scenes)} 个场景",
            "characters": len(chars), "scenes": len(scenes)}


def _ev_looks(db, pid: str) -> Dict[str, Any]:
    """定妆照/场景图：**每一个**角色和场景都得有图才算完。"""
    chars = db.list_characters(pid) or []
    scenes = db.list_scenes(pid) or []
    total = len(chars) + len(scenes)
    if not total:
        return {"done": False, "detail": "还没有角色/场景，先做上一步",
                "missing": [], "total": 0, "have": 0}
    missing = [c.get("name") for c in chars if not (c.get("reference_image_path") or "").strip()]
    missing += [s.get("name") for s in scenes if not (s.get("reference_image_path") or "").strip()]
    have = total - len(missing)
    if missing:
        return {"done": False,
                "detail": f"{have}/{total} 有图，还缺：{'、'.join(str(x) for x in missing[:8])}"
                          + ("…" if len(missing) > 8 else ""),
                "missing": missing, "total": total, "have": have}
    return {"done": True, "detail": f"{total} 个资产全部有参考图",
            "missing": [], "total": total, "have": total}


def _ev_shots(db, pid: str) -> Dict[str, Any]:
    """分镜这一步到底做完没有 —— **必须看"有没有挂上角色/场景"**。

    ★★ 2026-09-28 第 73 轮（用户原话：「整个流程没法跑通」）。

    实测《执棋者》的真实现状：

        流水线九步：全部 done（界面上看起来"全跑完了"）
        分镜 5 个：  角色引用 0 / 0 / 0 / 0 / 1，场景引用全空
        角色表却有 7 个角色

    也就是说原来这里**只看"有没有分镜"就判 done**，哪怕一个角色都没挂上。
    后果是链式的：出片时 `orchestrator` 从 `character_ids` 取不到参考图 ⇒
    提示词里没有人物锚点 ⇒ 用户说的「每个镜头一张脸」；白膜也不知道这一镜有谁。
    而这正是用户反复撞到的那个形状：**报成功，但其实没做**。

    现在的判据（刻意保守，不误伤正常项目）：
      · 项目里**一个角色都没有** → 谈不上关联，照旧按"有分镜"判（空景短片合法）；
      · **所有**分镜都没挂上任何角色（而项目有角色）→ **不算做完**，
        并明确告诉你去调 `relink_shots`；
      · 只有一部分没挂上 → 仍算做完，但把数量摆出来（真实情况如实报）。
    """
    shots = db.list_shots(pid) or []
    if not shots:
        return {"done": False, "detail": "还没有分镜"}
    no_char = sum(1 for s in shots if not _json_list(s.get("character_ids")))
    n_scene = sum(1 for s in shots if not s.get("scene_id"))
    try:
        chars = db.list_characters(pid) or []
    except Exception:  # noqa: BLE001
        chars = []

    # ★ 更准的一层：**剧本里这一场明明有人物，分镜却没挂上任何角色卡** ⇒
    #   那是"名字对不上"导致的关联失败（不是空景）。这种镜头一定会
    #   "每个镜头一张脸"，所以不能算做完。
    unresolved: List[int] = []
    try:
        from core import relink as _rl
        idx = _rl.build_link_index(db, pid)
        script = db.get_script(pid) or {}
        scenes = script.get("scenes")
        if isinstance(scenes, str):
            scenes = json.loads(scenes)
        scenes = scenes if isinstance(scenes, list) else []
        tlp_all = script.get("three_layer_prompts") or {}
        for s in shots:
            if _json_list(s.get("character_ids")):
                continue
            oi = s.get("order_index") or 0
            scene = scenes[oi] if oi < len(scenes) else {}
            num = (scene or {}).get("scene_number") or (oi + 1)
            detail = idx["details_map"].get(num) or {}
            names = []
            for src in (detail.get("characters"), (scene or {}).get("characters")):
                if isinstance(src, list) and src:
                    names = [str((x.get("name") if isinstance(x, dict) else x) or "").strip()
                             for x in src]
                    names = [n for n in names if n]
                    if names:
                        break
            if names:
                unresolved.append(oi + 1)
    except Exception as e:  # noqa: BLE001
        logger.warning("判「剧本有人物却没挂上」失败（退回粗判）：%s", e)

    if chars and (no_char == len(shots) or unresolved):
        why = (f"{len(unresolved)} 个镜头在**剧本里明明有人物**，却没挂上任何角色卡"
               f"（第 {('、'.join(str(x) for x in unresolved[:8]))} 镜）——"
               f"多半是分镜里的名字和「角色」页的名字对不上"
               if unresolved else
               f"{len(shots)} 个镜头**一个都没关联角色**（项目里有 {len(chars)} 个角色）")
        return {
            "done": False,
            "detail": (why + "。这样出片拿不到参考图，会「每个镜头一张脸」。"
                             "调 relink_shots 重新关联；对不上时它会告诉你差在哪"),
            "shots": len(shots), "without_character": no_char,
            "without_scene": n_scene, "unresolved": unresolved,
            "fix": "relink_shots",
        }
    d = f"{len(shots)} 个镜头"
    if no_char:
        d += f"（其中 {no_char} 个没关联角色）"
    if n_scene:
        d += f"（{n_scene} 个没关联场景）"
    return {"done": True, "detail": d, "shots": len(shots),
            "without_character": no_char, "without_scene": n_scene}


#: 「白膜这一步算做过了」的状态集合。
#:
#: ★★ 2026-09-29（第 9 条巡检时抓到的真 bug）：
#:   `review_previz` 同意白膜时写的是 `draft_approved`，而这里原来只认
#:   `("draft_ready", "approved")` —— **白膜明明确认过了，判据却不认**。
#:   后果正是用户抱怨的那条：「重复问用户定妆/白膜，问完不办事」——
#:   每次巡检都说"还有 N 个镜头没白膜"，于是又提一次提案。
#:   现在把"确认过"的各种写法都认下来（写成集合，往后加值不用改判据）。
PREVIZ_OK_STATES = ("draft_ready", "draft_approved", "approved", "final_ready")


def _ev_previz(db, pid: str) -> Dict[str, Any]:
    shots = db.list_shots(pid) or []
    if not shots:
        return {"done": False, "detail": "还没有分镜"}
    # ★ 白膜"做没做过"要看**两件事**：状态被标过，**或者**真的有一个白膜文件。
    #   只看状态会漏（历史数据状态不一），只看文件会把"渲了但没确认"当成已完成。
    ok = []
    for s in shots:
        stt = str(s.get("previz_status") or "none")
        pv = str(s.get("previz_path") or "")
        if stt in PREVIZ_OK_STATES or (pv and os.path.exists(pv)):
            ok.append(s)
    if not ok:
        return {"done": False, "detail": f"0/{len(shots)} 个镜头渲了白膜",
                "total": len(shots), "have": 0}
    return {"done": len(ok) >= len(shots),
            "detail": f"{len(ok)}/{len(shots)} 个镜头有白膜",
            "total": len(shots), "have": len(ok),
            # 有多少是"看过并同意"的（区别于"只是渲出来了"）
            "approved": len([s for s in shots
                             if str(s.get("previz_status") or "") in PREVIZ_OK_STATES])}


def _ev_draft(db, pid: str) -> Dict[str, Any]:
    r"""480P 小样：**必须真有"小样"这一档的片段**，不能拿任何片段充数。

    ★★ 2026-09-29（第 9 条巡检抓到的第二个真 bug）：
      原来只要 `candidates` **非空**就算"出过样片"。而用户的候选里是
      分镜重建后**认回的片段** + **配音版** —— 一版 480P 都没出过，
      判据却说"480P 已完成"，于是 `final` 那一步理直气壮地说
      「给 5 个**出过小样**的镜头出 1080P 成片」。
      用户看到的后果：**它让你直接出最贵的成片，而便宜的小样根本没做过** ——
      正是他最在意的"顺序不对、白花冤枉钱"。

    判据改成看**片段的来源**（`source`）：带 preview/480/draft 字样的才算小样。
    认回的、配音的都不算 —— 它们不是"以小样价试出来的那一版"。
    """
    shots = db.list_shots(pid) or []
    if not shots:
        return {"done": False, "detail": "还没有分镜"}
    n = 0
    rejected = 0
    for s in shots:
        has_rejected = False
        for c in (_json_list(s.get("candidates")) or []):
            if _candidate_was_rejected(c):
                src = str((c or {}).get("source") or "").lower()
                has_rejected = has_rejected or ("preview" in src or "480" in src or "draft" in src)
                continue
            src = str((c or {}).get("source") or "").lower()
            if "preview" in src or "480" in src or "draft" in src:
                n += 1
                break
        if has_rejected:
            rejected += 1
    if not n:
        detail = f"0/{len(shots)} 个镜头有通过验收的样片"
        if rejected:
            detail += f"；另有 {rejected} 个镜头已生成但一致性未通过，可播放/下载查看真实结果"
        return {"done": False, "detail": detail,
                "total": len(shots), "have": 0, "generated_rejected": rejected}
    return {"done": True, "detail": f"{n}/{len(shots)} 个镜头出过片段",
            "total": len(shots), "have": n, "generated_rejected": rejected}


def _ev_final(db, pid: str) -> Dict[str, Any]:
    r"""1080P 成片：**必须真有一版"成片"档的片段**。

    ★★ 2026-09-29（与 `_ev_draft` 同一类 bug，第 9 条巡检时一并抓出）：
      原来只看 `final_approved` 这个**标记位**。而标记位会滞后于现实 ——
      用户的第 1 镜带着 `final_approved=1`（早先流程留下的），
      磁盘上却**一版 1080P 都没有**。判据于是说「1/5 个镜头已出成片」，
      用户看到"已经做过一版成片了"，而实际什么都没有。

    判据改成看**片段来源**：带 final/1080/upgrade 字样的才算成片；
    标记位只作为**辅助**（有片段但没标记 → 也认，因为文件不会骗人；
    有标记但没片段 → **不认**）。
    """
    shots = db.list_shots(pid) or []
    if not shots:
        return {"done": False, "detail": "还没有分镜"}
    n = 0
    rejected = 0
    flagged_only = 0
    by_file = 0          # ★ 靠"问文件本身"认出来的（库里没有来源记录）
    for s in shots:
        has_clip = False
        has_rejected = False
        for c in (_json_list(s.get("candidates")) or []):
            if _candidate_was_rejected(c):
                src = str((c or {}).get("source") or "").lower()
                has_rejected = has_rejected or ("final" in src or "1080" in src or "upgrade" in src)
                continue
            src = str((c or {}).get("source") or "").lower()
            if "final" in src or "1080" in src or "upgrade" in src:
                has_clip = True
                break
        if not has_clip:
            # ★★★ 2026-09-29（用户第 9 条 + **差点让他白花一笔钱**）
            #
            #   实测：他 5 个镜头的片段全都标着 `recovered`（来源记录丢了），
            #   于是这里一路判「0/5 出过成片」，**反复催他再花钱出 1080P**。
            #   可 ffmpeg 一问：**全都是 1920x1088** —— 片子早就是这个画质了。
            #
            #   所以来源字段查不到时，**去问文件本身**：只要磁盘上那一版
            #   实际就是 1080P 档，那就是成片（只是库里的记录丢了）。
            #   这比"宁可判没做、让用户再花钱"要对得多。
            for c in (_json_list(s.get("candidates")) or []):
                if _candidate_was_rejected(c):
                    continue
                p = str((c or {}).get("path") or "")
                if not p or not os.path.exists(p):
                    continue
                try:
                    from core import media_assets as _MA
                    if _MA.tier_by_resolution(p) == "1080P 成片":
                        has_clip = True
                        by_file += 1
                        break
                except Exception:  # noqa: BLE001
                    pass
        if has_clip:
            n += 1
        elif has_rejected:
            rejected += 1
        elif s.get("final_approved"):
            # 只有标记、没有片段 —— 如实计入"标记过期"，不算完成
            flagged_only += 1
    note = (f"（其中 {by_file} 个是**按实际分辨率**认定的 —— 库里没有来源记录，"
            f"但文件确实是 1080P 档）" if by_file else "")
    if not n:
        return {"done": False,
                "detail": (f"0/{len(shots)} 个镜头有通过验收的 1080P 成片"
                           + (f"；另有 {rejected} 个镜头已生成但一致性未通过，"
                              f"可播放/下载查看真实结果" if rejected else "")
                           + (f"（另有 {flagged_only} 个镜头带着过期标记，"
                              f"但磁盘上没有成片）" if flagged_only else "")),
                "total": len(shots), "have": 0, "stale_flags": flagged_only,
                "generated_rejected": rejected}
    return {"done": n >= len(shots),
            "detail": f"{n}/{len(shots)} 个镜头已出成片{note}",
            "total": len(shots), "have": n, "stale_flags": flagged_only,
            "by_file": by_file, "generated_rejected": rejected}


def _ev_compose(db, pid: str) -> Dict[str, Any]:
    """成片有没有 —— 直接看文件系统。"""
    try:
        settings = db.get_all_settings() or {}
        out_dir = settings.get("output_dir") or os.path.join(
            os.environ.get("VIDEOFORGE_DATA_DIR", os.getcwd()), "outputs")
        d = os.path.join(str(out_dir), str(pid))
        if not os.path.isdir(d):
            return {"done": False, "detail": "还没有合成过成片"}
        vids = [f for f in os.listdir(d)
                if f.lower().endswith((".mp4", ".mov", ".mkv"))
                and os.path.isfile(os.path.join(d, f))]
        if not vids:
            return {"done": False, "detail": "还没有合成过成片"}
        vids.sort(key=lambda f: os.path.getmtime(os.path.join(d, f)), reverse=True)
        newest = vids[0]
        return {"done": True,
                "detail": f"已有成片 {newest}",
                "file": os.path.join(d, newest),
                "size_mb": round(os.path.getsize(os.path.join(d, newest)) / 1048576, 1),
                "files": vids[:5]}
    except Exception as e:
        return {"done": False, "detail": f"查成片失败：{e}"}


EVIDENCE: Dict[str, Callable[[Any, str], Dict[str, Any]]] = {
    "brief": _ev_brief,
    "script": _ev_script,
    "assets": _ev_assets,
    "looks": _ev_looks,
    "shots": _ev_shots,
    "previz": _ev_previz,
    "draft": _ev_draft,
    "final": _ev_final,
    "compose": _ev_compose,
}


# ──────────────────────────── 进度计算 ────────────────────────────


def load_skips(db, pid: str) -> Dict[str, Any]:
    """读这个项目**明确跳过**了哪些阶段（用户按「这步跳过」记下来的）。

    ★ 2026-09-25 修一个真缺陷：`skip_step` / `/stage/skip` 一直在往
      settings 里写 `stage_skips`，但 `stage_states()` **从来没读它** ——
      结果是"跳过"**实际上没跳过**：界面上点了跳过、提示"已跳过"，
      下一步还是那一步，`next_stage_key` 返回的还是它，
      用户再点确认又被 409 挡回来。

      存了不用 = 假功能，比没有更糟（用户以为跳过了）。
    """
    try:
        s = db.get_all_settings() or {}
        all_skips = s.get("stage_skips") or {}
        if isinstance(all_skips, str):
            import json as _j
            all_skips = _j.loads(all_skips or "{}")
        if not isinstance(all_skips, dict):
            return {}
        per = all_skips.get(str(pid)) or {}
        return per if isinstance(per, dict) else {}
    except Exception as e:  # noqa: BLE001
        logger.debug("读跳过记录失败（当作没跳过）：%s", e)
        return {}


def stage_states(db, pid: str, skips: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """算出每个阶段的真实状态。

    状态取值：
        done     已完成（库里有证据）
        skipped  用户明确跳过（不再挡后面的步骤）
        current  下一个该做的（前置都满足了）
        gate     待用户确认（有 pending 提案指着它）
        waiting  前置没满足，还轮不到
    """
    pend = pending_gate(pid)
    pend_stage = (pend or {}).get("stage") if pend else None
    if skips is None:
        skips = load_skips(db, pid)

    out: List[Dict[str, Any]] = []
    # ★ "前面都做完了/跳过了"才轮到这一步。
    #   跳过算"放行" —— 用户说这步不用做，就不该拿它挡住后面的。
    prereq_ok = True
    current_assigned = False

    for st in STAGES:
        try:
            ev = EVIDENCE[st.key](db, pid)
        except Exception as e:
            logger.warning("evidence %s 失败：%s", st.key, e)
            ev = {"done": False, "detail": f"判定失败：{e}"}

        skipped = st.key in skips
        if ev.get("done"):
            status = "done"
        elif skipped:
            status = "skipped"
        elif pend_stage == st.key:
            status = "gate"
            current_assigned = True
        elif prereq_ok and not current_assigned:
            status = "current"
            current_assigned = True
        else:
            status = "waiting"

        row = st.as_dict()
        row.update({"status": status, "evidence": ev})
        if skipped:
            row["skip_reason"] = str((skips.get(st.key) or {}).get("reason") or "")

        # 前置检查（只提示，不阻断 —— 允许用户跳步）
        #   ★ 被跳过的前置**不算缺**：用户已经决定不做了。
        missing = [k for k in st.needs
                   if not EVIDENCE[k](db, pid).get("done") and k not in skips]
        row["missing_prereq"] = missing
        out.append(row)

        if not ev.get("done") and not skipped and prereq_ok:
            prereq_ok = False

    return out


def summary(db, pid: str) -> Dict[str, Any]:
    """一句话进度 + 下一步。给 agent 和界面共用。"""
    skips = load_skips(db, pid)
    states = stage_states(db, pid, skips)
    done = [s for s in states if s["status"] == "done"]
    skipped = [s for s in states if s["status"] == "skipped"]
    cur = next((s for s in states if s["status"] in ("current", "gate")), None)
    pend = pending_gate(pid)

    if not cur:
        line = f"全部 {len(STAGES)} 个阶段都走完了（{len(skipped)} 个是跳过的）。"
    else:
        line = (f"已完成 {len(done)}/{len(STAGES)} 步"
                + (f"（另有 {len(skipped)} 步按你的要求跳过）" if skipped else "")
                + f"；下一步是「{cur['title']}」（{cur['cost_label']}）")

    return {
        "project_id": pid,
        "headline": line,
        "done_count": len(done),
        "skipped_count": len(skipped),
        "skipped": [s["key"] for s in skipped],
        "total": len(STAGES),
        "stages": states,
        "next": cur,
        "pending_gate": pend,
    }


def next_stage_key(db, pid: str) -> Optional[str]:
    """下一个该做的阶段。**已被跳过的不会被选上**（否则"跳过"就是假功能）。"""
    skips = load_skips(db, pid)
    cur = next((s for s in stage_states(db, pid, skips)
                if s["status"] in ("current", "gate")), None)
    return cur["key"] if cur else None


def stage_status(db, pid: str, key: str) -> str:
    """某一个阶段现在的状态：done / current / waiting / skipped / ''（不存在）。"""
    try:
        skips = load_skips(db, pid)
        for s in stage_states(db, pid, skips):
            if s["key"] == key:
                return str(s.get("status") or "")
    except Exception:  # noqa: BLE001
        logger.warning("stage_status 取状态失败：%s", key, exc_info=True)
    return ""


def already_done(db, pid: str, key: str) -> bool:
    """这一步**是不是已经做完了** —— 用来区分「第一次做」和「重做（会覆盖）」。

    ★★ 2026-09-27 第 45 轮加。为什么必须有它：

      `pending_gates.json` 是一条**持久**提案，可以挂很久（用户真实库里那条
      挂了 8.8 小时）。而 `/stage/confirm` 原来的逻辑是「有提案就执行那个提案」——
      **没有检查那一步是不是已经做完了**。于是：
        用户 17:42 建了项目 → 01:43 留下一条 `script` 提案 → 期间他若自己
        导入了剧本（或用别的方式让 script 变成 done）→ 回来点「确认」
        ⇒ **重跑 generate_script，把他自己的剧本覆盖掉**。
      这和"没有提案时只允许推进当前这一步"（那条已经有守卫）是**同一个风险**，
      只是走的是"有提案"这条岔路。
    """
    return stage_status(db, pid, key) == "done"


# ──────────────────────────── 待确认闸门（持久化）────────────────────────────
#
# 存成 JSON 文件而不是数据库表：这是一份**临时**状态（用户点确认或走开就失效），
# 不值得为它加一次 schema 迁移。放 data 目录下，重启不丢。

_GATE_LOCK = threading.RLock()
_GATE_TTL = 3600 * 6      # 6 小时后自动过期，避免"上次的提案"莫名其妙被确认

# 进程内缓存。
# ★ 为什么要缓存，而不每次都读文件：
#   ① 闸门检查在**每次**工具调用前都会跑，读文件太频繁；
#   ② 更要紧的 —— 文件偶发读不出来时（权限/占用/路径异常），
#      原来的实现会当作"没有提案"。而"没有提案"的后果是**拦住用户
#      已经确认过的那一步**（fail-closed），用户看到的是"我点了确认却报
#      '没有待确认的提案'"。有缓存就能扛过一次瞬时读失败。
_GATE_CACHE: Dict[str, Any] = {"data": None, "mtime": 0.0}


def _gate_dir() -> str:
    """闸门文件放哪。

    ★ 必须校验 `VIDEOFORGE_DATA_DIR`：实测踩到过把 "http://127.0.0.1:8899"
      这类值塞进来（测试脚本用 dirname(base_url) 当数据目录），
      结果拼出 `http:\\pending_gates.json` 这种路径 —— 不报错，
      只是每次读取都失败、每次写入都静默丢掉，闸门等于不存在。
      宁可退回当前目录，也不要"看起来在工作"。
    """
    root = (os.environ.get("VIDEOFORGE_DATA_DIR") or "").strip()
    if root and "://" not in root and not root.startswith(("http:", "https:")):
        try:
            p = os.path.abspath(os.path.expanduser(root))
            os.makedirs(p, exist_ok=True)
            return p
        except Exception as e:
            logger.warning("VIDEOFORGE_DATA_DIR 不可用（%s），闸门文件退回当前目录：%s",
                           root, e)
    return os.path.abspath(os.getcwd())


def _gate_path() -> str:
    return os.path.join(_gate_dir(), "pending_gates.json")


def _load_all(force: bool = False) -> Dict[str, Any]:
    """读全部项目的提案。带 mtime 缓存，避免每次工具调用都读盘。"""
    p = _gate_path()
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        # 文件不存在 = 真的没有提案（正常情况，不是错误）
        _GATE_CACHE["data"] = {}
        _GATE_CACHE["mtime"] = 0.0
        return {}

    if not force and _GATE_CACHE["data"] is not None and mtime == _GATE_CACHE["mtime"]:
        return _GATE_CACHE["data"]

    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        d = d if isinstance(d, dict) else {}
        _GATE_CACHE["data"] = d
        _GATE_CACHE["mtime"] = mtime
        return d
    except Exception as e:
        # 读失败：**不要**当作"没有提案" —— 那会拦住用户已确认的操作。
        # 用上次的缓存顶上；实在没有缓存才返回空。
        logger.warning("读 pending_gates.json 失败（沿用上次缓存）：%s", e)
        if _GATE_CACHE["data"] is not None:
            return _GATE_CACHE["data"]
        return {}


def _save_all(d: Dict[str, Any]) -> None:
    p = _gate_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
        _GATE_CACHE["data"] = d
        try:
            _GATE_CACHE["mtime"] = os.path.getmtime(p)
        except OSError:
            _GATE_CACHE["mtime"] = 0.0
    except Exception as e:
        # 写失败也要更新内存缓存：本次进程内闸门行为保持一致，
        # 否则"刚提案就查不到"，用户会以为按钮坏了。
        logger.warning("写 pending_gates.json 失败（本进程内仍生效）：%s", e)
        _GATE_CACHE["data"] = d


def set_pending(pid: str, stage: str, args: Optional[Dict[str, Any]] = None,
                note: str = "") -> Dict[str, Any]:
    """登记一个"待用户确认"的阶段提案。"""
    with _GATE_LOCK:
        allg = _load_all(force=True)
        rec = {
            "project_id": pid,
            "stage": stage,
            "args": args or {},
            "note": note,
            "created_at": time.time(),
            "created_str": time.strftime("%H:%M:%S"),
        }
        allg[str(pid)] = rec
        _save_all(allg)
        return rec


def pending_gate(pid: str) -> Optional[Dict[str, Any]]:
    """取当前待确认的提案（过期/不属于该项目的返回 None）。"""
    with _GATE_LOCK:
        rec = _load_all().get(str(pid))
    if not rec:
        return None
    if time.time() - float(rec.get("created_at") or 0) > _GATE_TTL:
        clear_pending(pid)
        return None
    return rec


def clear_pending(pid: str) -> None:
    with _GATE_LOCK:
        allg = _load_all(force=True)
        if str(pid) in allg:
            allg.pop(str(pid), None)
            _save_all(allg)


def consume_pending(pid: str) -> Optional[Dict[str, Any]]:
    """取走并清掉（确认/执行时用，保证一个提案只能被执行一次）。"""
    rec = pending_gate(pid)
    if rec:
        clear_pending(pid)
    return rec


# ──────────────────────────── 生成提案文案 ────────────────────────────


def proposal_text(db, pid: str, stage_key: str,
                  args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把"我要做什么"写成一张卡片（前端渲染 + 模型复述都用它）。"""
    st = STAGE_BY_KEY.get(stage_key)
    if not st:
        return {"ok": False, "error": f"没有这个阶段：{stage_key}"}

    try:
        ev = EVIDENCE[st.key](db, pid)
    except Exception as e:
        ev = {"done": False, "detail": f"判定失败：{e}"}

    missing = [k for k in st.needs if not EVIDENCE[k](db, pid).get("done")]
    missing_titles = [STAGE_BY_KEY[k].title for k in missing]

    args = args or {}
    # 把这一步"会拿什么去做"讲清楚 —— 用户才能判断要不要改
    doing = _doing_text(db, pid, st, args)

    # ★★ 2026-09-28 第 73 轮：如果**上一步其实没做完**（例如分镜没挂上角色），
    #   而用户现在又要做这一步，必须把这件事说出来 —— 否则用户会一路做下去，
    #   拿到一堆"每个镜头一张脸"的片子，还不知道问题出在哪一步。
    _fix_warn = ""
    try:
        for _k in (st.needs or []):
            _ev = EVIDENCE.get(_k, lambda *a: {})(db, pid)
            if _ev.get("fix") and not _ev.get("done"):
                _fix_warn = (f"⚠ 上一步「{STAGE_BY_KEY[_k].title}」其实**没做完**："
                             f"{_ev.get('detail')}")
                break
    except Exception:  # noqa: BLE001
        pass

    # ★★ 2026-09-28（用户第 1、2、7 条）：**把"到底缺什么"写在卡片上**。
    #
    #   实测的死循环：分镜挂的角色卡（兵丁甲/稽查兵丁甲/兵丁乙）**没有图**，
    #   而"定妆照"这一步的判据是"所有卡都有图" ⇒ 永远做不完 ⇒
    #   模型每次又问一遍"要不要做定妆照" ⇒ 用户体验成「重复问用户定妆，问完不办事」。
    #   卡片上把名单列全，用户一眼就知道要生成哪几张、还缺哪几个场景。
    _needs: Dict[str, Any] = {}
    try:
        from core import relink as _rl
        _gap = _rl.assets_gap(db, pid)
        if not _gap.get("ok"):
            _needs = {
                "characters_without_image": _gap.get("characters_without_image") or [],
                "scenes_without_image": _gap.get("scenes_without_image") or [],
                "shots_without_scene": _gap.get("shots_without_scene") or [],
            }
    except Exception as _e:  # noqa: BLE001
        logger.debug("统计缺失资产失败（不影响卡片）：%s", _e)

    # ★★ 2026-09-29（用户第 2、7、9 条）：**花钱之前**先查模型收不收参考图。
    #
    #   真跑实测（未计费，被平台判非法参数）：
    #     InvalidParameter: the specified task_type r2v does not support
    #                       model doubao-seedance-1-0-pro
    #   ⇒ 分镜挂的模型根本不收参考图，资产里的角色图**物理上用不上**；
    #     而能力表里早就写着（tags 里没有「图生视频」）—— 却没人**在花钱前**说。
    #   所以把这条判断放进卡片：用户点「确认」之前就知道"这个模型用不上我的图"，
    #   并且直接拿到"该换成哪个"的建议。
    _model_warn = ""
    _model_fix: Dict[str, Any] = {}
    try:
        if st.key in ("draft", "final", "previz"):
            from core import model_catalog as _mc
            _shots = db.list_shots(pid) or []
            _seen = set()
            for _s in _shots:
                _prov = str(_s.get("model_provider") or "").strip()
                _mid = str(_s.get("model_name") or "").strip()
                if not _mid or (_prov, _mid) in _seen:
                    continue
                _seen.add((_prov, _mid))
                _rs = _mc.reference_support(_prov, _mid)
                if _rs.get("warning"):
                    _model_warn = (f"（本片分镜用的是「{_rs.get('label') or _mid}」）"
                                   + _rs["warning"])
                    # ★ 让卡片能**一键换掉**：把"该换成哪些"也带上（界面据此画按钮），
                    #   用户不用自己去分镜页一个个改。
                    if _rs.get("supports_reference") is False:
                        _model_fix = {
                            "provider": _prov,
                            "current": _mid,
                            "current_label": _rs.get("label") or _mid,
                            "candidates": _rs.get("alternatives") or [],
                        }
                    break
    except Exception as _e:  # noqa: BLE001
        logger.debug("查模型能力失败（不影响卡片）：%s", _e)

    # ★★ 2026-09-29（真跑撞到的）**内容策略**：这一镜要用的参考图被平台拒过吗？
    #
    #   实测平台原话：
    #     InputImageSensitiveContentDetected.PrivacyInformation
    #       "the input image 'content[1]' may contain real person"
    #   写实人脸的定妆照会被隐私策略拒收 —— 而用户上一次只会看到一闪而过的报错。
    #   这里**提前**在卡片上说出来（数据来自 `core.imagepolicy` 的真实记录），
    #   并给出两条可执行的路：换风格重出 / 先只带场景图。
    _policy_warn = ""
    _policy_fix: Dict[str, Any] = {}
    try:
        if st.key in ("draft", "final"):
            from core import imagepolicy as _ipol
            _paths: List[str] = []
            for _s in (db.list_shots(pid) or []):
                _cids = _s.get("character_ids")
                if isinstance(_cids, str):
                    try:
                        _cids = json.loads(_cids) if _cids.strip() else []
                    except Exception:  # noqa: BLE001
                        _cids = []
                for _cid in (_cids or []):
                    _c = db.get_character(str(_cid))
                    if _c and _c.get("reference_image_path"):
                        _paths.append(str(_c["reference_image_path"]))
                if _s.get("scene_id"):
                    _sc = db.get_scene(str(_s["scene_id"]))
                    if _sc and _sc.get("reference_image_path"):
                        _paths.append(str(_sc["reference_image_path"]))
            _sum = _ipol.rejection_summary(list(dict.fromkeys(_paths)))
            if _sum.get("count"):
                _policy_warn = _sum["warning"]
                # ★ 还要告诉界面"这张图属于哪张资产" —— 否则卡片上没法直接
                #   给出「用非写实风格重出**这张**」的按钮。
                _by_path: Dict[str, Any] = {}
                for _c in (db.list_characters(pid) or []):
                    if _c.get("reference_image_path"):
                        _by_path[str(_c["reference_image_path"])] = (
                            "character", str(_c.get("id")), str(_c.get("name") or ""))
                for _sc2 in (db.list_scenes(pid) or []):
                    if _sc2.get("reference_image_path"):
                        _by_path[str(_sc2["reference_image_path"])] = (
                            "scene", str(_sc2.get("id")), str(_sc2.get("name") or ""))
                _imgs = []
                for _h in (_sum.get("rejected") or []):
                    _k, _aid, _nm = _by_path.get(str(_h.get("path")), ("", "", ""))
                    _imgs.append({**_h, "asset_kind": _k, "asset_id": _aid,
                                  "asset_name": _nm})
                _policy_fix = {
                    "images": _imgs,
                    # 非写实风格清单（重出定妆照时直接给用户选，不用自己想词）
                    "styles": [{"id": "chinese_ink", "label": "中国水墨（写意、非写实）"},
                               {"id": "anime", "label": "日式动画（干净线条）"},
                               {"id": "3d_render", "label": "3D 渲染（非照片感）"}],
                    # ★★ 2026-09-29：还有一条**更该先试**的路 ——
                    #   「只在原图上改画风」（指令式图像编辑），
                    #   它**保留五官/发型/服饰轮廓**，只把"照片感"去掉。
                    #   比重画一张更可能同时满足：
                    #     ① 平台隐私策略（不再是照片级人脸）
                    #     ② 用户第 2 条要的"角色还是那个角色"
                    "refine": {
                        "instruction": ("把这张图改成中国水墨插画风格：去掉照片质感与皮肤"
                                        "细节，改为写意笔触与色块；保持人物五官比例、"
                                        "发型、服饰轮廓与整体构图不变。"),
                        "keep": ["人物五官与脸型", "发型", "服饰轮廓", "构图与站姿"],
                        "label": "🖼 只改画风（保留长相，不重画）",
                    },
                }
    except Exception as _e:  # noqa: BLE001
        logger.debug("查参考图内容策略记录失败（不影响卡片）：%s", _e)

    return {
        "ok": True,
        "project_id": pid,          # ★ 卡片自带项目 id —— 前端不依赖界面"当前项目"
        "stage": st.key,
        "title": st.title,
        "tool": st.tool,
        "why": st.why,
        "cost": st.cost,
        "cost_label": st.cost_label,
        "eta": st.eta,
        "args": args,
        "doing": doing,
        "tweaks": st.tweaks,
        "needs_attention": _needs,
        # ★ 模型能力问题 + 一键换掉的候选（界面据此画「换成…」按钮）
        "model_fix": _model_fix,
        "policy_fix": _policy_fix,
        "already_done": bool(ev.get("done")),
        "done_detail": ev.get("detail"),
        "missing_prereq": missing,
        "missing_prereq_titles": missing_titles,
        "warning": "　".join(x for x in (
            _fix_warn,
            _model_warn,
            _policy_warn,
            (f"前置还没做：{'、'.join(missing_titles)}。"
             f"现在做可能效果不好，要不要先做它们？") if missing else "",
        ) if x),
        "await_confirm": True,
    }


def _doing_text(db, pid: str, st: Stage, args: Dict[str, Any]) -> str:
    """这一步具体会做什么（带真实数字，让用户能判断）。"""
    try:
        if st.key == "script":
            idea = args.get("idea") or ""
            return f"按你的想法「{idea[:60]}」生成剧本大纲和场次" if idea \
                else "生成剧本大纲和场次"
        if st.key == "assets":
            return "读剧本，抽出角色和场景，写进资产库"
        if st.key == "looks":
            chars = db.list_characters(pid) or []
            scenes = db.list_scenes(pid) or []
            todo = [c.get("name") for c in chars
                    if not (c.get("reference_image_path") or "").strip()]
            todo += [s.get("name") for s in scenes
                     if not (s.get("reference_image_path") or "").strip()]
            if not todo:
                return "所有资产都已经有参考图了，不需要再画"
            return (f"给这 {len(todo)} 个还没图的资产生成参考图："
                    f"{'、'.join(str(x) for x in todo[:10])}"
                    + ("…" if len(todo) > 10 else ""))
        if st.key == "shots":
            n = len(db.list_scenes(pid) or [])
            return f"按剧本的 {n} 场戏拆成镜头表，并关联角色/场景"
        if st.key == "previz":
            shots = db.list_shots(pid) or []
            # ★ 与 `_ev_previz` **同一套**判断（状态集合 + 白膜文件真的在）。
            #   原来这里只认 ("draft_ready","approved")，而同意白膜实际写的是
            #   "draft_approved" ⇒ 卡片永远说"给 N 个还没白膜的镜头渲白膜"，
            #   哪怕 5 个镜头的白膜早就确认过了（这正是"重复问"的来源）。
            pend = [s for s in shots
                    if str(s.get("previz_status") or "none") not in PREVIZ_OK_STATES
                    and not (s.get("previz_path")
                             and os.path.exists(str(s["previz_path"])))]
            return (f"给 {len(pend)} 个还没白膜的镜头渲白模预演（本地免费）"
                    if pend else "所有镜头都有白膜了")
        if st.key == "draft":
            shots = db.list_shots(pid) or []
            # 「白膜已确认」用与判据**同一套**状态（`PREVIZ_OK_STATES`）——
            # 这里原来只认字面 "approved"，而实际写进去的是 "draft_approved"，
            # 于是卡片永远说"还没有白膜确认过的镜头"，用户点不下去。
            appr = [s for s in shots
                    if str(s.get("previz_status") or "") in PREVIZ_OK_STATES
                    or (s.get("previz_path") and os.path.exists(str(s["previz_path"])))]
            return (f"给 {len(appr)} 个白膜已确认的镜头出 480P 小样"
                    if appr else "还没有白膜确认过的镜头，建议先做白膜")
        if st.key == "final":
            shots = db.list_shots(pid) or []
            # ★★ 必须按"**真的出过 480P 小样**"来数（与 `_ev_draft` 同一套判据）。
            #   原来数的是"有任何候选片段" —— 于是把"认回的片段/配音版"
            #   也算成小样，卡片就会说「给 5 个出过小样的镜头出 1080P 成片」，
            #   而用户**一版小样都没做过** ⇒ 直接跳到最贵的一步，顺序完全错。
            n = 0
            for s in shots:
                for c in (_json_list(s.get("candidates")) or []):
                    src = str((c or {}).get("source") or "").lower()
                    if "preview" in src or "480" in src or "draft" in src:
                        n += 1
                        break
            return (f"给 {n} 个出过 480P 小样的镜头出 1080P 成片（最贵的一步，"
                    f"会用那版小样**升级**、不重抽）" if n
                    else "还没有出过 480P 小样的镜头 —— 先出小样便宜得多，"
                         "满意之后再升级成 1080P（升级按草稿出片，不重新抽一版）")
        if st.key == "compose":
            shots = db.list_shots(pid) or []
            n = sum(1 for s in shots
                    if _json_list(s.get("candidates")) or _json_list(s.get("segments")))
            return f"把 {n} 个已有视频的镜头拼成一条成片"
        if st.key == "brief":
            return f"新建项目「{args.get('title') or '未命名'}」"
    except Exception as e:
        return f"（算不出细节：{e}）"
    return st.why

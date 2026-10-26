# -*- coding: utf-8 -*-
"""
资产锚点关联（白膜 ↔ 资产库 ↔ 提示词）
======================================

★ 要解决的问题（用户实测反馈）
──────────────────────────────
「白膜视频出好后，并没有跟资产里面对应的角色做一个对应！！！
  我反正在成品视频没看到对应关系！这个真的很重要，会影响连续剧视频的制作的！」

这句话点出的是**连续剧的命门**：第 1 集给陈砚建了角色卡、出了形象图，
第 5 集拍陈砚时，模型**必须**还是那张脸、那件短褐 —— 否则观众认不出是同一个人。

断裂发生在三个环节，每一环都要接上：

  ① 白膜（3D 预演）里的「陈砚」只是一个灰模方块，**没有 id**。
     它跟资产库里的角色卡 `c5fbfe22-…` 之间没有任何链接。
  ② 资产卡有 id 和形象图，但**没有任何地方把 id 写进提示词**。
     出片时提示词里只有「陈砚」两个字，模型不知道"陈砚"长什么样。
  ③ 就算模型肯读图，一张首帧图只能保住一个角色；
     多角色同框时另外几个人还是各拍各的。

★ 本模块做三件事
  1. **对应**：把白膜里的角色名/场景名，按 `@资产名` 匹配到资产库的 id。
     匹配不上、或资产库里多出来的，都如实报出来（不静默吞掉）。
  2. **锚点**：把每个资产压成 6~20 个汉字的**短锚点**
     （轮廓 / 服装主色 / 标志性配件 / 武器形态 / 场景固定结构）。
     来自 script-to-shot-engine 的 asset-anchor-protocol：
     每个 Clip 首次出现时写一次短锚点，后续只写资产名。
     这样第 N 集写「@陈砚（湿透的灰褐短褐、右眉尾一道旧疤）」，
     模型就有稳定的身份依据 —— 这比堆一整段外貌描述有效得多。
  3. **对照卡**：产出「美术资产对照卡」给用户核对
     （哪一项对上了、哪一项是推断、缺什么）。

★ 为什么不做成"自动改名"
  用户明确要求「按剧本来，不要灵活度，不得改名」。所以这里**只做匹配与引用**，
  名字以资产库为准；对不上时报告给用户，绝不自己改名或造一个不存在的资产。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("videoforge.asset_link")


# ══════════════════════════════════════════════════════════════
#  1. 短锚点
# ══════════════════════════════════════════════════════════════
#: 角色锚点优先保留的字段（越靠前越能锁定身份）
_ANCHOR_KEYS = ("silhouette", "costume_main", "props", "texture",
                "face", "palette", "state", "mark")
#: 场景锚点优先保留的字段。
#  ★ 场景不能走角色那套：它没有 silhouette/face，回落到 `description`
#    会把一整句话塞进锚点，截断后就变成「@灰巷·巷口（…巷宽 3.5m 两侧）」
#    这种断在牛句的残句。固定结构（墙高、巷宽、案高）才是场景的身份。
_SCENE_ANCHOR_KEYS = ("architecture", "terrain", "fixtures",
                      "atmosphere", "tone")

_ANCHOR_MAX = 20  # 汉字上限
_ANCHOR_MIN = 6

#: 截断时允许断开的位置（顿号/逗号/句号之后）
_BREAK_CHARS = "、，。；,;. 　"


def _clean(v: Any) -> str:
    if isinstance(v, (list, tuple)):
        return "、".join(str(x).strip() for x in v if str(x).strip())
    return str(v or "").strip()


def build_anchor(ch: Dict[str, Any]) -> Tuple[str, List[str]]:
    """
    把一个资产卡压成 6~20 汉字的短锚点。

    ★ 角色和场景用**两套**字段优先级：
      角色看轮廓/服装/道具/面部；场景看建筑/地形/陈设/氛围。
      场景如果回落到 `description`，会塞进一整句话，截断后是断在牛句的残句。
    ★ 截断只在顿号/逗号/句号处断，**不在词中间切**。
    """
    # 判定是场景还是角色：场景卡有 location_type / time_of_day
    is_scene = bool(_clean(ch.get("location_type")) or _clean(ch.get("time_of_day")))
    keys = _SCENE_ANCHOR_KEYS if is_scene else _ANCHOR_KEYS

    rf = ch.get("reference_features") or {}
    if isinstance(rf, str):
        try:
            import json as _j
            rf = _j.loads(rf)
        except Exception:  # noqa: BLE001
            rf = {}
    if not isinstance(rf, dict):
        rf = {}

    parts: List[str] = []
    used: List[str] = []
    for k in keys:
        v = _clean(rf.get(k))
        if v:
            # 单个字段本身过长时，先在那个字段内部按标点截到 20 字
            if len(v) > _ANCHOR_MAX:
                v = _cut_at_break(v, _ANCHOR_MAX)
            parts.append(v)
            used.append(k)
        if sum(len(p) for p in parts) >= _ANCHOR_MAX:
            break
    # 没有 reference_features（或不够）时，退回卡上的平文字段
    if not parts:
        fallback = (("description", "lighting") if is_scene
                    else ("costume_main", "props", "body_type", "description"))
        for k in fallback:
            v = _clean(ch.get(k))
            if v:
                if len(v) > _ANCHOR_MAX:
                    v = _cut_at_break(v, _ANCHOR_MAX)
                parts.append(v)
                used.append(k)
            if sum(len(p) for p in parts) >= _ANCHOR_MAX:
                break

    anchor = "、".join(p.strip("、，。 ") for p in parts if p.strip("、，。 "))
    if len(anchor) > _ANCHOR_MAX:
        anchor = _cut_at_break(anchor, _ANCHOR_MAX)
    return anchor.strip("、 "), used


def _cut_at_break(s: str, limit: int) -> str:
    """把 s 截到 limit 字以内，且**只在标点处断**；实在没有标点才硬切。

    ★ 特殊处理括号：截出来如果留下了没闭合的「（」，
      就把整个括号段去掉 —— 否则会得到
      「@灰巷·巷口（两侧残墙（宽 3.5m 巷 / 高 2）」这种残句，
      比不带括号更难看，模型也读不明白。
    """
    s = s.strip()
    if len(s) <= limit:
        return s
    head = s[:limit]
    for i in range(len(head) - 1, max(0, limit // 2) - 1, -1):
        if head[i] in _BREAK_CHARS:
            head = head[:i].strip("、，。；,;. 　") or head[:limit]
            break
    # 丢掉未闭合的尾部括号段
    while head.count("（") > head.count("）") and "（" in head:
        head = head[:head.rfind("（")].strip("、，。；,;. 　")
    return head


def anchor_text(ch: Dict[str, Any]) -> str:
    """`@陈砚（湿透的灰褐短褐、右眉尾旧疤）` 这种可直接写进提示词的引用串。"""
    name = _clean(ch.get("name")) or "未命名"
    a, _ = build_anchor(ch)
    return f"@{name}（{a}）" if a else f"@{name}"


# ══════════════════════════════════════════════════════════════
#  2. 名称匹配
# ══════════════════════════════════════════════════════════════
def _norm(s: Any) -> str:
    return "".join(str(s or "").split()).lower()


def _aliases(ch: Dict[str, Any]) -> List[str]:
    """一个资产可能的别名集合（角色卡上可能挂着"兵丁甲/兵丁 甲/甲"这类）。"""
    out = [_clean(ch.get("name"))]
    for k in ("alias", "aliases", "aka", "nickname", "other_names"):
        v = ch.get(k)
        if isinstance(v, (list, tuple)):
            out += [_clean(x) for x in v]
        else:
            s = _clean(v)
            if s:
                out += [x.strip() for x in s.replace("，", ",").split(",") if x.strip()]
    return [x for x in out if x]


def match_name(name: str, cards: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    把白膜里的名字匹配到资产卡。

    顺序：完全相等 → 别名相等 → 一方包含另一方。
    ★ 刻意**不做**模糊/拼音匹配：宁可报"对不上"让用户处理，
      也不要悄悄把「兵丁甲」配到「兵丁乙」上 —— 那样整集的脸就错了。
    """
    n = _norm(name)
    if not n:
        return None
    # ① 完全相等
    for c in cards:
        for a in _aliases(c):
            if _norm(a) == n:
                return c
    # ② 包含（"老周" in "棋摊老周"）
    cands = []
    for c in cards:
        for a in _aliases(c):
            an = _norm(a)
            if an and (an in n or n in an):
                cands.append((abs(len(an) - len(n)), c))
    if cands:
        cands.sort(key=lambda x: x[0])
        # 只在"最优明显更好"时才认，避免 甲/乙 这种同长度歧义
        if len(cands) == 1 or cands[0][0] < cands[1][0]:
            return cands[0][1]
    return None


# ══════════════════════════════════════════════════════════════
#  3. 关联（主入口）
# ══════════════════════════════════════════════════════════════
def link(db, project_id: str,
         previz_characters: Optional[List[str]] = None,
         previz_scenes: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    把白膜里出现的角色/场景与资产库做一一对应。

    Args:
        previz_characters: 白膜里出现的角色名列表（不传则从设计表读）
        previz_scenes:     白膜里出现的场景名列表

    Returns:
        {characters:[{name, asset_id, asset, anchor, matched, why}],
         scenes:[...], missing:[...], unused_assets:[...], ready: bool}
    """
    # 白膜里有哪些角色/场景
    if previz_characters is None or previz_scenes is None:
        try:
            from core import previz_blender as _pb
            d = _pb._load_design("")  # noqa: SLF001
            if previz_characters is None:
                previz_characters = sorted({c[0] for e in d.values() for c in e["cast"]})
            if previz_scenes is None:
                names = {"set_alley": "灰巷·巷口", "set_stall": "灰巷·老周棋摊"}
                previz_scenes = sorted({names[e["setg"].__name__]
                                        for e in d.values() if e["setg"].__name__ in names})
        except Exception as e:  # noqa: BLE001
            logger.warning("读白膜设计表失败：%s", e)
            previz_characters = previz_characters or []
            previz_scenes = previz_scenes or []

    chars = list(db.list_characters(project_id) or [])
    scenes = list(db.list_scenes(project_id) or [])

    out_chars: List[Dict[str, Any]] = []
    used_ids = set()
    for name in previz_characters:
        hit = match_name(name, chars)
        if hit:
            used_ids.add(hit.get("id"))
            a, keys = build_anchor(hit)
            out_chars.append({
                "name": name,
                "asset_id": hit.get("id"),
                "asset_name": _clean(hit.get("name")),
                "anchor": a,
                "anchor_fields": keys,
                "anchor_text": anchor_text(hit),
                "has_image": bool(hit.get("reference_image_path")),
                "matched": True,
                "why": ("名字完全相同"
                        if _norm(hit.get("name")) == _norm(name)
                        else "按名称包含关系匹配（请核对）"),
            })
        else:
            out_chars.append({
                "name": name, "asset_id": "", "asset_name": "",
                "anchor": "", "anchor_fields": [], "anchor_text": f"@{name}",
                "has_image": False, "matched": False,
                "why": "资产库里找不到这个角色 —— 白膜有、资产没有。"
                       "请到「角色」页建这张卡，否则这个人在成片里没有稳定形象。",
            })

    out_scenes: List[Dict[str, Any]] = []
    for name in previz_scenes:
        hit = match_name(name, scenes)
        if hit:
            used_ids.add(hit.get("id"))
            a, keys = build_anchor(hit)
            out_scenes.append({
                "name": name, "asset_id": hit.get("id"),
                "asset_name": _clean(hit.get("name")), "anchor": a,
                "anchor_fields": keys, "anchor_text": anchor_text(hit),
                "has_image": bool(hit.get("reference_image_path")),
                "matched": True,
                "why": ("名字完全相同"
                        if _norm(hit.get("name")) == _norm(name)
                        else "按名称包含关系匹配（请核对）"),
            })
        else:
            out_scenes.append({
                "name": name, "asset_id": "", "asset_name": "", "anchor": "",
                "anchor_fields": [], "anchor_text": f"@{name}",
                "has_image": False, "matched": False,
                "why": "资产库里找不到这个场景 —— 白膜有、资产没有。",
            })

    missing = [x for x in (out_chars + out_scenes) if not x["matched"]]
    unused = [{"id": c.get("id"), "name": _clean(c.get("name")), "kind": "character"}
              for c in chars if c.get("id") not in used_ids]
    unused += [{"id": s.get("id"), "name": _clean(s.get("name")), "kind": "scene"}
               for s in scenes if s.get("id") not in used_ids]

    no_img = [x["name"] for x in (out_chars + out_scenes)
              if x["matched"] and not x["has_image"]]

    return {
        "characters": out_chars,
        "scenes": out_scenes,
        "missing": missing,
        "unused_assets": unused,
        "no_image": no_img,
        "ready": not missing,
        "note": ("✓ 白膜里的角色/场景全部能在资产库找到对应"
                 if not missing else
                 f"有 {len(missing)} 项白膜里有、资产库没有："
                 + "、".join(x["name"] for x in missing)),
    }


# ══════════════════════════════════════════════════════════════
#  4. 输出：注入提示词的锚点串 + 给用户核对的对照卡
# ══════════════════════════════════════════════════════════════
def anchors_block(links: Dict[str, Any]) -> str:
    """
    生成**可直接注入提示词**的资产锚点块。

    这正是「让成片看到角色对应关系」的实际手段：
    提示词里写上 `@陈砚（湿透的灰褐短褐、右眉尾旧疤）`，
    模型就有了稳定的身份依据 —— 第 1 集和第 5 集的陈砚才是同一个人。
    """
    lines: List[str] = []
    for it in links.get("characters") or []:
        if it["matched"] and it["anchor"]:
            lines.append(it["anchor_text"])
        elif it["matched"]:
            lines.append(f"@{it['name']}")
        else:
            lines.append(f"@{it['name']}（⚠ 资产库没有这张卡）")
    for it in links.get("scenes") or []:
        if it["matched"] and it["anchor"]:
            lines.append(it["anchor_text"])
        elif it["matched"]:
            lines.append(f"@{it['name']}")
        else:
            lines.append(f"@{it['name']}（⚠ 资产库没有这张卡）")
    if not lines:
        return ""
    return ("【本片资产锚点·必须沿用这些名字，不得改名】\n"
            + "\n".join("· " + x for x in lines)
            + "\n（每个资产首次出现时写一次括号里的短锚点，后续只写 @资产名）")


def anchor_table_md(links: Dict[str, Any]) -> str:
    """「美术资产对照卡」（Markdown），给用户核对。"""
    rows = ["| 资产引用 | 短锚点 | 资产库 id | 有形象图 | 请核对 |",
            "|---|---|---|---|---|"]
    for it in (links.get("characters") or []) + (links.get("scenes") or []):
        rid = (it["asset_id"][:8] + "…") if it["asset_id"] else "—"
        rows.append(
            f"| @{it['name']} | {it['anchor'] or '（未提供）'} | {rid} | "
            f"{'✓' if it['has_image'] else '✗'} | {it['why']} |")
    md = "\n".join(rows)
    extra = []
    if links.get("unused_assets"):
        extra.append("**资产库里没有被白膜用到的**："
                     + "、".join(f"{u['name']}({u['kind'][:1]})"
                                 for u in links["unused_assets"][:12]))
    if links.get("no_image"):
        extra.append("**已对应但还没有形象图的**（出了图才能当首帧/参考图）："
                     + "、".join(links["no_image"]))
    if extra:
        md += "\n\n" + "\n\n".join(extra)
    return md

# -*- coding: utf-8 -*-
r"""分镜 ↔ 角色/场景 **重新关联**（把"文字里提到的人"真正挂到分镜上）

════════════════════════════════════════════════════════════════════════
为什么需要它（2026-09-28 第 73 轮，用户实测）
════════════════════════════════════════════════════════════════════════
用户原话：「整个流程没法跑通」。

排查《执棋者》时量到的**真实现状**：

```
流水线九步：全部 done（界面上看起来"全跑完了"）
分镜 5 个：  镜1 chars=0  镜2 chars=0  镜3 chars=0  镜4 chars=0  镜5 chars=1
             场景引用：5 个全部为空
角色表：     老者 / 年轻人 / 李队长 / 王医生 / 张二狗 / 裴鸢 / 小张
分镜里真正在说话的人：陈砚 / 兵丁甲 / 曹敬 / 老周 / 裴钧 / 沈阔 / 石头 / 阿桑 / 太后
```

也就是说：**分镜一个角色都没挂上**。后果是链式的：

  · `orchestrator` 从 `character_ids` 取参考图 ⇒ 取到空 ⇒ 出片提示词里没有人物锚点
    ⇒ 用户说的「每个镜头一张脸」；
  · 白膜拿不到"这一镜有谁" ⇒ 主体只能拿概览文字凑 ⇒ 走位推断也无从谈起；
  · 配音只能按台词名字临时选角（本次有效，下次又变）。

而原来自动关联的**唯一入口**是 `POST /api/projects/{pid}/shots/relink`
（一个 HTTP 路由，逻辑还写在 `create_app()` 的闭包里）——
**agent 用不了它**，所以模型看到"0 个镜头关联了角色"也只能如实告诉你，
没法自己修。这正是"整条流程用软件自己跑不通"的那一类断点。

⇒ 把关联逻辑搬到本模块（纯函数 + 一个 `db` 参数），
   **HTTP 路由与 agent 工具调的是同一个函数**。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("videoforge.relink")


def scene_keywords(name: Any) -> List[str]:
    r"""从一个场景名里拆出**可用于文字匹配的关键词**（由长到短）。

    ★★ 2026-09-28（用户第 2 条：「资产里已添加的角色图**用不上**」）

    实测：场景卡叫「老周棋摊」「灰巷巷口」，而分镜文字写的是
    「在一个昏暗的**棋摊**边…」—— 卡名不是原文的子串，于是旧逻辑
    （`k in key`）**匹配不上** ⇒ 4/5 个镜头 scene_id 是空的 ⇒
    场景参考图一张都没用上。用户看到的就是「资产里的图用不上」。

    这里把场景名拆成关键词：去掉修饰词，留下「棋摊」「巷口」「茶馆」
    这种 **2~4 字的语义核**，再拿它去分镜文字里找。
    """
    s = str(name or "").strip()
    if not s:
        return []
    out: List[str] = []
    parts = [p for p in re.split(r"[，,、·/（）()\s]+", s) if p]
    for p in parts + [s]:
        q = re.sub(r"^(古色古香的|古老的|废弃的|旧的|新的|老周的|小|大)", "", p).strip()
        for cand in (p, q):
            if 2 <= len(cand) <= 8 and cand not in out:
                out.append(cand)
        for n in (4, 3, 2):
            if len(q) > n:
                tail = q[-n:]
                if tail not in out:
                    out.append(tail)
    return sorted(set(out), key=lambda x: -len(x))


def pick_scene_by_text(text: Any, scene_list: List[dict]) -> Optional[dict]:
    r"""在**分镜文字**里找最匹配的场景卡（返回卡本身）。

    判据：卡名的关键词里，**最长的那个**出现在文字里 → 就是它。
    刻意要求 ≥2 字（避免"的/了"这种噪声命中），并且**取最长命中**，
    这样「茶馆内，棋盘前」不会被「城市街道」抢走。
    """
    t = str(text or "")
    if not t:
        return None
    best, best_len = None, 0
    for sc in (scene_list or []):
        for kw in scene_keywords(sc.get("name")):
            if len(kw) >= 2 and kw in t:
                if len(kw) > best_len:
                    best, best_len = sc, len(kw)
                break
    return best


def _norm(s: Any) -> str:
    s = str(s or "").strip()
    s = re.sub(r"[（(][^）)]*[）)]", "", s)          # 去掉括号注释
    s = re.sub(r"[，,、·\s\-—_]+", "", s)
    return s


def build_link_index(db, pid: str) -> Dict[str, Any]:
    """构建匹配索引 + 三个匹配器（角色名 / 场景名 / 某一场的出场角色）。

    与原来的闭包版本**逐行等价**（搬出来时没改判据），只是不再依赖闭包变量。
    """
    char_list = db.list_characters(pid) or []
    scene_list = db.list_scenes(pid) or []
    details_map: Dict[int, dict] = {}
    try:
        for d in (db.list_scene_details(pid) or []):
            if isinstance(d, dict) and d.get("scene_number"):
                details_map[d["scene_number"]] = d
    except Exception:  # noqa: BLE001
        pass

    scene_by_norm: Dict[str, dict] = {}
    for sc in scene_list:
        for key in (_norm(sc.get("name")), str(sc.get("name") or "").strip()):
            if key:
                scene_by_norm.setdefault(key, sc)
    chars_sorted = sorted(char_list, key=lambda c: -len(str(c.get("name") or "")))

    def match_char_name(name) -> Optional[dict]:
        """剧本称呼 → 角色资产：全等 → 别名 → 双向包含（「雪诺」↔「琼恩·雪诺」）"""
        nm = str(name or "").strip()
        if not nm:
            return None
        for c in char_list:
            if str(c.get("name") or "").strip() == nm:
                return c
        for c in char_list:
            try:
                rf = c.get("reference_features")
                rf = json.loads(rf) if isinstance(rf, str) else (rf or {})
                for a in (rf.get("aliases") or []):
                    if str(a).strip() == nm:
                        return c
            except Exception:  # noqa: BLE001
                pass
        n2 = _norm(nm)
        if not n2:
            return None
        for c in chars_sorted:      # 先长后短，避免「丹妮莉丝」抢走「丹妮莉丝·坦格利安」
            cn = _norm(c.get("name"))
            if cn and (cn == n2 or cn in n2 or n2 in cn):
                return c
        return None

    def match_scene_names(*cands) -> Optional[str]:
        for cand in cands:
            if not cand:
                continue
            key = _norm(cand)
            if not key:
                continue
            if key in scene_by_norm:
                return scene_by_norm[key]["id"]
            for k, sc in scene_by_norm.items():
                if k and len(k) >= 2 and (k in key or key in k):
                    return sc["id"]
        return None

    def match_chars_of(num, scene, tlp) -> List[str]:
        """优先级：**剧本细节的 characters** > 场次 characters > 文本命中
        （细节里的名字更完整：剧本写「雪诺」，细节写「琼恩·雪诺」）"""
        names: List[str] = []
        detail = details_map.get(num) or {}
        for src in (detail.get("characters"), (scene or {}).get("characters")):
            if isinstance(src, list) and src:
                names = [(x.get("name") if isinstance(x, dict) else x) for x in src]
                names = [str(x).strip() for x in names if x]
                if names:
                    break
        if not names:
            for blob in (json.dumps(tlp, ensure_ascii=False) if isinstance(tlp, dict) else "",
                         json.dumps(scene or {}, ensure_ascii=False)):
                if not blob:
                    continue
                hit = [str(c["name"]) for c in chars_sorted
                       if c.get("name") and str(c["name"]) in blob]
                if hit:
                    names = hit
                    break
        ids: List[str] = []
        for n in names:
            c = match_char_name(n)
            if c and c["id"] not in ids:
                ids.append(c["id"])
        if not ids:
            blob = json.dumps({"s": scene, "d": detail}, ensure_ascii=False)
            for c in chars_sorted:
                nm = str(c.get("name") or "")
                if nm and nm in blob and c["id"] not in ids:
                    ids.append(c["id"])
        return ids

    return {"details_map": details_map, "match_scene_names": match_scene_names,
            "match_chars_of": match_chars_of, "match_char_name": match_char_name,
            "chars": char_list, "scenes": scene_list,
            "char_count": len(char_list), "scene_count": len(scene_list)}


def assets_gap(db, pid: str) -> Dict[str, Any]:
    r"""把「**这一镜到底缺什么**」列清楚（角色/场景/图）。

    ★★ 2026-09-28（用户第 1、2、7 条）

    实测到的死循环：分镜上挂的角色卡（兵丁甲/稽查兵丁甲/兵丁乙）**没有图**，
    而"定妆照"这一步的判据是"所有卡都有图" ⇒ 永远做不完 ⇒
    模型每次都问一遍"要不要做定妆照" ⇒ 用户体验成「重复问用户定妆，问完不办事」。
    另一头：镜3/4/5 写的「金殿/天元座」在资产里**根本没有对应的场景卡**，
    所以场景图也永远用不上。

    这个函数把这些**如实列出来**，让卡片/工具能一句话说清
    "缺谁、缺哪张、去点哪个按钮"，而不是含糊地反复问。
    """
    chars = db.list_characters(pid) or []
    scenes = db.list_scenes(pid) or []
    shots = db.list_shots(pid) or []
    cbyid = {str(c.get("id")): c for c in chars}

    def _no_img(x) -> bool:
        p = str(x.get("reference_image_path") or "").strip()
        return (not p) or (not os.path.exists(p))

    want_img = [str(c.get("name") or c.get("id")) for c in chars if _no_img(c)]
    scene_no_img = [str(s.get("name") or s.get("id")) for s in scenes if _no_img(s)]

    no_scene: List[int] = []
    linked_chars_no_img: List[str] = []
    for s in shots:
        if not s.get("scene_id"):
            no_scene.append((s.get("order_index") or 0) + 1)
        cids = s.get("character_ids")
        if isinstance(cids, str):
            try:
                cids = json.loads(cids) if cids.strip() else []
            except Exception:  # noqa: BLE001
                cids = []
        for cid in (cids or []):
            c = cbyid.get(str(cid))
            if c and _no_img(c):
                nm = str(c.get("name") or cid)
                if nm not in linked_chars_no_img:
                    linked_chars_no_img.append(nm)
    return {"characters_without_image": want_img,
            "scenes_without_image": scene_no_img,
            "shots_without_scene": no_scene,
            "linked_characters_without_image": linked_chars_no_img,
            "ok": not (want_img or scene_no_img or no_scene)}


def propose_scenes_for_shots(db, pid: str) -> List[Dict[str, Any]]:
    r"""给**没有场景卡**的镜头提一张"该建什么场景"的草案（**不写库**）。

    ★★ 2026-09-29（用户第 2、7、8 条）

    实测：镜 3/4/5 的场景（金殿、天元座、灰巷）在资产里**没有对应的卡** ——
    所以它们的场景参考图永远用不上，而"新建场景卡"这一步用户得自己想名字、
    自己打字。这里把**草案**先摆出来：名字从分镜文字里取（第一句里最像地点
    的那一段），文字原样附上，用户看着改或直接确认。

    刻意**只提议不创建** —— 名字是我猜的，凭猜就往用户库里写卡是不对的。
    """
    scenes = db.list_scenes(pid) or []
    shots = sorted(db.list_shots(pid) or [], key=lambda s: s.get("order_index") or 0)
    out: List[Dict[str, Any]] = []
    for s in shots:
        if s.get("scene_id"):
            continue
        text = str(s.get("layer1_overview") or "").strip()
        if not text:
            continue
        first = re.split(r"[。！？!?\n]", text)[0]
        head = re.split(r"[，,、：:]", first)[0].strip()
        # ★ 收拾一下从散文里"抠"出来的地名：
        #   去掉开头的介词（在/于/从/到）和结尾的方位词（之上/中/里/内/下/上/边）。
        #   实测：「在天元座上」→「天元座」、「金殿之上」→「金殿」、
        #        「灰巷大火中」→「灰巷大火」—— 这样卡片名字才像资产名，
        #        而不是半句话。（用户可能还会改，草案只是给个起点。）
        name = re.sub(r"^(在|于|从|到|那|这)", "", head)
        trimmed = re.sub(r"(之上|之下|之中|里|内|中|上|下|边|旁)$", "", name)
        if len(trimmed) >= 2:
            name = trimmed
        name = name[:8] or ("第 %d 镜场景" % ((s.get("order_index") or 0) + 1))
        out.append({
            "shot_id": s.get("id"),
            "order": (s.get("order_index") or 0) + 1,
            "suggested_name": name,
            "from_text": text[:70],
            "why": "这个镜头没有关联场景卡；名字是从分镜文字第一段取的，可以改。",
            "existing_scene_names": [x.get("name") for x in scenes][:8],
        })
    return out


def create_scenes_for_shots(db, pid: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    r"""按用户确认过的草案**建场景卡并挂到镜头上**（这一步才写库）。

    `items` 形如 `[{"shot_id": "...", "name": "金殿"}]` —— 名字由调用方给
    （用户确认/改过的那个），不在这里猜。
    """
    made, failed = [], []
    for it in (items or []):
        sid = str((it or {}).get("shot_id") or "").strip()
        name = str((it or {}).get("name") or "").strip()
        if not sid or not name:
            failed.append({"shot_id": sid, "error": "缺 shot_id 或 name"})
            continue
        shot = None
        for s in (db.list_shots(pid) or []):
            if str(s.get("id")) == sid:
                shot = s
                break
        if not shot:
            failed.append({"shot_id": sid, "error": "镜头不存在"})
            continue
        try:
            sc = db.create_scene(
                project_id=pid, name=name,
                description=str((it or {}).get("description")
                                or (shot.get("layer1_overview") or "")[:120]))
            db.update_shot(sid, scene_id=sc.get("id"))
            made.append({"shot_id": sid, "scene_id": sc.get("id"), "name": name})
        except Exception as e:  # noqa: BLE001
            failed.append({"shot_id": sid, "error": f"{type(e).__name__}: {e}"})
    return {"ok": bool(made) and not failed, "created": made, "linked": len(made),
            "failed": failed,
            "note": (f"建了 {len(made)} 张场景卡并挂到对应镜头"
                     + (f"；{len(failed)} 个失败" if failed else "")
                     + "。接着给这些场景出参考图，它们才会作为环境参考进入视频生成。")}


def relink_project_shots(db, pid: str) -> Dict[str, Any]:
    """**原地**给已有分镜补上角色/场景关联（不重建分镜、不丢已生成的视频）。

    返回 `{ok, updated, total, assets:{characters,scenes}, detail:[...], hint}`。
    """
    script = db.get_script(pid) or {}
    shots = db.list_shots(pid) or []
    if not shots:
        return {"ok": False, "error": "该项目还没有分镜", "status": 400}
    idx = build_link_index(db, pid)
    scenes = (script or {}).get("scenes")
    if isinstance(scenes, str):
        try:
            scenes = json.loads(scenes)
        except Exception:  # noqa: BLE001
            scenes = []
    scenes = scenes if isinstance(scenes, list) else []
    tlp_all = (script or {}).get("three_layer_prompts") or {}

    updated, detail = 0, []
    for shot in shots:
        oi = shot.get("order_index") or 0
        scene = scenes[oi] if oi < len(scenes) else {}
        num = (scene or {}).get("scene_number") or (oi + 1)
        tlp = tlp_all.get(f"scene_{num}") or {}
        sid_m = idx["match_scene_names"](
            (scene or {}).get("location"), (scene or {}).get("name"),
            (idx["details_map"].get(num) or {}).get("location"),
            shot.get("layer1_overview"))
        cids = idx["match_chars_of"](num, scene or {}, tlp)
        # ★★ 2026-09-28（用户第 2 条）：场景**按分镜自己的文字**也要能匹配上。
        #   实测：剧本的场次记录里 `name`/`characters` 是空的（导入时丢了），
        #   于是 4/5 个镜头 scene_id 为空 ⇒ 场景参考图一张没用上。
        #   这里补一条"拿场景卡的名字关键词去分镜文字里找"的路。
        if not sid_m:
            try:
                _hit = pick_scene_by_text(
                    " ".join([str(shot.get("layer1_overview") or ""),
                              str(shot.get("layer2_timeline") or "")]),
                    idx["scenes"])
                if _hit:
                    sid_m = _hit.get("id")
            except Exception as _e:  # noqa: BLE001
                logger.warning("按分镜文字匹配场景失败：%s", _e)
        # ★ 除了"按剧本场次匹配"，再按**分镜文字里提到的角色名**补齐，
        #   并把**悬空 ID 剔除**（角色卡被删/重建后会留下指向旧卡的 ID）。
        try:
            from core import refcheck as _rc
            _chk = _rc.check_shot_refs(shot, idx["chars"], idx["scenes"])
            for _m in (_chk.get("mentioned_not_linked") or []):
                if _m.get("id") and _m["id"] not in cids:
                    cids.append(_m["id"])
            _valid = {str(c.get("id")) for c in idx["chars"]}
            cids = [c for c in cids if c in _valid]
        except Exception as _e:  # noqa: BLE001
            logger.warning("按文字补关联失败（沿用剧本匹配）：%s", _e)
        patch: Dict[str, Any] = {}
        if sid_m and sid_m != shot.get("scene_id"):
            patch["scene_id"] = sid_m
        if cids and list(cids) != list(shot.get("character_ids") or []):
            patch["character_ids"] = cids
        if patch:
            try:
                db.update_shot(shot["id"], **patch)
                updated += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("relink 更新失败 %s: %s", shot.get("id"), e)
        detail.append({
            "order_index": oi, "scene": bool(sid_m), "characters": len(cids),
            # 把名字也带上，用户/模型一眼能看出"挂上的是谁"
            "character_names": [
                str(next((c.get("name") for c in idx["chars"]
                          if str(c.get("id")) == str(i)), ""))
                for i in cids],
        })
    # ★ 结果里如实说清"还剩几个没挂上"（这正是用户看到的症状）
    still = [d for d in detail if not d["characters"]]
    hint = ""
    if not (idx["char_count"] or idx["scene_count"]):
        hint = ("该项目还没有角色/场景资产 —— 先在对话里说「从剧本提取角色和场景」，"
                "或调用 extract_assets，再做这一步")
    elif still:
        hint = (f"还有 {len(still)} 个分镜**没能挂上任何角色**："
                f"分镜文字里提到的名字和「角色」页的名字对不上。"
                f"两种解法：① 把剧本/角色卡的名字改成一致的；"
                f"② 直接告诉我「给第 X 镜加上某某角色」。")
    return {"ok": True, "updated": updated, "total": len(shots),
            "assets": {"characters": idx["char_count"], "scenes": idx["scene_count"]},
            "still_unlinked": len(still), "detail": detail, "hint": hint}

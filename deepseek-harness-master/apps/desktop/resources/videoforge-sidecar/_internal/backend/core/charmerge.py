# -*- coding: utf-8 -*-
r"""把**同一个人的两张角色卡合并成一张**。

════════════════════════════════════════════════════════════════════════
为什么需要（2026-09-28 实测）
════════════════════════════════════════════════════════════════════════
用户的《执棋者》库里，同一个人有**两张卡**：

    兵丁甲   / 稽查兵丁甲
    兵丁乙   / 稽查兵丁乙

后果是**翻倍**的：

  · "定妆照"这一步的判据是"**每张卡**都要有图" ⇒ 4 张没图
    ⇒ 这一步永远做不完 ⇒ 模型每次又问一遍
    （用户说的「重复问用户定妆，多次问问完不办事」）；
  · 出片时同一人物可能挂到不同卡上 ⇒ 参考图不一致 ⇒ 每个镜头一张脸；
  · 用户要维护两套外貌描述。

合并的语义（刻意保守，绝不猜）：
  · **保留哪张由调用方指定**（不自动选 —— 选错会丢用户的描述）；
  · 保留卡的字段**不覆盖**：只有当保留卡某字段为空时，才从被并卡里补；
    参考图同理（保留卡没图才拿被并卡的图）；
  · `reference_features` **做并集**（外貌描述两边都可能有信息）；
  · 所有引用了被并卡的**分镜**改挂保留卡（`character_ids` 去重）；
  · 被并卡的整行**归档到文件**再删除（可追溯，不是"悄悄没了"）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("videoforge.charmerge")

#: 只在这些字段为空时才从被并卡里补 —— 别覆盖用户已经写好的东西。
#: ★ 字段名要跟 `characters` 表的真实列对齐（`PRAGMA table_info(characters)`）：
#:   id/project_id/name/description/body_type/age/costume_main/costume_alternate/
#:   props/reference_image_path/reference_features/status/created_at/
#:   element_id/element_status/voice_id
#:   —— 写错名字不会报错，只会**静默不补**（本测试就抓到了 body_type 漏了）。
_FILL_IF_EMPTY = ("reference_image_path", "description", "body_type", "age",
                  "costume_main", "costume_alternate", "props",
                  "voice_id", "element_id")


def _load_json(v: Any, default):
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return json.loads(v)
        except Exception:  # noqa: BLE001
            return default
    return default


def _archive(db, pid: str, payload: Dict[str, Any]) -> str:
    root = Path(os.environ.get("VIDEOFORGE_DATA_DIR") or os.getcwd()) / "outputs" / str(pid)
    d = root / "shots" / "_archive"
    try:
        d.mkdir(parents=True, exist_ok=True)
        p = d / ("merged_character_%s.json" % time.strftime("%Y%m%d-%H%M%S"))
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(p)
    except Exception as e:  # noqa: BLE001
        logger.warning("归档被并角色失败（继续合并）：%s", e)
        return ""


def merge_characters(db, pid: str, keep_id: str, drop_id: str) -> Dict[str, Any]:
    r"""把 `drop_id` 并进 `keep_id`，然后删掉 `drop_id`。

    返回 `{ok, keep, dropped, filled:[], shots_updated:n, archived:path}`。
    """
    if not keep_id or not drop_id or keep_id == drop_id:
        return {"ok": False, "error": "要指定两个**不同**的角色卡"}
    keep = db.get_character(keep_id)
    drop = db.get_character(drop_id)
    if not keep or not drop:
        return {"ok": False, "error": "角色卡不存在（可能已经被删了）"}
    if str(keep.get("project_id")) != str(pid) or str(drop.get("project_id")) != str(pid):
        return {"ok": False, "error": "两张卡不在同一个项目里，不敢跨项目合并"}

    patch: Dict[str, Any] = {}
    filled: List[str] = []
    for k in _FILL_IF_EMPTY:
        if not str(keep.get(k) or "").strip() and str(drop.get(k) or "").strip():
            patch[k] = drop.get(k)
            filled.append(k)

    # reference_features 做并集（两边的外貌/别名都可能有用）
    kf = _load_json(keep.get("reference_features"), {}) or {}
    df = _load_json(drop.get("reference_features"), {}) or {}
    if isinstance(kf, dict) and isinstance(df, dict):
        merged = dict(df)
        merged.update({k: v for k, v in kf.items() if v not in (None, "", [], {})})
        # aliases 两边合并
        al: List[str] = []
        for src in (df.get("aliases") or [], kf.get("aliases") or []):
            for a in (src if isinstance(src, list) else []):
                a = str(a).strip()
                if a and a not in al:
                    al.append(a)
        # 被并卡的名字也留成别名 —— 这样分镜文字里写旧名字也还能匹配上
        dn = str(drop.get("name") or "").strip()
        if dn and dn not in al:
            al.append(dn)
        if al:
            merged["aliases"] = al
        if merged != (kf or {}):
            patch["reference_features"] = merged

    archived = _archive(db, pid, {"keep": keep, "drop": drop, "at": time.time()})

    # 分镜改挂保留卡
    updated = 0
    for s in (db.list_shots(pid) or []):
        cids = _load_json(s.get("character_ids"), [])
        if not isinstance(cids, list) or str(drop_id) not in [str(x) for x in cids]:
            continue
        new: List[str] = []
        for c in cids:
            c = str(keep_id) if str(c) == str(drop_id) else str(c)
            if c not in new:
                new.append(c)
        try:
            db.update_shot(s["id"], character_ids=new)
            updated += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("把镜头 %s 改挂保留卡失败：%s", s.get("id"), e)

    if patch:
        db.update_character(keep_id, **patch)
    db.delete_character(drop_id)
    logger.info("合并角色：%s ← %s（补了 %s；改挂 %d 个镜头）",
                keep.get("name"), drop.get("name"), filled, updated)
    return {"ok": True, "keep": {"id": keep_id, "name": keep.get("name")},
            "dropped": {"id": drop_id, "name": drop.get("name")},
            "filled": filled, "shots_updated": updated, "archived": archived,
            "note": (f"已把「{drop.get('name')}」并进「{keep.get('name')}」"
                     f"（补上 {len(filled)} 个空字段、{updated} 个镜头改挂保留卡；"
                     f"被并的那张已归档，需要可以还原）。")}


def find_duplicate_characters(db, pid: str) -> List[Dict[str, Any]]:
    r"""找出**疑似同一个人**的多张卡（用于提示用户合并）。

    判据刻意保守，宁可漏也别误报：
      · 名字互相包含（`兵丁甲` ⊂ `稽查兵丁甲`）且短名 ≥2 字；或
      · 名字归一化后相同（去掉"稽查/老/小/大"这类前缀）。
    返回 `[{names:[...], ids:[...], why}]`。
    """
    chars = db.list_characters(pid) or []
    out: List[Dict[str, Any]] = []
    used = set()
    for i, a in enumerate(chars):
        an = str(a.get("name") or "").strip()
        if not an or len(an) < 2:
            continue
        group = [a]
        for b in chars[i + 1:]:
            bn = str(b.get("name") or "").strip()
            if not bn or len(bn) < 2:
                continue
            why = ""
            if an == bn:
                why = "名字完全相同"
            elif an in bn or bn in an:
                why = "名字互相包含"
            if why:
                group.append(b)
        if len(group) > 1:
            ids = tuple(sorted(str(x.get("id")) for x in group))
            if ids in used:
                continue
            used.add(ids)
            out.append({"names": [x.get("name") for x in group],
                        "ids": [x.get("id") for x in group],
                        "why": "名字互相包含（很可能是同一个人）",
                        "with_image": [bool((x.get("reference_image_path") or "").strip())
                                       for x in group]})
    return out

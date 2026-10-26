# -*- coding: utf-8 -*-
r"""把「孤儿片段」认回来 —— 分镜被重建后，磁盘上的成片还能救。

════════════════════════════════════════════════════════════════════════
为什么需要它（2026-09-28 实测事故）
════════════════════════════════════════════════════════════════════════
用户的《执棋者》里，5 个镜头**每个都出过片**（raw + 配音版，共 10 个文件、
约 260 MB）。后来对话里跑了一次 `plan_shots(replace=true)`：

  · 旧分镜被**删掉重建**（id 从 `63938f22…` 变成 `3995924a…`）；
  · 新分镜的 `candidates` 是空的 ⇒ **已经花钱生成好的片段在库里"不见了"**；
  · 于是后面每一步都做不下去：
      配音 → "一个镜头都没配上（无台词或没有片段）"
      合成 → "先不拼了，拼出来会是无声片"
    用户看到的就是「流程跑不通」。

**文件其实一个都没丢**（一直在 `outputs/<pid>/shots/` 下），丢的只是**链接**。
所以这里按「顺序」把它们认回来：

  · 扫描 `shots/` 与 `shots/voiced/`，取出所有 .mp4；
  · 文件名前缀是**旧镜头 id** —— 在现有分镜里找不到就说明是孤儿；
  · 孤儿按文件名排序 ↔ 现有分镜按 order_index 排序，**数量一致时**逐一挂回；
  · 配音版额外认成 `source=voiced`，并顺手写回 `selected_candidate_id`
    （合成器就是按这个挑片段的）。
  · 数量不一致 / 找不到对应关系时**如实报告**，绝不硬凑。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger("videoforge.recover")


def _load(v: Any) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip():
        try:
            x = json.loads(v)
            return x if isinstance(x, list) else []
        except Exception:  # noqa: BLE001
            return []
    return []


def scan_orphan_clips(pid: str) -> Dict[str, Any]:
    """扫描磁盘，返回 {raw:[(旧id,路径)], voiced:[...]}（按文件名排序）。"""
    root = Path(os.environ.get("VIDEOFORGE_DATA_DIR") or os.getcwd()) / "outputs" / str(pid)
    out: Dict[str, List[tuple]] = {"raw": [], "voiced": []}
    for sub, key in (("shots", "raw"), ("shots/voiced", "voiced")):
        d = root / sub
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.mp4")):
            name = f.name
            if key == "voiced" and not name.endswith("_voiced.mp4"):
                continue
            if key == "raw" and name.endswith("_voiced.mp4"):
                continue
            old = name[:-len("_voiced.mp4")] if key == "voiced" else name[:-4]
            out[key].append((old, str(f)))
    return out


def archive_candidates(db, pid: str) -> Dict[str, Any]:
    r"""把现有分镜的候选片段**归档到文件**（重建分镜前先留一份底）。

    为什么不做成"自动恢复"就完事：重建后**旧 id 全变了**，
    "哪个旧镜头对应哪个新镜头"只能靠顺序猜。归档让人（或
    `relink_orphan_clips`）有据可查，而不是凭空失联。
    """
    shots = sorted((db.list_shots(pid) or []), key=lambda s: s.get("order_index") or 0)
    snap = {"pid": pid, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "shots": [{"id": s.get("id"), "order_index": s.get("order_index"),
                       "overview": (s.get("layer1_overview") or "")[:80],
                       "selected_candidate_id": s.get("selected_candidate_id"),
                       "candidates": _load(s.get("candidates"))} for s in shots]}
    root = Path(os.environ.get("VIDEOFORGE_DATA_DIR") or os.getcwd()) / "outputs" / str(pid)
    out_dir = root / "shots" / "_archive"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / ("candidates_%s.json" % time.strftime("%Y%m%d-%H%M%S"))
        p.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "path": str(p), "shots": len(shots)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def relink_orphan_clips(db, pid: str, *, apply: bool = True) -> Dict[str, Any]:
    r"""把磁盘上的孤儿片段按**顺序**挂回现有分镜。

    `apply=False` 时只报告会做什么（干跑），不写库。

    返回里 `relinked` 是真正挂回去的条数；`reason` 说明为什么没挂
    （数量对不上 / 镜头里已经有候选了 等）—— 不做任何"猜"。
    """
    shots = sorted((db.list_shots(pid) or []), key=lambda s: s.get("order_index") or 0)
    if not shots:
        return {"ok": False, "error": "这个项目没有分镜，没法挂片段", "relinked": 0}

    scanned = scan_orphan_clips(pid)
    have_ids = {str(s.get("id")) for s in shots}
    orphan_raw = [(o, p) for o, p in scanned["raw"] if o not in have_ids]
    orphan_voiced = [(o, p) for o, p in scanned["voiced"] if o not in have_ids]

    result: Dict[str, Any] = {
        "ok": True, "shots": len(shots),
        "orphan_raw": len(orphan_raw), "orphan_voiced": len(orphan_voiced),
        "relinked": 0, "applied": bool(apply), "detail": [], "reason": "",
    }

    if not orphan_raw and not orphan_voiced:
        result["reason"] = "没有孤儿片段（磁盘上每个文件都能在分镜里找到对应）"
        return result

    # ★ 只在**数量一致**时按顺序挂 —— 数量不一致说明对应关系不明确，
    #   硬凑会把 A 镜头的片子挂到 B 镜头上（比不挂更糟）。
    if orphan_raw and len(orphan_raw) != len(shots):
        result["reason"] = (f"磁盘上有 {len(orphan_raw)} 个孤儿片段，但分镜有 "
                            f"{len(shots)} 个 —— 数量对不上，不敢按顺序硬挂"
                            f"（会把某个镜头的片子挂错）。请人工确认对应关系。")
        result["ok"] = False
        return result

    made = 0
    for i, s in enumerate(shots):
        cands = _load(s.get("candidates"))
        # ★ 已经有候选的镜头**不动**（别覆盖人家自己做出来的东西）
        if cands:
            result["detail"].append({"index": i + 1, "skipped": "已经有候选片段"})
            continue
        entry = None
        if i < len(orphan_raw):
            _old, path = orphan_raw[i]
            entry = {
                "candidate_id": "recovered_%d" % int(time.time() * 1000 + i),
                "id": "recovered_%d" % int(time.time() * 1000 + i),
                "path": path,
                "url": "/api/media?path=" + path.replace("\\", "/"),
                "source": "recovered（分镜重建后按顺序认回）",
                "old_shot_id": _old,
            }
        if not entry:
            result["detail"].append({"index": i + 1, "skipped": "没有可分配的孤儿片段"})
            continue
        # 配音版优先作为选中项（有声 > 无声）
        sel = entry["candidate_id"]
        for _o, vp in orphan_voiced:
            if Path(vp).name.startswith(Path(entry["path"]).stem):
                vid = "recovered_voiced_%d" % int(time.time() * 1000 + i)
                extra = {"candidate_id": vid, "id": vid, "path": vp,
                         "url": "/api/media?path=" + vp.replace("\\", "/"),
                         "source": "recovered_voiced（配音版）"}
                if apply:
                    db.update_shot(s["id"], candidates=[entry, extra],
                                   selected_candidate_id=vid)
                sel = vid
                entry = None      # 已经一起写进去了
                break
        if entry is not None and apply:
            db.update_shot(s["id"], candidates=[entry], selected_candidate_id=sel)
        if apply:
            made += 1
        result["detail"].append({"index": i + 1, "path": os.path.basename(
            (orphan_raw[i][1] if i < len(orphan_raw) else ""))})
    result["relinked"] = made
    if made:
        result["note"] = (f"按顺序把 {made} 个镜头的孤儿片段挂回去了"
                          f"（磁盘上的文件一个都没动）。"
                          f"配音版优先作为选中片段 —— 这样合成出来是有声的。")
    return result

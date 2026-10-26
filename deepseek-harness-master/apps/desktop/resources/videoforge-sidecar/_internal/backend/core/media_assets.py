# -*- coding: utf-8 -*-
r"""「成片素材」—— 把**已经生成出来的视频**当成资产列出来。

════════════════════════════════════════════════════════════════════════
用户第 3 条原话
════════════════════════════════════════════════════════════════════════
    「资产还包括已经生成的白膜视频 + 成品视频；资产里面的角色 + 场景
      都可以用户手动添加后确认使用」

实测的问题：**生成出来的视频散在磁盘上，界面上看不到**。
用户只有"再生成一次"这一条路 —— 明明已经出过的东西：
  · `shot.previz_path`        白膜预演（G1）
  · `shot.candidates[]`       每一版片段（480P 草稿 / 1080P 成片 / 配音版 / 认回来的）
  · `shot.selected_candidate_id`  用户选中的那一版
  · `outputs/<pid>/…`         合成出来的成片

这个模块把它们**按镜头归拢**，每一条都给出：
  `kind`（白膜/480P/成片/配音/合成）、`path`、`url`（站内可播）、
  `exists`（文件真的在不在）、`selected`（是不是当前选中的那版）、体积/时间。

★ 为什么 `exists` 必须带上：我们踩过"库里记着路径、文件早没了"的坑
  （分镜重建导致候选失联）。列表里如实标出来，用户才不会点开一个死链。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

logger = logging.getLogger("videoforge.media_assets")


def _url(p: str) -> str:
    return "/api/media?path=%s" % quote(str(p or ""))


def _load(v: Any, default):
    if isinstance(v, (list, dict)):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return json.loads(v)
        except Exception:  # noqa: BLE001
            return default
    return default


#: 分辨率探测结果缓存（key=路径, value=(w,h)；测不出存 (0,0)）—— **不猜**
_RES_CACHE: Dict[str, Any] = {}

#: 宽度到这儿就算"1080P 档"（1920 标准；有的平台给 1080/1088 变体）
W_1080 = 1900
#: 480P 档的上界（720/854 这类都算小样档，别把 720p 当 1080p）
W_480_MAX = 1100


def probe_resolution(path: str) -> tuple:
    r"""用 ffmpeg 问一下这个视频**实际**多大 → `(宽, 高)`；测不出返回 `(0, 0)`。

    ★★ 2026-09-29（第 9 条 + 差点让用户白花一笔钱）

    实测：用户项目里 5 个镜头的片段**全都标着 `recovered`**
    （库里的来源记录在早先一次分镜重建里丢了），于是流水线一路判
    「0/5 出过成片」，**反复催他花钱出 1080P**。
    可用 ffmpeg 一问：**全都是 1920x1088、12.04 秒、还有配音版** ——
    片子**本来就是这个画质**，缺的只是一条来源记录。

    ⇒ 所以判据不能只信库里的 `source`：**文件本身不会骗人**。
    这里"问一次、缓存住"（ffmpeg 便宜，但也没必要每条都问）。
    """
    p = str(path or "")
    if not p or not os.path.exists(p):
        return (0, 0)
    if p in _RES_CACHE:
        return _RES_CACHE[p]
    wh = (0, 0)
    try:
        import re as _re
        import subprocess
        from core import ffmpeg_manager as _FM
        ff = _FM.find_or_install_ffmpeg()
        r = subprocess.run([ff, "-hide_banner", "-i", p],
                           capture_output=True, text=True, timeout=40)
        t = (r.stderr or "") + (r.stdout or "")
        m = _re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", t)
        if m:
            wh = (int(m.group(1)), int(m.group(2)))
    except Exception as e:  # noqa: BLE001
        logger.debug("探测分辨率失败 %s：%s", p, e)
    _RES_CACHE[p] = wh
    return wh


def tier_by_resolution(path: str) -> str:
    """**只看文件、不看来源**：这个片子的画质属于哪一档。

    返回 `"1080P 成片"` / `"480P 小样"` / `""`（判不出或都不像）。
    """
    w, _h = probe_resolution(path)
    if not w:
        return ""
    if w >= W_1080:
        return "1080P 成片"
    if w <= W_480_MAX:
        return "480P 小样"
    return ""


def classify(source: Any, path: str = "", resolution: Any = "") -> str:
    r"""把一条片段归类成**用户看得懂的名字**。

    依据是生成时写下的 provenance（`source` 字段）—— 我们一路都在记：
      · `generate_preview(480p,draft)`  → 480P 小样
      · `generate_final(1080p,from_draft)` → 1080P 成片（**升级**出来的）
      · 含 `voiced` / `_voiced`        → 配音版
      · `recovered`                    → 认回来的（分镜重建后失联又被找回）
      · `previz` / 路径在 previz 目录   → 白膜预演
    """
    s = str(source or "").lower()
    p = str(path or "").lower()
    if "voiced" in s or "_voiced" in p or p.endswith("_voiced.mp4"):
        return "配音版"
    if "recover" in s or "relink" in s:
        return "认回的片段"
    if "preview" in s or "480" in s:
        return "480P 小样"
    if "final" in s or "1080" in s:
        return "1080P 成片"
    if "previz" in s or "previz" in p:
        return "白膜预演"
    if "compose" in s or "film" in s:
        return "合成成片"
    return "片段"


def _entry(path: str, *, kind: str, cid: str = "", source: str = "",
           selected: bool = False, selectable: bool = True,
           extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    p = str(path or "")
    exists = bool(p) and os.path.exists(p)
    size = 0
    if exists:
        try:
            size = os.path.getsize(p)
        except OSError:
            size = 0
    e: Dict[str, Any] = {
        "id": cid or os.path.basename(p),
        "candidate_id": cid,
        "kind": kind,
        "source": source,
        "path": p,
        "filename": os.path.basename(p),
        "url": _url(p) if exists else "",
        "exists": exists,
        "size_bytes": size,
        "selected": bool(selected),
        # ★★ 「能不能被"就用这一版"选中」——**白膜不是候选片段**，
        #   它不在 `shot.candidates` 里，让界面给它画"就用这一版"，
        #   点下去只会收到 400（实测在 HTTP 审计里当场踩到）。
        "selectable": bool(selectable) and bool(cid),
    }
    if extra:
        e.update(extra)
    # ★★ 2026-09-29（用户第 3、9 条）：把**文件实际的分辨率**也带上。
    #
    #   为什么必须带：用户这 5 条片段的 `source` 全是 `recovered`（来源记录丢了），
    #   于是"成片素材"页把它们列成「认回的片段」，流水线也判「0/5 成片」——
    #   **一路催他花钱出 1080P**。可 ffmpeg 一问：**全都是 1920x1088**。
    #   所以列表里要如实标出"文件本身是什么画质"，用户才不会被标签误导。
    #
    #   ⚠ 只对**来源说不清**的条目探测（认回的/配音版），否则每列一次都要
    #     为每条 ffmpeg 一次；而且进程内有缓存（`_RES_CACHE`），不会反复问。
    if exists and kind in ("认回的片段", "配音版", "其它片段", ""):
        try:
            w, h = probe_resolution(p)
            if w:
                e["resolution"] = "%dx%d" % (w, h)
                tbf = tier_by_resolution(p)
                if tbf and tbf != kind:
                    e["tier_by_file"] = tbf
                    e["kind_note"] = (
                        "库里只记着「%s」，但**文件实际是 %s 画质**（%dx%d）——"
                        "流水线会按实际画质认它。" % (kind or "未记录", tbf, w, h))
        except Exception:  # noqa: BLE001
            pass
    return e


def list_media_assets(db, pid: str) -> Dict[str, Any]:
    r"""按镜头归拢所有**已经生成出来**的视频素材。"""
    shots = sorted(db.list_shots(pid) or [], key=lambda s: s.get("order_index") or 0)
    out_shots: List[Dict[str, Any]] = []
    n_previz = n_draft = n_final = n_voiced = n_missing = 0

    for s in shots:
        items: List[Dict[str, Any]] = []
        # ① 白膜预演
        pv = str(s.get("previz_path") or "")
        if pv:
            e = _entry(pv, kind="白膜预演", source="previz", cid="previz",
                       selectable=False)
            items.append(e)
            n_previz += 1 if e["exists"] else 0
            n_missing += 0 if e["exists"] else 1
        # ② 每一版候选片段
        sel = str(s.get("selected_candidate_id") or "")
        for c in (_load(s.get("candidates"), []) or []):
            if not isinstance(c, dict):
                continue
            cid = str(c.get("candidate_id") or c.get("id") or "")
            kind = classify(c.get("source"), c.get("path"))
            e = _entry(str(c.get("path") or ""), kind=kind, cid=cid,
                       source=str(c.get("source") or ""),
                       selected=bool(cid) and cid == sel,
                       extra={"added_at": c.get("added_at") or c.get("created_at") or ""})
            items.append(e)
            if not e["exists"]:
                n_missing += 1
            elif kind == "配音版":
                n_voiced += 1
            elif kind == "480P 小样":
                n_draft += 1
            elif kind == "1080P 成片":
                n_final += 1
        out_shots.append({
            "shot_id": s.get("id"),
            "order": (s.get("order_index") or 0) + 1,
            "title": (str(s.get("layer1_overview") or "")[:40]
                      or ("第 %d 镜" % ((s.get("order_index") or 0) + 1))),
            "duration_seconds": s.get("duration_seconds"),
            "resolution": s.get("resolution"),
            "status": s.get("status"),
            "previz_status": s.get("previz_status"),
            "final_approved": bool(s.get("final_approved")),
            "selected_candidate_id": sel,
            "items": items,
            "has_previz": any(i["kind"] == "白膜预演" and i["exists"] for i in items),
            "has_draft": any(i["kind"] == "480P 小样" and i["exists"] for i in items),
            "has_final": any(i["kind"] == "1080P 成片" and i["exists"] for i in items),
            "has_voiced": any(i["kind"] == "配音版" and i["exists"] for i in items),
        })

    # ③ 合成出来的成片（项目级：outputs/<pid>/ 下的 film*.mp4 / *_film.mp4）
    films: List[Dict[str, Any]] = []
    try:
        root = Path(os.environ.get("VIDEOFORGE_DATA_DIR") or os.getcwd()) / "outputs" / str(pid)
        if root.exists():
            for f in sorted(root.glob("*.mp4"), key=lambda x: -x.stat().st_mtime)[:8]:
                films.append(_entry(str(f), kind="合成成片", source="compose",
                                    cid=f.stem))
    except Exception as e:  # noqa: BLE001
        logger.warning("列合成成片失败：%s", e)

    # ★ 按**文件实际画质**再数一遍 —— 库里的来源标签可能不可靠
    #   （实测：5 条全是 1080P，但标签写着"认回的片段"，于是上面那两个数字都是 0）。
    n_final_by_file = 0
    n_draft_by_file = 0
    for sh in out_shots:
        for it in (sh.get("items") or []):
            if not it.get("exists"):
                continue
            if it.get("tier_by_file") == "1080P 成片" or it.get("resolution"):
                res = str(it.get("resolution") or "")
                m = None
                try:
                    import re as _re
                    m = _re.match(r"(\d+)x(\d+)", res)
                except Exception:  # noqa: BLE001
                    m = None
                if m:
                    w = int(m.group(1))
                    if w >= W_1080:
                        n_final_by_file += 1
                    elif w <= W_480_MAX:
                        n_draft_by_file += 1

    return {
        "ok": True,
        "project_id": pid,
        "shots": out_shots,
        "films": films,
        "totals": {
            "shots": len(out_shots),
            "previz": n_previz,
            "draft_480p": n_draft,
            "final_1080p": n_final,
            "voiced": n_voiced,
            "films": len(films),
            # ★ 库里有记录、但**文件不在**的条数 —— 如实报出来
            "missing_files": n_missing,
            # ★ 按**实际分辨率**数出来的（上面两个是按库里的来源标签数的）。
            #   两个都给，用户/界面就能看出"标签说没有、文件其实有"。
            "final_1080p_by_file": n_final_by_file,
            "draft_480p_by_file": n_draft_by_file,
        },
        "note": ("这些就是**已经生成出来**的视频。白膜是本地免费渲的；"
                 "480P / 成片是按次计费的，所以能复用就复用（选中哪一版就用哪一版）。"
                 + (f" ⚠ 有 {n_missing} 条记录的文件已经不在磁盘上了。"
                    if n_missing else "")
                 + ("　★ 注意：按**文件实际画质**数，有 %d 条是 1080P 档"
                    "（库里的来源标签没记全，所以上面按标签数的那两个数字偏低）。"
                    % n_final_by_file if n_final_by_file else "")),
    }

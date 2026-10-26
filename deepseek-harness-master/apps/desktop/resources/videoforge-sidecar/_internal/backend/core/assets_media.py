# -*- coding: utf-8 -*-
"""资产媒体聚合：**一个项目里所有能给人看的东西**，一处取齐。

为什么要有这个模块（2026-09-27 第 49 轮，用户第 5 条）：
    用户的原话是「**资产还包括已经生成的视频**」，以及
    「AI对话框还是不支持看图片和视频，我需要直接支持到对话框里面直接点击查看」。
    但在后端，这些东西**散在四个地方**：

        角色定妆照 / 场景图   `characters.reference_image_path` / `scenes....`
        镜头片段（已生成的）  `shots.candidates[].path`（还带 `url`）
        白膜视频              `<data>/cache/previz/<shot_id>.mp4`（**磁盘上**，
                              不在库里；库里只有 `previz_status`）
        成片                  `<data>/outputs/<pid>/*.mp4`（**磁盘上**）

    界面要显示它们，就得自己拼四条路 —— 而且白膜和成片**必须扫盘**
    （库里没有它们的路径），这件事放在前端做既慢又容易漏。
    所以这里提供一个**只读聚合**：给 pid，返回上面四类的统一形状，
    每项都带 `url`（`/api/media?path=...`，界面直接能 <img>/<video>）。

★ 只读：这个模块**只 SELECT + 列目录**，绝不写库、绝不删文件。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

logger = logging.getLogger("videoforge.assets_media")

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")
VIDEO_EXT = (".mp4", ".mov", ".webm", ".mkv", ".m4v")


def _url_of(path: Any) -> str:
    """把本地路径变成界面能直接用的 URL（和应用其它地方同一套）。"""
    s = str(path or "").strip()
    if not s:
        return ""
    if s.startswith(("http://", "https://", "/api/")):
        return s
    return "/api/media?path=" + quote(s)


def _size_mb(path: Path) -> float:
    try:
        return round(path.stat().st_size / 1048576.0, 2)
    except Exception:  # noqa: BLE001
        return 0.0


def _data_dir() -> Path:
    return Path(os.environ.get("VIDEOFORGE_DATA_DIR") or os.getcwd())


def _json_list(v: Any) -> List[Any]:
    import json
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip():
        try:
            d = json.loads(v)
            return d if isinstance(d, list) else []
        except Exception:  # noqa: BLE001
            return []
    return []


def _shot_videos(shot: Dict[str, Any]) -> List[Dict[str, Any]]:
    """一个镜头**已经生成出来**的片段（候选）—— 用户要的"已生成的视频"。"""
    out: List[Dict[str, Any]] = []
    for i, c in enumerate(_json_list(shot.get("candidates"))):
        if not isinstance(c, dict):
            continue
        p = c.get("path") or c.get("file") or c.get("local_path") or ""
        u = c.get("url") or _url_of(p)
        if not (p or u):
            continue
        sel = str(shot.get("selected_candidate_id") or "")
        out.append({
            "index": i,
            "path": str(p),
            "url": str(u),
            "kind": "video",
            "duration": c.get("duration") or shot.get("duration_seconds"),
            "selected": bool(sel) and str(c.get("id") or "") == sel,
            "exists": bool(p) and Path(str(p)).is_file(),
            "size_mb": _size_mb(Path(str(p))) if p and Path(str(p)).is_file() else 0.0,
        })
    return out


def _previz_video(shot: Dict[str, Any], data_dir: Path) -> Optional[Dict[str, Any]]:
    """白膜视频：**磁盘上**（`<data>/cache/previz/<shot_id>.mp4`），库里没有路径。"""
    sid = str(shot.get("id") or "")
    if not sid:
        return None
    cands = [shot.get("previz_path"),
             str(data_dir / "cache" / "previz" / ("%s.mp4" % sid))]
    for p in cands:
        if p and Path(str(p)).is_file():
            return {"shot_id": sid, "path": str(p), "url": _url_of(p), "kind": "video",
                    "status": str(shot.get("previz_status") or "none"),
                    "size_mb": _size_mb(Path(str(p)))}
    return None


def _loose_list(v: Any) -> List[Any]:
    """把「看起来像数组、其实是**单引号 Python 字面量**的字符串」解析成 list。

    ★★ 为什么需要它（2026-09-27 第 53 轮，**在用户真实库上实测出来的形状**）：

        `db.get_script(pid)["scenes"]` 本身**已经是 list**（5 项）✓，
        但每一项里的字段是**字符串形式的 Python repr**，不是 JSON：

            "scene_number": "1"                              ← 字符串，不是数字
            "characters":   "['陈砚', '稽查兵丁甲', '稽查兵丁乙']"
            "dialogues":    "[{'character': '陈砚', 'text': '…', 'emotion': '迷茫'}]"

        上一轮我照着函数签名猜"应该是 JSON"，用 `json.loads` 解析 ⇒ 全空 ✗
        （测试 5 条红，只好整体回退）。这一轮的结论是**先看真实数据再写代码**：
        `json.loads` 解析不了单引号 → 回落 `ast.literal_eval` ✓；
        两者都失败就返回 []（绝不抛，界面不能因为一个字段挂掉）。
    """
    import ast
    if isinstance(v, list):
        return v
    if not isinstance(v, str) or not v.strip():
        return []
    t = v.strip()
    try:
        d = json.loads(t)
        return d if isinstance(d, list) else []
    except Exception:  # noqa: BLE001
        pass
    try:
        d = ast.literal_eval(t)
        return list(d) if isinstance(d, (list, tuple)) else []
    except Exception:  # noqa: BLE001
        return []


def _scene_no(v: Any) -> Optional[int]:
    """场号：真实库里是字符串 "1"（不是数字 1）—— 统一转成 int 再比较。"""
    try:
        return int(str(v).strip())
    except Exception:  # noqa: BLE001
        return None


def script_grounding(db, pid: str) -> Dict[str, Any]:
    """★ 第 6 条（用户）：「资产要跟按剧本的实际情况生成对应的资产，不要脱离剧本」。

    出图那一步本来就把剧本上下文喂进提示词了（`casting.context_from_db`），
    但**界面上看不出来**这个角色到底来自剧本哪里 —— 用户没法判断
    "这个陈砚是不是我剧本里的陈砚"。这里给出**可核验的依据**（只读）：

        characters: {角色名: {"scenes": [场号…], "lines": 台词句数}}
        scenes:     {场景名: [场号…]}

    ★ 台词句数**只从 `dialogues` 里数**，不从正文正则数 ——
      实测正文里 `人物：`/`动作：`/`爽点：`/`时长：`/`可见代价：` 这些**字段标签**
      会被"名字："的正则一起认成说话人（10 个"说话人"里 5 个是标签）✗。
    """
    out: Dict[str, Any] = {"characters": {}, "scenes": {}}
    try:
        sc = db.get_script(pid) or {}
    except Exception:  # noqa: BLE001
        logger.warning("读剧本失败（资产不带剧本依据）", exc_info=True)
        return out
    for s in _loose_list(sc.get("scenes")):
        if not isinstance(s, dict):
            continue
        no = _scene_no(s.get("scene_number"))
        title = str(s.get("title") or s.get("location") or "").strip()
        if title:
            out["scenes"].setdefault(title, [])
            if no is not None and no not in out["scenes"][title]:
                out["scenes"][title].append(no)
        named = set()
        for c in _loose_list(s.get("characters")):
            name = str(c or "").strip()
            if name:
                named.add(name)
        for d in _loose_list(s.get("dialogues")):
            if isinstance(d, dict) and str(d.get("character") or "").strip():
                named.add(str(d["character"]).strip())
        for name in named:
            rec = out["characters"].setdefault(name, {"scenes": [], "lines": 0})
            if no is not None and no not in rec["scenes"]:
                rec["scenes"].append(no)
        for d in _loose_list(s.get("dialogues")):
            if not isinstance(d, dict):
                continue
            nm = str(d.get("character") or "").strip()
            if nm and nm in out["characters"]:
                out["characters"][nm]["lines"] += 1
    for rec in out["characters"].values():
        rec["scenes"] = sorted(rec["scenes"])
    return out


def project_media(db, pid: str, *, max_films: int = 30) -> Dict[str, Any]:
    """聚合一个项目的：角色图 / 场景图 / 镜头片段 / 白膜 / 成片。只读。

    返回结构（每项都带 `url`，界面直接可用）：

        {
          "project_id": ...,
          "characters": [{"name","image","url","has_image"}],
          "scenes":     [{"name","image","url","has_image"}],
          "shots":      [{"shot_id","index","overview","character_ids",
                          "videos":[...], "previz": {...}|None}],
          "films":      [{"name","path","url","size_mb","mtime"}],
          "counts":     {"characters":n,"scenes":n,"shot_videos":n,
                          "previz":n,"films":n},
          "note": "…给用户看的一句话…",
        }
    """
    data_dir = _data_dir()
    chars = list(db.list_characters(pid) or [])
    scenes = list(db.list_scenes(pid) or [])
    shots = list(db.list_shots(pid) or [])

    def _asset(a: Dict[str, Any]) -> Dict[str, Any]:
        p = str(a.get("reference_image_path") or "").strip()
        return {
            "id": a.get("id"), "name": a.get("name"),
            "image": p, "url": _url_of(p), "has_image": bool(p) and Path(p).is_file(),
            "description": str(a.get("description") or "")[:200],
            "costume_main": str(a.get("costume_main") or "")[:200],
            # ★ 第 9 条：角色固有音色（列可能还没有 —— 老库靠迁移补，
            #   这里用 .get，没有也不报错）
            "voice_id": str(a.get("voice_id") or ""),
        }

    out_shots: List[Dict[str, Any]] = []
    n_clips = n_previz = 0
    for i, s in enumerate(shots):
        vids = _shot_videos(s)
        pv = _previz_video(s, data_dir)
        n_clips += len(vids)
        n_previz += 1 if pv else 0
        out_shots.append({
            "shot_id": s.get("id"),
            "index": s.get("order_index", i),
            "duration": s.get("duration_seconds"),
            "status": s.get("status"),
            "overview": str(s.get("layer1_overview") or "")[:300],
            "character_ids": _json_list(s.get("character_ids")),
            "videos": vids,
            "previz": pv,
        })

    # 成片：`outputs/<pid>/*.mp4`（库里没有记录，必须扫盘）
    #   ★ 实测的目录布局（用户真实机器上）：`outputs/<pid>/final.mp4` 是成片，
    #     而**镜头片段**在 `outputs/<pid>/shots/<shot_id>.mp4` ✗ ——
    #     不排掉 `shots/` 子目录，就会把每个镜头片段都当"成片"列出来
    #     （第一版就是这样，测试里 films=2 露出来的）。
    films: List[Dict[str, Any]] = []
    od = data_dir / "outputs" / str(pid)
    if od.is_dir():
        files = [p for p in od.rglob("*")
                 if p.is_file() and p.suffix.lower() in VIDEO_EXT
                 and "shots" not in [q.lower() for q in p.relative_to(od).parts[:-1]]]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[:max_films]:
            films.append({"name": p.name, "path": str(p), "url": _url_of(p),
                          "size_mb": _size_mb(p),
                          "mtime": int(p.stat().st_mtime)})

    ground = script_grounding(db, pid)

    def _grounded(a: Dict[str, Any], kind: str) -> Dict[str, Any]:
        d = _asset(a)
        if kind == "character":
            g = ground["characters"].get(d["name"]) or {"scenes": [], "lines": 0}
        else:
            g = {"scenes": ground["scenes"].get(d["name"], []), "lines": 0}
        d["script"] = g
        d["in_script"] = bool(g["scenes"] or g["lines"])
        return d

    ch = [_grounded(a, "character") for a in chars]
    sc = [_grounded(a, "scene") for a in scenes]
    n_img = sum(1 for a in ch + sc if a["has_image"])
    return {
        "project_id": pid,
        "characters": ch,
        "scenes": sc,
        "shots": out_shots,
        "films": films,
        "data_dir": str(data_dir),
        "counts": {"characters": len(ch), "scenes": len(sc),
                   "in_script": sum(1 for a in ch + sc if a["in_script"]),
                   "with_image": n_img, "shot_videos": n_clips,
                   "previz": n_previz, "films": len(films)},
        "note": ("资产：%d 个角色、%d 个场景（%d 个已有图）；"
                 "镜头片段 %d 段、白膜 %d 段、成片 %d 个。"
                 % (len(ch), len(sc), n_img, n_clips, n_previz, len(films))),
    }

# -*- coding: utf-8 -*-
"""VideoForge · 剧本体检（把"成品剧本的写法"变成**可校验**的检查）

★★ 用户的要求（2026-09-14）：
    「剧本生成的话，之前我给过你直接的项目成品了。剧本上可以多向它学习，
      方法再融入到我们剧本生成里面」

那份成品就在本机：`%LOCALAPPDATA%\\VideoForge\\data\\cache\\imports\\...\\`
**《40集农村微短剧剧本：寒潮抢收玉米（全集分集台词+镜头）》**（426KB PDF）。
我把它的结构**量了一遍**（不是凭感觉写"要专业一点"），得到这些真实签名：

| 指标 | 实测（40 集全量） |
|---|---|
| 每集标题 | **4–10 字**（平均 6.6），全是"钩子"：`寒潮预警，全村慌了` / `有人带头拦着不卖` |
| 每集台词 | **0–3 句**（平均 1.35）；纯画面集靠"字幕"点题 |
| 每集正文 | **32–83 字**（平均 52.2）—— 一集只有一两句话，不写小说 |
| 画面说明 | 33/40 集显式写 `镜头：…`（地点 + 主体动作 + 天光） |
| 表演提示 | 4/40 集在角色名后写 `（喇叭喊话）（高声煽动）（走心台词）` |
| 前史动机 | 集中在揭示段（第 11–14 集：`我爹当年…`） |
| 群像摇摆 | 8/40 集（`议论 / 动摇 / 后悔 / 沉默 / 两人争执`） |
| 收尾 | 金句 + 字幕点题：`不贪即是赚，止损方为赢` |

所以"向它学习"不是往提示词里塞一句"要专业"，而是：
  ① 把上面这些**具体的量**写进生成要求（见 `llm.SCRIPT_GENERATION_SYSTEM`）；
  ② 生成完**按同一把尺子量一遍**（本模块）—— 不合格的地方指名道姓列出来。
  **方法只有能被检验，才算真的融进去了。**

纯函数，不碰数据库、不碰网络。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional


logger = logging.getLogger("videoforge.scriptaudit")


# ── 从成品剧本量出来的区间（改这些数字前请先重新量一遍真实物料）──
HOOK_TITLE_MIN, HOOK_TITLE_MAX = 4, 12      # 成品 4–10 字；上限放到 12 是留余量
LINES_PER_SCENE_MAX = 3                     # 成品 0–3 句
BODY_CHARS_MIN, BODY_CHARS_MAX = 15, 140    # 成品 32–83 字
CHARS_PER_SECOND = 4.0                      # 中文口语（与其它模块同源）
LENGTH_TOLERANCE = 1.25

_HISTORY_RE = re.compile(r"(当年|十年前|多年前|父亲|母亲|我爹|我娘|以前|曾经|旧事|往事|旧伤|那一年)")
_CROWD_RE = re.compile(r"(议论|动摇|犹豫|后悔|沉默|两拨|附和|质疑|慌了|争执|围观|纷纷|交头接耳|不敢)")
_ANTAG_ACT_RE = re.compile(r"(煽动|鼓动|挑拨|拦|堵|怼|嘲|骗|威胁|施压|造谣|带节奏|赌|哄|吓|忽悠|洗脑)")
_CLOSING_RE = re.compile(r"(才是|不是.*是|如此|亦然|止损|不贪|记住|终究|从来|无非|不过|与其|宁可)")


def _clean(s: Any) -> str:
    return re.sub(r"\s", "", str(s or ""))


def _scene_text(s: Dict[str, Any]) -> str:
    """把一场戏里所有**能读到的文字**拼起来（用于长度/线索检查）。"""
    parts: List[str] = [str(s.get("title") or ""), str(s.get("location") or "")]
    for k in ("actions", "action"):
        v = s.get(k)
        if isinstance(v, list):
            parts.extend(str(x) for x in v)
        elif v:
            parts.append(str(v))
    for d in (s.get("dialogues") or []):
        if isinstance(d, dict):
            parts.append(str(d.get("text") or ""))
        else:
            parts.append(str(d))
    parts.append(str(s.get("subtitle") or ""))
    parts.append(str(s.get("narration") or ""))
    return "\n".join(p for p in parts if p)


def _lines_of(s: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for d in (s.get("dialogues") or []):
        if isinstance(d, dict) and str(d.get("text") or "").strip():
            out.append(d)
        elif isinstance(d, str) and d.strip():
            out.append({"text": d, "character": ""})
    return out


def audit_script(script: Optional[Dict[str, Any]],
                 target_duration: Optional[int] = None) -> Dict[str, Any]:
    """按成品剧本的写法给这份剧本做体检。

    返回：
      `{"score", "errors", "warnings", "checks": [{id,level,title,detail,evidence}],
        "valid_for_review", "stats", "target_duration"}`

    计分沿用 `core.promptaudit` 的同一把尺子（参考库 `audit_prompt.py` 的做法）：
      `score = 100 - errors*15 - warnings*5`，`valid_for_review = (errors == 0)`。
    """
    sc = script if isinstance(script, dict) else {}
    if isinstance(sc.get("outline"), str) and not sc.get("scenes"):
        try:
            sc = json.loads(sc["outline"]) or {}
        except Exception:
            sc = {}
    scenes = [s for s in (sc.get("scenes") or []) if isinstance(s, dict)]
    checks: List[Dict[str, Any]] = []

    def add(cid: str, level: str, title: str, detail: str = "", evidence: str = ""):
        checks.append({"id": cid, "level": level, "title": title,
                       "detail": detail, "evidence": evidence[:200]})

    stats: Dict[str, Any] = {"scenes": len(scenes)}

    # ── 硬条件 ──
    if not scenes:
        add("S0", "error", "剧本里没有任何场次", "生成失败或未生成", "")
        return _finish(checks, stats, target_duration)

    # S1 每场必须有画面信息（成品 33/40 显式写"镜头：…"，其余也都有具体画面）
    bad_shot = []
    for i, s in enumerate(scenes):
        acts = s.get("actions") or s.get("action") or []
        if isinstance(acts, str):
            acts = [acts]
        has_act = any(str(a).strip() for a in acts)
        if not has_act or not str(s.get("location") or "").strip():
            bad_shot.append(f"第{s.get('scene_number') or i+1}场"
                            if not has_act else f"第{s.get('scene_number') or i+1}场（缺地点）")
    if bad_shot:
        add("S1", "error", "有场次没有画面/动作说明",
            f"{len(bad_shot)}/{len(scenes)} 场缺地点或动作", "、".join(bad_shot[:6]))

    # S2 每场必须有**可听内容**：台词，或（纯画面场）字幕/旁白
    silent = []
    for i, s in enumerate(scenes):
        if not _lines_of(s) and not (_clean(s.get("subtitle")) or _clean(s.get("narration"))):
            silent.append(f"第{s.get('scene_number') or i+1}场")
    if silent:
        add("S2", "error", "有场次既没有台词也没有字幕/旁白",
            f"{len(silent)}/{len(scenes)} 场是哑场",
            "、".join(silent[:6]))

    # S3 时长必须等于目标（后端已强制；这里再核一遍，防手工改过）
    total = 0
    for s in scenes:
        try:
            total += int(round(float(s.get("duration_seconds") or 0)))
        except Exception:
            pass
    stats["total_seconds"] = total
    if target_duration:
        if total != int(target_duration):
            add("S3", "error", "总时长与目标不一致",
                f"目标 {target_duration}s，实际 {total}s", "")
        else:
            add("S3", "ok", f"总时长 = 目标 {target_duration}s", "", "")

    # ── 软条件（成品都做到了，做不到会显得"不像那个水平的剧本"）──
    # S4 每场台词句数（成品 0–3，平均 1.35）
    too_many = []
    for i, s in enumerate(scenes):
        n = len(_lines_of(s))
        if n > LINES_PER_SCENE_MAX:
            too_many.append(f"第{s.get('scene_number') or i+1}场{n}句")
    stats["lines_total"] = sum(len(_lines_of(s)) for s in scenes)
    stats["lines_avg"] = round(stats["lines_total"] / len(scenes), 2) if scenes else 0
    if too_many:
        add("S4", "warn", f"有场次台词超过 {LINES_PER_SCENE_MAX} 句（成品每场 0–3 句）",
            f"{len(too_many)} 场偏多", "、".join(too_many[:6]))

    # S5 单句台词长度 vs 该场秒数（4 字/秒）
    long_lines = []
    for s in scenes:
        try:
            dur = float(s.get("duration_seconds") or 0)
        except Exception:
            dur = 0.0
        cap = max(6, int(dur * CHARS_PER_SECOND * LENGTH_TOLERANCE))
        for d in _lines_of(s):
            n = len(_clean(d.get("text")))
            if dur and n > cap:
                long_lines.append(f"{n}字/{dur:g}s「{_clean(d.get('text'))[:10]}」")
    if long_lines:
        add("S5", "warn", "有台词比它的时长能装下的更长（会被迫变速）",
            f"{len(long_lines)} 句", "、".join(long_lines[:5]))

    # S6 每场正文长度（成品 32–83 字；写成长篇会挤掉画面）
    fat = []
    for i, s in enumerate(scenes):
        n = len(_clean(_scene_text(s)))
        if n > BODY_CHARS_MAX:
            fat.append(f"第{s.get('scene_number') or i+1}场{n}字")
    if fat:
        add("S6", "warn", f"有场次正文超过 {BODY_CHARS_MAX} 字（成品每场 32–83 字）",
            f"{len(fat)} 场偏长", "、".join(fat[:6]))

    # S7 标题要是"钩子"（成品 4–10 字，短而抓人）
    weak_titles = []
    for i, s in enumerate(scenes):
        t = _clean(s.get("title"))
        if not t or len(t) < HOOK_TITLE_MIN or len(t) > HOOK_TITLE_MAX:
            weak_titles.append(f"第{s.get('scene_number') or i+1}场"
                               f"{('（无标题）' if not t else f'「{t[:14]}」{len(t)}字')}")
    if weak_titles:
        add("S7", "warn", f"标题不是短钩子（成品 {HOOK_TITLE_MIN}–{HOOK_TITLE_MAX} 字）",
            f"{len(weak_titles)} 场", "、".join(weak_titles[:6]))

    # S8 主角要有**前史动机**（成品：`我爹当年和你们一样…`）
    chars = [c for c in (sc.get("characters") or []) if isinstance(c, dict)]
    whole = "\n".join(_scene_text(s) for s in scenes) + "\n" + json.dumps(
        chars, ensure_ascii=False)
    if not _HISTORY_RE.search(whole):
        add("S8", "warn", "没看到主角的前史动机（成品靠它解释他为什么这么选）",
            "整份剧本里没有出现 当年/以前/父亲… 一类前史线索",
            _clean(sc.get("logline"))[:60])

    # S9 反派要有**行为方式**（成品：大嘴"爱煽动、赌行情"，而不是"他很坏"）
    antags = [c for c in chars
              if str(c.get("role") or "").lower() in ("antagonist", "反派")
              or re.search(r"(反派|对手)", str(c.get("role") or ""))]
    if antags:
        bad = [str(c.get("name")) for c in antags
               if not _ANTAG_ACT_RE.search(json.dumps(c, ensure_ascii=False) + whole)]
        if bad:
            add("S9", "warn", "反派的行为方式没写出来（只写了坏，没写他怎么坏）",
                "、".join(bad[:4]), "")
    else:
        add("S9", "warn", "剧本没有标出反派/对手角色", "对立体不明确，冲突会软", "")

    # S10 群像要**摇摆**（成品：村民分化→动摇→后悔→和解）
    if not _CROWD_RE.search(whole):
        add("S10", "warn", "没有群像摇摆的痕迹（成品靠群众议论/动摇/后悔撑住冲突）",
            "整份剧本里没有 议论/动摇/犹豫/后悔/两拨… 一类转变", "")

    # S11 收尾要有**金句/点题**（成品：`不贪即是赚，止损方为赢`）
    last = scenes[-1]
    tail = _clean(last.get("subtitle")) or _clean(
        (_lines_of(last) or [{}])[-1].get("text")) or _clean(
        (last.get("actions") or [""])[-1] if isinstance(last.get("actions"), list) else "")
    if not tail or not (_CLOSING_RE.search(tail) or len(tail) <= 24):
        add("S11", "warn", "结尾不像金句点题（成品结尾是短而狠的一句 + 字幕）",
            f"末场收尾文本：{tail[:40] or '（空）'}", "")

    return _finish(checks, stats, target_duration)


def _finish(checks: List[Dict[str, Any]], stats: Dict[str, Any],
            target_duration: Optional[int]) -> Dict[str, Any]:
    errors = sum(1 for c in checks if c["level"] == "error")
    warnings = sum(1 for c in checks if c["level"] == "warn")
    score = max(0, 100 - errors * 15 - warnings * 5)
    return {"score": score, "errors": errors, "warnings": warnings,
            "checks": checks, "valid_for_review": errors == 0,
            "stats": stats, "target_duration": target_duration,
            "note": ("按用户提供成品剧本《寒潮抢收玉米》量出来的写法检查"
                     "（标题钩子/每场镜头+台词/前史动机/反派行为/群像摇摆/金句收尾）")}


def audit_project_script(script_row: Optional[Dict[str, Any]],
                         target_duration: Optional[int] = None) -> Dict[str, Any]:
    """给接口用：直接吃 `db.get_script()` 的行（outline 是 JSON 字符串）。"""
    row = script_row or {}
    doc: Dict[str, Any] = {}
    out = row.get("outline")
    if isinstance(out, str):
        try:
            doc = json.loads(out) or {}
        except Exception:
            doc = {}
    elif isinstance(out, dict):
        doc = out
    if not doc.get("scenes"):
        doc["scenes"] = row.get("scenes") or []
    tgt = target_duration
    if tgt is None:
        dp = doc.get("duration_plan") if isinstance(doc, dict) else None
        if isinstance(dp, dict) and dp.get("target"):
            tgt = int(dp["target"])
    return audit_script(doc, tgt)

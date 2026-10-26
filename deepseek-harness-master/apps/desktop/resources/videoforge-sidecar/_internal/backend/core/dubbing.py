"""
VideoForge · 整片配音（把 `/voice/dub` 的逻辑从路由里搬出来）

════════════════════════════════════════════════════════════════════════
为什么要单独一个模块（2026-09-27 第 70 轮）
════════════════════════════════════════════════════════════════════════
用户的原话：

    「生成完**自动下载 + 记账** …… 整条流程**用软件自己跑通**（不是靠我的脚本兜底）」
    「目前出的视频没声音，我需要**每个角色都有自己适配的音色**」

而"按角色配音"这套逻辑**原来只活在 `main.py` 的一个路由闭包里**
（`POST /api/projects/{pid}/voice/dub`）：

  · agent 用不了它 —— 它不是 `@REGISTRY.register` 的工具，
    模型在对话里看不到"给整片配音"这个能力；
  · 只能靠 HTTP 调（我上一轮就是写了个 `scripts/_dub_and_compose.py`
    用 TestClient 打自己 → 那正是用户说的"靠脚本兜底"✗）；
  · 没法单测。

所以把它搬到这里，路由和 agent 工具**都调同一个函数**。
**行为一字不改**（选角/回填台词/挂音轨/设为选中候选都照抄），只是搬家 + 能被别人调。

════════════════════════════════════════════════════════════════════════
它到底做了什么
════════════════════════════════════════════════════════════════════════
  ① 拉**真实音色目录**（各厂商 list_voices），按角色卡的性别/年龄/定位/
     personality 打分选音色，并给出**可读的理由**；
  ② 整片一次定死（同一角色全片同一把嗓子，且尽量不撞音）；
  ③ 逐句按情绪算韵律（语速/音高/音量 + 气口/尾拍）；
  ④ 音轨**挂进这一镜**（只替换音轨、视频流原样 copy）——
     所以**不用重新调用视频模型就能听到对白**（对已有素材有效，零成本）；
  ⑤ 没有台词的分镜**不加旁白**，只如实说明。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

logger = logging.getLogger("videoforge.dubbing")


def _outputs_root(db) -> str:
    try:
        s = db.get_all_settings() or {}
    except Exception:  # noqa: BLE001
        s = {}
    return str(s.get("output_dir")
               or os.path.join(os.environ.get("VIDEOFORGE_DATA_DIR", os.getcwd()), "outputs"))


#: 引擎质量排序（**默认口径**，2026-09-27 第 71 轮）。
#:
#: 用户原话：「配音也是很大的问题：声音跟人物形象对不上，AI感太重
#: （你可以换渠道比如硅基流动或者 minimax 模型花钱都可以），
#: 发声太AI了，要跟上情景和人物的情感」。
#:
#: 为什么这个顺序是对的（不是拍脑袋）：
#:   · `minimax`（speech-2.8-hd）与 `siliconflow`（CosyVoice2）**支持 emotion 参数**
#:     —— 剧本的情绪能真的传进引擎；`speech-2.8-hd` 也是高保真档；
#:   · `edge` 是免费的，**不支持 emotion**（见 core/voice/edge_tts.py），
#:     只能调语速/音高/音量 —— 落在它身上就是"平铺直叙、AI 感重"。
#:   · `dashscope`（CosyVoice v2）也不错，排在 siliconflow 之后（本机实测可用性略低）。
#: 想改回"省钱优先"：在设置里填 `tts_prefer_providers`（逗号分隔）覆盖它。
DEFAULT_ENGINE_PREFERENCE = ("minimax", "siliconflow", "dashscope", "edge")


def engine_preference(settings: Optional[Dict[str, Any]]) -> List[str]:
    """这台机器上，配音引擎该按什么顺序挑（只保留**配了 Key** 的；edge 永远可用）。"""
    s = settings or {}
    raw = str(s.get("tts_prefer_providers") or "").strip()
    if raw:
        order = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
    else:
        order = list(DEFAULT_ENGINE_PREFERENCE)
    keys = (s.get("tts_api_keys") or {})
    try:
        from core.voice import dispatcher as _vd  # noqa: F401
    except Exception:  # noqa: BLE001
        pass
    out = []
    for p in order:
        if p == "edge":
            out.append(p)
            continue
        if keys.get(p):
            out.append(p)
    return out or ["edge"]


def _quality_default_voice(settings: Dict[str, Any], catalog: List[Dict[str, Any]],
                           gender: str = "") -> str:
    """给"没匹配上任何角色卡"的说话人挑一把**高保真**默认音色。

    ★ 为什么不能直接用设置里的 `default_voice`：那个默认值是
      `edge:zh-CN-XiaoxiaoNeural`（免费、且**不支持 emotion**）——
      凡是没匹配到角色的台词都会落到它身上，整片的"AI 感"主要来自这里。
      用户明说"花钱都可以"，所以在有付费高保真引擎时优先用它们的**通用音色**。
    """
    pref = engine_preference(settings)
    for prov in pref:
        if prov == "edge":
            continue
        pool = [v for v in (catalog or [])
                if str(v.get("provider")) == prov
                and (not gender or str(v.get("gender") or "") == gender)]
        if not pool:
            pool = [v for v in (catalog or []) if str(v.get("provider")) == prov]
        if pool:
            # 挑"通用叙述"最稳的一把：优先标签里带 沉稳/知性/自然 的
            def _rank(v):
                tags = set(v.get("tags") or [])
                return (0 if (tags & {"沉稳", "知性", "自然", "温和"}) else 1,
                        str(v.get("voice_id")))
            pool.sort(key=_rank)
            return str(pool[0].get("voice_id") or "")
    return str((settings or {}).get("default_voice") or "edge:zh-CN-XiaoxiaoNeural")


async def infer_genders_with_llm(db, chars: List[Dict[str, Any]],
                                 settings: Dict[str, Any]) -> Dict[str, str]:
    """角色卡读不出性别时，**让文字模型看一眼角色卡来判**（一次便宜的文字调用）。

    ★ 为什么值得多这一步（2026-09-27 第 71 轮）：
      规则只能抓**强线索**（胡须/齐肩发/庶女…）。像「年轻人」「裴鸢」「小张」
      这种卡，靠规则判不出来 ⇒ 只能"按气质挑" ⇒ 有一半概率配错性别，
      而用户的原话正是「声音跟人物形象对不上」。
      一张角色卡（年龄/外貌/服装）对模型来说是**很容易判**的，
      一次文字调用就能把整片判完 —— 用户也明确说过"花钱都可以"。

    返回 `{角色名: "男"/"女"}`（判不出来的不出现）。**任何失败都不抛异常**，
    调用方按"没推断出来"继续（绝不因为这一步失败而挡住配音）。
    """
    todo = [c for c in (chars or []) if not _known_gender(c)]
    if not todo:
        return {}
    try:
        from core.orchestrator import _get_llm_from_settings
        llm = _get_llm_from_settings(settings or {})
    except Exception as e:  # noqa: BLE001
        logger.info("建不出文字模型客户端，跳过性别推断：%s", e)
        return {}
    cards = []
    for c in todo:
        cards.append({
            "name": str(c.get("name") or ""),
            "age": str(c.get("age") or ""),
            "appearance": str(c.get("description") or "")[:300],
            "costume": str(c.get("costume_main") or "")[:160],
            "props": c.get("props") or [],
            # ★ 没有角色卡、只有台词的说话人：把他的台词给模型看
            #   —— 中文名字常能从用词/称谓看出性别，这比空着强得多。
            **({"lines": c.get("_lines") or []} if c.get("_lines") else {}),
        })
    prompt = (
        "下面是一批影视角色/说话人的卡片（中文）。请判断**每个人的性别**。\n"
        "只根据卡片本身判断（名字、年龄、外貌、服装，以及**如果有的话他说的台词**）。\n"
        "实在判不出来就填 \"不确定\"，**不要瞎猜**。\n"
        "只输出 JSON：{\"genders\": {\"角色名\": \"男\"|\"女\"|\"不确定\"}}\n\n"
        + json.dumps(cards, ensure_ascii=False)
    )
    try:
        raw = await llm.chat([
            {"role": "system", "content": "你是严谨的选角助理，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ])
    except Exception as e:  # noqa: BLE001
        logger.info("性别推断调用失败（按规则结果继续）：%s", e)
        return {}
    try:
        from core.llm import extract_json
        data = extract_json(raw if isinstance(raw, str) else str(raw)) or {}
    except Exception:  # noqa: BLE001
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            return {}
    g = (data or {}).get("genders") if isinstance(data, dict) else None
    if not isinstance(g, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in g.items():
        s = str(v or "").strip()
        if s in ("男", "男性", "male", "Male"):
            out[str(k).strip()] = "男"
        elif s in ("女", "女性", "female", "Female"):
            out[str(k).strip()] = "女"
    return out


def _known_gender(ch: Dict[str, Any]) -> str:
    """角色卡里**已经写明**的性别（读 reference_features.gender 或顶层 gender）。"""
    try:
        from core.voicecatalog import role_profile
        p = role_profile(ch)
        if p.get("gender") and not p.get("gender_inferred"):
            return str(p["gender"])
    except Exception:  # noqa: BLE001
        pass
    return ""


def persist_gender_value(db, ch: Dict[str, Any], value: str, source: str) -> bool:
    """把性别写回角色卡（`reference_features.gender`）。只在卡片本来没写时写。"""
    import json
    if str(value or "") not in ("男", "女"):
        return False
    try:
        rf = ch.get("reference_features")
        if isinstance(rf, str):
            rf = json.loads(rf) if rf.strip() else {}
        rf = dict(rf) if isinstance(rf, dict) else {}
        if str(rf.get("gender") or "").strip():
            return False
        rf["gender"] = value
        rf["gender_source"] = source
        db.update_character(ch["id"], reference_features=json.dumps(rf, ensure_ascii=False))
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("写回性别失败：%s", e)
        return False


def persist_inferred_gender(db, ch: Dict[str, Any], prof: Dict[str, Any]) -> bool:
    """把**推断出来的性别**写回角色卡（`reference_features.gender`）。

    ★ 为什么要写回：推断结论只活在内存里的话，下一次还得再推一遍；
      写回之后它就成了**显式事实** —— 界面上看得见、用户能改、
      选角也不再需要"猜"，理由里也不用再加那句免责声明。
      只在卡片**本来没有**性别时写，绝不覆盖用户填的。
    """
    import json
    g = str((prof or {}).get("gender") or "")
    if g not in ("male", "female"):
        return False
    try:
        rf = ch.get("reference_features")
        if isinstance(rf, str):
            rf = json.loads(rf) if rf.strip() else {}
        rf = dict(rf) if isinstance(rf, dict) else {}
        if str(rf.get("gender") or "").strip():
            return False
        rf["gender"] = "男" if g == "male" else "女"
        rf["gender_source"] = ("外貌/服装描述推断：" + str(prof.get("gender_evidence") or "")
                               if prof.get("gender_evidence") else "角色名/身份词推断")
        db.update_character(ch["id"],
                            reference_features=json.dumps(rf, ensure_ascii=False))
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("写回推断性别失败（不影响本次配音）：%s", e)
        return False


def _shots_out_dir(db, pid: str) -> str:
    return os.path.join(_outputs_root(db), pid, "shots")


def script_sources_for(db, pid: str) -> List[Dict[str, Any]]:
    """这一项目的**所有**剧本版本里"有台词的场"（用于给分镜回填台词）。

    ★ 用户实测「第一个镜头没有声音（剧本是有对话的）」的真实原因是：
      分镜的 `layer2_timeline` 里没有 dialogue 字段，而剧本那一场写着对话。
      这里把两版剧本（生成分镜用的 `scripts.scenes` + 工作台当前稿
      `script_scene_details`）都交给 `core.scriptlines`，
      **由它按"说话人是否真的出现在这一镜里"决定填不填**。
    """
    try:
        from core import scriptlines as _slg
        return _slg.collect_scene_sources(db.get_script(pid),
                                          db.list_scene_details(pid))
    except Exception as e:  # noqa: BLE001
        logger.warning("收集剧本台词失败（不做回填）：%s", e)
        return []


def shot_script_ctx(db, shot: Dict[str, Any]) -> Dict[str, Any]:
    """一镜回填台词要用的上下文：场景名 + 本镜角色名。"""
    name = ""
    try:
        sid = shot.get("scene_id")
        if sid:
            for sc in (db.list_scenes(shot.get("project_id")) or []):
                if sc.get("id") == sid:
                    name = str(sc.get("name") or "")
                    break
    except Exception:  # noqa: BLE001
        name = ""
    names: List[str] = []
    try:
        ids = shot.get("character_ids")
        if isinstance(ids, str):
            import json
            try:
                ids = json.loads(ids)
            except Exception:  # noqa: BLE001
                ids = []
        wanted = {str(i) for i in (ids or [])}
        for c in (db.list_characters(shot.get("project_id")) or []):
            if str(c.get("id")) in wanted:
                names.append(str(c.get("name") or ""))
    except Exception:  # noqa: BLE001
        pass
    return {"scene_name": name, "character_names": names}


def _register_voiced_candidate(db, shot: Dict[str, Any], path: str, duration: float) -> str:
    """把配好音的片段**追加**进候选并设为选中（键名两套都写，见 film/compose 的说明）。

    ★ 为什么不能只 `update_shot(candidates=[新的一条])`：
      那会把用户已有的候选（比如 1080P 成片、其它版本的样片）**整批冲掉**。
    """
    import json
    import time
    old = []
    try:
        c = shot.get("candidates") or []
        if isinstance(c, str):
            c = json.loads(c) if c.strip() else []
        if isinstance(c, list):
            old = [x for x in c if isinstance(x, dict)]
    except Exception:  # noqa: BLE001
        old = []
    for x in old:
        x["is_selected"] = False
    # ★ 同一个文件不重复登记（重跑一次不该让候选列表越长越长）。
    #   判据用**路径**而不是 id：复用场景下每次都会算出一个新的时间戳 id，
    #   但指向的是同一个文件。
    try:
        same = os.path.normcase(os.path.abspath(path))
        old = [x for x in old
               if not (x.get("path") and os.path.normcase(os.path.abspath(str(x["path"]))) == same)]
    except Exception:  # noqa: BLE001
        pass
    cid = "voiced_%d" % int(time.time())
    old.append({
        "candidate_id": cid, "id": cid,
        "path": path, "url": "/api/media?path=" + quote(path),
        "duration_seconds": duration, "is_selected": True,
        "source": "voiced",
        "size_bytes": (os.path.getsize(path) if os.path.exists(path) else 0),
        "created_at": time.time(),
    })
    db.update_shot(shot["id"], status="completed", progress=1.0,
                   candidates=old, selected_candidate_id=cid, error_message="")
    return cid


def _is_voiced_path(p: str) -> bool:
    """这个路径是不是"已经配过音的产物"？（`.../voiced/xxx_voiced.mp4`）"""
    s = str(p or "").replace("\\", "/").lower()
    return "/voiced/" in s or s.endswith("_voiced.mp4")


def _source_clip_for_dub(shot: Dict[str, Any]) -> str:
    """配音时该拿**哪一条视频**当画面底。

    ★★ 必须是"**没有配过音的原始片段**"（2026-09-27 第 71 轮，实测踩到）。

    为什么这条不能省：`dub_project` 配完音之后会把**带配音的那一版**
    设为该镜的选中候选（这是对的，合成时才拿得到声音）。于是**第二次**配音时
    `_selected_candidate_path(shot)` 返回的就是 `.../voiced/<id>_voiced.mp4`，
    而输出路径**也是**它 ⇒ ffmpeg 直接拒绝：

        Output ...\\voiced\\<id>_voiced.mp4 same as Input #0 - exiting
        Error opening output files: Invalid argument

    后果不是"报个错"那么轻 —— 它是**静默的**：每一镜都返回
    "音轨没能挂进这一镜"，配音结果被丢弃，成片继续用**旧的声音**。
    实测现场：用户要求换音色后重配，5 个镜头全部失败，桌面成片还是旧嗓音，
    而日志里只有一行 `配音轨并进画面失败`（还只打了 stderr 的尾巴 200 字，
    正好把 `same as Input` 那句切掉了）。所以这里按**候选来源**筛，
    而不是靠猜文件名。
    """
    cands = shot.get("candidates") or []
    if isinstance(cands, str):
        try:
            cands = json.loads(cands) if cands.strip() else []
        except Exception:  # noqa: BLE001
            cands = []
    good: List[Dict[str, Any]] = []
    for c in (cands if isinstance(cands, list) else []):
        if not isinstance(c, dict):
            continue
        p = str(c.get("path") or c.get("video_path") or "")
        if not p or _is_voiced_path(p):
            continue
        if not os.path.exists(p) or os.path.getsize(p) < 10240:
            continue
        good.append(c)
    if good:
        sel = str(shot.get("selected_candidate_id") or "")
        for c in good:
            if str(c.get("candidate_id") or c.get("id") or "") == sel:
                return str(c.get("path") or c.get("video_path") or "")
        # 没选中项（或选中的是配音版）→ 取**最后一条**原始片段（最新生成的）
        return str(good[-1].get("path") or good[-1].get("video_path") or "")
    # 长视频分段路径：段落本身就是原始素材
    segs = shot.get("segments") or []
    if isinstance(segs, str):
        try:
            segs = json.loads(segs) if segs.strip() else []
        except Exception:  # noqa: BLE001
            segs = []
    okseg = [s for s in (segs if isinstance(segs, list) else [])
             if isinstance(s, dict) and s.get("status") == "ok"
             and s.get("path") and os.path.exists(str(s["path"]))
             and not _is_voiced_path(str(s["path"]))]
    if okseg:
        okseg.sort(key=lambda s: s.get("index") or 0)
        return str(okseg[-1]["path"])
    # 兜底：只剩配音版了（比如原始片段被删了）—— 那就用它，但上层会因
    # "输入==输出" 走到临时文件那条保护路径，不会静默失败。
    try:
        from core.compose import _selected_candidate_path as _scp
        return _scp(shot) or ""
    except Exception:  # noqa: BLE001
        return ""


def speakers_in_shots(shots: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """分镜台词里**实际出现过的说话人** → 他说过的几句台词（样本）。

    ★★ 为什么必须有这一步（2026-09-27 第 71 轮，实测）：
      实测《执棋者》的真实数据 —— 角色卡是「老者/年轻人/李队长/王医生/张二狗/
      裴鸢/小张」，而**分镜台词里的说话人**是「陈砚/兵丁甲/曹敬/老周/裴钧/沈阔/
      石头/阿桑/太后」。也就是说**剧本和角色卡是两套名字**（用户中途换过稿）。
      而选角只拿角色卡去建表 ⇒ 每一个真正开口的人**都匹配不上** ⇒
      全部落回默认音色（而且是 Edge 免费引擎）⇒
      用户听到的就是「声音跟人物形象对不上 + AI 感太重」。
      ⇒ 选角必须覆盖"**真的会说话的人**"，而不只是"建过卡的人"。
    """
    out: Dict[str, List[str]] = {}
    try:
        from core.dialogue import timeline_lines as _tl
    except Exception:  # noqa: BLE001
        _tl = None
    for sh in (shots or []):
        tl = sh.get("layer2_timeline")
        if isinstance(tl, str):
            try:
                tl = json.loads(tl) if tl.strip() else []
            except Exception:  # noqa: BLE001
                tl = []
        if not isinstance(tl, list) or not tl:
            continue
        lines = []
        if _tl is not None:
            try:
                lines = _tl(tl, float(sh.get("duration_seconds") or 5)) or []
            except Exception:  # noqa: BLE001
                lines = []
        for ln in (lines or []):
            who = str((ln or {}).get("character") or "").strip()
            txt = str((ln or {}).get("text") or "").strip()
            if not who:
                continue
            out.setdefault(who, [])
            if txt and len(out[who]) < 6:
                out[who].append(txt)
    return out


def cast_with_real_speakers(chars: List[Dict[str, Any]],
                            speakers: Dict[str, List[str]]) -> List[Dict[str, Any]]:
    """把"没有角色卡但真的在说话"的人补成**虚拟角色卡**，一起参与选角。

    虚拟卡只用于**本次选角**，不会写进库（不污染用户的资产列表）；
    但它的性别一旦被推断出来，会作为"这个说话人用什么声音"的依据，
    并在返回里如实报告。
    """
    try:
        from core.voicecast import _match_character, _name_index
        by = _name_index(chars or [])
    except Exception:  # noqa: BLE001
        by, _match_character = {}, None
    extra: List[Dict[str, Any]] = []
    for who, lines in (speakers or {}).items():
        hit = None
        if _match_character is not None:
            try:
                hit = _match_character(who, by)
            except Exception:  # noqa: BLE001
                hit = None
        if hit is not None:
            continue                      # 已经有角色卡，走正常选角
        extra.append({
            "name": who,
            # 把台词样本当"描述"喂给推断逻辑：名字之外，台词也是性别/气质的线索
            "description": "；".join(lines)[:220],
            "_virtual": True,
            "_lines": lines[:3],
        })
    return list(chars or []) + extra


def _existing_voiced(shot: Dict[str, Any]) -> str:
    """这一镜**已经配过音**了吗？配过就返回那个文件（别再花一次 TTS 的钱）。

    ★ 和"生成前先看本地"是同一条原则（用户明确要求过"别重复花钱"）：
      配音虽然比视频便宜得多，但整片重跑一次也是白花钱，
      而且**候选会越堆越多**（每次都 append 一条 `voiced_*`）。
    """
    import json
    cands = shot.get("candidates") or []
    if isinstance(cands, str):
        try:
            cands = json.loads(cands) if cands.strip() else []
        except Exception:  # noqa: BLE001
            cands = []
    if not isinstance(cands, list):
        return ""
    for c in cands:
        if not isinstance(c, dict):
            continue
        if str(c.get("source") or "") != "voiced":
            continue
        p = c.get("path") or ""
        try:
            if p and os.path.exists(str(p)) and os.path.getsize(str(p)) > 102400:
                return str(p)
        except Exception:  # noqa: BLE001
            continue
    return ""


async def dub_project(
    db,
    pid: str,
    *,
    select: bool = True,
    settings: Optional[Dict[str, Any]] = None,
    progress: Optional[Callable[[float, str], None]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """给整个项目的每个镜头按角色配音，并把音轨挂进该镜（不重跑视频模型）。

    返回结构与原 `/voice/dub` 路由**完全一致**（ok/cast/reasons/collisions/
    catalog/shots/warnings/tts/done/skipped/summary），路由直接把它当响应体。
    """
    def tick(p: float, msg: str) -> None:
        if progress:
            try:
                progress(p, msg)
            except Exception:  # noqa: BLE001
                pass

    from core import voicecast as _vc
    from core.dialogue import timeline_lines as _tl

    proj = db.get_project(pid)
    if not proj:
        return {"ok": False, "error": "项目不存在", "status": 404}
    shots = sorted(db.list_shots(pid) or [], key=lambda s: s.get("order_index") or 0)
    if not shots:
        return {"ok": False, "error": "这个项目还没有分镜", "status": 400}

    settings = settings if settings is not None else (db.get_all_settings() or {})
    chars = list(db.list_characters(pid) or [])
    # ★ 选角之前先把**占位符垃圾卡**剔掉（实测库里真有一个叫「角色名称」的），
    #   否则它会白占一把音色、还可能把别人的嗓子挤掉。
    try:
        from core.assets import is_placeholder_character as _is_ph
        real_chars = [c for c in chars if not _is_ph(c)]
        junk = [str(c.get("name") or "") for c in chars if _is_ph(c)]
        if junk:
            logger.info("配音选角忽略占位符角色：%s", "、".join(junk))
        chars = real_chars
    except Exception:  # noqa: BLE001
        pass

    dv = str(settings.get("default_voice") or "edge:zh-CN-XiaoxiaoNeural")

    tick(0.02, "拉取各厂商真实音色目录")
    catalog = await _vc.load_voice_catalog(settings)

    # ★ 2026-10-23（用户要求：「音色太烂了…我这个是产品宣传片！」）：
    #   **体裁 → 默认音色**。宣传片/广告/发布会/口播这类商业体裁，默认应该是
    #   成熟/主持人音色（稳、可信、不像客服），而不是少女音或 Edge 免费音。
    #   判据只用强关键词（标题/简介/剧本）；判不出来就完全不动原逻辑。
    _promo = False
    try:
        _proj = {}
        for _fn in ("get_project", "get_project_detail"):
            _f = getattr(db, _fn, None)
            if callable(_f):
                _proj = _f(pid) or {}
                break
        _blob = " ".join(str(_proj.get(k) or "") for k in
                         ("title", "logline", "synopsis", "script", "description"))
        _promo = any(w in _blob for w in (
            "宣传片", "广告", "推广片", "发布会", "口播", "产品片", "品牌片",
            "promo", "brand film", "commercial"))
    except Exception as _e:  # noqa: BLE001
        logger.warning("体裁判定失败（不切音色）：%s", _e)
    if _promo:
        _have = {str(v.get("voice_id") or "") for v in (catalog or [])}
        for _cand in ("agentai:speech-2.8:presenter_female",
                      "agentai:speech-2.8:presenter_male",
                      "agentai:speech-2.8:female-chengshu",
                      "minimax:presenter_female", "minimax:presenter_male",
                      "minimax:female-chengshu", "minimax:audiobook_female_1",
                      "minimax:audiobook_male_1"):
            if _cand in _have:
                if dv != _cand:
                    logger.info("体裁=商业宣传片 → 默认音色 %s ⇒ %s（成熟/主持，不再是少女音）",
                                dv, _cand)
                dv = _cand
                break
    pref = engine_preference(settings)
    # ★★ 默认音色也要挑**高保真**的（2026-09-27 第 71 轮）。
    #   设置里的 `default_voice` 出厂值是 `edge:zh-CN-XiaoxiaoNeural` ——
    #   免费引擎且**不支持 emotion**。凡是没有对应角色卡的说话人（"兵丁甲"
    #   "水手乙"这类）都会落到它身上，整片的"AI 感"主要来自这里。
    #   用户明说"花钱都可以"，所以在配了付费引擎时换成它们的通用音色。
    # ★★ 选角要覆盖"**真的会说话的人**"，不只是"建过卡的人"（见
    #    `speakers_in_shots` 的说明：实测剧本与角色卡是两套名字，
    #    结果每个开口的人都落回 Edge 默认音色）。
    _shots_all = sorted(db.list_shots(pid) or [],
                        key=lambda s: s.get("order_index") or 0)
    _speakers = speakers_in_shots(_shots_all)
    cast_input = cast_with_real_speakers(chars, _speakers)
    _virtual = [c["name"] for c in cast_input if c.get("_virtual")]

    # ★ 规则判不出来的性别，交给文字模型看一眼角色卡（便宜的一次调用）。
    #   不成功就按原样继续 —— 绝不因为这一步挡住配音。
    _llm_fix: List[str] = []
    _conflict: List[str] = []
    try:
        _need = [c for c in cast_input if not _known_gender(c)]
        if _need:
            got = await infer_genders_with_llm(db, cast_input, settings)
            for ch in _need:
                n = str(ch.get("name") or "").strip()
                if not got.get(n):
                    continue
                # ★ 规则推断与模型判定**不一致**时如实报出来（不静默取舍）：
                #   两者都可能错，让用户看到分歧才好判断。
                try:
                    from core.voicecatalog import role_profile as _rp
                    _r = (_rp(ch).get("gender") or "")
                    _r = ("男" if _r == "male" else "女" if _r == "female" else "")
                    if _r and _r != got[n]:
                        _conflict.append(f"{n}（外貌描述看着像{_r}声，模型判{got[n]}）")
                except Exception:  # noqa: BLE001
                    pass
                if ch.get("_virtual"):
                    # 虚拟说话人：只用于本次选角，不写库
                    ch["gender"] = "男" if got[n] == "男" else "女"
                    ch["_gender_inferred"] = got[n]
                    _llm_fix.append(f"{n}→{got[n]}（临时按台词判定，没有角色卡）")
                elif persist_gender_value(db, ch, got[n], "文字模型按角色卡判断"):
                    _llm_fix.append(f"{n}→{got[n]}")
            if any(not c.get("_virtual") for c in _need):
                # 写回之后要**重新读一遍**角色卡，让选角用上新的性别
                chars = list(db.list_characters(pid) or [])
                try:
                    from core.assets import is_placeholder_character as _is_ph2
                    chars = [c for c in chars if not _is_ph2(c)]
                except Exception:  # noqa: BLE001
                    pass
                cast_input = cast_with_real_speakers(chars, _speakers)
    except Exception as e:  # noqa: BLE001
        logger.warning("性别推断（模型）整体失败（按原样继续）：%s", e)

    dv_q = _quality_default_voice(settings, catalog)
    if dv_q and dv_q != dv:
        logger.info("默认音色：%s → %s（优先高保真引擎）", dv, dv_q)
        dv = dv_q
    plan = _vc.cast_plan(cast_input, dv, catalog=catalog, prefer_providers=pref)

    # ★★ 音频质量档如实报出来（用户原话「AI感太重…你可以换渠道比如硅基流动
    #    或者 minimax 模型花钱都可以」）。他得看得见**这次到底用了哪个引擎**，
    #    否则"换了渠道没有"永远无从判断。
    eng_used: Dict[str, int] = {}
    for v in (plan.get("voices") or {}).values():
        _p = str(v).split(":")[0]
        eng_used[_p] = eng_used.get(_p, 0) + 1
    quality = {
        "engines_used": eng_used,
        "preference": pref,
        "high_fidelity": [p for p in eng_used if p != "edge"],
        "on_free_engine": list(plan.get("on_free_engine") or []),
        "note": ("用了支持情感的高保真引擎：" + "、".join(p for p in eng_used if p != "edge")
                 if any(p != "edge" for p in eng_used)
                 else "本次全部落在 Edge（免费引擎，**不支持情感参数**）—— "
                      "想更自然就在「设置 → 配音」里配 minimax 或硅基流动的 Key"),
        "emotion_capable": [p for p in eng_used if p in ("minimax", "siliconflow", "dashscope")],
    }

    # ★ 把"推断出来的性别"写回角色卡：写回之后它变成显式事实，
    #   界面看得见、用户能改，下次选角也不用再猜。
    _gfix = []
    try:
        profs = plan.get("profiles") or {}
        for ch in chars:
            n = str(ch.get("name") or "").strip()
            pr = profs.get(n) or {}
            if pr.get("gender_inferred") and persist_inferred_gender(db, ch, pr):
                _gfix.append(f"{n}→{'男' if pr['gender'] == 'male' else '女'}"
                             + (f"（依据「{pr.get('gender_evidence')}」）"
                                if pr.get("gender_evidence") else ""))
    except Exception as e:  # noqa: BLE001
        logger.warning("写回推断性别整体失败（不影响配音）：%s", e)

    res: Dict[str, Any] = {
        "ok": True, "cast": plan.get("voices") or {},
        "reasons": plan.get("reasons") or {},
        "collisions": plan.get("collisions") or [],
        "catalog": {"size": len(catalog), "used": bool(plan.get("catalog_used")),
                    "note": ("用各厂商真实音色目录按角色卡打分"
                             if plan.get("catalog_used")
                             else "拉不到音色目录，退回内置 8 音色池")},
        "quality": quality,
        "gender_fixed": _gfix + _llm_fix,
        "virtual_speakers": _virtual,
        "shots": [], "warnings": [],
    }
    if _virtual:
        res["warnings"].append(
            "这些说话人**没有角色卡**（分镜台词里的名字和「角色」页对不上）—— "
            "已单独给他们选角，音色只对本次有效："
            + "、".join(_virtual)
            + "。想要更准（性别/年龄/音色由你定）就在「资产 → 角色」里把他们建成角色卡。")
    if _llm_fix:
        res["warnings"].append(
            "这几个角色的性别角色卡里没写，已让文字模型看过角色卡后判定并**记到卡上**："
            + "、".join(_llm_fix) + "。判错了在「资产 → 角色」改一下，或直接指定音色。")
    if _conflict:
        res["warnings"].append(
            "⚠ 性别的两套判据**不一致**（按外貌描述 vs 模型判定）：" + "、".join(_conflict)
            + "。本次采用了模型判定；如果听着不对，请在「资产 → 角色」里直接改，"
              "或在角色卡里补一个「性别」明确写清楚。")
    if _gfix:
        res["warnings"].append(
            "角色卡没写性别，已按外貌/服装描述推断并**记到角色卡**上："
            + "、".join(_gfix) + "。不对的话在「资产 → 角色」里改一下，或直接指定音色。")
    if not quality["high_fidelity"]:
        res["warnings"].append(str(quality["note"]))
    _onfree = list(plan.get("on_free_engine") or [])
    if _onfree and quality["high_fidelity"]:
        res["warnings"].append(
            "这些角色落到了**免费引擎 Edge**（AI 感会更重）："
            + "、".join(_onfree)
            + "。原因是**高保真音色不够分**（两个角色同一把嗓子比音质更出戏）。"
              "想全部用高保真：多配一家引擎的 Key（设置 → 配音），"
              "或在「资产 → 角色」里给这几个角色直接指定音色。")
    if not catalog:
        res["warnings"].append(
            "没拉到音色目录（可能是没网或各厂商都没配 Key）——"
            "本次用内置的 8 个音色兜底选角，角色区分度会差一些。")
    if plan.get("unmatched"):
        res["warnings"].append("这些角色没配上音色：" + "、".join(plan["unmatched"]))

    # ★ 把**实际生效的配音模型**一起返回：用户要的是"真正的去选择配音模型"，
    #   那结果里就得看得见"这一次用的是哪个型号"，否则选了也不知道有没有生效。
    try:
        from core.voice import dispatcher as _vd0
        _provs = await _vd0.list_providers(settings)
        res["tts"] = {
            "models": {p["name"]: p.get("selected_model") or ""
                       for p in _provs
                       if p.get("has_api_key") and p.get("supports_model")
                       and p.get("name") != "silent"},
            "no_model_engines": {p["name"]: p.get("model_note") or ""
                                 for p in _provs
                                 if p.get("has_api_key") and not p.get("supports_model")
                                 and p.get("name") != "silent"},
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("查配音模型失败：%s", e)

    voiced_dir = os.path.join(_shots_out_dir(db, pid), "voiced")
    _srcs = script_sources_for(db, pid)
    n_done = n_skip = n_reuse = 0
    for i, sh in enumerate(shots):
        tick(0.05 + 0.9 * i / max(1, len(shots)), "配音 " + str(i + 1))
        item: Dict[str, Any] = {"index": i + 1, "ok": False, "warnings": []}
        # ★★ 画面底必须是**没配过音的原始片段**（见 `_source_clip_for_dub` 的说明：
        #   拿配音版当输入、输出又写回同一个文件 → ffmpeg "same as Input" 拒绝，
        #   而且这件事是静默的，成片会继续用旧声音）。
        clip = _source_clip_for_dub(sh)
        if not clip or not os.path.exists(clip):
            item["warnings"].append("这一镜还没有可用的原始片段，跳过")
            res["shots"].append(item)
            n_skip += 1
            continue
        dur = float(sh.get("duration_seconds") or 0) or 0.0
        try:
            from core.compose import probe_duration as _pd
            dur = float(await _pd(clip) or dur)
        except Exception:  # noqa: BLE001
            pass

        # ★ 已经配过音的这一镜**不重做**（省钱 + 不堆候选）。
        #   注意：`select` 仍然要执行 —— 用户可能选了别的候选，
        #   这一镜需要把"带配音的那一版"重新设为选中。
        if not force:
            have = _existing_voiced(sh)
            if have:
                item.update({"ok": True, "path": have, "reused": True,
                             "url": f"/api/media?path={quote(have)}"})
                if select:
                    try:
                        cid = _register_voiced_candidate(db, sh, have, dur)
                        item["candidate_id"] = cid
                        item["selected"] = True
                    except Exception as e:  # noqa: BLE001
                        item["warnings"].append(f"设为选中候选失败：{e}")
                item["warnings"].append("这一镜已经配过音，直接复用（没有重新调用 TTS）")
                res["shots"].append(item)
                n_done += 1
                n_reuse += 1
                continue

        lines = _tl(sh.get("layer2_timeline"), dur)
        # ★★ 用户实测「第一个镜头没有声音（剧本是有对话的）」：
        #   AI 写分镜时漏了 dialogue 字段，而剧本那一场明明写着对话。
        #   这里从剧本回填，但**只填说话人确实在这一镜里的** ——
        #   按场号硬塞会把别人的台词配到画面上，比静音更糟。
        _ctx = shot_script_ctx(db, sh)
        _rec = None
        if not lines and _srcs:
            try:
                from core import scriptlines as _slg
                _inj = _slg.recover_and_inject(
                    sh, _srcs, scene_name=_ctx["scene_name"],
                    character_names=_ctx["character_names"],
                    order_index=sh.get("order_index"))
                if _inj.get("added"):
                    sh = dict(sh)
                    sh["layer2_timeline"] = _inj["timeline"]
                    lines = _tl(_inj["timeline"], dur)
                    if lines:
                        _rec = _inj
            except Exception as e:  # noqa: BLE001
                item["warnings"].append(f"从剧本回填台词失败：{e}")
        item["lines"] = len(lines)
        if not lines:
            item["warnings"].append(
                "这一镜没有台词 —— 按你的要求不会自动加旁白念剧情；"
                "要让角色说话请先补台词"
                + ("。（剧本里有对话，但说话人都不在这一镜的画面/角色表里，"
                   "不硬塞）" if _srcs else ""))
            res["shots"].append(item)
            n_skip += 1
            continue
        if _rec:
            item["recovered"] = {
                "from": _rec.get("source"), "scene": _rec.get("scene_title"),
                "added": _rec.get("added"), "placed": _rec.get("placed"),
                "skipped": _rec.get("skipped"),
            }
            item["warnings"].append(
                f"📜 分镜里漏写了台词，已从剧本"
                f"「{_rec.get('scene_title') or _rec.get('source')}」回填 "
                f"{_rec.get('added')} 句 —— 这一镜现在有声音了。")
        try:
            dub = await _vc.dub_shot(
                sh, characters=chars, settings=settings,
                out_dir=os.path.join(voiced_dir, sh["id"]),
                default_voice=dv, duration=dur, cast=plan.get("voices"),
                catalog=catalog, script_sources=_srcs,
                scene_name=_ctx["scene_name"],
                shot_order=sh.get("order_index"),
                character_names=_ctx["character_names"])
        except Exception as e:  # noqa: BLE001
            item["warnings"].append(f"配音失败：{e}")
            res["shots"].append(item)
            continue
        if not dub.get("ok"):
            item["warnings"].append(f"配音失败：{dub.get('error')}")
            res["shots"].append(item)
            continue
        out = os.path.join(voiced_dir, f"{sh['id']}_voiced.mp4")
        try:
            from core.compose import _mux_clip_audio as _mux
            muxed = await _mux(clip, dub["path"], out, dur)
        except Exception as e:  # noqa: BLE001
            muxed = False
            item["warnings"].append(f"挂音轨异常：{e}")
        if not muxed:
            item["warnings"].append("音轨没能挂进这一镜（预览仍是静音）")
            res["shots"].append(item)
            continue
        item.update({"ok": True, "path": out,
                     "url": f"/api/media?path={quote(out)}",
                     "cast": dub.get("cast") or {},
                     "prosody": dub.get("prosody_notes") or []})
        item["warnings"].extend(str(w) for w in (dub.get("warnings") or [])[:3])
        if select:
            try:
                item["candidate_id"] = _register_voiced_candidate(db, sh, out, dur)
                item["selected"] = True
            except Exception as e:  # noqa: BLE001
                item["warnings"].append(f"设为选中候选失败：{e}")
        res["shots"].append(item)
        n_done += 1

    res["done"] = n_done
    res["skipped"] = n_skip
    res["reused"] = n_reuse
    res["voiced_dir"] = voiced_dir
    res["summary"] = (f"{n_done} 个镜头已配好并挂上音轨"
                      + (f"（其中 {n_reuse} 个是复用已有的配音，没重复花钱）"
                         if n_reuse else "")
                      + (f"，{n_skip} 个跳过（无台词或没有片段）" if n_skip else ""))
    if not n_done:
        res["ok"] = False
        res["warnings"].append(
            "一个镜头都没配上 —— 检查这些镜头是否有台词、是否已选中片段")
    tick(1.0, res["summary"])
    return res

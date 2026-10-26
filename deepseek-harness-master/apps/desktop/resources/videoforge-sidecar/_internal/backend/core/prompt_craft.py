# -*- coding: utf-8 -*-
"""
VideoForge · 电影手法库（把"好片子的技巧"变成可编译的提示词）
==============================================================

## 为什么需要这一层

`prompt_spec.py` 解决了"缺槽位"（基调/处境/节拍/动机/层级/声音）。
但槽位填得对，不等于片子好看 —— 决定好看的是**用什么手法**：
同样一句"她在敲键盘"，用「反应镜头给她的眼睛」和用「俯拍手部特写」是两部片子。

所以这里把**成熟的电影/广告手法**做成结构化资产：
  每条手法 = 它解决什么问题 + 什么时候用 + 怎么写成提示词 + 常见误用。

## 来源与诚实说明

· 这些是**影视工业与广告制作的通用手法**（反应镜头、单点透视、英雄镜头、
  可用光、匹配剪辑、视线空间、前景遮挡、逆光轮廓、静物过渡…），
  不是某一篇文档的原文。我在注释里标了它属于哪一派/哪类做法。
· 用户提到的《Doubao Seedance 2.5 提示词指南》文档我**没能抓到正文**
  （volcengine 文档站是前端渲染，HTML 里没有内容；试过 3 个 API 端点也不通）。
  所以本文件**没有**引用它的原文。若拿到指南文本，可直接往 `CRAFT_MOVES`
  里补条目，编译器会自动用上。
· 模特/产品的具体参数（光圈值、色温）给的是**可执行区间**，不是唯一答案。

## 用法

    from core.prompt_craft import pick_moves, craft_block
    moves = pick_moves(function="情绪反应", genre="promo", duration=6)
    text  = craft_block(moves, subject="阿岚", prop="屏幕上的分镜")

纯数据 + 纯函数，零网络、零费用。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# ── 镜头职能：这一镜在片子里干什么活 ──────────────────────────────
#: 职能 -> (识别关键词, 该职能的核心任务, 默认手法)
SHOT_FUNCTIONS: Dict[str, Dict[str, Any]] = {
    "开场建立": {
        "hints": ("开场", "建立", "交代", "第一眼", "亮相", "定场"),
        "task": "让观众在第一秒知道这是哪、谁、什么状态",
        "moves": ("establish_wide", "foreground_frame"),
    },
    "细节证据": {
        "hints": ("细节", "证据", "特写", "质", "数据", "屏幕", "手", "产品"),
        "task": "用一个可被相信的细节替代形容词",
        "moves": ("macro_detail", "hero_product", "shallow_dof"),
    },
    "情绪反应": {
        "hints": ("反应", "情绪", "表情", "愣", "笑", "沉默", "眼神", "听"),
        "task": "让观众看见人物**心里发生了什么**",
        "moves": ("reaction_closeup", "rim_light"),
    },
    "动作推进": {
        "hints": ("动作", "推进", "操作", "走", "跑", "打", "敲", "按"),
        "task": "用动作把叙事推进一步（状态必须改变）",
        "moves": ("motion_follow", "match_cut"),
    },
    "结果落点": {
        "hints": ("结果", "落点", "成片", "完成", "出现了", "结束"),
        "task": "给出努力的后果，收住这一段的情绪",
        "moves": ("hero_product", "slow_push_in"),
    },
    "转场过渡": {
        "hints": ("转场", "过渡", "空镜", "切换"),
        "task": "把两个时空接起来（有动机的转场，不是随便切）",
        "moves": ("pillow_shot", "match_cut"),
    },
}

# ── 手法库 ────────────────────────────────────────────────────────
#: key -> dict(name, school, solves, use_when, prompt, misuse)
CRAFT_MOVES: Dict[str, Dict[str, str]] = {
    "establish_wide": {
        "name": "建立镜头（远景/全景定场）",
        "school": "通用叙事剪辑（continuity editing）",
        "solves": "观众不知道自己在哪、空间有多大，后面所有镜头都失去参照",
        "use_when": "段落开头、换场景的第一镜",
        "prompt": "远景/全景，人物在画面中占比较小，交代完整空间与人物在空间里的位置；"
                  "画面里有一条清楚的前后纵深（近处物件—中景人物—远处背景三层）",
        "misuse": "拿远景拍情绪（人太小，看不出表情，等于浪费一秒）",
    },
    "foreground_frame": {
        "name": "前景遮挡构图（foreground framing）",
        "school": "电影摄影通用（含黑泽明式多平面纵深）",
        "solves": "画面像摆拍、没有空间感、像绿幕",
        "use_when": "室内、需要真实感与纵深时",
        "prompt": "前景有虚焦的真实物件（门框/枝叶/桌沿/杯子）压住画面一角，"
                  "主体在中景清晰，背景仍有可辨的环境；浅景深让前景化开",
        "misuse": "前景物件挡住主体脸部；或前景与故事无关、纯装饰",
    },
    "reaction_closeup": {
        "name": "反应镜头（reaction shot）",
        "school": "斯皮尔伯格式剪辑观：情绪来自听者，不是说着",
        "solves": "人物在说话但观众不被打动；情绪靠台词喊出来",
        "use_when": "有台词、有冲突、有信息的时刻",
        "prompt": "近景/特写给**听的那个人**：面部占画面 1/3 以上，眼睛有高光，"
                  "表情有细微变化（眉、嘴角、呼吸），背景虚化，画面稳定不晃",
        "misuse": "两个镜头都给说话的人，观众没有代入的位置；或特写时演员表情过满（假）",
    },
    "hero_product": {
        "name": "英雄镜头（hero shot）",
        "school": "商业广告摄影（product cinematography）",
        "solves": "产品像随手拍、没有质感、看不出高级",
        "use_when": "产品/成果/关键道具要立住的镜头",
        "prompt": "产品置于干净背景（纯色或极简环境），45° 顶侧光打出高光带与轮廓光，"
                  "表面材质有可控反光（金属/玻璃要有环境反射），镜头略低于产品中心，"
                  "浅景深，画面留白给文字或品牌留位",
        "misuse": "背景杂乱抢主体；光打太正导致塑料感；产品占满画面没有呼吸感",
    },
    "macro_detail": {
        "name": "微距细节（macro detail）",
        "school": "广告与纪录片通用",
        "solves": "观众不信「好用」，因为没看见「到底怎么好用」",
        "use_when": "要证明工艺/精度/质地时",
        "prompt": "微距特写某一处细节（纹理/接口/字迹/水珠/按键行程），"
                  "极浅景深，光线掠射以显出表面起伏，画面干净只有这一处细节",
        "misuse": "细节与卖点无关；或微距里出现模型画不准的东西（手指、文字）",
    },
    "one_point": {
        "name": "单点透视（one-point perspective）",
        "school": "库布里克式构图",
        "solves": "画面松散、没有压迫感或秩序感",
        "use_when": "走廊/工位/屏幕正面/对称空间",
        "prompt": "严格对称的单点透视：所有纵深线汇聚到画面中心，机位与视平线齐平，"
                  "主体压在消失点附近，画面左右平衡",
        "misuse": "广角把边缘拉变形（人脸在边缘会畸变）；对称却没有内容，变成摆拍",
    },
    "available_light": {
        "name": "可用光（available light）",
        "school": "芬奇式摄影：少灯、真实光源入画",
        "solves": "打光痕迹重、像电视剧、不真实",
        "use_when": "室内夜戏、办公室、真实感要求高的段落",
        "prompt": "只用场景里真实存在的光源（窗光/台灯/屏幕光/走廊灯），光源方向可见，"
                  "暗部保留细节但不过度补光，色温略偏冷，对比中等偏高",
        "misuse": "为了「好看」补一个大柔光，结果变成影棚感；或暗到看不清表情",
    },
    "rim_light": {
        "name": "轮廓光/逆光（rim / backlight）",
        "school": "通用人像与电影摄影",
        "solves": "人物和背景糊在一起，没有分离度",
        "use_when": "人物需要从环境里立出来（尤其暗背景）",
        "prompt": "逆光或侧逆光在人物发丝与肩线勾出一条亮边，面部仍有一侧柔光保证可读，"
                  "背景适度压暗使轮廓更明显",
        "misuse": "逆光过强导致脸部死黑（观众看不见表情）",
    },
    "shallow_dof": {
        "name": "浅景深分离（shallow depth of field）",
        "school": "通用电影镜头语言",
        "solves": "画面信息过载、主体不突出",
        "use_when": "近景/特写、需要观众只看一件事时",
        "prompt": "大光圈浅景深，主体清晰、前后景柔和虚化，虚化里仍能辨认环境（不是糊成一片）",
        "misuse": "全身远景也用大光圈（人糊了）；或虚化到看不出在哪",
    },
    "motion_follow": {
        "name": "跟拍/运动镜头（follow / tracking）",
        "school": "通用运镜",
        "solves": "动作看起来像原地摆拍，没有速度感与空间连续",
        "use_when": "人物走动、操作过程、需要「跟着他走」的时刻",
        "prompt": "镜头跟随主体同向移动，保持主体在画面同一位置（稳定跟拍），"
                  "背景产生连续视差（证明空间是真的），速度与人物一致、不超前不滞后",
        "misuse": "跟拍时主体出画/被裁切；镜头晃动幅度与人物质感不匹配",
    },
    "match_cut": {
        "name": "匹配剪辑（match cut / 动接动）",
        "school": "经典剪辑（库布里克《2001》式转场）",
        "solves": "镜头之间硬切、断裂、不丝滑",
        "use_when": "两个镜头要接起来，且形状/动作/方向可以对齐",
        "prompt": "这一镜的结尾动作/形状与下一镜的开头对齐：相同构图位置、相同运动方向、"
                  "相同物体形状或相似色块，让切点看不出来",
        "misuse": "强行对齐但内容无关，观众感觉是巧合；或方向反了（左边出画右边也出画）",
    },
    "pillow_shot": {
        "name": "静物过渡镜头（pillow shot）",
        "school": "小津安二郎式「pillow shot」：用空镜换气",
        "solves": "段落之间硬切、情绪没有落点",
        "use_when": "段落收尾、时间跳跃、情绪需要留白",
        "prompt": "静态空镜：一个与剧情相关但没有人物的物件（杯子/窗外/门/雪地脚印），"
                  "机位固定、时长略长（2-3 秒），光线与前后段落一致",
        "misuse": "空镜与故事无关变成废镜；或用空镜掩盖叙事断裂",
    },
    "slow_push_in": {
        "name": "缓推（slow push-in）",
        "school": "通用情绪运镜",
        "solves": "情绪到不了、观众没有被「带进去」",
        "use_when": "人物领悟/决定/情绪转折的瞬间",
        "prompt": "极缓慢推近（dolly in），幅度小于画面 10%，速度均匀、呼吸般稳定，"
                  "推的过程中人物表情有细微变化",
        "misuse": "推太快像恐怖片；或推的同时还晃（两个运动打架）",
    },
}

#: 职能识别顺序（前面的优先）
_FUNC_ORDER = ("开场建立", "情绪反应", "结果落点", "细节证据", "动作推进", "转场过渡")


def detect_function(*texts: str) -> str:
    """从分镜文字判断这一镜的职能。判断不出则归"动作推进"。"""
    blob = " ".join(str(t or "") for t in texts)
    for fn in _FUNC_ORDER:
        if any(h in blob for h in SHOT_FUNCTIONS[fn]["hints"]):
            return fn
    return "动作推进"


def _camera_tier(camera: str) -> str:
    """景别粗分：wide（远/全）/ medium（中）/ close（近/特）。"""
    c = str(camera or "")
    if any(k in c for k in ("特写", "近景", "特")):
        return "close"
    if any(k in c for k in ("远景", "全景", "大全", "远")):
        return "wide"
    if any(k in c for k in ("中景", "中近", "中")):
        return "medium"
    return ""


def pick_moves(function: str = "", genre: str = "", duration: float = 6,
               *, limit: int = 2, camera: str = "") -> List[Dict[str, str]]:
    """按职能挑手法（外加体裁修正）。返回 [{key, name, school, prompt, misuse}]。"""
    fn = function if function in SHOT_FUNCTIONS else detect_function(function)
    keys = list(SHOT_FUNCTIONS.get(fn, {}).get("moves") or ("motion_follow",))
    g = (genre or "").lower()
    # 宣传片：情绪镜头偏好缓推 + 轮廓光；不要"暗调可用光"
    if ("宣传" in g or "广告" in g or "promo" in g):
        if fn == "情绪反应":
            keys = ["reaction_closeup", "rim_light"]
        elif fn == "结果落点":
            keys = ["hero_product", "slow_push_in"]
    # 剧情/悬疑：可用光 + 浅景深，避免"广告腔英雄镜头"
    if ("剧情" in g or "短剧" in g or "悬疑" in g) and fn == "细节证据":
        keys = ["macro_detail", "shallow_dof"]
    # ★ v2.1：手法不能和景别打架（要求中近景却给"远景定场"，模型会执行矛盾指令）
    tier = _camera_tier(camera)
    if tier == "close":
        keys = [k for k in keys if k not in ("establish_wide", "foreground_frame")] or ["shallow_dof"]
    elif tier == "wide":
        keys = [k for k in keys if k not in ("reaction_closeup", "macro_detail")] or ["establish_wide"]
    out: List[Dict[str, str]] = []
    for k in keys[:limit]:
        m = CRAFT_MOVES.get(k)
        if m:
            out.append({"key": k, **m})
    return out


def craft_block(moves: List[Dict[str, str]], *, subject: str = "",
                prop: str = "") -> str:
    """把选中的手法写成提示词里的一段（给【镜头】段用）。"""
    if not moves:
        return ""
    bits: List[str] = []
    for m in moves:
        p = m.get("prompt") or ""
        if subject and m.get("key") in ("reaction_closeup", "rim_light", "slow_push_in"):
            p = p.replace("人物", subject).replace("主体", subject)
        if prop and m.get("key") in ("hero_product", "macro_detail"):
            p = p.replace("产品", prop)
        bits.append("%s：%s" % (m.get("name"), p))
    return "；".join(bits)


def misuse_warnings(moves: List[Dict[str, str]]) -> List[str]:
    """这一组手法的常见误用（写进自检，防止模型跑偏）。"""
    return [m.get("misuse") for m in moves if m.get("misuse")]

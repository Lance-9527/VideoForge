# -*- coding: utf-8 -*-
"""VideoForge · AIGC 失败码（F-xxx）与责任层

学自用户指定的方法论文档 **Hell-Grind-AIGC-Skill**：
`references/failure-diagnosis.md`（62 个错误码，按 6 个责任层组织）
+ `references/iteration-selection.md:82-90`（变量隔离顺序）。

为什么值得照搬这套东西：
    它把"结果不好"这种**没法行动**的描述，变成了
    「错误码 → 症状与证据 → 先检查 → 责任层 → 最小修复」——
    而责任层顺序（资产 → 镜头契约 → 提示词 → 平台适配 → 随机性 → 后期）
    决定了**该去哪儿改**。我们以前的告警是一大段中文，用户看完不知道该动哪一步。

本模块只做两件事，都是纯函数：
  ① `classify(text)` —— 把我们已有的错误/告警文本归到 F-xxx 与责任层；
  ② `explain(code)` —— 给出该失败码的"先检查 / 最小修复"（照抄方法论里的四段式）。

★ 诚实边界：这是**文本归类**，不是语义理解 —— 归类结果仅用于把用户引到正确的
  责任层，**不能**当作"诊断结论"。分类规则命中的是关键词，落不到就只能返回空。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple


logger = logging.getLogger("videoforge.failcodes")


# 责任层（顺序即"先改哪一层"，来自 iteration-selection.md:86-88）
LAYERS: Tuple[str, ...] = (
    "asset",        # 资产事实（角色/场景参考图、身份与状态）
    "contract",     # 场景/镜头契约（时长、动作、运镜、空间）
    "prompt",       # 主提示词表达
    "adapter",      # 平台适配（参数、能力、分辨率/画幅）
    "randomness",   # 生成随机性
    "post",         # 后期（剪辑/合成/调色/字幕）
)

LAYER_ZH = {
    "asset": "资产事实（角色/场景参考图）",
    "contract": "镜头契约（时长/动作/运镜/空间）",
    "prompt": "主提示词表达",
    "adapter": "平台适配（参数/能力/画幅）",
    "randomness": "生成随机性",
    "post": "后期（剪辑/合成/字幕）",
}

# 失败码字典：code → (责任层, 人话症状, 先检查, 最小修复)
# 文案与分类法取自 failure-diagnosis.md:32-101（F-* 六组）。
CODES: Dict[str, Dict[str, str]] = {
    # 资产与身份
    "F-ID-DRIFT": {"layer": "asset", "zh": "人脸/体型/发型漂移，同一角色换了个长相",
                   "check": "角色参考图是否已关联并真的发给了模型；身份描述是否被改写",
                   "fix": "补齐角色参考图；只引用已批准的资产版本，不要每镜重写人物描述"},
    "F-STATE-DRIFT": {"layer": "asset", "zh": "服装/伤势/携带物回退",
                      "check": "本镜是否继承了上一镜的状态（服装、伤势、道具）",
                      "fix": "把状态写进 must_hold；必要时拆镜"},
    "F-REF-SCOPE": {"layer": "asset", "zh": "参考图的构图/背景/光线被带进了画面",
                    "check": "提示词是否写明了参考图只继承身份、排除构图/背景/光线",
                    "fix": "补 inherit/exclude 说明；或先按分镜画幅裁参考图"},
    "F-DUP-SUBJECT": {"layer": "asset", "zh": "同一个角色在画面里出现两次",
                      "check": "主体数量与镜面/倒影/屏幕反射",
                      "fix": "写明精确人数与各出现一次，并把倒影列入禁止"},
    "F-ASSET-MERGE": {"layer": "asset", "zh": "两个角色/道具的特征被融合",
                      "check": "多张参考图是否分别对应明确的名字与站位",
                      "fix": "把角色与站位一一对应写清；必要时分开镜头"},
    # 空间与连续性
    "F-COUNT": {"layer": "contract", "zh": "人数不对或多出人物",
                "check": "本镜允许出现的角色清单（character_ids）",
                "fix": "写明恰好 N 个主体；把不该出现的角色写进 must_not_appear"},
    "F-SCREEN-DIR": {"layer": "contract", "zh": "左右/朝向翻转",
                     "check": "轴线与屏幕方向是否在相邻镜头里一致",
                     "fix": "在提示词里写清朝向；与上一镜的 continuity 对齐"},
    "F-SPATIAL-RESET": {"layer": "contract", "zh": "场景布局被重建（房间/家具变了）",
                        "check": "场景参考图与固定锚点是否一致",
                        "fix": "引用场景锚点；不要用一张构图参考控制所有机位"},
    "F-PROP-DUP": {"layer": "contract", "zh": "关键道具被复制",
                   "check": "道具的数量/所有者/位置/接触关系",
                   "fix": "写明「唯一一份，在谁手里」"},
    # 动作与表演
    "F-ACTION-OVERLOAD": {"layer": "contract", "zh": "动作被压缩/遗漏/顺序错（时长不够）",
                          "check": "这一镜的时长能否装下这些动作与台词",
                          "fix": "删装饰动作 → 缩台词 → 降运镜 → 拆镜"},
    "F-PHYSICS": {"layer": "prompt", "zh": "漂浮、没有重量、碰撞没有反作用",
                  "check": "动作是否写了 准备→发力→接触→反作用→落定",
                  "fix": "补完整物理链与环境反馈"},
    "F-PERFORMANCE": {"layer": "prompt", "zh": "表演僵硬或情绪读不出来",
                      "check": "情绪是否被转译成可见信号（视线/呼吸/停顿/重心/手）",
                      "fix": "选 1–2 个表演信号写具体，不要只写「很愤怒」"},
    "F-UNSCRIPTED-MOVE": {"layer": "prompt", "zh": "静止的角色自己转头/做手势/说话",
                          "check": "是否写了「保持不动」却没写允许的微动",
                          "fix": "列允许微动（眨眼、极弱呼吸）与禁止动作"},
    # 摄影与剪辑
    "F-CAMERA-CONFLICT": {"layer": "contract", "zh": "一个镜头里塞了多个主运镜",
                          "check": "时间轴里是不是出现了两种以上运镜",
                          "fix": "只保留一个主运动，或把镜头拆开"},
    "F-FRAMING": {"layer": "contract", "zh": "景别/主体占比/方向不对",
                  "check": "起始构图（景别）与主体占比是否写明",
                  "fix": "用可画的构图与占比替代「电影感」"},
    "F-JITTER": {"layer": "adapter", "zh": "手持变成故障抖动",
                 "check": "稳定方式是否写具体（幅度/频率）",
                 "fix": "写明人体幅度与节奏；冲击震动绑定明确碰撞时刻"},
    "F-CUT": {"layer": "contract", "zh": "镜头数/切点/转场不对",
              "check": "这一镜实际需要的镜头数与切点",
              "fix": "写明精确镜头数与切点"},
    # 对白与声音
    "F-DIALOGUE-TEXT": {"layer": "prompt", "zh": "台词加词/漏词/说话人错",
                        "check": "台词是否逐字写进 dialogue.text，说话人是否在场",
                        "fix": "逐字写死台词与说话人；不要在提示词里写「说出台词」"},
    "F-DIALOGUE-TIME": {"layer": "contract", "zh": "台词太快/被截断/挤掉动作",
                        "check": "台词字数 vs 该段时间（中文约 4 字/秒）",
                        "fix": "缩短台词或加长时间片，必要时拆镜"},
    "F-DIALOGUE-VISUALIZED": {"layer": "prompt", "zh": "台词内容被画成了闪回/额外人物",
                              "check": "是否写了「对白不视觉化」",
                              "fix": "补「对白只作为声音，不触发闪回/插入画面」"},
    "F-LIPSYNC": {"layer": "adapter", "zh": "没说话的人在动嘴 / 口型错",
                  "check": "本镜唯一的口型承担者是否写明",
                  "fix": "写明只有某角色开口；其他人嘴唇静止"},
    "F-AUDIO-POLLUTION": {"layer": "prompt", "zh": "平台自动加了配乐/旁白/字幕",
                          "check": "提示词是否写了音频边界（有无音乐/字幕/旁白）",
                          "fix": "在提示词里明确音频与字幕边界"},
    # 平台适配
    "F-ASPECT-MISMATCH": {"layer": "adapter", "zh": "出片画幅与分镜画幅不一致",
                          "check": "发给平台的 ratio/resolution 参数是否合法；模型能力表里该组合的实际画幅",
                          "fix": "改正 ratio 写法（`16:9` 而不是 `16x9`）或换分辨率；不要用裁剪掩盖"},
    "F-PARAM-REJECTED": {"layer": "adapter", "zh": "平台拒绝了参数",
                         "check": "参数名/取值/组合是否在该模型的能力范围内",
                         "fix": "只改平台适配层；不要把参数问题写进提示词"},
    "F-MODEL-MISSING": {"layer": "adapter", "zh": "模型不可用（没有开通/型号不存在）",
                        "check": "控制台里该模型是否已开通；型号 id 是否写对",
                        "fix": "开通模型或换型号；不要重试同一个 id"},
    "F-BALANCE": {"layer": "adapter", "zh": "账号余额/额度不足",
                  "check": "控制台余额与限额",
                  "fix": "充值或换 Key；重试无用"},
    "F-TIMEOUT": {"layer": "randomness", "zh": "平台超时/任务失败",
                  "check": "是否偶发（同参数再来一次）",
                  "fix": "同参数重试一次；连续两次失败就别再抽卡"},
    # 生成随机性
    "F-RANDOM-VARIANCE": {"layer": "randomness", "zh": "同参数下结果差异大",
                          "check": "本批次是否真的固定了所有变量（提示词/参数/参考）",
                          "fix": "固定变量，只改一个责任层再比"},
    # 后期
    "F-POST-FIX": {"layer": "post", "zh": "更适合在剪辑/合成阶段修",
                   "check": "这个问题能否用剪辑/调色/字幕修好",
                   "fix": "记录 route_post，别继续抽卡"},
}

# 关键词 → 失败码（顺序敏感：更具体的放前面）
_RULES: Tuple[Tuple[str, str], ...] = (
    # 平台/适配层（我们的真实事故都在这类）
    (r"(画幅不符|画幅.*不一致|aspect.*mismatch|crop_loss|会裁掉)", "F-ASPECT-MISMATCH"),
    (r"(ratio|16x9|非法参数|invalid param|参数不合法|unsupported aspect)", "F-PARAM-REJECTED"),
    (r"(ModelNotOpen|model.*not.*(open|exist|found)|未开通|模型不存在|model id)", "F-MODEL-MISSING"),
    (r"(余额|额度|insufficient|quota|balance|1008)", "F-BALANCE"),
    (r"(超时|timeout|task.*failed|任务失败)", "F-TIMEOUT"),
    (r"(口型|lipsync|lip sync)", "F-LIPSYNC"),
    (r"(抖动|jitter|故障抖动)", "F-JITTER"),
    # 对白与声音
    (r"(台词.*(加词|漏词|说话人)|说话人.*没能|没能判断是谁说的)", "F-DIALOGUE-TEXT"),
    (r"(台词.*(太长|放不进|变速上限|抢话)|overflow.*\d)", "F-DIALOGUE-TIME"),
    (r"(旁白|配乐|自动加|audio pollution|字幕.*自动)", "F-AUDIO-POLLUTION"),
    # 资产与身份
    (r"(参考图|角色图|场景图|identity|角色.*不一致|人物.*不像)", "F-REF-SCOPE"),
    (r"(多出|多余.*(人|角色)|人数|额外人物)", "F-COUNT"),
    (r"(没有可用的首帧|没有台词|no.*reference)", "F-REF-SCOPE"),
    # 镜头契约
    (r"(画幅|运镜.*(冲突|多个)|camera conflict|多个主运镜)", "F-CAMERA-CONFLICT"),
    (r"(动作.*(装不下|超载)|时长.*装不下|overload)", "F-ACTION-OVERLOAD"),
    (r"(景别|构图|主体占比|framing)", "F-FRAMING"),
    (r"(越轴|朝向|左右翻转|screen direction)", "F-SCREEN-DIR"),
    # 提示词
    (r"(没有画面内容|has_action|提示词.*空|audit.*error)", "F-PERFORMANCE"),
    (r"(物理|漂浮|反作用|physics)", "F-PHYSICS"),
)


def classify(text: Optional[str], limit: int = 2) -> List[Dict[str, Any]]:
    """把一段错误/告警文本归到 F-xxx（按 `_RULES` 顺序，最多返回 `limit` 条）。

    返回 `[{"code","layer","layer_zh","zh","check","fix"}]`；
    **归不到就返回空列表** —— 不硬套一个错误码（"结果不好"不是错误码，
    这是方法论里明确写的元规则：`failure-diagnosis.md:16`）。
    """
    t = str(text or "")
    if not t.strip():
        return []
    out: List[Dict[str, Any]] = []
    seen = set()
    for pat, code in _RULES:
        if code in seen:
            continue
        if re.search(pat, t, re.I):
            seen.add(code)
            out.append(explain(code))
            if len(out) >= max(1, int(limit)):
                break
    return out


def explain(code: str) -> Dict[str, Any]:
    """给出失败码的责任层与"先检查/最小修复"（四段式）。未知码原样返回。"""
    c = CODES.get(str(code or "").strip())
    if not c:
        return {"code": code, "layer": "", "layer_zh": "", "zh": "",
                "check": "", "fix": ""}
    return {"code": code, "layer": c["layer"],
            "layer_zh": LAYER_ZH.get(c["layer"], c["layer"]),
            "zh": c["zh"], "check": c["check"], "fix": c["fix"]}


def annotate(text: Optional[str], limit: int = 2) -> Dict[str, Any]:
    """给一段告警/错误加上失败码 —— 界面可以直接显示"这属于哪一层、该改什么"。"""
    hits = classify(text, limit=limit)
    if not hits:
        return {"codes": [], "layer": "", "hint": ""}
    h = hits[0]
    return {"codes": [x["code"] for x in hits],
            "layer": h["layer"], "layer_zh": h["layer_zh"],
            "hint": f"[{h['code']}｜{h['layer_zh']}] {h['zh']}。"
                    f"先检查：{h['check']}。最小修复：{h['fix']}",
            "all": hits}


def stop_conditions(tries: int, same_code_streak: int = 0, budget_hit: bool = False
                    ) -> List[str]:
    """抽卡停止条件（`iteration-selection.md:156-167` 的 6 条里可自动判定的 3 条）。

    为什么要它：连续两批在同一错误上没改善时继续抽卡**只是烧钱**。
    """
    out = []
    if same_code_streak >= 2:
        out.append("连续两个批次在同一失败码上没有改善 —— 该回到责任层重写或拆镜，"
                   "不要继续用同一个提示词抽卡。")
    if budget_hit:
        out.append("已达到你设定的预算/次数上限。")
    if tries >= 6:
        out.append(f"同一镜已经试了 {tries} 次 —— 先按失败码定位责任层，再决定下一步。")
    return out

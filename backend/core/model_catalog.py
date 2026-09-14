# -*- coding: utf-8 -*-
"""
VideoForge · 模型能力目录（用户看得懂的选项）

═══════════════════════════════════════════════════════════════════
为什么需要这个
═══════════════════════════════════════════════════════════════════
用户反馈："模型版本选择能不能类型更多…你得在可选类型里面都标注一下，
而不是这些我看不懂的"。

只给一个模型 ID 下拉框是不够的 —— 用户不知道：
  · 这个版本清不清晰（512P / 768P / 1080P）
  · 能出多长（6s / 10s，而且**上限跟分辨率挂钩**）
  · 适合什么场景（文生视频 / 图生视频 / 导演运镜）
  · 贵不贵、快不快

本模块把这些做成结构化数据，前端直接渲染成人话选项。

`verified` 字段含义：
  True  = 在本机用真实账号实测过该能力矩阵（可信）
  False = 来自公开资料/官方文档，未在本机逐项实测（可能有偏差）
诚实地标出来，比假装全都验证过要强。
"""

from __future__ import annotations
from typing import Any, Dict, List, Optional

# ────────────────────────────────────────────────────────────────
# 视频模型目录
#   resolutions / durations 是**可用组合**，写成 "分辨率: 支持的时长列表"
#
# audio.native = 这个厂商的模型**生成出来的视频本身有没有声音**
#   True  → 片子里自带对白/音效，后期就不该再叠「配音」「字幕」（会盖掉原声）
#   False → 出的是无声画面，声音必须靠后期加
#   None  → 不确定 / 看具体版本，前端会提示"以厂商文档为准"
# ────────────────────────────────────────────────────────────────

VIDEO_CATALOG: Dict[str, Dict[str, Any]] = {
    "hailuo": {
        "display": "海螺 Hailuo（MiniMax）",
        "note": "中文语义理解好。**两件必须注意**：① 分辨率越高，可出时长越短；"
                "② 带「图生视频」的版本**必须先用场景图/角色图生成过图片**，否则一定失败",
        "docs": "https://platform.minimaxi.com/docs",
        # 海螺出的是无声画面：声音必须在后期加（配音 / 本地合成）
        "audio": {"native": False,
                  "note": "海螺只出画面，视频本身没有声音。对白/旁白要在「后期」用②配音加。"},
        # mode: t2v=文生视频 / i2v=图生视频（必须首帧）/ ref=主体参考
        "models": [
            {
                "id": "video-01",
                "label": "video-01 · 最灵活",
                "mode": "t2v", "mode_zh": "文生视频",
                "tags": ["文生视频", "1080P/6s"],
                # ★★ 2026-09-13 实测修正：`video-01` **同时忽略分辨率和时长**。
                #   三次样本（全部 status_code=0，即"请求被接受"）：
                #     1080P + 6s  → 1280×720，5.64s
                #     1080P + 10s → 1280×720，5.64s
                #      768P + 10s → 1280×720，5.64s
                #   也就是说：要 10 秒它只给 5.64 秒、要 1080P 它给 1280×720，
                #   **都不会报错**。旧表写 `1080P: [6, 10]`，规划层于是按
                #   "10 秒一次出完"排，成片里就出现"声明 10 秒、实际 5.6 秒"的缺口
                #   （§16.11 修过的"假时长"是同一类病的另一个入口）。
                #   改成全档 [6] 之后，规划会老实拆成 6+6+6…，
                #   成片时长才和台词对得上（代价是调用次数变多，这是必须付的）。
                #   ⚠ 512P 没单独测过；但"分辨率被忽略"已在 768P/1080P 两档上实测，
                #     512P 按同一机制处理（这条是**推断**，已在上面写明）。
                "caps": {"512P": [6], "768P": [6], "1080P": [6]},
                "best_for": "兼容性好；注意它只出 1280×720 / 约 5.6 秒，"
                            "要更长请换 MiniMax-Hailuo-02（768P 支持 10 秒）",
                "verified": True,
            },
            {
                "id": "T2V-01",
                "label": "T2V-01 · 全组合可用",
                "mode": "t2v", "mode_zh": "文生视频",
                "tags": ["文生视频", "全组合"],
                "caps": {"512P": [6, 10], "768P": [6, 10], "1080P": [6, 10]},
                "best_for": "兼容性最好的版本，512P~1080P 全部支持 6 秒与 10 秒",
                "verified": True,
            },
            {
                "id": "MiniMax-Hailuo-2.3",
                "label": "Hailuo-2.3 · 画质最新",
                "mode": "t2v", "mode_zh": "文生视频",
                "tags": ["文生视频", "最新画质"],
                "caps": {"768P": [6, 10], "1080P": [6]},
                "best_for": "追画质、能接受 1080P 只出 6 秒；动作更自然",
                "verified": True,
            },
            {
                "id": "MiniMax-Hailuo-02",
                "label": "Hailuo-02 · 稳定版",
                "mode": "t2v", "mode_zh": "文生视频",
                "tags": ["文生视频", "稳定"],
                "caps": {"768P": [6, 10], "1080P": [6]},
                "best_for": "默认推荐；768P 可出 10 秒，1080P 只能 6 秒",
                "verified": True,
            },
            {
                "id": "T2V-01-Director",
                "label": "T2V-01-Director · 导演运镜",
                "mode": "t2v", "mode_zh": "文生视频（支持运镜指令）",
                "tags": ["文生视频", "运镜控制"],
                "caps": {"512P": [6, 10], "768P": [6]},
                "best_for": "需要在提示词里精确控制镜头运动（如「[推进]」）时用",
                "verified": True,
            },
            {
                "id": "I2V-01",
                "label": "I2V-01 · 图生视频",
                "mode": "i2v", "mode_zh": "图生视频（必须首帧）",
                "tags": ["图生视频", "画面可控"],
                "caps": {"512P": [6], "768P": [6], "1080P": [6]},
                "best_for": "用你的场景图当首帧，画面与场景资产严格一致。"
                            "**前提：该分镜已关联场景且场景图已生成**",
                "verified": True,
            },
            {
                "id": "I2V-01-Director",
                "label": "I2V-01-Director · 图生视频 + 运镜",
                "mode": "i2v", "mode_zh": "图生视频（必须首帧）",
                "tags": ["图生视频", "运镜控制"],
                "caps": {"512P": [6], "768P": [6], "1080P": [6]},
                "best_for": "场景图当首帧 + 控制运镜",
                "verified": True,
            },
            {
                "id": "I2V-01-live",
                "label": "I2V-01-live · 图生视频（动画感）",
                "mode": "i2v", "mode_zh": "图生视频（必须首帧）",
                "tags": ["图生视频", "动画风"],
                "caps": {"512P": [6], "768P": [6], "1080P": [6]},
                "best_for": "把静态图做成有轻微动态的动画感镜头",
                "verified": True,
            },
            {
                "id": "video-01-live2d",
                "label": "video-01-live2d · 图生视频（Live2D 风）",
                "mode": "i2v", "mode_zh": "图生视频（必须首帧）",
                "tags": ["图生视频", "Live2D"],
                "caps": {"512P": [6], "768P": [6], "1080P": [6]},
                "best_for": "二次元形象动起来",
                "verified": True,
            },
        ],
        # 明确记录「探测过但不可用」的版本，避免再被列进选项
        "unavailable": [
            {"id": "MiniMax-Hailuo-02-Fast", "reason": "该模型名不存在（实测报 incorrect model param input）"},
            {"id": "MiniMax-Hailuo-2.3-Fast", "reason": "不支持文生视频"},
            {"id": "S2V-01", "reason": "需要 subject_reference 主体参考参数，当前未接入"},
        ],
    },

    "seedance": {
        "display": "豆包 Seedance（字节火山）",
        "note": "模型 ID 带日期后缀，且**需要账号已开通该模型**；建议用「🔍 拉取可用模型清单」",
        # 1.0 系列实测是无声画面；2.x 是否带音轨各版本不一致，这里标"不确定"，
        # 前端会提示用户以厂商文档 / 实测为准，不替他下结论。
        "audio": {"native": None,
                  "note": "Seedance 1.0 系列是无声画面；2.x 各版本是否带音轨不一致，"
                          "请以厂商文档或实拍结果为准。发现有声就把后期②③跳过。"},
        # ★ 2026-09-14 按官方文档校正（《创建视频生成任务》docs.volcengine.com/docs/82379/1520757）：
        #   · seedance 2.0 / 2.0 fast / 1.5 pro：`duration` 合法区间 **[4,15]**（或 -1 跟随素材）
        #   · seedance 1.0 pro / pro-fast / lite：**[2,12]**
        #   · **2.0 fast 不支持 1080p**（所以它的 caps 里没有 1080P 这一档）
        #   适配器里那个标量 `max_duration` 单独改不够，逐型号的 caps 才是真正拦请求的地方。
        "models": [
            {"id": "doubao-seedance-2-5-260628", "label": "Seedance 2.5 · 最新最强",
             "tags": ["文生视频", "图生视频"], "caps": {"480P": [5, 10], "720P": [5, 10, 15], "1080P": [5, 10, 15]},
             "best_for": "当前最强，支持首尾帧与角色参考", "verified": False},
            {"id": "doubao-seedance-2-0-260128", "label": "Seedance 2.0 · 综合均衡",
             "tags": ["文生视频", "图生视频"], "caps": {"720P": [5, 10, 15], "1080P": [5, 10, 15]},
             "best_for": "性价比选择", "verified": False},
            {"id": "doubao-seedance-2-0-fast-260128", "label": "Seedance 2.0 Fast · 更快更便宜",
             "tags": ["快速"], "caps": {"480P": [5, 10], "720P": [5, 10, 15]},
             "best_for": "批量试拍、草稿（**不支持 1080p**）", "verified": False},
            {"id": "doubao-seedance-2-0-mini-260615", "label": "Seedance 2.0 Mini · 最便宜",
             "tags": ["最便宜"], "caps": {"720P": [5]}, "best_for": "大量铺量", "verified": False},
            {"id": "doubao-seedance-1-0-pro-250528", "label": "Seedance 1.0 Pro · 老版本",
             "tags": ["稳定"], "caps": {"720P": [5, 10], "1080P": [5, 10]},
             "best_for": "如果你的账号只开通了这一版", "verified": False},
        ],
    },

    "kling": {
        "display": "可灵 Kling（快手）",
        "note": "国内画面质量口碑最好之一；标准版/专业版价格差较大",
        "models": [
            {"id": "kling-2.0", "label": "可灵 2.0 · 最新", "tags": ["文生视频"],
             "caps": {"720P": [5, 10], "1080P": [5, 10]}, "best_for": "追画质", "verified": False},
            {"id": "kling-1.6", "label": "可灵 1.6 · 稳定常用", "tags": ["文生视频"],
             "caps": {"720P": [5, 10], "1080P": [5, 10]}, "best_for": "常规使用", "verified": False},
            {"id": "kling-1.5", "label": "可灵 1.5 · 便宜", "tags": ["经济"],
             "caps": {"720P": [5, 10]}, "best_for": "省成本", "verified": False},
        ],
    },

    "wanx": {
        "display": "通义万相 Wanx（阿里云）",
        "note": "阿里云 DashScope；Turbo 快而便宜，Plus 画质更好",
        "models": [
            {"id": "wanx2.1-t2v-turbo", "label": "万相 2.1 Turbo · 快 + 便宜", "tags": ["文生视频", "经济"],
             "caps": {"480P": [5], "720P": [5]}, "best_for": "批量草稿", "verified": False},
            {"id": "wanx2.1-t2v-plus", "label": "万相 2.1 Plus · 画质更好", "tags": ["文生视频"],
             "caps": {"720P": [5], "1080P": [5]}, "best_for": "要画质", "verified": False},
            {"id": "wanx2.1-i2v-turbo", "label": "万相 2.1 Turbo · 图生视频", "tags": ["图生视频"],
             "caps": {"720P": [5]}, "best_for": "用你的场景图当首帧", "verified": False},
        ],
    },

    "jimeng": {
        "display": "即梦 Jimeng（字节火山）",
        "note": "与 Seedance 同一平台，需在火山方舟开通",
        "models": [
            {"id": "jimeng-video-3.0", "label": "即梦视频 3.0", "tags": ["文生视频"],
             "caps": {"720P": [5], "1080P": [5]}, "best_for": "通用", "verified": False},
            {"id": "jimeng-video-2.0", "label": "即梦视频 2.0", "tags": ["经济"],
             "caps": {"720P": [5]}, "best_for": "省成本", "verified": False},
        ],
    },

    "runway": {
        "display": "Runway",
        "note": "国外老牌，Gen-4 画质好但按秒计费较贵",
        "models": [
            {"id": "gen4_turbo", "label": "Gen-4 Turbo · 最新", "tags": ["文生视频", "图生视频"],
             "caps": {"720P": [5, 10]}, "best_for": "追画质（较贵）", "verified": False},
            {"id": "gen3a_turbo", "label": "Gen-3 Alpha Turbo · 便宜些", "tags": ["经济"],
             "caps": {"720P": [5, 10]}, "best_for": "省成本", "verified": False},
        ],
    },

    "luma": {
        "display": "Luma Dream Machine",
        "note": "运镜自然，适合空镜与转场",
        "models": [
            {"id": "ray-2", "label": "Ray 2 · 画质优先", "tags": ["文生视频"],
             "caps": {"720P": [5, 9]}, "best_for": "空镜、运镜", "verified": False},
            {"id": "ray-flash-2", "label": "Ray Flash 2 · 快速便宜", "tags": ["经济"],
             "caps": {"720P": [5, 9]}, "best_for": "快速试拍", "verified": False},
        ],
    },

    "pika": {
        "display": "Pika Labs",
        "note": "特效风格见长",
        "models": [
            {"id": "pika-2.2", "label": "Pika 2.2", "tags": ["特效"], "caps": {"720P": [5, 10]},
             "best_for": "风格化特效", "verified": False},
        ],
    },

    "sora": {
        "display": "OpenAI Sora",
        "note": "需要 OpenAI 账号且地区受限；自带音轨",
        "models": [
            {"id": "sora-2", "label": "Sora 2 · 自带音频", "tags": ["自带音频", "长镜头"],
             "caps": {"720P": [5, 10, 20]}, "best_for": "模型自带声音，可关掉本地配音", "verified": False},
        ],
    },

    "cogvideox": {
        "display": "智谱 CogVideoX",
        "note": "开源模型，可本地部署",
        "audio": {"native": False, "note": "开源版只出画面，没有音轨。"},
        "models": [
            {"id": "cogvideox", "label": "CogVideoX · 本地可跑", "tags": ["开源", "本地"],
             "caps": {"720P": [6]}, "best_for": "不外传素材、本地部署", "verified": False},
        ],
    },
}


# 这几家出的是无声画面（按各家官方能力说明；非实测的部分标 None）
_SILENT_AUDIO = {"native": False, "note": "该厂商生成的是无声画面，声音要在「后期」加。"}

for _p in ("kling", "wanx", "jimeng", "runway", "luma", "pika"):
    VIDEO_CATALOG[_p].setdefault("audio", dict(_SILENT_AUDIO))

# Sora 2 原生带同步对白与音效（官方明确说明）——用它就不该再叠配音/字幕
VIDEO_CATALOG["sora"]["audio"] = {
    "native": True,
    "note": "Sora 2 原生带同步对白与音效。用它生成的片子本身就有人声，"
            "到「后期」把 ②配音、③字幕 直接跳过，否则会盖掉原声。",
}


# ═══════════════════════════════════════════════════════════════
# 规范化：保证**每一个模型**都带齐用户看得懂的字段
# ═══════════════════════════════════════════════════════════════
# 用户的诉求原话是"你得在可选类型里面都标注一下，而不是这些我看不懂的…
# 包括其他视频模型还有文本模型也是…不能敷衍"。
# 与其靠人肉记得给 30 个模型逐个填 mode_zh，不如在这里做一次规范化：
# 以后新增模型忘了写，也会自动补上，界面上永远不会出现"光秃秃的 ID"。

_MODE_ZH = {
    "t2v": "文生视频",
    "i2v": "图生视频（必须首帧）",
    "both": "文生+图生视频",
    "ref": "主体参考",
    "v2v": "视频转视频",
}


def _normalize_video_catalog() -> None:
    for prov, entry in VIDEO_CATALOG.items():
        for m in entry.get("models") or []:
            tags = [str(t) for t in (m.get("tags") or [])]
            # 1) 推断 mode。★ 同时支持文生和图生的**不能**判成纯图生视频
            #    （那会误导用户以为必须先生成首帧），要判成 both。
            mode = str(m.get("mode") or "").strip()
            if not mode:
                t2v = any("文生" in t or "t2v" in t.lower() for t in tags)
                i2v = any("图生" in t or "i2v" in t.lower() for t in tags)
                if t2v and i2v:
                    mode = "both"
                elif i2v:
                    mode = "i2v"
                elif any("参考" in t for t in tags):
                    mode = "ref"
                else:
                    mode = "t2v"
            m["mode"] = mode
            # 2) 补 mode_zh
            if not m.get("mode_zh"):
                m["mode_zh"] = _MODE_ZH.get(mode, "文生视频")
            # 3) 标签里带上模式名，保证下拉里永远能看出这是什么类型
            if m["mode_zh"] not in tags:
                tags.insert(0, m["mode_zh"])
            m["tags"] = tags
            # 4) 兜底能力与说明，绝不留空
            if not m.get("caps"):
                m["caps"] = {"720P": [5]}
            if not m.get("best_for"):
                m["best_for"] = "通用场景"
            if not m.get("label"):
                m["label"] = m.get("id") or "未命名模型"
            if "verified" not in m:
                m["verified"] = False
        if not entry.get("display"):
            entry["display"] = prov
        if not entry.get("note"):
            entry["note"] = ""


_normalize_video_catalog()


# ────────────────────────────────────────────────────────────────
# 文本（LLM）模型目录
# ────────────────────────────────────────────────────────────────

LLM_CATALOG: Dict[str, Dict[str, Any]] = {
    "minimax": {
        "display": "MiniMax（稀宇）",
        "note": "你当前在用的对话模型供应商",
        "models": [
            # ★ 2026-09-14 用户点名要 M3。**先说清归属**：M3 是 MiniMax 的
            #   **语言模型**（官方模型日志 2026-06-01："the latest M-series
            #   language model for agentic reasoning, tool use, coding,
            #   multimodal chat input, and long-context tasks"，1M 上下文），
            #   所以它出现在**这里**（剧本/分镜/提示词），而不是「配音模型」。
            {"id": "MiniMax-M3", "label": "MiniMax-M3 · 最新旗舰（1M 上下文）",
             "tags": ["最新", "长文本", "多模态"],
             "best_for": "长剧本、复杂分场（1M 上下文，能一次读完整个剧本）",
             "verified": False},
            {"id": "MiniMax-Text-01", "label": "MiniMax-Text-01 · 长文本强", "tags": ["长文本"],
             "best_for": "剧本大纲、分场（上下文长）", "verified": True},
            {"id": "MiniMax-M1", "label": "MiniMax-M1 · 推理强", "tags": ["推理"],
             "best_for": "需要想清楚的复杂剧情", "verified": False},
            {"id": "MiniMax-VL-01", "label": "MiniMax-VL-01 · 能看图", "tags": ["视觉"],
             "best_for": "读图、看图写描述", "verified": False},
            {"id": "abab6.5s-chat", "label": "abab6.5s · 快而便宜", "tags": ["经济"],
             "best_for": "批量生成、草稿", "verified": False},
        ],
    },
    "deepseek": {
        "display": "DeepSeek（深度求索）",
        "note": "中文创作性价比很高",
        "models": [
            {"id": "deepseek-chat", "label": "deepseek-chat · 通用", "tags": ["通用", "便宜"],
             "best_for": "剧本、分镜、提示词（首选）", "verified": False},
            {"id": "deepseek-reasoner", "label": "deepseek-reasoner · 推理强", "tags": ["推理"],
             "best_for": "复杂剧情结构", "verified": False},
        ],
    },
    "qwen": {
        "display": "通义千问（阿里云）",
        "note": "中文创作稳，长上下文版本多",
        "models": [
            {"id": "qwen-max", "label": "qwen-max · 最强", "tags": ["最强"],
             "best_for": "关键剧本", "verified": False},
            {"id": "qwen-plus", "label": "qwen-plus · 均衡", "tags": ["均衡"],
             "best_for": "日常使用", "verified": False},
            {"id": "qwen-turbo", "label": "qwen-turbo · 快而便宜", "tags": ["经济"],
             "best_for": "批量", "verified": False},
        ],
    },
    "glm": {
        "display": "智谱 GLM",
        "note": "国产老牌，中文写作不错",
        "models": [
            {"id": "glm-4-plus", "label": "GLM-4-Plus · 最强", "tags": ["最强"], "best_for": "关键内容", "verified": False},
            {"id": "glm-4-flash", "label": "GLM-4-Flash · 便宜快速", "tags": ["经济"], "best_for": "批量", "verified": False},
        ],
    },
    "kimi": {
        "display": "Kimi（月之暗面）",
        "note": "超长上下文，适合长剧本",
        "models": [
            {"id": "moonshot-v1-128k", "label": "moonshot-v1-128k · 超长上下文", "tags": ["长文本"],
             "best_for": "长剧本、多集连续剧", "verified": False},
            {"id": "moonshot-v1-32k", "label": "moonshot-v1-32k · 常规", "tags": ["常规"], "best_for": "短片", "verified": False},
        ],
    },
    "doubao": {
        "display": "豆包（字节火山）",
        "note": "与 Seedance 同一平台",
        "models": [
            {"id": "doubao-pro-32k", "label": "豆包 Pro 32k · 通用", "tags": ["通用"], "best_for": "通用", "verified": False},
            {"id": "doubao-lite-32k", "label": "豆包 Lite 32k · 便宜", "tags": ["经济"], "best_for": "批量", "verified": False},
        ],
    },
    "openai": {
        "display": "OpenAI",
        "note": "需要外网环境",
        "models": [
            {"id": "gpt-4o", "label": "GPT-4o · 综合强", "tags": ["最强"], "best_for": "关键内容", "verified": False},
            {"id": "gpt-4o-mini", "label": "GPT-4o mini · 便宜", "tags": ["经济"], "best_for": "批量", "verified": False},
        ],
    },
}


def get_video_catalog(provider: str = "") -> Any:
    if provider:
        return VIDEO_CATALOG.get(provider.lower(), {"display": provider, "models": []})
    return VIDEO_CATALOG


def get_llm_catalog(provider: str = "") -> Any:
    if provider:
        return LLM_CATALOG.get(provider.lower(), {"display": provider, "models": []})
    return LLM_CATALOG


def find_video_model(model_id: str, provider: str = "") -> Dict[str, Any]:
    """找模型的完整能力条目：**先在指定厂商里找，找不到就全表找**。

    为什么要跨厂商找：分镜表里存着 `model_provider` 和 `model_name` 两个字段，
    它们可能不一致 —— 实测线上就有 `provider=hailuo` 而 `model_name=kling-1.6`
    的记录（早期切模型时留下的）。只按 provider 找就会查不到能力表，
    于是规划退回"最保守 5 秒"，用户本来能出 10 秒的镜头被压成 5+5 两段，
    多花一次钱还掉画质。跨表兜底能直接消掉这一类。

    返回 {} 表示这个模型确实不认识 —— 调用方应当如实告诉用户，
    而不是假装知道。
    """
    mid = str(model_id or "").strip()
    if not mid:
        return {}
    p = (provider or "").strip().lower()
    if p:
        for m in ((VIDEO_CATALOG.get(p) or {}).get("models") or []):
            if str(m.get("id") or "").strip() == mid:
                return m
    for prov, conf in VIDEO_CATALOG.items():
        for m in (conf.get("models") or []):
            if str(m.get("id") or "").strip() == mid:
                hit = dict(m)
                hit.setdefault("found_under", prov)
                return hit
    # 最后再试一次大小写无关的匹配（有的地方把 id 规范化过）
    low = mid.lower()
    for prov, conf in VIDEO_CATALOG.items():
        for m in (conf.get("models") or []):
            if str(m.get("id") or "").strip().lower() == low:
                hit = dict(m)
                hit.setdefault("found_under", prov)
                return hit
    return {}


def caps_for(video_provider: str, model_id: str) -> Dict[str, List[int]]:
    """取某模型的分辨率→可用时长映射（适配器用它做参数校验/自动降级）。

    ★★ 这里会**再叠一层"实测时长台账"**（`core.duraledger`）：
      静态表是**文档**，台账是**这台机器上真跑出来的事实**。
      实测栽过：`video-01` 请求 1080P/10s，厂商接受却只给 5.64s ——
      静态表写着 `[6, 10]`，规划层于是按"10 秒一次出完"排，
      成片就出现"声明 10 秒、实际 5.6 秒"的缺口。
      现在只要同一组合观测 ≥2 次且明显偏短，规划层就会自动避开那一档。
      想关掉（调试/单测）设 `VIDEOFORGE_NO_DURALEDGER=1`。
    """
    conf = VIDEO_CATALOG.get((video_provider or "").lower()) or {}
    for m in (conf.get("models") or []):
        if m.get("id") == model_id:
            caps = dict(m.get("caps") or {})
            try:
                from core import duraledger
                caps, _notes = duraledger.filter_caps(caps, video_provider, model_id)
            except Exception:
                pass
            return caps
    return {}


def caps_for_notes(video_provider: str, model_id: str) -> List[str]:
    """静态表被台账改动过的地方，给人话说明（规划器把它拼进 adjustments）。"""
    conf = VIDEO_CATALOG.get((video_provider or "").lower()) or {}
    for m in (conf.get("models") or []):
        if m.get("id") == model_id:
            try:
                from core import duraledger
                _caps, notes = duraledger.filter_caps(
                    dict(m.get("caps") or {}), video_provider, model_id)
                return list(notes or [])
            except Exception:
                return []
    return []


def pick_valid_combo(video_provider: str, model_id: str,
                     want_res: str = "", want_dur: int = 0) -> Dict[str, Any]:
    """在模型允许范围内挑一组**最接近用户意图**且一定合法的参数。

    这是"1080P + 10 秒"这类组合报错的根治办法：
    与其让用户撞墙，不如自动挑一个能跑的，并说明做了什么调整。
    """
    caps = caps_for(video_provider, model_id)
    if not caps:
        return {"resolution": want_res or "720P", "duration": want_dur or 6, "adjusted": False}
    res_list = list(caps.keys())

    def _num(r: str) -> int:
        import re as _re
        m = _re.match(r"^(\d{3,4})", str(r))
        return int(m.group(1)) if m else 0

    # 分辨率：优先精确命中；否则取最接近的
    res = want_res if want_res in caps else ""
    if not res and want_res:
        res = min(res_list, key=lambda r: abs(_num(r) - _num(want_res)))
    if not res:
        res = "1080P" if "1080P" in caps else res_list[-1]

    allowed = caps.get(res) or [6]
    # ★ 低于需求时**向上取**，不是取"最近值"。
    #   依据（MoneyPrinterTurbo 的注释伦理，material.py:741-743）：
    #   "生成比请求更长不会影响成片（剪辑流程仍按片段时长裁剪）；
    #     生成比请求更短的情况只发生在请求超过模型上限时。"
    #   也就是说：多给的秒数可以在合成时裁掉，少给的秒数会让内容缺一块。
    #   旧代码用 `abs(d - want)` 取最近值 —— 想要 7 秒时会选 6（少 1 秒），
    #   而正确选择是 10（多 3 秒，合成时裁掉）。
    if want_dur and want_dur not in allowed:
        upper = [d for d in sorted(allowed) if d >= want_dur]
        dur = upper[0] if upper else max(allowed)
    else:
        dur = want_dur or allowed[0]
    adjusted = bool(want_res and (res != want_res)) or bool(want_dur and (dur != want_dur))
    return {"resolution": res, "duration": dur, "adjusted": adjusted,
            "allowed": allowed, "all_caps": caps,
            "clamped_up": bool(want_dur and want_dur not in allowed and dur > want_dur)}

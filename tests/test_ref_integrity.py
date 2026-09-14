# -*- coding: utf-8 -*-
r"""分镜**资产关联完整性**的离线回归（"我关联了角色，人却没出现"）。

用户实测反馈（2026-09-14）：
    「我用了 seedance 2.5，关联了场景+角色，还是没出现相关的人物」

查真数据后确认：**不是模型不听话，是关联断了而且没人说**。
项目 `db380190` 的分镜 `39844f1a` 关联的是
`["ffe56cb9…(用户)", "931af29e…(旧 AI 神)"]`，
而角色表现存的是 `["ffe56cb9…(用户)", "e97e0a9e…(新 AI 神)"]`
—— 旧卡被删/重建过，分镜上留着**悬空 ID**。后果完全静默：
  · 我们丢掉悬空 ID → 提示词里只剩「恰好 **1** 个主体：用户」；
  · 「AI 神」只出现在动作描述里 → 模型**自己编了个发光壮汉**；
  · 界面上没有任何提示，用户看到"人物不对/没出现"。

本套件锁死：
  · 悬空 ID 必须被报成 **error**（参考方法论 `validate_project.py` 的
    `MISSING_REFERENCE`：引用不存在的资产 = error）；
  · 分镜文字里**提到**某角色卡但没关联它 → 也是 error（模型会自己造一个）；
  · 名字匹配必须**忽略空格/间隔号**（角色卡「AI神」vs 正文「AI 神」）；
  · 修好之后：提示词里主体数正确，且**图与人绑定编号**（`[图1]=用户`）。
"""
import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))

from core.refcheck import check_shot_refs, check_project_shots   # noqa: E402
from core.videoprompt import build_video_prompt                  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


# ──────────── 真实形状的 fixture（照 db380190 抄下来）────────────
CHARS_NOW = [
    {"id": "ffe56cb9", "name": "用户", "reference_image_path": r"C:\x\user.jpeg",
     "description": "20-35岁，冷白肤色，浅棕色短发", "age": "20-35岁",
     "costume_main": "银灰色轻质纳米面料连体衣"},
    {"id": "e97e0a9e", "name": "AI 神", "reference_image_path": r"C:\x\god.jpeg",
     "description": "无实体，蓝白色光影粒子构成的面部投影", "age": "未知",
     "costume_main": "流动的光影"},
]
SCENES = [{"id": "64a35df3", "name": "未来感科技空间",
           "reference_image_path": r"C:\x\scene.jpeg",
           "description": "青色发光机房走廊"}]
SHOT_BROKEN = {
    "id": "s1", "order_index": 0, "scene_id": "64a35df3",
    # ★ 悬空 ID：931af29e 已经不在角色表里了
    "character_ids": ["ffe56cb9", "931af29e"],
    "layer1_overview": "用户打开 DeepSeek Harness，随即一个巨大的虚拟投影——AI 神 浮现而出。",
    "layer2_timeline": [
        {"start": 0, "end": 5,
         "action": "用户站在空间中央伸手触碰设备，随后 AI 神 的投影低头注视用户。",
         "expression": "震撼", "camera": "拉镜"}],
    "layer3_constraints": {"must_appear": ["DeepSeek Harness", "AI 神 的投影"]},
}
SHOT_FIXED = dict(SHOT_BROKEN, character_ids=["ffe56cb9", "e97e0a9e"])


def main():
    print("=" * 70)
    print("分镜资产关联完整性 离线回归")
    print("=" * 70)

    # ── T1 悬空引用 + 提到但未关联 → 两个 error ──
    r = check_shot_refs(SHOT_BROKEN, CHARS_NOW, SCENES)
    codes = [i["code"] for i in r["issues"] if i["level"] == "error"]
    check("T1.1 悬空角色 ID 被报成 error", "MISSING_REFERENCE" in codes, str(codes))
    check("T1.2 文字提到但未关联也被报成 error",
          "MENTIONED_BUT_NOT_LINKED" in codes, str(codes))
    check("T1.3 整体判定为不可放行（ok=False / severity=error）",
          (not r["ok"]) and r["severity"] == "error")
    check("T1.4 报出了具体是哪个角色（AI 神）",
          any(m["name"] == "AI 神" for m in r["mentioned_not_linked"]),
          str(r["mentioned_not_linked"]))
    check("T1.5 指出它的参考图是有的（白关联了）",
          any(m["has_reference"] for m in r["mentioned_not_linked"]))
    check("T1.6 给出可操作的 hint（先别花钱 / 去补关联）",
          ("补关联" in r["hint"]) and ("花钱" in r["hint"]), r["hint"][:60])

    # ── T2 修好之后必须放行 ──
    r2 = check_shot_refs(SHOT_FIXED, CHARS_NOW, SCENES)
    check("T2.1 关联补齐后 0 个 error", r2["severity"] == "ok" and r2["ok"],
          f"{r2['severity']} {r2['issues']}")
    check("T2.2 体检摘要里写清关联了 2 个角色",
          "2 个" in r2["summary"], r2["summary"])
    check("T2.3 场景也在（不是 MISSING_SCENE）",
          (not r2["missing_scene"]) and r2["scene"] == "未来感科技空间")

    # ── T3 名字匹配忽略空格/间隔号（AI神 ↔ AI 神）──
    shot_nospace = dict(SHOT_FIXED, character_ids=["ffe56cb9"],
                        layer2_timeline=[{"start": 0, "end": 5,
                                          "action": "AI神浮现而出。", "camera": "拉镜"}])
    r3 = check_shot_refs(shot_nospace, CHARS_NOW, SCENES)
    check("T3.1 正文写「AI神」而卡名是「AI 神」也要认出来",
          any(m["name"] == "AI 神" for m in r3["mentioned_not_linked"]),
          str(r3["mentioned_not_linked"]))
    shot_dot = dict(SHOT_FIXED, character_ids=[],
                    layer2_timeline=[{"start": 0, "end": 5,
                                      "action": "琼恩·雪诺 抬头。", "camera": "推镜"}])
    chars2 = CHARS_NOW + [{"id": "c9", "name": "琼恩·雪诺", "reference_image_path": ""}]
    r3b = check_shot_refs(shot_dot, chars2, SCENES)
    check("T3.2 间隔号写法（琼恩·雪诺 ↔ 琼恩雪诺）也认得出",
          any(m["name"] == "琼恩·雪诺" for m in r3b["mentioned_not_linked"]),
          str(r3b["mentioned_not_linked"]))

    # ── T4 场景悬空 → 只 warn（还能出片）──
    r4 = check_shot_refs(dict(SHOT_FIXED, scene_id="不存在的场景"), CHARS_NOW, SCENES)
    check("T4.1 场景不存在 → warn 而不是 error",
          r4["severity"] == "warn" and r4["missing_scene"],
          f"{r4['severity']} {r4['missing_scene']}")

    # ── T5 整片体检 ──
    p = check_project_shots([SHOT_BROKEN, SHOT_FIXED], CHARS_NOW, SCENES)
    check("T5.1 整片体检数出 1 个有问题的镜头",
          p["total"] == 2 and p["error_shots"] == 1, json.dumps(p)[:80])
    check("T5.2 空输入不炸", check_project_shots([], CHARS_NOW, SCENES)["ok"] is True)

    # ── T6 提示词：修好之后主体数与人名都要对 ──
    vp = build_video_prompt(SHOT_FIXED, scene=SCENES[0], characters=CHARS_NOW,
                            provider="seedance", duration=5,
                            aspect_ratio="16:9", resolution="1080p", ref_scope=True)
    txt = vp["prompt"]
    check("T6.1 主体数是 2（旧代码这里只有 1，因为悬空 ID 被静默丢掉）",
          "恰好 2 个主体" in txt, txt[:60])
    check("T6.2 两个角色名都进了主体行", ("用户" in txt) and ("AI 神" in txt))
    check("T6.3 图与人绑定编号（`[图1]=用户`、`[图2]=AI 神`）",
          ("[图1] 用户" in txt) and ("[图2] AI 神" in txt),
          txt[txt.find("参考图对应关系"):txt.find("参考图对应关系") + 60])
    check("T6.4 场景图也有编号（[图3]=场景环境）", "[图3]=场景环境" in txt)
    check("T6.5 明确要求人物必须与对应参考图是同一个人",
          "是同一个人" in txt)
    check("T6.6 绑定编号只在有参考图时出现（ref_scope=False 不该有）",
          "[图1]" not in build_video_prompt(SHOT_FIXED, scene=SCENES[0],
                                            characters=CHARS_NOW, provider="seedance",
                                            duration=5, ref_scope=False)["prompt"])

    # ── T7 提示词顺序必须与适配器发送顺序同源（角色在前、场景在后）──
    check("T7.1 主体行里角色编号连续且从 1 开始",
          "[图1] 用户" in txt and "[图2] AI 神" in txt)

    # ── T8 源码级锁定：生成前必须体检、补关联必须能修悬空引用 ──
    main_src = io.open(os.path.join(HERE, "..", "backend", "main.py"),
                       encoding="utf-8").read()
    check("T8.1 生成前跑了引用体检（生成前门）",
          "check_shot_refs" in main_src and "生成前引用体检" in main_src)
    check("T8.2 体检结果随生成结果返回（ref_check + refs_sent）",
          '"ref_check": ref_check' in main_src and '"refs_sent"' in main_src)
    check("T8.3 补关联会按文字提到的角色补齐、并剔除悬空 ID",
          "mentioned_not_linked" in main_src and "剔除悬空 ID" in main_src)
    check("T8.4 有独立的体检接口",
          '/api/projects/{pid}/shots/ref-check' in main_src)

    print("\n" + "=" * 70)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
    for f in FAIL:
        print("  [x] " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

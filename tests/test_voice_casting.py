# -*- coding: utf-8 -*-
r"""选角（`core.voicecatalog`）与音色可用性记忆的离线回归。

用户的原话：「不是你那几个固定的 NPC 发音。配音没这么简单的」。

查出的事实：工具能列到 **52 个音色**（中文 44），而 `voicecast` 里硬编码的
池子只有 **8 个**，选角靠"角色名哈希取模"。角色卡其实有
`age`（"40岁上下"）、`role`（protagonist/antagonist）、`personality`
（很多卡直接把"说话语气强硬且带煽动性"写进去了）可用。

这里锁死：
  ① 音色名 → 年龄段/气质标签的解析（含"名字没写年龄时保守推断"）；
  ② 角色卡 → 说话画像（性别/年龄/定位/气质）；
  ③ 打分选角：性别硬条件、气质命中、**不撞音**、理由可读；
  ④ 群体角色（"村民们"）与性别缺失要被**标记出来**，不能悄悄配一把异性嗓子；
  ⑤ 音色可用性记忆：失败过的音色不再进候选（Edge 静态表里有 14 个是坏的）。
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))

from core import voicecatalog as vc                            # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def main() -> int:
    # ① 音色名解析（用真实标签）
    a = vc.parse_voice_attrs("晓梦 · 儿童女声", "female")
    check("T1 儿童女声 → child", a["age_band"] == "child" and "童趣" in a["tags"], str(a))
    a = vc.parse_voice_attrs("晓睿 · 成熟女声", "female")
    check("T2 成熟女声 → mature", a["age_band"] == "mature", str(a))
    a = vc.parse_voice_attrs("低沉男声（alex）", "male")
    check("T3 名字没写年龄 → 由气质保守推断（阴冷→不推断年龄）",
          a["age_band"] == "" and "阴冷" in a["tags"], str(a))
    a = vc.parse_voice_attrs("云希 · 阳光男声", "male")
    check("T4 阳光 → young（推断，且标明是推断）",
          a["age_band"] == "young" and a["age_inferred"] is True, str(a))

    # ② 年龄描述解析
    for txt, want in (("40岁上下", "adult"), ("20岁左右", "young"),
                      ("50岁左右", "mature"), ("20-30岁", "young"),
                      ("各年龄段", ""), ("无明确年龄", "")):
        got = vc.age_band_from_text(txt)
        check(f"T5 年龄解析 {txt!r} → {want or '不确定'}", got == want, f"得到 {got!r}")

    # ③ 角色画像（用真实卡片的原话）
    p = vc.role_profile({"name": "李队长", "age": "40岁上下",
                         "reference_features": '{"gender":"male","role":"protagonist",'
                                               '"personality":"沉着冷静，机智果断，言语简洁有力，带有激励性。"}'})
    check("T6 画像：性别/年龄/定位/气质都读出来",
          p["gender"] == "male" and p["age_band"] == "adult" and p["role"] == "protagonist"
          and "沉稳" in p["traits"] and "有力" in p["traits"], str(p))
    p = vc.role_profile({"name": "小鬼子军官", "age": "35岁左右",
                         "reference_features": '{"gender":"male","role":"antagonist",'
                                               '"personality":"傲慢自大，凶狠残暴。"}'})
    check("T7 画像：反派读出强硬/阴冷", "强硬" in p["traits"] or "阴冷" in p["traits"], str(p["traits"]))
    p = vc.role_profile({"name": "村民们", "age": "各年龄段",
                         "reference_features": '{"gender":"mixed","role":"minor"}'})
    check("T8 群体角色被标记（不能当个体处理）", p["is_group"] is True, str(p))

    # ④ 打分与选角（用合成目录，纯函数、可离线重复）
    cat = [
        {"voice_id": "edge:m1", "gender": "male", "label": "云希 · 阳光男声",
         "provider": "edge", "verified": True, "age_band": "young", "tags": ["阳光"]},
        {"voice_id": "x:deep", "gender": "male", "label": "低沉男声 benjamin",
         "provider": "siliconflow", "verified": False, "age_band": "", "tags": ["阴冷"]},
        {"voice_id": "edge:f1", "gender": "female", "label": "晓晓 · 温柔女声",
         "provider": "edge", "verified": True, "age_band": "young", "tags": ["温柔"]},
        {"voice_id": "x:mature", "gender": "male", "label": "成熟男性音色",
         "provider": "minimax", "verified": False, "age_band": "mature", "tags": ["沉稳"]},
    ]
    chars = [
        {"name": "刺客", "reference_features": '{"gender":"male","role":"antagonist",'
                                              '"personality":"冷酷无情，善于伪装。"}'},
        {"name": "青年战士", "reference_features": '{"gender":"male","age":"22岁",'
                                                 '"role":"protagonist","personality":"勇敢热血。"}'},
        {"name": "中年农夫", "reference_features": '{"gender":"male","personality":"沉稳老练，沉默寡言。"}'},
    ]
    r = vc.cast_from_catalog(chars, cat)
    check("T9 刺客（阴冷）→ 低沉男声", r["voices"].get("刺客") == "x:deep",
          r["reasons"].get("刺客", ""))
    check("T10 青年战士（热血/22岁）→ 阳光男声", r["voices"].get("青年战士") == "edge:m1",
          r["reasons"].get("青年战士", ""))
    check("T11 中年农夫（沉稳）→ 成熟男性音色（气质+年龄都指向它）",
          r["voices"].get("中年农夫") == "x:mature", r["reasons"].get("中年农夫", ""))
    check("T12 三个角色**不撞音**", not r["collisions"], str(r["collisions"]))
    check("T13 每个选择都有可读理由",
          all(r["reasons"].get(c["name"]) for c in chars), str(r["reasons"]))

    # 性别是硬条件：不许把男性角配成女声
    r2 = vc.cast_from_catalog([{"name": "硬汉", "reference_features": '{"gender":"male"}'}], cat)
    check("T14 性别是硬条件（不会配到女声）",
          r2["voices"].get("硬汉") not in ("edge:f1",), str(r2["voices"]))

    # 性别缺失要被标记出来
    r3 = vc.cast_from_catalog([{"name": "水手"}], cat)
    check("T15 角色卡没写性别时，理由里明确提示（而不是悄悄配一把）",
          "没写性别" in (r3["reasons"].get("水手") or ""), r3["reasons"].get("水手", ""))

    # ⑤ 音色可用性记忆：失败过的不再进候选
    from core import voicecast as vcw
    tmp = tempfile.mkdtemp(prefix="vf_vh_")
    old = os.environ.get("VIDEOFORGE_DATA_DIR")
    os.environ["VIDEOFORGE_DATA_DIR"] = tmp
    try:
        vcw.mark_voice_health("edge:zh-CN-XiaoruiNeural", False, "NoAudioReceived")
        h = vcw.load_voice_health()
        check("T16 失败的音色被记下来（学到「这把嗓子发不出声」）",
              h.get("edge:zh-CN-XiaoruiNeural", {}).get("ok") is False, str(h))
        vcw.mark_voice_health("edge:zh-CN-YunxiNeural", True)
        h = vcw.load_voice_health()
        check("T17 成功的音色也记下来，且不破坏已有记录",
              h.get("edge:zh-CN-YunxiNeural", {}).get("ok") is True
              and h.get("edge:zh-CN-XiaoruiNeural", {}).get("ok") is False, str(h))
    finally:
        if old is None:
            os.environ.pop("VIDEOFORGE_DATA_DIR", None)
        else:
            os.environ["VIDEOFORGE_DATA_DIR"] = old

    print("\n" + "=" * 74)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
    for f in FAIL:
        print("  [x] " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

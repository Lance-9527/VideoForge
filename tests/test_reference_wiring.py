# -*- coding: utf-8 -*-
r"""「角色图/场景图到底有没有被采用」这条链的回归。

用户反馈：「AI 生成的角色、场景与分镜关联后，出视频时人物形象和场景跟图不符」。
查出的事实（`tests/probe_reference_usage.py` 可复现）：

  · 10 个适配器里**只有 seedance 读** `reference_images` / `character_reference`，
    其余 9 个（含用户在用的 hailuo）**源码里压根没读这两个字段** → 静默丢弃；
  · `validate_request` 原来**不校验**这两个字段 → 上层那套"优雅降级"
    永远不知道有东西被丢 → **用户界面上一个字都没有**；
  · 于是人物形象只能靠提示词里的文字签名，跟 AI 生成的形象图对不上。

这个套件锁死修好之后的行为：
  ① 不支持参考图的模型，校验必须**明确报出来**（否则又是静默丢弃）；
  ② 降级必须**收敛**，且只丢该丢的（首帧图绝不能丢）；
  ③ 降级失败时必须**全部还原**，不许留下半残请求；
  ④ 支持参考图的模型（seedance）不许被误降级。
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))

from core.adapters import (VideoGenRequest, degrade_request,  # noqa: E402
                           get_adapter, list_providers)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


REFS = ["data:image/jpeg;base64,AAAA"]


def mkreq(**kw):
    base = dict(prompt="测试", duration=6, aspect_ratio="16:9", resolution="768P",
                reference_images=list(REFS), character_reference=list(REFS),
                first_frame="data:image/png;base64,BBBB")
    base.update(kw)
    return VideoGenRequest(**base)


def main() -> int:
    hailuo = get_adapter("hailuo", api_key="x",
                         config={"model_name": "MiniMax-Hailuo-02"})
    i2v = get_adapter("hailuo", api_key="x", config={"model_name": "I2V-01"})
    seed = get_adapter("seedance", api_key="x",
                       config={"model_name": "doubao-seedance-2-5-260628"})
    check("T0 拿到三个适配器", all((hailuo, i2v, seed)))

    # ① 不支持参考图 → 必须报出来
    v = hailuo.validate_request(mkreq()) or ""
    check("T1 不支持参考图的模型会明确报出（不再静默丢弃）",
          ("参考图" in v) or ("reference" in v.lower()), f"verr={v!r}")
    v2 = seed.validate_request(mkreq(resolution="720P", duration=5)) or ""
    check("T2 支持参考图的模型（seedance）不会因为参考图报错",
          "参考图" not in v2, f"verr={v2!r}")

    # ② 降级收敛：丢掉参考图、保住首帧
    req = mkreq()
    verr, deg = degrade_request(hailuo, req)
    check("T3 降级收敛（不再出现「两个字段互相还原」的死循环）",
          verr is None and set(deg) >= {"reference_images", "character_reference"},
          f"verr={verr!r} degraded={deg}")
    check("T4 降级只丢参考图、**保住首帧图**",
          bool(req.first_frame) and not (req.reference_images or
                                         req.character_reference),
          f"first_frame={bool(req.first_frame)} "
          f"refs={len(req.reference_images or [])}/{len(req.character_reference or [])}")

    # 必须首帧的版本（I2V）：更不能把首帧丢掉
    req2 = mkreq()
    verr2, deg2 = degrade_request(i2v, req2)
    check("T5 图生视频版本（I2V-01）降级后仍然带首帧",
          verr2 is None and bool(req2.first_frame), f"verr={verr2!r}")

    # ③ 降级解决不了 → 全部还原，原样报错
    req3 = mkreq(aspect_ratio="4:3")          # 画幅非法，不在可剥离字段里
    verr3, deg3 = degrade_request(hailuo, req3)
    check("T6 降级解决不了时**全部还原**并如实报错（不留半残请求）",
          verr3 and not deg3 and bool(req3.reference_images)
          and bool(req3.character_reference) and bool(req3.first_frame),
          f"verr={verr3!r} degraded={deg3} refs={len(req3.reference_images or [])}")

    # ④ seedance 支持参考图 → 不该被降级
    req4 = mkreq(resolution="720P", duration=5)
    verr4, deg4 = degrade_request(seed, req4)
    check("T7 支持参考图的模型不被降级（参考图仍在）",
          verr4 is None and deg4 == [] and bool(req4.character_reference),
          f"verr={verr4!r} degraded={deg4}")

    # ⑤ 契约记录：现在到底哪些 provider 真的会用参考图
    users = []
    for p in list_providers():
        name = p.get("name") or ""
        try:
            a = get_adapter(name, api_key="x",
                            config={"model_name": (p.get("models") or [{}])[0].get("id")})
        except Exception:
            a = None
        if a is not None and a.supports_character_ref:
            users.append(name)
    check("T8 契约记录：目前真的会采用人物参考图的 provider 数量（少是事实，不是 bug）",
          len(users) >= 1, f"会用的：{users or '（一个都没有）'}")

    # ⑥ 首帧用哪张图：显式取舍 + 回退
    from core.adapters import pick_first_frame as pick
    r = pick(scene_ref="S", char_refs=["C1", "C2"], chain_ref="T",
             scene_path="s.jpg", chain_path="t.mp4", priority="auto")
    check("T9 auto：有上一镜尾帧就用它（衔接优先）",
          r["kind"] == "chain" and r["first_frame"] == "T", str(r))
    r = pick(scene_ref="S", char_refs=["C1", "C2"], chain_ref="T",
             scene_path="s.jpg", chain_path="t.mp4", priority="scene")
    check("T10 scene：固定用场景图（不接尾帧）",
          r["kind"] == "scene" and r["first_frame"] == "S", str(r))
    r = pick(scene_ref="S", char_refs=["C1", "C2"], chain_ref="T",
             scene_path="s.jpg", chain_path="t.mp4", priority="character")
    check("T11 character：用第一个角色的形象图",
          r["kind"] == "character" and r["first_frame"] == "C1", str(r))
    r = pick(scene_ref="S", char_refs=[], chain_ref="T", scene_path="s.jpg",
             priority="character")
    check("T12 character 但没有角色图 → 回退到场景图（不是回退成「没有图」）",
          r["kind"] == "scene" and r["first_frame"] == "S", str(r))
    r = pick(priority="scene")
    check("T13 一张图都没有 → 明确 kind=none（上层据此告警）",
          r["kind"] == "none" and not r["first_frame"], str(r))

    print("\n" + "=" * 72)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
    for f in FAIL:
        print("  [x] " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

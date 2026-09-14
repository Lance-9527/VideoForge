# -*- coding: utf-8 -*-
r"""厂商参数与参考图装配的离线回归（seedance / jimeng）。

用户实测反馈（2026-09-14）：
    「就算我用了 seedance 视频模型，关联了我生成的图片还有场景，但是生成的视频
      并没有按我的剧本来做，比如该出现的人物并没有在我的视频里面体现（场景我看到了，有）」

查应用日志与真数据后，根因是**三个各自独立的缺陷**（都不是"模型不听话"）：

  ① **画幅参数写错了**：`videoprompt.py` 把 `16:9` 用 `.replace(":", "x")` 变成
     `16x9` 写进提示词后缀（`--ratio 16x9`），`seedance.py` 的
     `parameters.ratio` 也是 `"16x9"` —— 方舟不认，按默认出了 **960x960（1:1）**。
     （讽刺的是我自己的探针 `probe_ark_which.py` 用的是正确的 `16:9`：
       产品代码与探针代码不一致，谁也没去对。）
  ② **参考图与首帧二选一**：`seedance.py` 是 `if 角色图 … elif 首帧图` ——
     只要传了角色图，**场景首帧永远不发**；而且只发**第一张**角色图，
     第二个角色（本例的「AI神」）直接被丢掉，模型只能凭空造。
  ③ **画幅守卫把已付费的片段丢掉了** → 上层退回本地合成（一张图的运镜），
     用户看到的"没按剧本演"其实是兜底幻灯片。

本套件锁死①和②（③见 `tests/probe_project_dub_dryrun.py` 与 main.py 里的登记逻辑），
外加两个"学自 Hell-Grind 方法论"的新东西：
  · 参考图必须写明**继承什么、排除什么**（`reference-asset-control.md:64-81`）；
  · 失败码 F-xxx 与责任层（`failure-diagnosis.md`）。
"""
import asyncio
import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))

from core.aspect import normalize_ratio                        # noqa: E402
from core.videoprompt import build_video_prompt                # noqa: E402
from core.adapters import VideoGenRequest                      # noqa: E402
from core.adapters.seedance import SeedanceAdapter             # noqa: E402
from core.adapters.jimeng import JimengAdapter                 # noqa: E402
from core import failcodes as fc                               # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


class _FakeResp:
    status_code = 200

    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


class _FakeClient:
    """假 httpx：把请求体留下来，**不碰网络、不花钱**。"""

    def __init__(self):
        self.calls = []
        self.is_closed = False

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json})
        # 直接返回"任务已成功"，省掉轮询
        return _FakeResp({"id": "cgt-test", "status": "succeeded",
                          "content": {"video_url": "http://example/x.mp4"}})

    async def get(self, url, **kw):
        return _FakeResp({"status": "succeeded", "content": {"video_url": "http://example/x.mp4"}})

    async def aclose(self):
        self.is_closed = True


SHOT = {
    "id": "s1", "order_index": 0, "duration_seconds": 5,
    "layer1_overview": "未来感科技空间里，用户打开 DeepSeek Harness，巨大的虚拟投影 AI神 浮现。",
    "layer2_timeline": [
        {"start": 0, "end": 5, "action": "用户抬头看向 AI神 投影，镜头缓慢推近。",
         "expression": "震撼", "camera": "缓慢推近"}],
    "layer3_constraints": {"must_not_appear": ["实体人物"], "must_keep": ["投影"]},
}
CHARS = [{"name": "用户", "description": "冷白肤色、细长眼、短发深棕",
          "costume": "深色连帽衫"},
         {"name": "AI神", "description": "无实体，蓝白色光影粒子构成的面部投影",
          "costume": "流动的光影"}]


def build(provider="seedance", **kw):
    return build_video_prompt(SHOT, scene={"name": "未来感科技空间"},
                              characters=CHARS, provider=provider,
                              duration=5, aspect_ratio="16:9", resolution="720p", **kw)


def main():
    print("=" * 70)
    print("厂商参数 / 参考图装配 离线回归")
    print("=" * 70)

    # ── T1 画幅写法统一 ──
    cases = {"16:9": "16:9", "16x9": "16:9", "16X9": "16:9", "9:16": "9:16",
             "1:1": "1:1", "4:3": "4:3", "21:9": "21:9", "16:9:9": "16:9:9"}
    bad = {k: normalize_ratio(k) for k, v in cases.items() if normalize_ratio(k) != v}
    check("T1.1 画幅写法统一成 W:H（16x9 → 16:9）", not bad, str(bad))
    check("T1.2 空值回默认", normalize_ratio("") == "16:9")

    # ── T2 参数**只在适配器 body 里写一次**（官方：body 强校验 / 后缀弱校验）──
    p_seed = build("seedance")["prompt"]
    check("T2.1 提示词里不再追加 --ratio 后缀（两处参数打架就是这次事故来源）",
          "--ratio" not in p_seed and "--resolution" not in p_seed, p_seed[-80:])
    check("T2.2 提示词里不出现 16x9（旧代码的非法写法）", "16x9" not in p_seed)
    check("T2.3 厂商参数改从 meta.vendor_params 透出（供上层核对）",
          ((build("seedance").get("meta") or {}).get("vendor_params") or {}).get("ratio") == "16:9",
          str((build("seedance").get("meta") or {}).get("vendor_params")))
    p_jm = build("jimeng")["prompt"]
    check("T2.4 jimeng 同样不写后缀", "--ratio" not in p_jm)
    p_hailuo = build("hailuo")["prompt"]
    check("T2.5 非火山系平台本来就没有后缀", "--ratio" not in p_hailuo)

    # ── T3 seedance 的 content：首帧 + 全部角色图都要发 ──
    req = VideoGenRequest(prompt=p_seed, duration=5, aspect_ratio="16:9",
                          resolution="720p",
                          first_frame="data:image/jpeg;base64,SCENE",
                          character_reference=["data:image/jpeg;base64,USER",
                                               "data:image/jpeg;base64,GOD"],
                          negative_prompt="实体人物")
    ad = SeedanceAdapter(api_key="k", config={"model_name": "doubao-seedance-2-0-fast"})
    fake = _FakeClient()
    ad._client = fake
    r = asyncio.run(ad.generate(req))
    body = fake.calls[0]["json"] if fake.calls else {}
    roles = [c.get("role") for c in (body.get("content") or []) if c.get("type") == "image_url"]
    check("T3.1 生成调用成功（假客户端）", r.success, str(r.error))
    check("T3.2 ratio/resolution/duration 在 body 顶层（官方强校验路径）",
          body.get("ratio") == "16:9" and body.get("resolution") == "720p"
          and body.get("duration") == 5 and "parameters" not in body,
          f"ratio={body.get('ratio')} parameters={'parameters' in body}")
    check("T3.3 有角色图时走多模态参考：角色图 + 场景图都作为 reference_image",
          roles.count("reference_image") == 3, str(roles))
    check("T3.4 互斥规则：reference_image 与 first_frame **不能同时出现**",
          "first_frame" not in roles and "last_frame" not in roles, str(roles))
    check("T3.5 文本提示词在最前面", (body.get("content") or [{}])[0].get("type") == "text")
    check("T3.6 角色图排在场景图**前面**（身份锚点优先，且与提示词里的 [图N] 编号同源）",
          (body["content"][1].get("image_url") or {}).get("url", "").endswith("USER")
          and (body["content"][3].get("image_url") or {}).get("url", "").endswith("SCENE"),
          str([(c.get("image_url") or {}).get("url", "")[-6:] for c in body["content"]
               if c.get("type") == "image_url"]))

    # ── T4 没有场景首帧时也不该出错 ──
    req2 = VideoGenRequest(prompt=p_seed, duration=5, aspect_ratio="9:16",
                           resolution="720p",
                           character_reference=["data:image/jpeg;base64,USER"])
    fake2 = _FakeClient()
    ad2 = SeedanceAdapter(api_key="k", config={})
    ad2._client = fake2
    asyncio.run(ad2.generate(req2))
    b2 = fake2.calls[0]["json"]
    roles2 = [c.get("role") for c in b2["content"] if c.get("type") == "image_url"]
    check("T4.1 没有首帧时不硬造 first_frame", "first_frame" not in roles2, str(roles2))
    check("T4.2 9:16 传下去也是 9:16（且在顶层）",
          b2.get("ratio") == "9:16" and "parameters" not in b2, str(b2.get("ratio")))

    # ── T5 参考图范围（学自 reference-asset-control.md:64-81）──
    with_scope = build("seedance", ref_scope=True)["prompt"]
    without = build("seedance", ref_scope=False)["prompt"]
    check("T5.1 有参考图时写了要继承什么", "继承" in with_scope)
    check("T5.2 也写了要排除什么", "排除" in with_scope)
    check("T5.3 明确排除原图的构图/背景/光线（否则参考图会把背景带进来）",
          all(k in with_scope for k in ("构图", "背景", "光线")), with_scope[-160:])
    check("T5.4 没有参考图时不写这段（不浪费预算）", "只继承" not in without)

    # ── T6 主体锁定（契约 #2）没被改坏 ──
    check("T6.1 提示词里有恰好N个主体、且声明未列出的角色不在画面",
          "恰好" in p_seed and "未列出的角色不在画面" in p_seed)
    check("T6.2 一个镜头只有一个主运动（没有第二个运镜词）",
          "只有这一个主运动" in p_seed)

    # ── T7 失败码：用**真实报错原文**验证归类 ──
    real = ("真实模型 seedance 失败：第 1/1 段（720p）：画幅不符：分镜要 16:9，"
            "实际出的是 960x960（1:1）；铺满成片会裁掉 44% 的画面")
    a = fc.annotate(real)
    check("T7.1 画幅不符归到 F-ASPECT-MISMATCH", a["codes"][:1] == ["F-ASPECT-MISMATCH"],
          str(a["codes"]))
    check("T7.2 归到平台适配层（该改参数，不是改提示词）",
          a["layer"] == "adapter", a["layer"])
    check("T7.3 hint 里带上了先检查与最小修复",
          ("先检查" in a["hint"]) and ("最小修复" in a["hint"]), a["hint"][:80])
    check("T7.4 参数被拒 → F-PARAM-REJECTED",
          fc.classify("[2013] invalid params, param 'ratio'")[0]["code"] == "F-PARAM-REJECTED")
    check("T7.5 模型未开通 → F-MODEL-MISSING",
          fc.classify("ModelNotOpen: the model is not open")[0]["code"] == "F-MODEL-MISSING")
    check("T7.6 余额不足 → F-BALANCE（不要再重试抽卡）",
          fc.classify("MiniMax 语音失败 [1008] 余额不足")[0]["code"] == "F-BALANCE")
    check("T7.7 认不出来的文本**不硬套**错误码（方法论元规则）",
          fc.classify("本分镜没有可念的台词") == [])
    check("T7.8 空文本不炸", fc.classify(None) == [] and fc.explain("F-XXX")["layer"] == "")
    check("T7.9 连续两批同一错误 → 给出停止条件（别继续抽卡）",
          any("连续两个批次" in x for x in fc.stop_conditions(3, same_code_streak=2)),
          str(fc.stop_conditions(3, same_code_streak=2))[:70])
    check("T7.10 责任层顺序表是完整 6 层",
          len(fc.LAYERS) == 6 and fc.LAYERS[0] == "asset" and fc.LAYERS[-1] == "post")

    # ── T8 源码级锁定：画幅不符时必须**登记候选**，不能再丢 ──
    main_src = io.open(os.path.join(HERE, "..", "backend", "main.py"),
                       encoding="utf-8").read()
    check("T8.1 main.py 在画幅 fatal 分支里登记了候选（付费片段不丢）",
          "aspect_mismatch" in main_src and "_register_local_candidate(shot, _reg_meta" in main_src)
    check("T8.2 画幅 fatal 的 hint 里说清了没丢、可以怎么用",
          "这条片段没有丢" in main_src)
    check("T8.3 画幅 fatal 的返回里带回失败码与责任层",
          "failcodes" in main_src and "responsibility_layer" in main_src)

    print("\n" + "=" * 70)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
    for f in FAIL:
        print("  [x] " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

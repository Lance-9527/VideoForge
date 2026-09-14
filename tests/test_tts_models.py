# -*- coding: utf-8 -*-
r"""配音模型选择（"真正的去选择配音模型"）的离线回归。

用户原话：
    「最好是可以真正的去选择配音模型（模型新增 minimax M3 模型）」
    「一键配音在我软件上还是没能体现，我点击后还是单独的那个 AI 女音」

在此之前"选配音模型"是**不可能**的，有两个各自独立的断点：
  ① `minimax_tts.py` 读 `(self.config or {}).get("model") or "speech-02-hd"`，
     而 `dispatcher.synthesize()` 构造 provider 时**只传 `api_key=`**，
     `config` 从来没传过 → 型号实际上**写死在代码里**；
  ② 设置模型（`models.SettingsUpdate`）里**没有** `tts_models` 字段，
     界面上也没有任何型号下拉。

所以本套件锁的是"设置 → dispatcher → 请求体"这条链**真的通了**，
外加一件事必须说清、也锁在这里：
    **MiniMax M3 是语言模型，不是配音模型**（官方模型日志：M-series
    language model）。它应该在 LLM 目录里，不该出现在 `speech-*` 清单中。
"""
import asyncio
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))

from core.voice.base import TTSRequest, TTSResult                    # noqa: E402
from core.voice import dispatcher as vd                              # noqa: E402
from core.voice.minimax_tts import MiniMaxTTS, MINIMAX_MODELS        # noqa: E402
from core.voice.edge_tts import EdgeTTS                              # noqa: E402
from core.voice.siliconflow_tts import SiliconFlowTTS                # noqa: E402
from core.voice.dashscope_tts import DashScopeTTS                    # noqa: E402
from core.model_catalog import LLM_CATALOG                           # noqa: E402
from models import SettingsUpdate                                    # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


class _FakeResp:
    status_code = 200
    text = "{}"

    def json(self):
        return {"data": {"audio": "0011223344"}, "base_resp": {"status_code": 0}}


class _FakeClient:
    """假 httpx 客户端：把请求体留下来，不碰网络、不花钱。"""

    def __init__(self):
        self.calls = []
        self.is_closed = False

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return _FakeResp()

    async def aclose(self):
        self.is_closed = True


def main():
    print("=" * 70)
    print("配音模型选择（tts_models）离线回归")
    print("=" * 70)

    # ── T1 型号解析 ──
    check("T1.1 没设置时用厂商默认（minimax = speech-2.8-hd）",
          vd.resolve_tts_model("minimax", {}) == "speech-2.8-hd",
          vd.resolve_tts_model("minimax", {}))
    check("T1.2 设置了就用用户选的",
          vd.resolve_tts_model("minimax",
                               {"tts_models": {"minimax": "speech-2.6-turbo"}})
          == "speech-2.6-turbo")
    check("T1.3 厂商名大小写不敏感（MiniMax / minimax）",
          vd.resolve_tts_model("minimax",
                               {"tts_models": {"MiniMax": "speech-2.6-hd"}})
          == "speech-2.6-hd")
    check("T1.4 界面上填的额外型号也放行（厂商上新不用等发版）",
          vd.resolve_tts_model("minimax",
                               {"tts_models": {"minimax": "speech-9.9-hd"}})
          == "speech-9.9-hd")
    check("T1.5 空值不算选了型号（回退默认）",
          vd.resolve_tts_model("minimax", {"tts_models": {"minimax": ""}})
          == "speech-2.8-hd")
    check("T1.6 没有型号概念的引擎返回空串（不编一个假型号）",
          vd.resolve_tts_model("edge", {}) == "")

    # ── T2 ★ 最关键的回归：型号真的进了请求体 ──
    #   这是原来断掉的那一环（dispatcher 不传 config）。
    p = MiniMaxTTS(api_key="k", config={"model": "speech-2.6-turbo"})
    fake = _FakeClient()
    p._client = fake
    with tempfile.TemporaryDirectory() as tmp:
        r = asyncio.run(p.synthesize(TTSRequest(
            text="你是谁？", voice_id="minimax:male-qn-qingse",
            output_path=os.path.join(tmp, "a.mp3"),
            emotion="angry", pitch_semitones=2.0)))
    body = fake.calls[0]["json"] if fake.calls else {}
    check("T2.1 合成成功（假客户端）", r.success, str(r.error))
    check("T2.2 请求体里的 model = 用户选的型号",
          body.get("model") == "speech-2.6-turbo", str(body.get("model")))
    check("T2.3 情绪真的发出去了（官方支持 voice_setting.emotion）",
          (body.get("voice_setting") or {}).get("emotion") == "angry",
          str((body.get("voice_setting") or {}).get("emotion")))
    check("T2.4 音高不再是写死的 0（韵律算出来的半音发出去）",
          (body.get("voice_setting") or {}).get("pitch") == 2,
          str((body.get("voice_setting") or {}).get("pitch")))
    check("T2.5 端点是 t2a_v2（MiniMax 的 hex 音频接口）",
          "t2a_v2" in (fake.calls[0]["url"] if fake.calls else ""),
          str(fake.calls[0]["url"] if fake.calls else ""))

    # ── T3 情绪映射：只发有把握的，映射不到就不发 ──
    p2 = MiniMaxTTS(api_key="k", config={"model": "speech-2.8-hd"})
    f2 = _FakeClient()
    p2._client = f2
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(p2.synthesize(TTSRequest(
            text="嗯", voice_id="minimax:female-yujie",
            output_path=os.path.join(tmp, "b.mp3"),
            emotion="斯德哥尔摩综合征")))     # 故意给一个怪情绪
    vs = (f2.calls[0]["json"] or {}).get("voice_setting") or {}
    check("T3.1 认不出的情绪**不发** emotion 字段（宁可不演，不乱演）",
          "emotion" not in vs, str(vs))
    check("T3.2 认不出的情绪不影响其它参数",
          vs.get("voice_id") == "female-yujie" and vs.get("pitch") == 0, str(vs))

    # ── T4 dispatcher 真的把 config 传下去（原来的断点）──
    captured = {}

    class _StubProv:
        async def synthesize(self, req):
            return TTSResult(success=True, audio_path="stub")

        async def close(self):
            pass

    _orig = vd._base_get_provider

    def _fake_get(name, api_key="", config=None):
        captured["name"], captured["config"], captured["api_key"] = name, config, api_key
        return _StubProv()

    vd._base_get_provider = _fake_get
    try:
        asyncio.run(vd.synthesize(
            TTSRequest(text="hi", voice_id="minimax:male-qn-qingse"),
            {"tts_api_keys": {"minimax": "k"},
             "tts_models": {"minimax": "speech-2.6-hd"}}))
    finally:
        vd._base_get_provider = _orig
    check("T4.1 dispatcher 把型号传给了 provider（旧代码这里恒为空）",
          captured.get("config") == {"model": "speech-2.6-hd"},
          str(captured.get("config")))
    check("T4.2 Key 也照旧传下去", captured.get("api_key") == "k")

    # ── T5 型号清单本身要站得住 ──
    ids = [m["id"] for m in MINIMAX_MODELS]
    check("T5.1 minimax 声明了 supports_model 与默认型号",
          MiniMaxTTS.supports_model and MiniMaxTTS.default_model == "speech-2.8-hd")
    check("T5.2 默认型号在清单里（不能声明的默认自己都不认识）",
          MiniMaxTTS.default_model in ids, str(ids))
    check("T5.3 官方文档实证的型号在清单里（2.8-hd / 2.8-turbo）",
          "speech-2.8-hd" in ids and "speech-2.8-turbo" in ids, str(ids))
    check("T5.4 每个型号都有 id/label/note（界面要能解释清楚）",
          all(m.get("id") and m.get("label") and m.get("note") for m in MINIMAX_MODELS))
    check("T5.5 M3 **不在**配音型号清单里（它是语言模型）",
          not any("m3" in i.lower() for i in ids), str(ids))

    # ── T6 不能选型号的引擎要如实说明，而不是给个假开关 ──
    check("T6.1 Edge：不能选型号，且写明了原因",
          (not EdgeTTS.supports_model) and bool(EdgeTTS.model_note), EdgeTTS.model_note)
    check("T6.2 硅基流动：模型跟着音色走，如实说明",
          (not SiliconFlowTTS.supports_model) and "音色" in SiliconFlowTTS.model_note,
          SiliconFlowTTS.model_note)
    check("T6.3 百炼：能选型号且有默认值",
          DashScopeTTS.supports_model and DashScopeTTS.default_model == "cosyvoice-v2")

    # ── T7 /api/tts/models 的载荷 ──
    provs = asyncio.run(vd.list_providers({"tts_api_keys": {"minimax": "k"},
                                           "tts_models": {"minimax": "speech-2.6-hd"}}))
    by = {x["name"]: x for x in provs}
    check("T7.1 silent 之外每个引擎都有型号字段",
          all(k in by["edge"] for k in ("supports_model", "models", "selected_model",
                                        "default_model", "model_known", "model_note")))
    check("T7.2 选中的型号透出来了",
          by["minimax"]["selected_model"] == "speech-2.6-hd",
          by["minimax"]["selected_model"])
    check("T7.3 用户选自带型号时标成 known=true",
          by["minimax"]["model_known"] is True)
    check("T7.4 没 Key 也照样能列出型号（先看再配 Key）",
          by["dashscope"]["has_api_key"] is False
          and by["dashscope"]["supports_model"] is True)

    # ── T8 M3 归位：在 LLM 目录里 ──
    llm_ids = [m["id"] for m in LLM_CATALOG["minimax"]["models"]]
    check("T8.1 MiniMax-M3 已加到语言模型列表（用户点名要它）",
          "MiniMax-M3" in llm_ids, str(llm_ids))
    check("T8.2 M3 的说明里点明了它是最新旗舰/长上下文",
          any("M3" in m["id"] and ("1M" in m.get("label", "")
                                   or "上下文" in m.get("best_for", ""))
              for m in LLM_CATALOG["minimax"]["models"]))

    # ── T9 设置字段真的存在（否则接口层会静默丢掉它）──
    flds = set(SettingsUpdate.model_fields.keys())
    check("T9.1 SettingsUpdate 有 tts_models 字段", "tts_models" in flds)
    check("T9.2 SettingsUpdate 有 tts_api_keys 字段（老断点别再回来）",
          "tts_api_keys" in flds)

    # ── T10 型号不可用 → 退回备用型号，并如实带出"实际用了哪个" ──
    #   为什么必须有这层：型号是**账号相关**的。新账号没开通 2.8 时若直接失败，
    #   上层会回退到默认音色 → 又变回用户抱怨的"全片一把 AI 女音"。
    #   但**绝不能静默换**：真实用的型号要写在 `raw.model_fallback` 里报出去。
    calls10 = []

    class _ProvModelReject:
        # ★ 备用型号清单直接用**真实适配器声明的**那份：这样"厂商声明了备用型号"
        #   这件事本身也被这条测试锁住（改坏了测试会红）。
        model_fallbacks = MiniMaxTTS.model_fallbacks

        def __init__(self):
            self.config = {}

        async def synthesize(self, req):
            calls10.append(self.config.get("model"))
            if self.config.get("model") == "speech-2.8-hd":
                return TTSResult(success=False, error="[2013] invalid model: speech-2.8-hd")
            return TTSResult(success=True, audio_path="ok",
                             raw={"model": self.config.get("model")})

        async def close(self):
            pass

    p10 = _ProvModelReject()

    def _mk(inst):
        # ★ 桩必须**把 config 真的应用上去**：被测的就是"型号有没有传下来、
        #   回退时有没有换掉它"。忽略 config 的桩会把这条链测成永真/永假。
        def _f(name, api_key="", config=None):
            inst.config = dict(config or {})
            return inst
        return _f

    orig10 = vd._base_get_provider
    vd._base_get_provider = _mk(p10)
    try:
        r10 = asyncio.run(vd.synthesize(
            TTSRequest(text="hi", voice_id="minimax:male-qn-qingse"),
            {"tts_api_keys": {"minimax": "k"},
             "tts_models": {"minimax": "speech-2.8-hd"}}))
    finally:
        vd._base_get_provider = orig10
    check("T10.1 型号被拒后合成仍然成功（不是把这句丢掉）", r10.success, str(r10.error))
    check("T10.2 实际用了备用型号", calls10[:2] == ["speech-2.8-hd", "speech-2.6-hd"],
          str(calls10))
    check("T10.3 回退被如实记录（from/to）",
          (r10.raw or {}).get("model_fallback") == {"from": "speech-2.8-hd",
                                                    "to": "speech-2.6-hd"},
          str(r10.raw))
    check("T10.4 也记下了实际使用的型号",
          (r10.raw or {}).get("model_used") == "speech-2.6-hd")

    # 反例：不是型号问题（余额/Key）就不该乱试备用型号
    calls10.clear()

    class _ProvNoBalance:
        def __init__(self):
            self.config = {}

        async def synthesize(self, req):
            calls10.append(self.config.get("model"))
            return TTSResult(success=False, error="[1008] 余额不足，请充值")

        async def close(self):
            pass

    p11 = _ProvNoBalance()
    vd._base_get_provider = _mk(p11)
    try:
        r11 = asyncio.run(vd.synthesize(
            TTSRequest(text="hi", voice_id="minimax:male-qn-qingse"),
            {"tts_api_keys": {"minimax": "k"},
             "tts_models": {"minimax": "speech-2.8-hd"}}))
    finally:
        vd._base_get_provider = orig10
    check("T10.5 余额不足不触发型号回退（不浪费第二次调用）",
          (not r11.success) and len(calls10) == 1, str(calls10))
    check("T10.6 余额不足的原始报错保留着（不被盖掉）",
          "余额不足" in str(r11.error), str(r11.error))

    print("\n" + "=" * 70)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
    for f in FAIL:
        print("  [x] " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

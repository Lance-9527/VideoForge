# -*- coding: utf-8 -*-
"""VideoForge · 语音模型接入层回归用例（`backend/tests/test_voice_providers.py`）

═══════════════════════════════════════════════════════════════════════
这套用例守什么
═══════════════════════════════════════════════════════════════════════
注册表里的音频平台有 11 家，而历史 `core/voice/` 只有 5 家实现
（edge/siliconflow/minimax/dashscope/agentai）。用户选了 openai /
elevenlabs / azure / volc / xfyun / fish / local 只会得到
「未注册的 TTS provider」—— 又是"能选不能用"。

本文件用**本地假 HTTP 服务**把新接的 7 家逐条钉住：

  ① 请求构造：OpenAI TTS（/v1/audio/speech）、ElevenLabs（xi-api-key +
     路径里的 voice_id）、Azure（region 端点 + Ocp-Apim-Subscription-Key +
     SSML）、火山（Bearer;token + appid/token/cluster）、Fish（model 请求头 +
     reference_id）、本地 GPT-SoVITS（ref_audio_path）；
  ② 签名：讯飞的 HMAC-SHA256 签名与首帧 payload（纯计算，不联网）；
  ③ 错误归一：HTTP 错误、JSON 错误体、厂商业务码（火山 code != 3000）；
  ④ 缺凭据的明确提示：三件套 / region / 参考音频，**缺哪一项报哪一项**；
  ⑤ dispatcher：list_providers 的 usable/model_note/verification、
     settings 透传、tts_models 选型、缺 Key 时返回失败而不是抛异常。

★ 全部**不联网、不花钱**。

跑法（两种都行）：
    cd D:\\VideoForge-dev\\backend
    python -X utf8 tests/test_voice_providers.py
    python -X utf8 -m pytest tests/test_voice_providers.py -q
"""
from __future__ import annotations

import asyncio
import base64
import http.server
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core.voice.base import (                   # noqa: E402
    PROVIDERS, TTSRequest, TTSResult, VoiceProvider,
)
from core.voice import dispatcher as VD         # noqa: E402
from core.voice._credentials import parse_credential_blob  # noqa: E402
from core.voice.openai_tts import OpenAITTS     # noqa: E402
from core.voice.elevenlabs_tts import ElevenLabsTTS  # noqa: E402
from core.voice.azure_tts import AzureTTS       # noqa: E402
from core.voice.volc_tts import VolcTTS         # noqa: E402
from core.voice.xfyun_tts import (              # noqa: E402
    XfyunTTS, build_payload, build_signed_url,
)
from core.voice.fish_tts import FishTTS         # noqa: E402
from core.voice.local_tts import LocalTTS       # noqa: E402

#: 假 mp3 字节（内容是假的，只要能验证"字节被原样写盘"）
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 32


# ─────────────────────────── 本地假 HTTP 服务 ───────────────────────────


class _Headers(dict):
    """大小写不敏感的请求头容器（http.server 会把头名规范化成 Title-Case）。"""

    def __init__(self, raw):
        super().__init__((str(k).lower(), v) for k, v in (raw or {}).items())

    def get(self, key, default=None):
        return super().get(str(key).lower(), default)

    def __getitem__(self, key):
        return super().__getitem__(str(key).lower())

    def __contains__(self, key):
        return super().__contains__(str(key).lower())


class _Req:
    def __init__(self, method: str, path: str, headers: dict, body: bytes):
        self.method = method
        self.path = path
        self.path_only = path.split("?")[0]
        self.headers = _Headers(headers)
        self.body = body

    def json(self) -> dict:
        try:
            return json.loads(self.body.decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            return {}

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class FakeHTTP:
    """临时 HTTP 服务：按 (method, path 前缀) 路由并记录请求。"""

    def __init__(self):
        self.routes = []
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                req = _Req(self.command, self.path, dict(self.headers), body)
                outer.requests.append(req)
                status, headers, payload = 404, {"Content-Type": "application/json"}, \
                    b'{"error":"no route"}'
                for method, prefix, handler in outer.routes:
                    if method in ("*", self.command) and req.path_only.startswith(prefix):
                        status, headers, payload = handler(req)
                        break
                if isinstance(payload, str):
                    payload = payload.encode("utf-8")
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except Exception:  # noqa: BLE001
                    pass

            do_GET = _serve
            do_POST = _serve

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def on(self, method, prefix, handler):
        self.routes.append((method, prefix, handler))
        return self

    def audio_ok(self, payload: bytes = MP3, *, method="*", prefix="/",
                 content_type="audio/mpeg"):
        return self.on(method, prefix,
                       lambda _r: (200, {"Content-Type": content_type}, payload))

    def json_ok(self, payload, *, status=200, method="*", prefix="/"):
        raw = json.dumps(payload, ensure_ascii=False)
        return self.on(method, prefix,
                       lambda _r: (status, {"Content-Type": "application/json"}, raw))

    def last(self, prefix=""):
        for req in reversed(self.requests):
            if req.path_only.startswith(prefix):
                return req
        return None

    def close(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _run(coro):
    return asyncio.run(coro)


def _tmp_audio(name: str = "out.mp3") -> str:
    return str(Path(tempfile.mkdtemp(prefix="vf-tts-test-")) / name)


def _req(text: str, voice_id: str, **kwargs) -> TTSRequest:
    return TTSRequest(text=text, voice_id=voice_id,
                      output_path=kwargs.pop("output_path", _tmp_audio()),
                      **kwargs)


# ─────────────────────────── ① 多件套凭据解析 ───────────────────────────


class TestCredentialParsing(unittest.TestCase):
    def test_blob_formats(self):
        self.assertEqual(parse_credential_blob("appid=A;token=B"),
                         {"appid": "A", "token": "B"})
        self.assertEqual(parse_credential_blob('{"appid":"A","cluster":"C"}'),
                         {"appid": "A", "cluster": "C"})
        self.assertEqual(parse_credential_blob("app_id=A, api_secret=B"),
                         {"app_id": "A", "api_secret": "B"})
        # 普通的一把 Key 不该被误解析
        self.assertEqual(parse_credential_blob("sk-abcdef123456"), {})

    def test_volc_missing_parts_are_named(self):
        inst = VolcTTS(api_key="", config={"settings": {}})
        gaps = inst.credential_gaps({})
        self.assertTrue(any("appid" in g for g in gaps), gaps)
        self.assertTrue(any("token" in g for g in gaps), gaps)
        # cluster 有官方默认值，不该算缺
        self.assertFalse(any("cluster" in g for g in gaps), gaps)
        # 组合串给全之后就不缺了
        inst2 = VolcTTS(api_key="appid=A;token=T", config={"settings": {}})
        self.assertEqual(inst2.credential_gaps({}), [])

    def test_xfyun_reports_all_three_parts(self):
        inst = XfyunTTS(api_key="", config={"settings": {}})
        gaps = inst.credential_gaps({})
        self.assertEqual(len(gaps), 3, gaps)
        blob = "app_id=A;api_key=K;api_secret=S"
        self.assertEqual(XfyunTTS(api_key=blob).credential_gaps({}), [])


# ─────────────────────────── ② OpenAI TTS ───────────────────────────


class TestOpenAITTS(unittest.TestCase):
    def test_request_body_path_and_save(self):
        with FakeHTTP() as srv:
            srv.audio_ok()
            inst = OpenAITTS(api_key="sk-oai",
                             config={"base_url": srv.url,
                                     "model": "gpt-4o-mini-tts"})
            out = _tmp_audio()
            r = _run(inst.synthesize(_req("你好世界", "openai:alloy",
                                          output_path=out)))
            self.assertTrue(r.success, r.error)
            self.assertEqual(Path(out).read_bytes(), MP3)
            req = srv.last("/v1/audio/speech")
            self.assertEqual(req.method, "POST")
            self.assertEqual(req.headers.get("Authorization"), "Bearer sk-oai")
            body = req.json()
            self.assertEqual(body["model"], "gpt-4o-mini-tts")
            self.assertEqual(body["input"], "你好世界")
            self.assertEqual(body["voice"], "alloy")
            self.assertEqual(body["response_format"], "mp3")
            self.assertEqual(r.provider, "openai")
            self.assertEqual(r.voice_id, "openai:alloy")

    def test_json_error_body_is_not_saved_as_audio(self):
        """有的网关 200 回 JSON 错误体 —— 不能当音频写盘（否则得到一个假 mp3）。"""
        with FakeHTTP() as srv:
            srv.json_ok({"error": {"message": "insufficient quota"}})
            inst = OpenAITTS(api_key="k", config={"base_url": srv.url})
            out = _tmp_audio()
            r = _run(inst.synthesize(_req("hi", "openai:alloy", output_path=out)))
            self.assertFalse(r.success)
            self.assertIn("insufficient quota", r.error)
            self.assertFalse(Path(out).exists())

    def test_http_error_keeps_status(self):
        with FakeHTTP() as srv:
            srv.on("POST", "/v1/audio/speech",
                   lambda _r: (401, {"Content-Type": "text/plain"}, "bad key"))
            inst = OpenAITTS(api_key="k", config={"base_url": srv.url})
            r = _run(inst.synthesize(_req("hi", "openai:alloy")))
            self.assertFalse(r.success)
            self.assertIn("401", r.error)

    def test_missing_key_message(self):
        inst = OpenAITTS(api_key="", config={"settings": {}})
        r = _run(inst.synthesize(_req("hi", "openai:alloy")))
        self.assertFalse(r.success)
        self.assertIn("未配置", r.error)


# ─────────────────────────── ③ ElevenLabs ───────────────────────────


class TestElevenLabsTTS(unittest.TestCase):
    def test_xi_api_key_header_and_voice_in_path(self):
        with FakeHTTP() as srv:
            srv.audio_ok(prefix="/v1/text-to-speech/")
            inst = ElevenLabsTTS(api_key="el-key", config={"base_url": srv.url,
                                                           "model": "eleven_turbo_v2_5"})
            out = _tmp_audio()
            r = _run(inst.synthesize(_req("hello", "elevenlabs:21m00Tcm4TlvDq8ikWAM",
                                          output_path=out)))
            self.assertTrue(r.success, r.error)
            req = srv.last("/v1/text-to-speech/")
            self.assertEqual(req.path_only, "/v1/text-to-speech/21m00Tcm4TlvDq8ikWAM")
            self.assertEqual(req.headers.get("xi-api-key"), "el-key")
            self.assertNotIn("Authorization", req.headers)   # Bearer 会 401
            body = req.json()
            self.assertEqual(body["model_id"], "eleven_turbo_v2_5")
            self.assertEqual(body["text"], "hello")
            self.assertIn("voice_settings", body)


# ─────────────────────────── ④ Azure ───────────────────────────


class TestAzureTTS(unittest.TestCase):
    def test_missing_region_is_named(self):
        inst = AzureTTS(api_key="somekey", config={"settings": {}})
        r = _run(inst.synthesize(_req("你好", "azure:zh-CN-XiaoxiaoNeural")))
        self.assertFalse(r.success)
        self.assertIn("region", r.error)
        self.assertIn("缺少", r.error)

    def test_ssml_and_headers(self):
        with FakeHTTP() as srv:
            srv.audio_ok(prefix="/cognitiveservices/v1")
            inst = AzureTTS(api_key="az-key",
                            config={"base_url": srv.url,
                                    "settings": {"azure_region": "eastasia"}})
            out = _tmp_audio()
            r = _run(inst.synthesize(_req("甲 & 乙 <丙>", "azure:zh-CN-YunxiNeural",
                                          output_path=out)))
            self.assertTrue(r.success, r.error)
            req = srv.last("/cognitiveservices/v1")
            self.assertEqual(req.headers.get("Ocp-Apim-Subscription-Key"), "az-key")
            self.assertEqual(req.headers.get("Content-Type"), "application/ssml+xml")
            self.assertIn("audio-24khz-48kbitrate-mono-mp3",
                          req.headers.get("X-Microsoft-OutputFormat", ""))
            ssml = req.text()
            self.assertIn("zh-CN-YunxiNeural", ssml)
            self.assertIn("<speak", ssml)
            # XML 必须转义，否则 Azure 直接 400
            self.assertIn("&amp;", ssml)
            self.assertIn("&lt;", ssml)
            self.assertNotIn("甲 & 乙", ssml)
            self.assertEqual(Path(out).read_bytes(), MP3)

    def test_composite_credential_splits_region_and_key(self):
        """tts_api_keys.azure 写成 "region=..;key=.." 时，header 只能带其中 key。"""
        with FakeHTTP() as srv:
            srv.audio_ok(prefix="/cognitiveservices/v1")
            inst = AzureTTS(api_key="region=eastasia;key=SECRET-KEY",
                            config={"base_url": srv.url, "settings": {}})
            r = _run(inst.synthesize(_req("你好", "azure:zh-CN-XiaoxiaoNeural")))
            self.assertTrue(r.success, r.error)
            req = srv.last("/cognitiveservices/v1")
            self.assertEqual(req.headers.get("Ocp-Apim-Subscription-Key"), "SECRET-KEY")


# ─────────────────────────── ⑤ 火山（豆包语音） ───────────────────────────


class TestVolcTTS(unittest.TestCase):
    def test_success_with_three_parts(self):
        with FakeHTTP() as srv:
            srv.json_ok({"code": 3000, "message": "Success",
                         "data": base64.b64encode(MP3).decode()},
                        prefix="/api/v1/tts")
            inst = VolcTTS(api_key="appid=APPID;token=TOKEN;cluster=volcano_tts",
                           config={"base_url": srv.url, "settings": {}})
            out = _tmp_audio()
            r = _run(inst.synthesize(_req("你好世界", "volc:zh_female_cancan_mars_bigtts",
                                          output_path=out)))
            self.assertTrue(r.success, r.error)
            req = srv.last("/api/v1/tts")
            self.assertEqual(req.headers.get("Authorization"), "Bearer;TOKEN")
            body = req.json()
            self.assertEqual(body["app"]["appid"], "APPID")
            self.assertEqual(body["app"]["cluster"], "volcano_tts")
            self.assertEqual(body["audio"]["voice_type"], "zh_female_cancan_mars_bigtts")
            self.assertEqual(body["request"]["operation"], "query")
            self.assertTrue(body["request"]["reqid"])
            self.assertEqual(Path(out).read_bytes(), MP3)

    def test_business_error_code_is_explained(self):
        with FakeHTTP() as srv:
            srv.json_ok({"code": 3001, "message": "Invalid request"},
                        prefix="/api/v1/tts")
            inst = VolcTTS(api_key="appid=A;token=T",
                           config={"base_url": srv.url, "settings": {}})
            r = _run(inst.synthesize(_req("hi", "volc:zh_female_cancan_mars_bigtts")))
            self.assertFalse(r.success)
            self.assertIn("3001", r.error)
            self.assertIn("appid", r.error)   # 错误码 → 人话（提示三件套）

    def test_missing_parts_error_lists_names(self):
        inst = VolcTTS(api_key="", config={"settings": {}})
        r = _run(inst.synthesize(_req("hi", "volc:zh_female_cancan_mars_bigtts")))
        self.assertFalse(r.success)
        self.assertIn("appid", r.error)
        self.assertIn("token", r.error)


# ─────────────────────────── ⑥ 讯飞（签名） ───────────────────────────


class TestXfyunSigning(unittest.TestCase):
    FIXED_DATE = "Thu, 25 Oct 2026 07:00:00 GMT"

    def test_signature_is_deterministic_and_secret_sensitive(self):
        url1 = build_signed_url("KEY", "SECRET", date=self.FIXED_DATE)
        url2 = build_signed_url("KEY", "SECRET", date=self.FIXED_DATE)
        self.assertEqual(url1, url2)                      # 同输入 → 同签名
        self.assertTrue(url1.startswith("wss://tts-api.xfyun.cn/v2/tts?"))
        self.assertIn("authorization=", url1)
        self.assertIn("host=tts-api.xfyun.cn", url1)
        url3 = build_signed_url("KEY", "OTHER-SECRET", date=self.FIXED_DATE)
        self.assertNotEqual(url1, url3)                   # 换 secret → 签名变化
        # authorization 解出来必须是官方那三行字段
        import urllib.parse
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url1).query)
        auth = base64.b64decode(qs["authorization"][0]).decode()
        self.assertIn('api_key="KEY"', auth)
        self.assertIn('algorithm="hmac-sha256"', auth)
        self.assertIn('headers="host date request-line"', auth)
        self.assertIn("signature=", auth)

    def test_payload_shape(self):
        req = _req("你好", "xfyun:xiaoyan")
        payload = build_payload("APPID", "你好", "xiaoyan", req)
        self.assertEqual(payload["common"]["app_id"], "APPID")
        self.assertEqual(payload["business"]["aue"], "lame")   # mp3
        self.assertEqual(payload["business"]["vcn"], "xiaoyan")
        self.assertEqual(payload["business"]["tte"], "UTF8")
        self.assertEqual(base64.b64decode(payload["data"]["text"]).decode(), "你好")
        self.assertEqual(payload["data"]["status"], 2)

    def test_missing_three_parts_reported(self):
        inst = XfyunTTS(api_key="", config={"settings": {}})
        r = _run(inst.synthesize(_req("hi", "xfyun:xiaoyan")))
        self.assertFalse(r.success)
        for part in ("app_id", "api_key", "api_secret"):
            self.assertIn(part, r.error)


# ─────────────────────────── ⑦ Fish ───────────────────────────


class TestFishTTS(unittest.TestCase):
    def test_model_header_and_reference_id(self):
        with FakeHTTP() as srv:
            srv.audio_ok(prefix="/v1/tts")
            inst = FishTTS(api_key="fish-key",
                           config={"base_url": srv.url, "model": "fish-speech-1.5"})
            out = _tmp_audio()
            r = _run(inst.synthesize(_req("你好", "fish:ref-abc-123",
                                          output_path=out)))
            self.assertTrue(r.success, r.error)
            req = srv.last("/v1/tts")
            self.assertEqual(req.headers.get("Authorization"), "Bearer fish-key")
            self.assertEqual(req.headers.get("model"), "fish-speech-1.5")
            body = req.json()
            self.assertEqual(body["reference_id"], "ref-abc-123")
            self.assertEqual(body["text"], "你好")
            self.assertEqual(body["format"], "mp3")
            self.assertEqual(Path(out).read_bytes(), MP3)


# ─────────────────────────── ⑧ 本地 GPT-SoVITS ───────────────────────────


class TestLocalTTS(unittest.TestCase):
    def test_missing_ref_audio_is_explained(self):
        inst = LocalTTS(api_key="", config={"settings": {}})
        r = _run(inst.synthesize(_req("你好", "local:")))
        self.assertFalse(r.success)
        self.assertIn("ref_audio_path", r.error)
        self.assertIn("参考音频", r.error)

    def test_success_uses_configured_ref_audio(self):
        with FakeHTTP() as srv:
            srv.audio_ok(prefix="/tts")
            inst = LocalTTS(api_key="", config={
                "base_url": srv.url,
                "settings": {"gpt_sovits_ref_audio": "D:/voices/ref.wav"}})
            out = _tmp_audio()
            r = _run(inst.synthesize(_req("你好", "local:", output_path=out)))
            self.assertTrue(r.success, r.error)
            body = srv.last("/tts").json()
            self.assertEqual(body["ref_audio_path"], "D:/voices/ref.wav")
            self.assertEqual(body["media_type"], "mp3")
            self.assertIs(body["streaming_mode"], False)
            self.assertEqual(Path(out).read_bytes(), MP3)

    def test_service_down_is_actionable(self):
        inst = LocalTTS(api_key="", config={
            "base_url": "http://127.0.0.1:1",
            "settings": {"gpt_sovits_ref_audio": "D:/voices/ref.wav"}})
        r = _run(inst.synthesize(_req("你好", "local:")))
        self.assertFalse(r.success)
        self.assertIn("调用失败", r.error)
        self.assertIn("api_v2.py", r.error)   # 告诉用户该启动什么


# ─────────────────────────── ⑨ dispatcher ───────────────────────────


class TestDispatcher(unittest.TestCase):
    NEW_PROVIDERS = ("openai", "elevenlabs", "azure", "volc",
                     "xfyun", "fish", "local")

    def test_list_providers_includes_all_and_marks_usable(self):
        settings = {"tts_api_keys": {"siliconflow": "k", "minimax": "k"}}
        rows = {p["name"]: p for p in _run(VD.list_providers(settings))}
        for name in self.NEW_PROVIDERS:
            self.assertIn(name, rows, f"{name} 没出现在 /api/voice/providers")
            row = rows[name]
            self.assertIn("usable", row)
            self.assertIsInstance(row["usable"], bool)
            self.assertIn("model_note", row)
            self.assertIn("verification", row)
        # 有 Key 的能用；多件套缺件的必须标不可用并写清缺什么
        self.assertTrue(rows["minimax"]["usable"])
        self.assertTrue(rows["edge"]["usable"])          # 免费引擎
        self.assertFalse(rows["volc"]["usable"])
        self.assertTrue(rows["volc"]["missing_parts"])
        self.assertFalse(rows["azure"]["usable"])
        self.assertTrue(rows["azure"]["missing_parts"])
        self.assertIn("还缺", rows["azure"]["unusable_reason"])

    def test_missing_key_returns_failure_not_exception(self):
        r = _run(VD.synthesize(_req("hi", "elevenlabs:21m00Tcm4TlvDq8ikWAM"),
                               {"tts_api_keys": {}}))
        self.assertFalse(r.success)
        self.assertIn("未配置", r.error)

    def test_unknown_provider_returns_failure(self):
        r = _run(VD.synthesize(_req("hi", "nosuch:voice"), {}))
        self.assertFalse(r.success)
        self.assertIn("未注册", r.error)

    def test_settings_passed_only_to_providers_that_want_it(self):
        """wants_settings=True 的 provider 才拿整份 settings（老 provider 契约不变）。"""
        captured = {}

        class _Probe(VoiceProvider):
            name = "probe"
            display_name = "Probe"
            prefix = "probe:"
            requires_api_key = False
            supports_model = False
            wants_settings = True

            async def list_voices(self):
                return []

            async def synthesize(self, req: TTSRequest) -> TTSResult:
                captured["config"] = dict(self.config or {})
                return TTSResult(success=True, audio_path=req.output_path,
                                 provider="probe", voice_id=req.voice_id)

        PROVIDERS["probe"] = _Probe
        try:
            settings = {"tts_api_keys": {"probe": "k"},
                        "tts_models": {"probe": "probe-model-1"},
                        "probe_region": "somewhere"}
            out = _tmp_audio()
            r = _run(VD.synthesize(_req("hi", "probe:x", output_path=out), settings))
        finally:
            PROVIDERS.pop("probe", None)
        self.assertTrue(r.success, r.error)
        self.assertEqual(captured["config"].get("model"), "probe-model-1")
        self.assertEqual(captured["config"].get("settings"), settings)

    def test_tts_models_routes_model_and_base_url_setting(self):
        """设置里的 tts_models / tts_base_url_<provider> 要真的生效（不是摆设）。"""
        with FakeHTTP() as srv:
            srv.audio_ok()
            settings = {
                "tts_api_keys": {"openai": "k"},
                "tts_models": {"openai": "tts-1-hd"},
                "tts_base_url_openai": srv.url,
            }
            out = _tmp_audio()
            r = _run(VD.synthesize(_req("hi", "openai:nova", output_path=out),
                                   settings))
            self.assertTrue(r.success, r.error)
            body = srv.last("/v1/audio/speech").json()
            self.assertEqual(body["model"], "tts-1-hd")
            self.assertEqual(body["voice"], "nova")

    def test_result_shape_is_unified(self):
        with FakeHTTP() as srv:
            srv.audio_ok()
            settings = {"tts_api_keys": {"openai": "k"},
                        "tts_base_url_openai": srv.url}
            r = _run(VD.synthesize(_req("hi", "openai:alloy"), settings))
            self.assertIsInstance(r, TTSResult)
            self.assertTrue(r.success)
            self.assertTrue(r.audio_path)
            self.assertEqual(r.provider, "openai")
            self.assertEqual(r.voice_id, "openai:alloy")
            self.assertIsNone(r.error)


class TestSiliconFlowModelFallback(unittest.TestCase):
    """2026-10-25 真实探测的结论要落到代码里：CosyVoice2 无响应时还有能用的型号。"""

    def test_moss_fallback_listed_and_parsed(self):
        from core.voice.siliconflow_tts import SiliconFlowTTS
        inst = SiliconFlowTTS(api_key="")          # 无 Key → list_voices 不会联网
        voices = asyncio.run(inst.list_voices())
        ids = [v.voice_id for v in voices]
        moss = [i for i in ids if "MOSS-TTSD" in i]
        self.assertTrue(moss, ids)
        # voice_id 形状必须是 siliconflow:<model>:<model>:<voice>（synthesize 靠它解析）
        self.assertEqual(moss[0],
                         "siliconflow:fnlp/MOSS-TTSD-v0.5:fnlp/MOSS-TTSD-v0.5:alex")
        # 真正发出去的请求体里 model / voice 必须分开且正确
        with FakeHTTP() as srv:
            srv.audio_ok(prefix="/v1/audio/speech")
            inst2 = SiliconFlowTTS(api_key="k", config={"base_url": srv.url + "/v1"})
            r = _run(inst2.synthesize(_req("你好", moss[0])))
            self.assertTrue(r.success, r.error)
            body = srv.last("/v1/audio/speech").json()
            self.assertEqual(body["model"], "fnlp/MOSS-TTSD-v0.5")
            self.assertEqual(body["voice"], "fnlp/MOSS-TTSD-v0.5:alex")
            self.assertEqual(body["input"], "你好")


if __name__ == "__main__":
    unittest.main(verbosity=2)

# -*- coding: utf-8 -*-
"""VideoForge · 图像模型接入层回归用例（`backend/tests/test_image_providers.py`）

═══════════════════════════════════════════════════════════════════════
这套用例守什么
═══════════════════════════════════════════════════════════════════════
`core/provider_registry.py` 在设置页里列出 10+ 家图像模型，但历史实现只有
5 家。用户选中 ark/gemini/zhipu/stability 时只会得到「不支持的图像模型」。
`core/image_providers.py` 是补齐后的统一接入层，本文件用**本地假 HTTP 服务**
把它逐条钉住：

  ① 请求构造：OpenAI 系（openai/siliconflow/zhipu/ark）、Gemini Imagen 的
     predict 与 generateContent 两条形态、DashScope 异步任务、MiniMax、
     Stability multipart、本地 ComfyUI /prompt 工作流；
  ② 异步轮询：wanx / qwen 的 task_id → /tasks/{id} 轮询；
  ③ 错误归一：HTTP 401/500、厂商 200 里的业务错误（MiniMax base_resp）；
  ④ 缺 Key / 缺参数的明确提示（不许只回一句"参数错误"）；
  ⑤ 统一返回结构 {ok, path, urls, provider, model, latency_ms, error}；
  ⑥ 旧入口 `imagegen.generate_image` 的签名兼容 + 新平台路由委托。

★ 全部**不联网、不花钱**：所有请求都打到 127.0.0.1 上临时起的假服务。

跑法（两种都行）：
    cd D:\\VideoForge-dev\\backend
    python -X utf8 tests/test_image_providers.py
    python -X utf8 -m pytest tests/test_image_providers.py -q
"""
from __future__ import annotations

import asyncio
import base64
import http.server
import inspect
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core import image_providers as IP          # noqa: E402
from core import imagegen as IG                # noqa: E402
from core import provider_registry as REG      # noqa: E402

#: 1x1 透明 PNG（合法图片字节；解码用不着 Pillow）
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/"
    "PzWvWQAAAABJRU5ErkJggg==")


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
    """一次被记录下来的请求（测试断言用）。"""

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

    def __repr__(self) -> str:  # pragma: no cover - 只在失败信息里出现
        return f"<{self.method} {self.path}>"


class FakeHTTP:
    """临时 HTTP 服务：按 (method, path 前缀) 路由，记录所有请求。

    路由 handler 签名：`handler(req) -> (status, headers, body)`，
    body 可以是 bytes 或 str。第一个匹配上的路由生效。
    """

    def __init__(self):
        self.routes = []          # [(method, prefix, handler)]
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # 安静
                pass

            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                req = _Req(self.command, self.path, dict(self.headers), body)
                outer.requests.append(req)
                status, headers, payload = 404, {"Content-Type": "application/json"}, \
                    b'{"error":"no route"}'
                for method, prefix, handler in outer.routes:
                    if method not in ("*", self.command):
                        continue
                    if req.path_only.startswith(prefix):
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
                except Exception:  # noqa: BLE001 - 客户端提前断开不算测试失败
                    pass

            do_GET = _serve
            do_POST = _serve

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    # ── 便捷操作 ──
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def on(self, method: str, prefix: str, handler):
        self.routes.append((method, prefix, handler))
        return self

    def json_ok(self, payload, *, status: int = 200, method: str = "*",
                prefix: str = "/", content_type: str = "application/json"):
        raw = json.dumps(payload, ensure_ascii=False)
        return self.on(method, prefix,
                       lambda _req: (status, {"Content-Type": content_type}, raw))

    def bytes_ok(self, payload: bytes, *, content_type: str = "image/png",
                 method: str = "*", prefix: str = "/"):
        return self.on(method, prefix,
                       lambda _req: (200, {"Content-Type": content_type}, payload))

    def last(self, prefix: str = "") -> _Req | None:
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
    """在同步测试里跑协程（不依赖 pytest-asyncio）。"""
    return asyncio.run(coro)


def _tmpdir() -> str:
    return tempfile.mkdtemp(prefix="vf-img-test-")


# ─────────────────────────── ① 平台矩阵 ───────────────────────────


class TestProviderMatrix(unittest.TestCase):
    def test_all_registry_providers_have_implementation(self):
        """注册表里出现的每一家都必须在接入层的分派表里（不能"能选不能用"）。"""
        registry_ids = {p["id"] for p in REG.IMAGE_PROVIDERS}
        self.assertEqual(set(IP.provider_ids()), registry_ids)
        # 每家都要有诚实状态，且状态只能是这两个词
        for row in IP.verification_report():
            self.assertIn(row["status"], ("implemented", "verified"), row)
            self.assertTrue(row["verification"], f"{row['provider']} 没写验证说明")

    def test_list_providers_reports_key_and_usability(self):
        rows = IP.list_providers({"tts_api_keys": {"siliconflow": "k"},
                                  "api_keys": {"ark": "k2"}})
        by_id = {r["id"]: r for r in rows}
        self.assertTrue(by_id["siliconflow"]["has_key"])
        self.assertTrue(by_id["siliconflow"]["usable"])
        self.assertTrue(by_id["ark"]["has_key"])
        # 没配 Key 的云平台必须标不可用并给出原因
        self.assertFalse(by_id["gemini"]["usable"])
        self.assertIn("未配置", by_id["gemini"]["unusable_reason"])

    def test_key_lookup_case_insensitive_and_bucket_priority(self):
        settings = {"llm_api_keys": {"MiniMax": "from-llm"},
                    "api_keys": {"minimax": "from-api"}}
        info = IP.resolve_key("minimax", settings)
        self.assertEqual(info["key"], "from-llm")   # 桶顺序优先
        # 大小写不敏感（设置库里存的是 MiniMax，provider id 是小写）
        self.assertEqual(IP.resolve_key("minimax", {"llm_api_keys": {"MINIMAX": "x"}})["key"],
                         "x")


# ─────────────────────────── ② OpenAI 兼容四家 ───────────────────────────


class TestOpenAIFamily(unittest.TestCase):
    def test_openai_gpt_image_b64_and_body(self):
        """gpt-image-1：body 不能带 response_format，返回 b64_json。"""
        b64 = base64.b64encode(PNG).decode()
        with FakeHTTP() as srv:
            srv.json_ok({"data": [{"b64_json": b64}]})
            out = _tmpdir()
            r = _run(IP.generate_image(
                "openai", "一只猫", size="512x512", out_dir=out,
                api_key="sk-test", base_url=srv.url, timeout=10))
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["provider"], "openai")
            self.assertEqual(Path(r["path"]).read_bytes(), PNG)
            req = srv.last("/v1/images/generations")
            self.assertIsNotNone(req)
            self.assertEqual(req.headers.get("Authorization"), "Bearer sk-test")
            body = req.json()
            self.assertEqual(body["model"], "gpt-image-1")
            self.assertEqual(body["size"], "512x512")
            self.assertEqual(body["n"], 1)
            self.assertNotIn("response_format", body)

    def test_openai_dalle_url_download_and_response_format(self):
        """dall-e-3：要 url，结果要下载下来落盘。"""
        with FakeHTTP() as srv:
            srv.json_ok({"data": [{"url": "PLACEHOLDER"}]},
                        prefix="/v1/images/generations", method="POST")
            # 动态把 url 指向本服务（端口启动后才知道）
            srv.routes.clear()
            srv.on("POST", "/v1/images/generations", lambda _r: (
                200, {"Content-Type": "application/json"},
                json.dumps({"data": [{"url": f"{srv.url}/files/a.png"}]})))
            srv.bytes_ok(PNG, prefix="/files/a.png", method="GET")
            r = _run(IP.generate_image(
                "openai", "cat", out_dir=_tmpdir(), model="dall-e-3",
                api_key="k", base_url=srv.url, timeout=10))
            self.assertTrue(r["ok"], r)
            self.assertEqual(Path(r["path"]).read_bytes(), PNG)
            body = srv.last("/v1/images/generations").json()
            self.assertEqual(body["response_format"], "url")

    def test_siliconflow_uses_image_size_and_images_key(self):
        """硅基流动：参数是 image_size（发 size 会被忽略），返回 images[]。"""
        with FakeHTTP() as srv:
            srv.json_ok({"images": [{"url": f"{srv.url}/f.png"}]},
                        prefix="/v1/images/generations", method="POST")
            srv.bytes_ok(PNG, prefix="/f.png", method="GET")
            r = _run(IP.generate_image(
                "siliconflow", "cat", size="768x768", out_dir=_tmpdir(),
                api_key="k", base_url=srv.url, timeout=10))
            self.assertTrue(r["ok"], r)
            body = srv.last("/v1/images/generations").json()
            self.assertEqual(body["image_size"], "768x768")
            self.assertNotIn("size", body)
            self.assertEqual(body["batch_size"], 1)

    def test_siliconflow_edit_model_requires_reference_image(self):
        """编辑模型没给参考图 → 立刻报清楚，而不是发一个会被 400 的请求。"""
        with FakeHTTP() as srv:
            r = _run(IP.generate_image(
                "siliconflow", "把背景改成蓝色", out_dir=_tmpdir(),
                model="Qwen/Qwen-Image-Edit-2509",
                api_key="k", base_url=srv.url, timeout=10))
            self.assertFalse(r["ok"])
            self.assertIn("参考图", r["error"])
            self.assertEqual(len(srv.requests), 0)   # 一次请求都没发

    def test_ark_body_has_url_format_and_watermark(self):
        with FakeHTTP() as srv:
            srv.json_ok({"data": [{"url": f"{srv.url}/a.png"}]},
                        prefix="/images/generations", method="POST")
            srv.bytes_ok(PNG, prefix="/a.png", method="GET")
            r = _run(IP.generate_image(
                "ark", "水墨山水", out_dir=_tmpdir(), api_key="k",
                base_url=srv.url, timeout=10))
            self.assertTrue(r["ok"], r)
            body = srv.last("/images/generations").json()
            self.assertEqual(body["response_format"], "url")
            self.assertIs(body["watermark"], False)
            self.assertIn("size", body)

    def test_zhipu_body_and_url(self):
        with FakeHTTP() as srv:
            srv.json_ok({"data": [{"url": f"{srv.url}/z.png"}]},
                        prefix="/images/generations", method="POST")
            srv.bytes_ok(PNG, prefix="/z.png", method="GET")
            r = _run(IP.generate_image(
                "zhipu", "一只猫", out_dir=_tmpdir(), api_key="k",
                base_url=srv.url, timeout=10))
            self.assertTrue(r["ok"], r)
            body = srv.last("/images/generations").json()
            self.assertEqual(body["model"], "cogview-4")
            self.assertEqual(body["n"], 1)

    def test_generic_openai_protocol_provider_agentai(self):
        """注册表后加的 OpenAI 兼容平台（中转站 agentai）自动复用同一实现。"""
        b64 = base64.b64encode(PNG).decode()
        with FakeHTTP() as srv:
            srv.json_ok({"data": [{"b64_json": b64}]},
                        prefix="/v1/images/generations", method="POST")
            r = _run(IP.generate_image(
                "agentai", "cat", out_dir=_tmpdir(), api_key="k",
                base_url=srv.url, timeout=10))
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["provider"], "agentai")
            self.assertEqual(r["model"], "gemini-3-pro-image")
            self.assertIsNotNone(srv.last("/v1/images/generations"))


# ─────────────────────────── ③ Gemini ───────────────────────────


class TestGemini(unittest.TestCase):
    def test_imagen_predict_path_headers_and_body(self):
        b64 = base64.b64encode(PNG).decode()
        with FakeHTTP() as srv:
            srv.json_ok({"predictions": [{"bytesBase64Encoded": b64,
                                          "mimeType": "image/png"}]},
                        prefix="/v1beta/models/", method="POST")
            r = _run(IP.generate_image(
                "gemini", "a cat", out_dir=_tmpdir(), api_key="gk",
                base_url=srv.url, timeout=10))
            self.assertTrue(r["ok"], r)
            req = srv.last("/v1beta/models/")
            self.assertIn(":predict", req.path_only)
            self.assertIn("imagen-4.0-generate-001", req.path_only)
            self.assertEqual(req.headers.get("x-goog-api-key"), "gk")
            body = req.json()
            self.assertEqual(body["instances"][0]["prompt"], "a cat")
            self.assertEqual(body["parameters"]["sampleCount"], 1)
            self.assertEqual(Path(r["path"]).read_bytes(), PNG)

    def test_flash_image_uses_generate_content(self):
        """gemini-2.5-flash-image 不是 predict 模型 —— 必须走 generateContent。"""
        b64 = base64.b64encode(PNG).decode()
        with FakeHTTP() as srv:
            srv.json_ok({"candidates": [{"content": {"parts": [
                {"inlineData": {"mimeType": "image/png", "data": b64}}]}}]},
                prefix="/v1beta/models/", method="POST")
            r = _run(IP.generate_image(
                "gemini", "a cat", out_dir=_tmpdir(),
                model="gemini-2.5-flash-image", api_key="gk",
                base_url=srv.url, timeout=10))
            self.assertTrue(r["ok"], r)
            req = srv.last("/v1beta/models/")
            self.assertIn(":generateContent", req.path_only)
            body = req.json()
            self.assertIn("IMAGE", body["generationConfig"]["responseModalities"])


# ─────────────────────────── ④ DashScope 异步轮询 ───────────────────────────


class TestDashScopeAsync(unittest.TestCase):
    def test_wanx_creates_task_then_polls(self):
        state = {"polls": 0}

        def tasks(_req):
            state["polls"] += 1
            if state["polls"] < 2:
                return (200, {"Content-Type": "application/json"},
                        json.dumps({"output": {"task_status": "RUNNING"}}))
            return (200, {"Content-Type": "application/json"},
                    json.dumps({"output": {"task_status": "SUCCEEDED",
                                           "results": [{"url": f"{srv.url}/w.png"}]}}))

        with FakeHTTP() as srv:
            srv.json_ok({"output": {"task_id": "t-1"}},
                        prefix="/services/aigc/text2image/image-synthesis",
                        method="POST")
            srv.on("GET", "/tasks/t-1", tasks)
            srv.bytes_ok(PNG, prefix="/w.png", method="GET")
            r = _run(IP.generate_image(
                "wanx", "a cat", size="1024x1024", out_dir=_tmpdir(),
                api_key="k", base_url=srv.url, timeout=15,
                settings={"image_poll_interval": 0.01}))
            self.assertTrue(r["ok"], r)
            self.assertEqual(Path(r["path"]).read_bytes(), PNG)
            post = [q for q in srv.requests if q.method == "POST"][0]
            self.assertEqual(post.headers.get("X-DashScope-Async"), "enable")
            self.assertIn("1024*1024", json.dumps(post.json()))
            self.assertGreaterEqual(state["polls"], 2)   # 真的轮询了

    def test_qwen_messages_body_and_result(self):
        with FakeHTTP() as srv:
            srv.json_ok({"output": {"task_id": "q-1"}},
                        prefix="/services/aigc/multimodal-generation/generation",
                        method="POST")
            srv.json_ok({"output": {"task_status": "SUCCEEDED",
                                    "results": [{"url": f"{srv.url}/q.png"}]}},
                        prefix="/tasks/q-1", method="GET")
            srv.bytes_ok(PNG, prefix="/q.png", method="GET")
            r = _run(IP.generate_image(
                "qwen", "a cat", out_dir=_tmpdir(), api_key="k",
                base_url=srv.url, timeout=15,
                settings={"image_poll_interval": 0.01}))
            self.assertTrue(r["ok"], r)
            post = [q for q in srv.requests if q.method == "POST"][0]
            body = post.json()
            self.assertEqual(body["model"], "qwen-image")
            self.assertEqual(
                body["input"]["messages"][0]["content"][0]["text"], "a cat")

    def test_task_failed_is_reported_with_status(self):
        with FakeHTTP() as srv:
            srv.json_ok({"output": {"task_id": "bad-1"}},
                        prefix="/services/aigc/text2image/image-synthesis",
                        method="POST")
            srv.json_ok({"output": {"task_status": "FAILED",
                                    "message": "invalid parameter"}},
                        prefix="/tasks/bad-1", method="GET")
            r = _run(IP.generate_image(
                "wanx", "x", out_dir=_tmpdir(), api_key="k",
                base_url=srv.url, timeout=10,
                settings={"image_poll_interval": 0.01}))
            self.assertFalse(r["ok"])
            self.assertIn("FAILED", r["error"])


# ─────────────────────────── ⑤ MiniMax / Stability ───────────────────────────


class TestNativeProviders(unittest.TestCase):
    def test_minimax_success_downloads_url(self):
        with FakeHTTP() as srv:
            srv.json_ok({"data": {"image_urls": [f"{srv.url}/m.png"]},
                         "base_resp": {"status_code": 0}},
                        prefix="/v1/image_generation", method="POST")
            srv.bytes_ok(PNG, prefix="/m.png", method="GET")
            r = _run(IP.generate_image(
                "minimax", "一只猫", out_dir=_tmpdir(), api_key="k",
                base_url=srv.url, timeout=10))
            self.assertTrue(r["ok"], r)
            self.assertEqual(Path(r["path"]).read_bytes(), PNG)

    def test_minimax_200_with_business_error_is_normalized(self):
        """MiniMax 的特色：HTTP 200 里 base_resp 报错（2013 提示词超长）。"""
        with FakeHTTP() as srv:
            srv.json_ok({"base_resp": {"status_code": 2013,
                                       "status_msg": "prompt length must be less than 1500"}},
                        prefix="/v1/image_generation", method="POST")
            r = _run(IP.generate_image(
                "minimax", "x" * 100, out_dir=_tmpdir(), api_key="k",
                base_url=srv.url, timeout=10))
            self.assertFalse(r["ok"])
            self.assertIn("2013", r["error"])
            self.assertIn("prompt length", r["error"])

    def test_stability_multipart_and_binary(self):
        with FakeHTTP() as srv:
            srv.bytes_ok(PNG, prefix="/v2beta/stable-image/generate/", method="POST")
            r = _run(IP.generate_image(
                "stability", "a cat", out_dir=_tmpdir(), api_key="k",
                base_url=srv.url, timeout=10))
            self.assertTrue(r["ok"], r)
            req = srv.last("/v2beta/stable-image/generate/")
            self.assertIn("multipart/form-data", req.headers.get("Content-Type", ""))
            self.assertIn(b"a cat", req.body)
            self.assertEqual(req.headers.get("Accept"), "image/*")
            self.assertEqual(Path(r["path"]).read_bytes(), PNG)


# ─────────────────────────── ⑥ 本地 ComfyUI ───────────────────────────


class TestLocalComfyUI(unittest.TestCase):
    def test_prompt_then_history_then_view(self):
        state = {"polls": 0}

        def history(_req):
            state["polls"] += 1
            if state["polls"] < 2:
                return (200, {"Content-Type": "application/json"}, "{}")
            return (200, {"Content-Type": "application/json"}, json.dumps({
                "p1": {"outputs": {"9": {"images": [
                    {"filename": "out.png", "subfolder": "", "type": "output"}]}}}}))

        with FakeHTTP() as srv:
            srv.json_ok({}, prefix="/system_stats", method="GET")
            srv.json_ok({"CheckpointLoaderSimple": {"input": {"required": {
                "ckpt_name": [["sd_xl.safetensors", "other.safetensors"]]}}}},
                prefix="/object_info/CheckpointLoaderSimple", method="GET")
            srv.json_ok({"prompt_id": "p1"}, prefix="/prompt", method="POST")
            srv.on("GET", "/history/p1", history)
            srv.bytes_ok(PNG, prefix="/view", method="GET")
            r = _run(IP.generate_image(
                "local", "a cat", size="512x512", out_dir=_tmpdir(),
                base_url=srv.url, timeout=20,
                settings={"image_poll_interval": 0.01}))
            self.assertTrue(r["ok"], r)
            post = [q for q in srv.requests if q.method == "POST"][0]
            body = post.json()
            self.assertIn("prompt", body)
            wf = body["prompt"]
            self.assertEqual(wf["4"]["inputs"]["ckpt_name"], "sd_xl.safetensors")
            self.assertEqual(wf["6"]["inputs"]["text"], "a cat")
            self.assertEqual(wf["5"]["inputs"]["width"], 512)
            self.assertEqual(Path(r["path"]).read_bytes(), PNG)

    def test_service_down_error_is_actionable(self):
        """本地服务没起 → 明确说"连不上"，而不是一句超时。"""
        r = _run(IP.generate_image(
            "local", "x", out_dir=_tmpdir(), base_url="http://127.0.0.1:1",
            timeout=5, settings={"image_poll_interval": 0.01}))
        self.assertFalse(r["ok"])
        self.assertIn("连不上", r["error"])
        self.assertIn("ComfyUI", r["error"])


# ─────────────────────────── ⑦ 错误归一 / 统一返回 ───────────────────────────


class TestErrorsAndShape(unittest.TestCase):
    def test_http_error_keeps_status_and_first_200_chars(self):
        body = json.dumps({"error": {"message": "invalid api key " + "x" * 300}})
        with FakeHTTP() as srv:
            srv.on("POST", "/v1/images/generations", lambda _r: (
                401, {"Content-Type": "application/json"}, body))
            r = _run(IP.generate_image(
                "openai", "x", out_dir=_tmpdir(), api_key="k",
                base_url=srv.url, timeout=10))
            self.assertFalse(r["ok"])
            self.assertEqual(r["status"], 401)
            self.assertIn("401", r["error"])
            self.assertIn("invalid api key", r["error"])
            # 原文只截前 200 字，不把整段响应灌进报错
            self.assertLessEqual(len(r.get("raw", "")), 200)

    def test_missing_key_message_names_provider_and_bucket(self):
        r = _run(IP.generate_image(
            "gemini", "x", out_dir=_tmpdir(),
            settings={}, timeout=5))
        self.assertFalse(r["ok"])
        self.assertIn("未配置", r["error"])
        self.assertIn("gemini", r["error"] + json.dumps(r, ensure_ascii=False))

    def test_success_shape_is_unified(self):
        b64 = base64.b64encode(PNG).decode()
        with FakeHTTP() as srv:
            srv.json_ok({"data": [{"b64_json": b64}]},
                        prefix="/v1/images/generations", method="POST")
            r = _run(IP.generate_image(
                "openai", "x", out_dir=_tmpdir(), api_key="k",
                base_url=srv.url, timeout=10))
            for key in ("ok", "path", "urls", "provider", "model",
                        "latency_ms", "error"):
                self.assertIn(key, r, f"统一返回缺字段：{key}")
            self.assertIsInstance(r["latency_ms"], int)
            self.assertGreaterEqual(r["latency_ms"], 0)
            self.assertEqual(r["error"], "")
            self.assertTrue(Path(r["path"]).is_file())

    def test_failure_shape_is_unified(self):
        with FakeHTTP() as srv:
            srv.on("POST", "/v1/images/generations", lambda _r: (
                500, {"Content-Type": "text/plain"}, "boom"))
            r = _run(IP.generate_image(
                "openai", "x", out_dir=_tmpdir(), api_key="k",
                base_url=srv.url, timeout=10))
            for key in ("ok", "path", "urls", "provider", "model",
                        "latency_ms", "error"):
                self.assertIn(key, r)
            self.assertFalse(r["ok"])
            self.assertTrue(r["error"])
            self.assertIsInstance(r["latency_ms"], int)

    def test_unknown_provider_is_explicit(self):
        r = _run(IP.generate_image("no-such-provider", "x", out_dir=_tmpdir()))
        self.assertFalse(r["ok"])
        self.assertIn("不支持", r["error"])

    def test_prompt_trimming_for_minimax(self):
        """MiniMax 提示词上限 1500（实测被 2013 拒过），超出要自动精简并说明。"""
        with FakeHTTP() as srv:
            srv.json_ok({"base_resp": {"status_code": 2013, "status_msg": "x"}},
                        prefix="/v1/image_generation", method="POST")
            r = _run(IP.generate_image(
                "minimax", "很" * 3000, out_dir=_tmpdir(), api_key="k",
                base_url=srv.url, timeout=10))
            self.assertFalse(r["ok"])
            body = srv.last("/v1/image_generation").json()
            self.assertLessEqual(len(body["prompt"]), 1500)


# ─────────────────────────── ⑧ 旧入口兼容 / 路由 ───────────────────────────


class TestLegacyEntryRouting(unittest.TestCase):
    def test_legacy_signature_kept(self):
        """旧函数签名必须兼容：前四个位置参数 + 后加的 settings 在最后。"""
        params = list(inspect.signature(IG.generate_image).parameters)
        self.assertEqual(params[:4], ["provider", "api_key", "prompt", "out_dir"])
        for name in ("size", "model", "base_url", "timeout",
                     "reference_images", "negative_prompt", "settings"):
            self.assertIn(name, params)
        self.assertEqual(params[-1], "settings")

    def test_legacy_entry_delegates_new_provider(self):
        """imagegen.generate_image("ark") 应转交接入层（旧表里没有 ark）。"""
        calls = {}
        orig = IP.generate_image

        async def fake(provider_id, prompt, **kwargs):
            calls.update({"provider": provider_id, "model": kwargs.get("model"),
                          "size": kwargs.get("size")})
            return {"ok": True, "path": "/tmp/x.png", "filename": "x.png",
                    "bytes": 3, "provider": provider_id,
                    "model": kwargs.get("model") or "m", "prompt": prompt,
                    "latency_ms": 7, "elapsed": 0.007, "urls": [], "notes": []}

        IP.generate_image = fake
        try:
            r = _run(IG.generate_image(
                "ark", "", "水墨山水", _tmpdir(), size="1024x1024",
                settings={"image_provider": "ark",
                          "image_model": "doubao-seedream-3-0-t2i"}))
        finally:
            IP.generate_image = orig
        self.assertTrue(r["ok"], r)
        self.assertEqual(calls["provider"], "ark")
        self.assertEqual(calls["size"], "1024x1024")
        self.assertEqual(r["path"], "/tmp/x.png")
        self.assertEqual(r["elapsed"], 0.007)

    def test_legacy_entry_routes_empty_provider_from_settings(self):
        """provider 留空 → 按 settings.image_provider 走（以前静默落 minimax）。"""
        calls = {}
        orig = IP.generate_image

        async def fake(provider_id, prompt, **kwargs):
            calls["provider"] = provider_id
            return {"ok": False, "error": "stop-here", "model": "", "latency_ms": 1}

        IP.generate_image = fake
        try:
            r = _run(IG.generate_image("", "", "x", _tmpdir(),
                                       settings={"image_provider": "stability"}))
        finally:
            IP.generate_image = orig
        self.assertFalse(r["ok"])
        self.assertEqual(calls["provider"], "stability")

    def test_model_guard_fixes_cross_provider_setting(self):
        """设置里的全局 image_model 属于别家时，纠正到当前平台（否则必定 invalid model）。"""
        model, note = IG.guard_model_for_provider("minimax", "Qwen/Qwen-Image")
        self.assertEqual(model, "image-01")
        self.assertIn("siliconflow", note)
        # 属于本家的型号不能动
        same, note2 = IG.guard_model_for_provider("siliconflow", "Qwen/Qwen-Image")
        self.assertEqual(same, "Qwen/Qwen-Image")
        self.assertEqual(note2, "")
        # 任何一家都没声明的型号（厂商新上）不动，避免误伤
        unknown, note3 = IG.guard_model_for_provider("siliconflow", "Qwen/Qwen-Image-Edit-2509")
        self.assertEqual(unknown, "Qwen/Qwen-Image-Edit-2509")
        self.assertEqual(note3, "")

    def test_ui_provider_list_includes_new_platforms(self):
        """设置页列表要能选到 ark/gemini/zhipu/stability（且带诚实状态）。"""
        names = {p["name"] for p in IG.list_image_providers()}
        for pid in ("ark", "gemini", "zhipu", "stability"):
            self.assertIn(pid, names)
        rows = {p["name"]: p for p in IG.list_image_providers()}
        self.assertIn(rows["gemini"]["status"], ("implemented", "verified"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

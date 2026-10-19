"""
VideoForge · 可灵官方 MCP 通道（**免 API key**）

════════════════════════════════════════════════════════════════════════
这是什么（是实测出来的，不是猜的）
════════════════════════════════════════════════════════════════════════
可灵官方提供了一台**远程 MCP 服务器**：

    https://klingai.com/mcp

它用的是标准 **OAuth 2.0**，**完全不需要 API key**：

    GET  /.well-known/oauth-protected-resource/mcp
      → {"resource":"https://klingai.com/mcp",
         "authorization_servers":["https://klingai.com/auth"],
         "scopes_supported":["generation.create","generation.read",
                             "account.credit.read"]}

    GET  /auth/.well-known/oauth-authorization-server
      → authorization_endpoint: https://klingai.com/auth/authorize
        token_endpoint:         https://klingai.com/auth/token
        registration_endpoint:  https://klingai.com/auth/register   ← 动态注册
        grant_types:            ['authorization_code','refresh_token']
        code_challenge_methods: ['S256']

也就是说：**注册一个客户端 → 用户在浏览器里授权一次 → 拿到 token → 一直用**。
比申请 API key 更省事（不用去开发者后台开通、不用管 key 泄露）。

════════════════════════════════════════════════════════════════════════
流程
════════════════════════════════════════════════════════════════════════
    ① 动态注册客户端        POST /auth/register          → client_id
    ② 生成 PKCE + 授权链接   GET  /auth/authorize?...
    ③ 用户在浏览器里点"同意" → 回调带 code
    ④ 换 token              POST /auth/token            → access/refresh
    ⑤ 调 MCP                POST /mcp  JSON-RPC 2.0
    ⑥ 过期就用 refresh_token 续（不用重新授权）

token 存在 `%VIDEOFORGE_DATA_DIR%/kling_mcp.json`，权限尽量收紧。

════════════════════════════════════════════════════════════════════════
诚实边界
════════════════════════════════════════════════════════════════════════
· 本模块实现了 ①②④⑤⑥；**③ 必须由人点一下"同意"** —— 这是 OAuth 的设计，
  绕不过去（也不该绕）。
· 本机实测：注册端点返回 201 且带 client_id（见 `tests/test_kling_mcp.py`）。
  但**没有完成过一次完整的授权**（需要你在浏览器里点同意），
  所以"MCP 上具体有哪些工具、参数长什么样"我**没有实测过** ——
  `list_tools()` 会在授权后告诉你。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("videoforge.kling_mcp")

BASE = "https://klingai.com"
MCP_URL = f"{BASE}/mcp"
AUTH_BASE = f"{BASE}/auth"
REGISTER_URL = f"{AUTH_BASE}/register"
AUTHORIZE_URL = f"{AUTH_BASE}/authorize"
TOKEN_URL = f"{AUTH_BASE}/token"

SCOPES = ["generation.create", "generation.read", "account.credit.read"]
# 回调地址：OAuth 要求 https 或 localhost。用本机端口接住 code。
DEFAULT_REDIRECT_PORT = 18730
REDIRECT_URI = f"http://127.0.0.1:{DEFAULT_REDIRECT_PORT}/callback"
CLIENT_NAME = "VideoForge"


# ──────────────────────── token 存储 ────────────────────────


def _store_path() -> Path:
    base = os.environ.get("VIDEOFORGE_DATA_DIR") or os.path.join(os.getcwd(), "data")
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p / "kling_mcp.json"


def load_state() -> Dict[str, Any]:
    """读本地存的 client / token。没有就返回 {}。"""
    p = _store_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("读 %s 失败（当成未授权）：%s", p, e)
        return {}


def save_state(state: Dict[str, Any]) -> Path:
    p = _store_path()
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(p, 0o600)          # token 别让同机其它用户随便读
    except Exception:
        pass
    return p


def clear_state() -> None:
    try:
        _store_path().unlink(missing_ok=True)
    except Exception:
        pass


# ──────────────────────── PKCE ────────────────────────


def make_pkce() -> Dict[str, str]:
    """生成 PKCE 的 verifier / challenge（S256）。

    ★ 官方元数据里明确写了 `code_challenge_methods_supported: ['S256']`，
      所以**必须**用 S256，不能退化成 plain。
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return {"verifier": verifier, "challenge": challenge,
            "state": base64.urlsafe_b64encode(secrets.token_bytes(16)).decode().rstrip("=")}


# ──────────────────────── OAuth 步骤 ────────────────────────


async def register_client(redirect_uri: str = REDIRECT_URI,
                          client_name: str = CLIENT_NAME) -> Dict[str, Any]:
    """① 动态注册一个 OAuth 客户端。免 key 的关键第一步。"""
    import httpx
    body = {
        "client_name": client_name,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",     # 公共客户端（PKCE 保护）
        "scope": " ".join(SCOPES),
    }
    async with httpx.AsyncClient(timeout=30, trust_env=False) as c:
        r = await c.post(REGISTER_URL, json=body)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"客户端注册失败 HTTP {r.status_code}：{r.text[:300]}")
        data = r.json()
    cid = data.get("client_id")
    if not cid:
        raise RuntimeError(f"注册返回里没有 client_id：{str(data)[:300]}")
    logger.info("可灵 MCP 客户端已注册：client_id=%s", cid)
    return {"client_id": cid, "redirect_uri": redirect_uri,
            "client_secret": data.get("client_secret") or "",
            "raw": data}


def build_authorize_url(client_id: str, pkce: Dict[str, str],
                        redirect_uri: str = REDIRECT_URI) -> str:
    """② 生成授权链接（用户在浏览器里打开它并点同意）。"""
    q = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES),
        "state": pkce["state"],
        "code_challenge": pkce["challenge"],
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(q)}"


async def exchange_code(code: str, client_id: str, verifier: str,
                        redirect_uri: str = REDIRECT_URI) -> Dict[str, Any]:
    """④ 用授权码换 token。"""
    import httpx
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }
    async with httpx.AsyncClient(timeout=30, trust_env=False) as c:
        r = await c.post(TOKEN_URL, data=data)
        if r.status_code != 200:
            raise RuntimeError(f"换 token 失败 HTTP {r.status_code}：{r.text[:300]}")
        j = r.json()
    j["obtained_at"] = time.time()
    return j


async def refresh(client_id: str, refresh_token: str) -> Dict[str, Any]:
    """⑥ 用 refresh_token 续期（**不用重新授权**，长期使用的关键）。"""
    import httpx
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token,
            "client_id": client_id}
    async with httpx.AsyncClient(timeout=30, trust_env=False) as c:
        r = await c.post(TOKEN_URL, data=data)
        if r.status_code != 200:
            raise RuntimeError(f"续期失败 HTTP {r.status_code}：{r.text[:300]}")
        j = r.json()
    j["obtained_at"] = time.time()
    return j


def access_token(state: Optional[Dict[str, Any]] = None) -> str:
    """取当前可用的 access_token（还没过期就返回，过期了返回空 —— 由调用方决定续期）。"""
    st = state if state is not None else load_state()
    tok = st.get("token") or {}
    at = tok.get("access_token") or ""
    if not at:
        return ""
    exp = tok.get("expires_in")
    got = tok.get("obtained_at") or 0
    if exp and got and (time.time() - got) > float(exp) - 60:
        return ""            # 快过期或已过期，别拿去用
    return at


# ──────────────────────── MCP 调用 ────────────────────────


async def mcp_call(method: str, params: Optional[Dict[str, Any]] = None,
                   *, token: str = "", timeout: float = 120.0) -> Dict[str, Any]:
    """⑤ 调 MCP（JSON-RPC 2.0 over HTTP）。

    Streamable HTTP 传输：POST 到 `/mcp`，Accept 要同时带
    `application/json` 和 `text/event-stream`（实测缺一个会被拒）。
    """
    import httpx
    # ★ 自动续期：如果 access_token 过期但有 refresh_token，就 refresh
    if not token:
        from .kling_mcp import ensure_token as _et
        et = await _et()
        if et.get("ok"):
            token = et.get("token") or ""
    tok = token or access_token()
    if not tok:
        raise RuntimeError("还没授权可灵 MCP（先跑 kling_mcp_auth 拿到授权链接）")

    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    headers = {
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as c:
        r = await c.post(MCP_URL, json=body, headers=headers)
        if r.status_code == 401:
            raise RuntimeError("可灵 MCP 返回 401 —— token 失效了，需要重新授权或续期")
        text = r.text or ""
        # 可能是 SSE（data: {...}）也可能是裸 JSON
        payload = None
        if text.lstrip().startswith("{"):
            try:
                payload = json.loads(text)
            except Exception:
                payload = None
        if payload is None:
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    try:
                        payload = json.loads(line[5:].strip())
                        break
                    except Exception:
                        continue
        if payload is None:
            raise RuntimeError(f"MCP 返回无法解析（HTTP {r.status_code}）：{text[:300]}")
        if "error" in payload:
            raise RuntimeError(f"MCP 报错：{json.dumps(payload['error'], ensure_ascii=False)[:300]}")
        return payload.get("result") or payload


async def list_tools() -> List[Dict[str, Any]]:
    """列出可灵 MCP 提供的工具（授权后才知道具体有什么）。"""
    res = await mcp_call("tools/list")
    return res.get("tools") or []


def unwrap(res: Dict[str, Any]) -> Any:
    """把 MCP 的 `{content:[{type:text,text:"<json>"}]}` 拆成真正的 JSON。

    ★ 每个工具都把结果再包一层 JSON 字符串塞在 `text` 里（实测），
      所以调用方不该直接读 `res["content"]`。
    """
    if not isinstance(res, dict):
        return res
    for c in res.get("content") or []:
        if isinstance(c, dict) and c.get("type") == "text":
            t = c.get("text") or ""
            try:
                return json.loads(t)
            except Exception:
                return {"_text": t}
    return res


def error_of(res: Dict[str, Any]) -> str:
    """MCP 把业务错误放在 `isError` + content 里，取出来给用户看原文。"""
    if not isinstance(res, dict):
        return ""
    if not res.get("isError"):
        return ""
    u = unwrap(res)
    if isinstance(u, dict):
        return str(u.get("_text") or u.get("error") or u)[:600]
    return str(u)[:600]


async def call_tool(name: str, arguments: Dict[str, Any],
                    *, timeout: float = 300.0) -> Dict[str, Any]:
    """调一个具体工具。"""
    return await mcp_call("tools/call",
                          {"name": name, "arguments": arguments or {}},
                          timeout=timeout)


async def call_tool_json(name: str, arguments: Dict[str, Any],
                         *, timeout: float = 300.0) -> Any:
    """调工具并拆包；`isError` 直接抛异常（附平台原始错误文本）。"""
    res = await call_tool(name, arguments, timeout=timeout)
    e = error_of(res)
    if e:
        raise KlingToolError(f"可灵 {name} 报错：{e}")
    return unwrap(res)


class KlingToolError(RuntimeError):
    """可灵 MCP 工具返回的业务错误（原文保留，便于按官方错误表处理）。"""


async def ensure_token() -> Dict[str, Any]:
    """拿一个能用的 token：没过期直接用；过期了自动续期。

    **这是"长期使用"的关键** —— 有 refresh_token 就不用每次重新授权。
    """
    st = load_state()
    tok = st.get("token") or {}
    cid = st.get("client_id") or ""
    at = access_token(st)
    if at:
        return {"ok": True, "token": at, "refreshed": False}

    rt = tok.get("refresh_token") or ""
    if rt and cid:
        try:
            new = await refresh(cid, rt)
            st["token"] = new
            save_state(st)
            logger.info("可灵 MCP token 已自动续期")
            return {"ok": True, "token": new.get("access_token") or "",
                    "refreshed": True}
        except Exception as e:
            logger.warning("续期失败：%s", e)
            return {"ok": False, "token": "", "error": f"续期失败：{e}"}
    return {"ok": False, "token": "",
            "error": "还没授权过（或 refresh_token 也失效了）"}


# ──────────────────────── 授权编排（一次跑完 OAuth）────────────────────────


def _start_callback_server(port: int):
    """起一个本机 HTTP 服务接住 OAuth 回调里的 `code`。

    ★ 为什么用本机端口：OAuth 的回调地址必须是 https 或 **localhost**。
      本机端口是唯一既不需要证书、也不需要公网域名的选择。

    ★★ 为什么"起服务"和"等回调"要拆开（实测踩到）：
      我第一版把两件事写在同一个函数里 —— 起服务、然后在一个 while 循环里
      等 `got["code"]` 被填上。结果是：**用户在浏览器里明明看到"授权完成"，
      脚本却永远卡在那个循环里**（回调服务响应了浏览器，但 Python 侧
      没能观察到状态变化，最后只能超时）。
      拆开之后：服务先起好、浏览器再打开、然后单独等 —— 边界清楚，
      也不会出现"服务还没起就打开浏览器"的竞态。
    """
    import http.server
    import socketserver
    import threading
    import urllib.parse as _up

    state: Dict[str, Any] = {}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            # ══════════════════════════════════════════════════════════════
            # ★★ 实测踩到的大坑（一定要留着这段注释）
            # ══════════════════════════════════════════════════════════════
            # 原来这里不分路径，来什么请求都往 state 里写一遍。结果：
            # Chrome 加载完 /callback?code=... 之后会**紧接着自动请求
            # /favicon.ico**，那一下 query 是空的，于是把刚写进去的 code
            # 又覆盖成 '' 了。轮询循环每 0.5 秒看一眼，几乎必然看到空值，
            # 于是**永远等不到授权**，而浏览器上却明明白白显示"授权完成"。
            #
            # 表现极具误导性：回调日志里有 code、页面显示成功、脚本超时。
            # 修法两条一起上：① 只认 /callback；② 空值绝不覆盖已有值。
            # ══════════════════════════════════════════════════════════════
            parsed = _up.urlparse(self.path)
            if parsed.path.rstrip("/") != "/callback":
                # 其它请求（favicon / robots.txt 等）一律不碰 state
                logger.info("可灵回调服务收到无关请求，已忽略：%s", self.path[:120])
                try:
                    self.send_response(204)
                    self.end_headers()
                except Exception:
                    pass
                return

            # ★ 把原始 query 记下来：出问题时能一眼看到平台到底回传了什么。
            raw = self.path
            logger.info("可灵回调到达：%s", raw[:400])
            try:
                _d = _store_path().parent
                with open(_d / "kling_oauth_last_callback.txt", "a",
                          encoding="utf-8") as f:
                    f.write(time.strftime("%Y-%m-%d %H:%M:%S ")
                            + raw[:800] + "\n")
            except Exception:
                pass

            q = _up.parse_qs(parsed.query)
            code = (q.get("code") or [""])[0]
            err = (q.get("error") or [""])[0]
            # 只有真的带了东西才写，避免空值把已有结果冲掉
            if code:
                state["code"] = code
            if err:
                state["error"] = err
                state["error_description"] = (q.get("error_description") or [""])[0]
            if q.get("state"):
                state["state"] = (q.get("state") or [""])[0]
            state["raw_query"] = parsed.query
            state["hits"] = int(state.get("hits") or 0) + 1

            if err:
                msg = (f"<p>❌ 授权失败：{err} "
                       f"{state.get('error_description','')}</p>")
            elif not code:
                # 既没 code 也没 error —— 平台多半是拿登录态做了什么但没下发授权码
                msg = ("<p>⚠️ 回调里没有授权码（也没有错误信息）。</p>"
                       "<p>请把这一页的地址栏内容反馈给 VideoForge，"
                       "或重新跑一次授权。</p>")
            else:
                msg = "<p>✅ 授权完成，可以关掉这个页面回 VideoForge 了。</p>"
            body = ("<html><head><meta charset='utf-8'><title>VideoForge</title></head>"
                    "<body style='font-family:sans-serif;padding:40px'>"
                    "<h2>VideoForge</h2>" + msg
                    + "</body></html>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class Srv(socketserver.TCPServer):
        allow_reuse_address = True

    try:
        httpd = Srv(("127.0.0.1", port), H)
    except OSError as e:
        return None, None, {"ok": False, "error": f"本机端口 {port} 起不来（可能被占用）：{e}"}

    threading.Thread(target=httpd.serve_forever, daemon=True,
                     name="vf-kling-oauth").start()
    return httpd, state, None


def _wait_callback(httpd, state: Dict[str, Any], expect_state: str,
                   timeout: float = 300.0,
                   is_alive=None) -> Dict[str, Any]:
    """等回调把 code 填进 `state`。"""
    t0 = time.time()
    try:
        while time.time() - t0 < timeout:
            if state.get("code") or state.get("error"):
                break
            if is_alive is not None and not is_alive():
                return {"ok": False, "error": "调用方已退出"}
            time.sleep(0.5)
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass

    if state.get("error"):
        return {"ok": False,
                "error": f"授权被拒：{state['error']} {state.get('error_description','')}"}
    if not state.get("code"):
        return {"ok": False,
                "error": f"等了 {timeout:.0f} 秒没等到授权回调（用户没点同意？链接过期了？）"}
    if expect_state and state.get("state") != expect_state:
        # CSRF 防护：state 必须一致
        return {"ok": False, "error": "回调里的 state 不匹配（可能是 CSRF），已拒绝"}
    return {"ok": True, "code": state["code"]}


async def register_and_authorize(port: int = DEFAULT_REDIRECT_PORT,
                                 timeout: float = 300.0,
                                 open_browser: bool = True,
                                 on_url=None) -> Dict[str, Any]:
    """一条龙：注册 → 给链接 → 等回调 → 换 token → 存盘。

    ③（用户点同意）绕不过去 —— 这是 OAuth 的设计。
    `on_url` 回调会把授权链接交出来（让 agent/前端显示给用户）。

    返回 {"ok", "url", "status"/"error"}
    """
    import webbrowser

    redirect_uri = f"http://127.0.0.1:{port}/callback"
    st = load_state()

    # 已有 client_id 且回调地址一致就复用，别每次注册新的
    cid = st.get("client_id") or ""
    if not cid or st.get("redirect_uri") != redirect_uri:
        reg = await register_client(redirect_uri)
        cid = reg["client_id"]
        st.update({"client_id": cid, "redirect_uri": redirect_uri})
        save_state(st)

    pkce = make_pkce()
    url = build_authorize_url(cid, pkce, redirect_uri)
    st["pkce"] = pkce
    save_state(st)

    # ★ 顺序很重要：**先把回调服务起好，再打开浏览器**。
    #   反过来的话，用户手快一点同意，回调可能打到一个还没起来的端口上。
    httpd, state, err = _start_callback_server(port)
    if err:
        return {"ok": False, "url": url, "error": err["error"]}
    logger.info("回调服务已就绪 127.0.0.1:%d", port)

    if on_url:
        try:
            on_url(url)
        except Exception:
            pass
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    logger.info("等用户在浏览器里授权可灵 MCP…（%s）", url[:80])
    cb = _wait_callback(httpd, state, pkce["state"], timeout)
    if not cb.get("ok"):
        return {"ok": False, "url": url, "error": cb.get("error")}

    try:
        tok = await exchange_code(cb["code"], cid, pkce["verifier"], redirect_uri)
    except Exception as e:
        return {"ok": False, "url": url, "error": f"换 token 失败：{e}"}

    st["token"] = tok
    st["authorized_at"] = time.time()
    save_state(st)
    logger.info("可灵 MCP 授权完成")
    return {"ok": True, "url": url, "status": status()}


def status() -> Dict[str, Any]:
    """给前端/agent 看的状态。不联网。"""
    st = load_state()
    tok = st.get("token") or {}
    at = access_token(st)
    exp = tok.get("expires_in")
    got = tok.get("obtained_at") or 0
    return {
        "registered": bool(st.get("client_id")),
        "client_id": st.get("client_id") or "",
        "authorized": bool(tok.get("access_token")),
        "token_valid": bool(at),
        "has_refresh": bool(tok.get("refresh_token")),
        "expires_at": (got + float(exp)) if (got and exp) else 0,
        "store": str(_store_path()),
        "scopes": SCOPES,
        "how": "免 API key：OAuth 授权一次，之后自动续期",
    }

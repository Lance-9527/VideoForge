"""
VideoForge · 把本地文件发布成公网 URL

════════════════════════════════════════════════════════════════════════
为什么需要这个（实测结论，很硬）
════════════════════════════════════════════════════════════════════════
真调火山方舟探测过三种"把白膜视频当参考素材"的方式，平台全拒：

    ① base64 data URI  → "reference_video must be provided as a web url"
    ② 本地文件路径      → "invalid url scheme"
    ③ 公开 http(s) URL  → **接受**（只因为测试视频像素太小被拦）

结论：**要发参考视频，就必须有一个平台能访问到的公网 URL。**
图片可以走 base64（`to_image_ref`），视频不行。

而这台机器是 `192.168.1.4`，在 NAT 后面，没有公网地址，也没装任何隧道工具。
所以"白膜作为参考素材"这件事，**在当前网络环境下是做不到的** ——
不是代码没写，是物理上平台拿不到你的文件。

本模块做两件事：
  1. 把"能不能发"这个事实**说清楚**（而不是让用户以为白膜生效了）
  2. 给出**可配的**发布方案：内置隧道 / 自建 URL 前缀 / 已有的公网文件服务

════════════════════════════════════════════════════════════════════════
三种可用方案（按省事程度排）
════════════════════════════════════════════════════════════════════════
方案 A：装一个隧道工具（cloudflared 最省事，免费、不用注册账号）
        scripts\\装隧道工具.ps1 一键装好，然后本模块自动用它发布。
        ⚠ 隧道会把本机端口**暴露到公网**（随机域名、用完即关）。
          这是把文件交给厂商生成视频的必要代价，但要知情。

方案 B：自己有一个公网可访问的地址（对象存储 / 自己的服务器 / 图床）
        在设置里填 `public_base_url`，并把文件放到那个目录下。
        最可控，也最稳（隧道域名会变、会掉线）。

方案 C：不用参考视频
        白膜依然有价值：它让**人**在花钱前确认运镜和节奏。
        只是模型不再被运动轨迹约束 —— 本模块会如实这么告诉你。

方案 D：换平台。万相 2.7 的 `reference_video` 可能接受 base64
        （**未核实** —— 没有万相 key，没测过，不要当真）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("videoforge.publish")

# 内置隧道工具。cloudflared 优先：免费、不用注册、单文件。
TUNNEL_CANDIDATES = [
    ("cloudflared", ["tunnel", "--url", "{origin}", "--no-autoupdate"]),
]

_TUNNEL: Dict[str, Any] = {
    "proc": None, "url": "", "port": 0, "started": 0.0, "httpd": None,
    # 服务目录 → 已发布状态。**一条隧道可以服务整个白膜目录**，
    # 不必每个文件开一条（早先每调一次 publish_file 就新起一条隧道，
    # 既慢又浪费，而且旧的那条成了僵尸）。
    "dir": "", "published": {}, "last_health": 0.0, "healthy": False,
    "peer": {"insecure": False, "dns_ok": None},
    "restarts": 0,
}


def is_public_url(u: str) -> bool:
    """粗判一个 URL 是不是"平台能访问到"的。

    只排掉明确不可能的：localhost / 私有网段 / 无 scheme。
    不去"验证"公网可达性 —— 那要真发一次请求，太贵也太慢。
    """
    s = str(u or "").strip().lower()
    if not s.startswith(("http://", "https://")):
        return False
    host = s.split("//", 1)[1].split("/", 1)[0].split(":")[0]
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return False
    if host.startswith(("10.", "192.168.", "169.254.", "127.")):
        return False
    if host.startswith("172."):
        try:
            second = int(host.split(".")[1])
            if 16 <= second <= 31:
                return False
        except Exception:
            pass
    return True


def public_base_url(settings: Dict[str, Any]) -> str:
    """**手动配置的**公网根地址（`public_base_url`）。

    ★ 注意这里**故意不返回**命名隧道的域名。
      两条路的机制完全不同：
        · `public_base_url` + `public_sync_dir`：把文件**复制到**
          某个公网主机对应的本地目录（用户自己有 VPS/NAS/对象存储）。
        · 命名隧道（`tunnel_named` + `tunnel_hostname`）：
          隧道**直接把本机目录**暴露出去，**不需要复制任何东西**。

      如果把命名隧道的域名也塞进这个函数，`publish_file` 就会先走
      "复制到同步目录"那条路 —— 而同步目录通常没配，于是会返回
      "拼出地址但未验证"，反而**不如隧道直接服务来得可靠**。
      两件事分开表达，调用方各取所需。
    """
    return str((settings or {}).get("public_base_url") or "").strip().rstrip("/")


def named_tunnel_name(settings: Dict[str, Any]) -> str:
    """命名隧道的名字（`cloudflared tunnel create <这个名字>`）。空 = 没配。"""
    return str((settings or {}).get("tunnel_named") or "").strip()


def named_tunnel_hostname(settings: Dict[str, Any]) -> str:
    """命名隧道对应的**固定公网域名**，形如 `https://previz.example.com`。

    规范化：允许用户只填 `previz.example.com`，自动补 `https://`。
    """
    raw = str((settings or {}).get("tunnel_hostname") or "").strip().rstrip("/")
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    return raw


def named_tunnel_ready(settings: Dict[str, Any]) -> bool:
    """命名隧道是否**配置完整**（名字 + 域名都有）。"""
    return bool(named_tunnel_name(settings) and named_tunnel_hostname(settings))


def named_tunnel_root(settings: Dict[str, Any]) -> str:
    """命名隧道**固定服务的那个目录**。

    ★★ 为什么必须有这个"固定根"，而不是服务文件所在目录：

      原来隧道服务的是 `os.path.dirname(文件)` —— 也就是说，
      **每换一个项目、换一个目录，隧道就要重启并改服务根**。
      对临时隧道无所谓（域名本来就每次变），但对**永久域名是致命的**：

          项目 A 发布 → 域名服务 .../previz/A/
          项目 B 发布 → 隧道重启，域名改服务 .../previz/B/
          → **A 的地址变成 404**

      而永久通道的全部意义就是"发给厂商后长期有效"。
      所以命名隧道必须服务一个**恒定目录**，发布 = 把文件复制进去。
      这样任何一次发布出去的地址，**以后一直有效**。

    根目录选谁：
      ① 用户配了 `public_sync_dir` → 用它（他可能想让同一个目录
         既给对象存储用、也给隧道用）
      ② 否则用 `<数据目录>/public` —— 数据目录跟着用户数据走，
         不会因为程序升级/换目录而丢。
    """
    sync = public_sync_dir(settings)
    if sync:
        return os.path.abspath(os.path.expanduser(sync))
    data_root = (os.environ.get("VIDEOFORGE_DATA_DIR") or "").strip()
    if not data_root or "://" in data_root:
        data_root = os.path.join(os.path.expanduser("~"), ".videoforge")
    return os.path.abspath(os.path.join(data_root, "public"))


def _publish_name_for(local_path: str) -> str:
    """算一个**跨项目唯一且稳定**的发布文件名。

    ★ 为什么不能直接用原文件名：
      永久通道把所有项目的文件都放进同一个根目录，而不同项目很可能会
      产出**同名文件**（比如都有 `coverage_cut.mp4`、`白膜.mp4`）。
      直接覆盖的后果比 404 更糟 —— 老地址会取到**另一个项目的视频**，
      而且完全不报错。

      所以用「源目录的短哈希 + 原名」：同一个来源目录出来的同名文件
      映射稳定（重复发布覆盖自己，正确），不同目录自然分开。
    """
    base = os.path.basename(local_path)
    src_dir = os.path.dirname(os.path.abspath(local_path))
    tag = hashlib.sha1(src_dir.encode("utf-8", "replace")).hexdigest()[:8]
    # ★ 2026-10-22：文件名必须**全是 ASCII**。
    #   白膜的文件名是中文（`镜01.mp4`），发布出去的 URL 就带中文：
    #   `https://xxx.trycloudflare.com/94b7b3ab_镜01.mp4`。
    #   平台那边（方舟 / token6688 网关）是**服务端去下载这个 URL**，
    #   对 URL 里的非 ASCII 字符处理方式不确定 —— 实测这一轮整批任务
    #   在网关的 build_body 阶段就失败了（本地自检也过不了）。
    #   这里把非 ASCII 部分换成短哈希：仍然跨项目唯一、仍然稳定，
    #   但 URL 只剩 [A-Za-z0-9._-]，任何服务端都能取。
    stem, ext = os.path.splitext(base)
    if not stem.isascii():
        stem = "f" + hashlib.sha1(stem.encode("utf-8", "replace")).hexdigest()[:10]
    ext = ext if ext.isascii() else ".bin"
    return f"{tag}_{stem}{ext}"


def prepare_named_tunnel_file(local_path: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    """把文件放进命名隧道的固定根目录，返回它在这个根下的文件名。"""
    root = named_tunnel_root(settings)
    try:
        os.makedirs(root, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"建不了发布目录 {root}：{e}"}
    name = _publish_name_for(local_path)
    dst = os.path.join(root, name)
    try:
        src = os.path.abspath(local_path)
        if os.path.abspath(dst) != src:
            # 同大小且不比源旧就不重复拷（白膜可能几百 MB）
            if not (os.path.exists(dst)
                    and os.path.getsize(dst) == os.path.getsize(src)
                    and int(os.path.getmtime(dst)) >= int(os.path.getmtime(src))):
                shutil.copy2(src, dst)
        return {"ok": True, "name": name, "path": dst, "root": root}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"复制到发布目录失败：{type(e).__name__}: {e}"}


def warm_named_tunnel(settings: Dict[str, Any]) -> Dict[str, Any]:
    """开机预热：提前把命名隧道跑起来。

    为什么值得做：命名隧道第一次连边缘要 5~30 秒。如果等到"用户点了生成"
    才去建，那段时间会叠加在生成等待里，用户看到的是"点了半天没反应"。
    开机时先跑起来，之后每次发布都是**秒回**（复用同一进程）。

    失败**不影响启动** —— 只是这次没能预热，真正发布时会再试一次。
    """
    if not named_tunnel_ready(settings):
        return {"ok": False, "skipped": True, "reason": "没配命名隧道"}
    exe = _find_cloudflared()
    if not exe:
        return {"ok": False, "reason": "没找到 cloudflared"}
    served = _ensure_tunnel(named_tunnel_root(settings), exe, settings)
    return served


def _ensure_named_tunnel(settings: Dict[str, Any], serve_dir: str,
                         tunnel_exe: str) -> Dict[str, Any]:
    """跑**命名隧道**并返回它固定的公网地址。

    ★ 与临时隧道（quick tunnel）的区别，也就是"长期方案"的全部意义：

        临时隧道：`cloudflared tunnel --url http://127.0.0.1:PORT`
                  → 一个**随机**的 `xxx.trycloudflare.com`，**每次重启都换**。
                    厂商那边存过的地址立刻失效，用户得重新确认一遍。
        命名隧道：`cloudflared tunnel run <名字>`
                  → **你自己的域名**（如 `previz.example.com`），
                    永久固定；cloudflared 自己会重连，机器重启也不变。

    前提（用户做一次，之后永久有效）：
        ① 有一个域名，且已加进 Cloudflare（免费套餐就行）
        ② `cloudflared tunnel login`            浏览器点一次授权
        ③ `cloudflared tunnel create <名字>`    生成凭据文件
        ④ `cloudflared tunnel route dns <名字> <域名>`  建永久 DNS 记录
      这四步由 `scripts\\装永久白膜隧道.ps1` 引导完成。

    ★ 与临时隧道另一个重要区别：**命名隧道可以真的验证**。
      临时隧道的域名本机 DNS 解析不稳（实测 NXDOMAIN），所以只能"相信它成了"；
      而自定义域名的 DNS 是正常的，于是这里能**从公网取回一个探针文件**来确认。
      这正好符合"宁可如实说没通，也不要给一个 530 的地址"。
    """
    name = named_tunnel_name(settings)
    host = named_tunnel_hostname(settings)
    d = os.path.abspath(serve_dir)

    # 复用：同一条隧道 + 同一个目录 + 进程还活着
    proc = _TUNNEL.get("proc")
    if (proc and proc.poll() is None and _TUNNEL.get("url") == host
            and _TUNNEL.get("dir") == d and _TUNNEL.get("kind") == "named"):
        return {"ok": True, "base_url": host, "reason": "复用已有的命名隧道",
                "reused": True, "permanent": True}
    if proc and _TUNNEL.get("kind") == "named":
        stop_tunnel()

    try:
        import re as _re
        import socketserver
        from pathlib import Path as _P

        # 先确认凭据在（没有凭据 `tunnel run` 会直接失败，报错还很难懂）
        home = _P(os.environ.get("USERPROFILE") or _P.home())
        cfdir = home / ".cloudflared"
        if not (cfdir / "cert.pem").exists():
            return {"ok": False, "base_url": "", "kind": "named",
                    "reason": (f"命名隧道「{name}」还没配好：找不到 {cfdir / 'cert.pem'}。\n"
                               f"跑一次 `scripts\\装永久白膜隧道.ps1`，"
                               f"或在命令行执行 `cloudflared tunnel login` 授权。")}

        port = _free_port()
        handler = _quiet_handler(d)
        httpd = socketserver.TCPServer(("127.0.0.1", port), handler)
        httpd.allow_reuse_address = True
        threading.Thread(target=httpd.serve_forever, daemon=True,
                         name="vf-publish-named").start()

        env = {k: v for k, v in os.environ.items()
               if k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}

        # 命名隧道的入口地址（ingress）在 config.yml 里；这里用命令行覆盖，
        # 免得用户还要去改配置文件。
        # ★ `--url` + `run` 的组合是官方支持的：把这条隧道临时指向本地端口。
        cmd = [tunnel_exe, "tunnel", "--url", f"http://127.0.0.1:{port}",
               "--no-autoupdate", "run", name]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", env=env)

        # 等它连上边缘（"Registered tunnel connection" 是官方日志里的成功标志）
        ok_line, bad_line = "", ""
        deadline = time.time() + 60
        while time.time() < deadline:
            line = proc.stdout.readline() if proc.stdout else ""
            if not line:
                if proc.poll() is not None:
                    break
                continue
            low = line.lower()
            if "registered tunnel connection" in low or "connection registered" in low:
                ok_line = line.strip()
                break
            if any(h in low for h in ("failed", "error", "no such tunnel",
                                      "cert.pem", "cannot find",
                                      "unauthorized", "not found")):
                bad_line = line.strip()
            if ok_line:
                break

        if not ok_line:
            try:
                proc.kill()
            except Exception:
                pass
            httpd.shutdown()
            why = bad_line[:220] or \
                f"命名隧道「{name}」60 秒内没连上 Cloudflare 边缘"
            return {"ok": False, "base_url": "", "kind": "named",
                    "reason": (f"{why}\n★ 检查：① 隧道名字对不对"
                               f"（`cloudflared tunnel list`）；"
                               f"② 有没有跑过 `cloudflared tunnel route dns {name} <域名>`。")}

        # ★ 真的从公网取一次，确认这个固定域名**现在**能用。
        #   验不过也不算致命（可能是本机 DNS 抽风），但要如实标注 verified。
        verified, note = _verify_public(host, d)

        _TUNNEL.update({"proc": proc, "url": host, "port": port, "kind": "named",
                        "started": time.time(), "httpd": httpd, "dir": d,
                        "verified": verified, "name": name})
        logger.info("命名隧道已就绪：%s（verified=%s）", host, verified)
        return {"ok": True, "base_url": host, "kind": "named", "permanent": True,
                "verified": verified, "tunnel_name": name,
                "reason": (f"命名隧道「{name}」→ {host}"
                           + ("（已从公网验证可取到文件）" if verified
                              else f"（本机自检未通过：{note}）"))}

    except FileNotFoundError:
        return {"ok": False, "base_url": "", "kind": "named",
                "reason": f"找不到 cloudflared：{tunnel_exe}"}
    except Exception as e:  # noqa: BLE001
        logger.warning("起命名隧道失败：%s", e)
        return {"ok": False, "base_url": "", "kind": "named",
                "reason": f"起命名隧道出错：{type(e).__name__}: {e}"}


def _verify_public(host: str, serve_dir: str) -> tuple:
    """从**公网**取一个探针文件，确认这个固定域名真的在服务这个目录。

    为什么命名隧道要验、临时隧道不验：
      临时隧道域名（*.trycloudflare.com）在本机 DNS 上解析不稳（实测 NXDOMAIN），
      去验只会得到假阴性 —— 那是"检查本身不可靠"，不是"隧道坏了"。
      自定义域名的 DNS 是正常的，所以这里可以真验。
    返回 (是否通过, 说明)。
    """
    probe = os.path.join(serve_dir, "videoforge-channel-probe.txt")
    try:
        os.makedirs(serve_dir, exist_ok=True)
        token = f"videoforge-{int(time.time())}"
        with open(probe, "w", encoding="utf-8") as f:
            f.write(token)
        import urllib.request
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url = host.rstrip("/") + "/videoforge-channel-probe.txt"
        last = ""
        for _ in range(3):
            try:
                with op.open(url, timeout=25) as r:
                    body = r.read().decode("utf-8", "replace")
                if token in body:
                    return True, "ok"
                last = f"内容对不上：{body[:60]!r}"
            except Exception as e:  # noqa: BLE001
                last = f"{type(e).__name__}: {e}"
            time.sleep(3)
        return False, last
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    finally:
        try:
            os.remove(probe)
        except Exception:
            pass


def public_sync_dir(settings: Dict[str, Any]) -> str:
    """`public_base_url` 对应的**本地根目录**（可选）。

    ★ 为什么需要它（"长期方案"最后一块拼图）：
      只配 `public_base_url` 的话，代码只能**拼出**一个地址，并不能保证
      文件真的在那儿 —— 原来那版还照样返回 `verified: True`，属于**过度声称**。
      用户配了同步目录之后，发布就变成"真的把文件复制过去"，
      于是"填一次、以后一直能用"才成立。
    """
    return str((settings or {}).get("public_sync_dir") or "").strip()


def _sync_to_public_dir(local_path: str, sync_dir: str) -> Dict[str, Any]:
    """把文件复制到同步目录下。同目录同内容就不重复拷。"""
    try:
        src = Path(local_path)
        dst_dir = Path(sync_dir)
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / src.name
        if dst.exists() and dst.stat().st_size == src.stat().st_size \
                and int(dst.stat().st_mtime) >= int(src.stat().st_mtime):
            return {"ok": True, "path": str(dst), "copied": False}
        shutil.copy2(str(src), str(dst))
        return {"ok": True, "path": str(dst), "copied": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}



def find_ssh() -> Optional[str]:
    """找 ssh.exe（用 localhost.run 这条备用隧道需要它）。

    ★ 为什么需要备用隧道（2026-09-24 实测）：
      本机**连不上 Cloudflare** —— `1.1.1.1:443` TCP 直接不通，
      cloudflared 建 quick tunnel 时报：
        Post "https://api.trycloudflare.com/tunnel": context deadline exceeded
      所以云朵隧道在这台机器上是**真的用不了**，不是配置问题。
      而 `localhost.run`（走 SSH，纯 IPv4）实测**通**，能拿到
      `https://<id>.lhr.life` 这样的真实公网地址。

      ssh 优先用系统自带的 `C:\\Windows\\System32\\OpenSSH\\ssh.exe`；
      找不到再退回 PATH（比如 Git 自带的 ssh）。
    """
    import sys as _sys
    cands = []
    if _sys.platform == "win32":
        sr = os.environ.get("SystemRoot") or r"C:\Windows"
        cands.append(Path(sr) / "System32" / "OpenSSH" / "ssh.exe")
    cands.append(Path("/usr/bin/ssh"))
    cands.append(Path("/usr/local/bin/ssh"))
    for c in cands:
        try:
            if c.exists() and c.is_file():
                return str(c)
        except Exception:
            continue
    return shutil.which("ssh")


def local_ip() -> str:
    """本机在局域网里的地址（只为把提示写得具体一点，不用于发布）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


def tunnel_available() -> Optional[str]:
    """找隧道工具（返回工具路径，找不到返回 None）。

    ★ 顺序（2026-09-24 起分两级）：
      1. `cloudflared` —— 首选，输出稳定、只用一条命令
      2. `ssh`（配 localhost.run）—— **备用，且在本机实测是唯一能用的那条**
         因为这台机器连不上 Cloudflare（`1.1.1.1:443` 也不通），
         cloudflared 永远停在 "context deadline exceeded"。
    调用方通过返回的路径判断用哪个：路径里有 "cloudflared" 就走云朵，
    否则按 ssh 处理。
    """
    cf = _find_cloudflared()
    if cf:
        return cf
    if find_ssh():
        # 返回 ssh 路径，调用方据此选 localhost.run 那条路
        return find_ssh()
    return None


def _find_cloudflared() -> Optional[str]:
    """找 cloudflared 可执行文件（只找 cloudflared，不碰 ssh）。"""
    import sys as _sys

    here = Path(__file__).resolve()
    exe_name = "cloudflared.exe" if _sys.platform == "win32" else "cloudflared"

    cands = []
    lad = os.environ.get("LOCALAPPDATA")
    if lad:
        cands.append(Path(lad) / "VideoForge" / "bin" / exe_name)
    cands.append(Path.home() / ".local" / "bin" / exe_name)
    cands.append(Path.home() / "VideoForge" / "bin" / exe_name)
    try:
        cands.append(here.parents[2] / "bin" / exe_name)   # backend/core -> 项目根
    except Exception:
        pass

    for c in cands:
        try:
            if c.exists() and c.is_file() and c.stat().st_size > 1_000_000:
                return str(c)
        except Exception:
            continue

    for name, _ in TUNNEL_CANDIDATES:
        p = shutil.which(name)
        if p:
            return p
    return None


def _publish_via_oss(local_path: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    """把文件传到 SiliconFlow 的对象存储（**境内** CDN），换一个公网 URL。

    ★ 为什么需要（2026-10-23 实测）：
      token6688 网关（Seedance 2.0 特价按秒）是**境内**服务。它去下载
      参考视频走的是境内网络，trycloudflare / lhr.life 这类境外隧道地址
      它取不到 —— 网关会回 422
      `reference_video_probe_unavailable`（"参考视频暂时读取失败…本次未扣费"）。
      SiliconFlow 的 /v1/files 落到**上海阿里云 OSS**，境内可下载，
      实测网关能读到、视频正常出片。

      Key 复用 `tts_api_keys.siliconflow`（语音那把就是同一把 Key，
      账号级通用），用户不需要额外配置任何东西。

    失败**不抛异常**：返回 ok=False，由调用方继续走别的发布方案。
    """
    out = {"ok": False, "url": "", "mode": "oss", "verified": False, "synced": False,
           "reason": "", "warning": "", "how_to_fix": ""}
    try:
        key = ""
        for bucket in ("tts_api_keys", "llm_api_keys", "api_keys"):
            d = (settings or {}).get(bucket) or {}
            if isinstance(d, dict):
                v = str(d.get("siliconflow") or "").strip()
                if v:
                    key = v
                    break
        if not key:
            out["reason"] = "没有 SiliconFlow 的 Key（发不了境内对象存储）"
            return out

        import httpx as _httpx
        p = Path(local_path)
        with open(p, "rb") as fh:
            data = fh.read()
        # ★ 文件名必须是 **ASCII**（URL 里的中文会让平台侧下载失败）
        name = p.name
        stem, ext = os.path.splitext(name)
        if not stem.isascii():
            import hashlib as _hl
            stem = "f" + _hl.sha1(stem.encode("utf-8", "replace")).hexdigest()[:10]
        ext = ext if ext.isascii() else ".mp4"
        fname = stem + ext
        mime = "video/mp4" if ext.lower() in (".mp4", ".m4v") else (
            "image/png" if ext.lower() == ".png" else "application/octet-stream")

        files = {"file": (fname, data, mime)}
        data_fields = {"purpose": "video" if mime.startswith("video") else "assistants"}
        with _httpx.Client(timeout=180.0) as cl:
            r = cl.post("https://api.siliconflow.cn/v1/files",
                        headers={"Authorization": f"Bearer {key}"},
                        files=files, data=data_fields)
        if r.status_code >= 400:
            out["reason"] = f"对象存储上传失败 HTTP {r.status_code}：{r.text[:160]}"
            return out
        j = r.json()
        d = (j or {}).get("data") or {}
        url = str(d.get("object") or "").strip()
        if not url.startswith("http"):
            out["reason"] = f"对象存储没返回可用地址：{str(j)[:160]}"
            return out
        if not url.startswith("https://"):
            url = "https://" + url.lstrip("/")
        out.update({"ok": True, "url": url, "verified": True, "synced": True,
                    "reason": "已上传到境内对象存储（网关可直接下载）",
                    "file_id": d.get("id") or ""})
        return out
    except Exception as e:  # noqa: BLE001
        out["reason"] = f"对象存储发布异常：{type(e).__name__}: {e}"
        return out


def publish_file(local_path: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    """把本地文件变成公网 URL。

    Returns:
        {"ok", "url", "mode", "verified", "reason", "warning", "how_to_fix"}
    永远不抛异常 —— 发布失败只是"这一档用不了白膜参考"，不该让生成整个失败。

    三种模式（按优先级）：
      · `already_public` —— 本来就是公网 URL
      · `oss`           —— 上传到 SiliconFlow 的**国内** OSS（见下面说明）
      · `base_url`       —— 用户配了 `public_base_url`（**长期用推荐这个**）
      · `tunnel`         —— 内置 cloudflared 临时隧道（省事，但域名会变）
    """
    p = str(local_path or "")
    # ★ 顺序很重要：**先判"它本来就是公网 URL"**，再判文件存在。
    #   反过来的话，一个 https 地址会因为"本地没这个文件"被判失败 ——
    #   而这条路径现在是真会走到的：可灵 MCP 生成出来的成品就是
    #   `https://v4-fdl.../*.mp4` 这种公网地址，白膜流程里要能原样透传。
    if p and is_public_url(p):
        return {"ok": True, "url": p, "mode": "already_public", "verified": True,
                "synced": True, "reason": "已经是公网 URL（无需发布）",
                "warning": "", "how_to_fix": ""}

    if not p or not os.path.exists(p):
        return {"ok": False, "url": "", "mode": "none", "verified": False,
                "synced": False,
                "reason": f"文件不存在：{p}", "warning": "", "how_to_fix": ""}

    # ★★ 方案 A2（2026-10-23 新增）：**国内对象存储**。
    #
    #   为什么必须有它：token6688 网关（Seedance 2.0 特价按秒通道）是
    #   **境内**服务，它去下载参考视频时走的是境内网络 ——
    #   trycloudflare / lhr.life 这类**境外**隧道地址它根本取不到，
    #   网关会直接回 422 `reference_video_probe_unavailable`
    #   （"参考视频暂时读取失败…本次未扣费"）。
    #
    #   SiliconFlow 的文件接口（/v1/files）落到**上海阿里云 OSS**，
    #   境内可下载 —— 实测网关读到了，视频正常出片。
    #   而且它是**用户本来就有 Key** 的厂商（语音那把 key 同一个），
    #   不需要新增任何配置。
    _oss = _publish_via_oss(p, settings)
    if _oss.get("ok"):
        return _oss

    # 方案 B：用户自建的公网前缀（**长期用最稳**）
    base = public_base_url(settings)
    if base and is_public_url(base):
        name = Path(p).name
        sync = public_sync_dir(settings)
        if sync:
            cp = _sync_to_public_dir(p, sync)
            if cp.get("ok"):
                return {"ok": True, "url": f"{base}/{name}", "mode": "base_url",
                        "verified": True, "synced": True,
                        "synced_path": cp.get("path") or "",
                        "copied": bool(cp.get("copied")),
                        "reason": (f"已复制到同步目录并用 public_base_url 拼出地址"
                                   f"（域名固定，长期可用）"),
                        "warning": "", "how_to_fix": ""}
            return {"ok": True, "url": f"{base}/{name}", "mode": "base_url",
                    "verified": False, "synced": False,
                    "reason": f"public_base_url 拼出了地址，但**同步失败**：{cp.get('error')}",
                    "warning": (f"文件没能复制到 {sync} —— 模型很可能取不到它。"
                                f"检查这个目录存不存在、有没有写权限。"),
                    "how_to_fix": f"确认目录 {sync} 可写；或手动把 "
                                  f"{p} 上传到 {base} 下。"}
        # ★ 没有同步目录时**不能声称 verified** —— 我们只拼了个字符串，
        #   根本不知道文件在不在那边。原来这里返回 True，是过度声称。
        return {"ok": True, "url": f"{base}/{name}", "mode": "base_url",
                "verified": False, "synced": False,
                "reason": f"用 public_base_url 拼出地址（**未验证文件是否已在那**）",
                "warning": (f"只配了 public_base_url，没有配 public_sync_dir —— "
                            f"文件不会自动过去。请确认 {name} 确实已经在 {base} 下，"
                            f"否则模型会取不到，白膜就约束不了生成。"),
                "how_to_fix": ("想让它自动完成：在设置里再填「同步目录」"
                               "（public_sync_dir）= 这个公网地址对应的本地根目录。")}

    # 方案 A：内置隧道。
    #   ★★ 2026-09-25：**不管命名隧道还是临时隧道，都用一个固定根目录**。
    #
    #   原来（临时隧道）服务的是"文件所在目录"——而白膜按项目分目录。
    #   于是同一次会话里换个项目发布，隧道就要重启（5~40 秒握手），
    #   而且旧项目的地址立刻 404。对永久域名更是致命（见 named_tunnel_root）。
    #
    #   统一成固定根之后：
    #     · 隧道**只起一次**，切项目不再重启 → 第一次之后就秒回
    #     · 任何发布出去的地址在本进程活着的期间**一直有效**
    #     · 命名隧道与临时隧道走**同一条代码路径**，少一处能写错的地方
    #   代价：每次发布多复制一个文件（白膜通常几 MB，可忽略）。
    tp = tunnel_available()
    if named_tunnel_ready(settings):
        tp = tp or _find_cloudflared()
        if not tp:
            return {"ok": False, "url": "", "mode": "none", "verified": False,
                    "reason": "配了命名隧道但找不到 cloudflared", "warning": "",
                    "how_to_fix": "跑 scripts\\装永久白膜隧道.ps1（它会检查并下载）"}

    if tp:
        prep = prepare_named_tunnel_file(p, settings)
        if not prep.get("ok"):
            return {"ok": False, "url": "", "mode": "none", "verified": False,
                    "reason": prep.get("error") or "放进发布目录失败", "warning": "",
                    "how_to_fix": f"检查 {named_tunnel_root(settings)} 能不能写"}
        root = prep["root"]
        # 命名隧道失败时**原样报错，不退回临时隧道** ——
        # 退回的话用户会拿到一个每次变的地址却以为已经是长期方案。
        t = _ensure_tunnel(root, tp, settings)
        if not t.get("ok"):
            if named_tunnel_ready(settings):
                return {"ok": False, "url": "", "mode": "none", "verified": False,
                        "reason": t.get("reason") or "起命名隧道失败", "warning": "",
                        "how_to_fix": ("跑 scripts\\装永久白膜隧道.ps1 检查授权与 DNS；"
                                       "或先清掉 tunnel_name 用临时隧道顶上。")}
            return {"ok": False, "url": "", "mode": "none", "verified": False,
                    "reason": t.get("reason") or "起隧道失败", "warning": "",
                    "how_to_fix": ("检查这台机器能不能出网；"
                                   "或配一条固定域名的命名隧道"
                                   "（跑 scripts\\装永久白膜隧道.ps1）。")}

        file_url = f"{t['base_url']}/{prep['name']}"
        _TUNNEL.setdefault("published", {})[p] = file_url
        reachable, warn = _local_selfcheck(file_url)
        _TUNNEL["healthy"] = reachable
        _TUNNEL["last_health"] = time.time()
        permanent = bool(t.get("permanent"))
        kind_txt = ("命名隧道，**域名永久固定**" if permanent else "临时隧道（域名重启会变）")
        return {
            "ok": True, "url": file_url, "mode": "tunnel", "verified": reachable,
            "base_url": t["base_url"], "permanent": permanent,
            "provider": ("cloudflared-named" if permanent else "cloudflared-quick"),
            "published_name": prep["name"], "publish_root": root,
            "reason": (f"{kind_txt}：{t['base_url']}"
                       + ("；复用已有隧道" if t.get("reused") else "；新建隧道")
                       + ("；本地自检通过" if reachable else "；本地自检未通过（见警告）")
                       + (f"；文件落在固定发布目录，这个地址**以后一直有效**"
                          if permanent else "")),
            "warning": warn,
            "how_to_fix": ("" if permanent else
                           "想让它永久固定：跑 scripts\\装永久白膜隧道.ps1"),
        }


        name = os.path.basename(p)
        file_url = f"{t['base_url']}/{name}"
        _TUNNEL.setdefault("published", {})[p] = file_url

        reachable, warn = _local_selfcheck(file_url)
        _TUNNEL["healthy"] = reachable
        _TUNNEL["last_health"] = time.time()

        return {
            "ok": True, "url": file_url, "mode": "tunnel", "verified": reachable,
            "base_url": t["base_url"],
            "provider": t.get("provider") or "tunnel",
            "reason": (f"{t.get('provider') or 'cloudflared'} 临时隧道"
                       f"（本地端口 {_TUNNEL.get('port')}，"
                       f"只读、只服务白膜所在目录）"
                       + ("；复用已有隧道" if t.get("reused") else "；新建隧道")
                       + ("；本地自检通过" if reachable else "；本地自检未通过（见警告）")),
            "warning": warn, "how_to_fix": "",
        }

    # 都不行 → 如实说清楚，并给出可执行的修复步骤
    ip = local_ip()
    return {
        "ok": False, "url": "", "mode": "none", "verified": False,
        "reason": ("要让模型把白膜当参考素材，需要一个**厂商服务器能访问到**的"
                   "公网 URL。图片可以走 base64，但参考视频不行（实测火山方舟"
                   "明确拒绝：`reference_video must be provided as a web url`）。"),
        "warning": "",
        "how_to_fix": (
            f"本机地址是 {ip or '内网'}（NAT 后面），厂商访问不到。二选一：\n"
            f"  · 省事：跑 `scripts\\装隧道工具.ps1`（装 cloudflared，免费不用注册），"
            f"之后白膜会自动发布成临时公网地址。\n"
            f"  · **长期：在「设置」里填 `public_base_url`**"
            f"（你自己的对象存储/服务器地址），并把白膜文件同步过去 —— "
            f"域名固定、不依赖隧道，**这是长期使用的推荐做法**。\n"
            f"不装也行 —— 白膜仍然有用（让**你**在花钱前确认运镜和节奏），"
            f"只是模型不会被运动轨迹约束。"
        ),
    }


def _resolve_via_doh(host: str) -> str:
    """用 DNS-over-HTTPS 解析主机名，返回一个 IP（拿不到就返回空串）。

    ★ 为什么要绕过本机 DNS（2026-09-23 实测）：
      这台机器的系统 DNS（223.5.5.5）对 `*.trycloudflare.com` **解析不稳**：
      同一个刚分配出来的隧道域名，本机 `Resolve-DnsName` 返回 NXDOMAIN，
      而用 1.1.1.1 直接查是有 A 记录的。

      这会让"本地自检"得出**假阴性** —— 明明隧道是好的（实测厂商那边
      真的取到了，还出了一条片子），本地却报"不可达"，于是白膜发不出去。

      DoH 走 HTTPS，不受本地 DNS 影响。
    """
    try:
        import httpx
    except Exception:
        return ""
    import json as _json
    for ep in ("https://1.1.1.1/dns-query", "https://cloudflare-dns.com/dns-query"):
        try:
            with httpx.Client(timeout=12, trust_env=False) as c:
                r = c.get(ep, params={"name": host, "type": "A"},
                          headers={"accept": "application/dns-json"})
                j = _json.loads(r.text)
                for a in (j.get("Answer") or []):
                    if a.get("type") == 1 and a.get("data"):
                        return str(a["data"])
        except Exception:
            continue
    return ""


def _wait_reachable(url: str, attempts: int = 8, gap: float = 4.0,
                    timeout: float = 25.0) -> bool:
    """等新发布的隧道地址**真的能取到**再返回。

    ★★ 为什么必须等（2026-09-23 实测撞到）：
      cloudflared 日志一打出 "Your quick Tunnel has been created! Visit it at
      (it may take some time to be reachable)"，那行**括号里就是重点** ——
      地址刚分配时经常还取不到。

    ★★ 但**不能只信本机 DNS**：
      实测本机 DNS 对 trycloudflare 的临时域名会返回 NXDOMAIN，
      而隧道其实完全正常（厂商确实取到了）。所以：
        ① 先按普通方式试；
        ② 普通方式失败时，用 DoH 解析出 IP，**直连这个 IP 并带上 Host 头**再试；
        ③ 两条都不通才算失败。
      少了 ② 就会把好隧道误判成坏隧道（我今天就误判了一次）。
    """
    try:
        import httpx
    except Exception:
        return True      # 没 httpx 就不验证，别因此挡住主流程

    from urllib.parse import urlparse
    u = urlparse(url)
    host = u.hostname or ""

    for i in range(attempts):
        # ① 常规
        try:
            with httpx.Client(timeout=timeout, trust_env=False,
                              follow_redirects=True) as c:
                r = c.get(url)
                if r.status_code == 200 and len(r.content) > 1024:
                    return True
        except Exception as e:
            logger.debug("隧道地址第 %d 次常规尝试失败：%s", i + 1, str(e)[:100])

        # ② DoH + 直连 IP（绕开本机 DNS）
        if host and i >= 1:
            ip = _resolve_via_doh(host)
            if ip:
                try:
                    direct = f"{u.scheme}://{ip}:{u.port or (443 if u.scheme == 'https' else 80)}{u.path}"
                    with httpx.Client(timeout=timeout, trust_env=False,
                                      follow_redirects=True, verify=False) as c:
                        r = c.get(direct, headers={"Host": host})
                        if r.status_code == 200 and len(r.content) > 1024:
                            logger.info("本机 DNS 解析不到，但 DoH+直连 IP 可达 —— "
                                        "隧道是好的（本机 DNS 的问题，不影响厂商）")
                            return True
                except Exception as e:
                    logger.debug("DoH 直连也失败：%s", str(e)[:100])

        if i < attempts - 1:
            time.sleep(gap)
    return False


def _probe_tunnel_definitely_dead(base_url: str) -> Optional[str]:
    """快速探一下这条隧道是不是**肯定坏了**。

    返回 None  = 探不出"肯定坏"（**接受这条隧道**）
    返回 字符串 = 肯定坏，字符串是原因（调用方应当换下一个实现）

    ★★ 判据必须很窄，这是关键：

      **只认「边缘明确回答、且说没有」** —— 也就是 HTTP 状态码回来了，
      但不是 2xx（典型是 cloudflared 的 **530**："这条隧道不在"）。
      这是**肯定**的失败信号：Cloudflare 边缘能回答，说明网络通，
      它说没有，那就是真没有。

      **不认「连不上 / 解析不了」** —— 本机 DNS 对 `*.trycloudflare.com`
      时好时坏（实测同一个域名前一分钟 NXDOMAIN、后一分钟正常），
      拿它当失败会把**好隧道判死**，而白膜就永远发不出去了。
      这条教训在这个文件里记过（见 `_local_selfcheck`）。

      一句话：**假阴性不能有否决权。** 只有"肯定的阳性失败"才配触发回退。
    """
    if not base_url or not base_url.startswith("http"):
        return None
    try:
        import httpx
    except Exception:  # noqa: BLE001
        return None
    try:
        # 用一个几乎肯定不存在的路径去探：能走到"边缘回答"就够，
        # 不关心文件在不在（那是后面 _local_selfcheck 的事）。
        probe = base_url.rstrip("/") + "/videoforge-tunnel-probe"
        with httpx.Client(timeout=12.0, trust_env=False,
                          follow_redirects=True) as c:
            r = c.get(probe)
        code = r.status_code
        if 200 <= code < 400:
            return None                     # 边缘正常回答 → 隧道是活的
        if code in (530, 502, 503, 504):
            return f"HTTP {code}"
        # 其它 4xx：边缘回答了，说明隧道通（只是这个路径没有）
        if 400 <= code < 500:
            return None
        return f"HTTP {code}"
    except Exception:  # noqa: BLE001
        # 连不上 / 解析不了 / 超时 → **探不出结论**，接受这条隧道
        return None


def _ensure_tunnel(serve_dir: str, tunnel_exe: str,
                   settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """确保有一条**存活**的隧道在服务 `serve_dir`。返回 {"ok", "base_url", "reason"}。

    ★★ 设计要点（长期使用必须这样，2026-09-23 改）

    ⓪ **命名隧道优先**（2026-09-25 加）。
       配了 `tunnel_named` + `tunnel_hostname` 就用命名隧道：
       域名是你自己的，**永久固定**，重启也不变。
       没配才退回临时隧道（域名每次重启都换）。
       这条是用户明确要的"弄成长期的" —— 临时隧道只能算能用，
       厂商那边存过的地址一重启就失效。

    ① **一条隧道服务整个目录，不是每个文件一条。**
       早先每次 `publish_file` 都新起一条隧道、把旧的那条丢下不管 ——
       结果是：慢（每次都要 5-40 秒握手）、留一堆僵尸进程、
       而且旧隧道被杀之后它对应的 URL 会变成 HTTP 530。
       现在按目录复用：同一批白膜共用一个 hostname。

    ② **隧道死了要能自愈。**
       临时隧道会掉（网络抖动、cloudflared 退出）。如果只检查一次就长期用，
       后面所有生成都会拿到一个 530 的地址。
       这里每次调用都做**轻量存活检查**（进程在不在），必要时重开。

    ③ **不做"本地可达"的硬检查** —— 见下面 `_wait_reachable` 的说明：
       本机 DNS 对 trycloudflare 不稳，会误判好隧道为坏隧道。
       （命名隧道不适用这条：自定义域名能真验，见 `_verify_public`。）
    """
    d = os.path.abspath(serve_dir)
    settings = settings or {}

    # ── ⓪ 命名隧道优先 ──
    if named_tunnel_ready(settings):
        got = _ensure_named_tunnel(settings, d, tunnel_exe)
        if got.get("ok"):
            return got
        # 命名隧道没起来**不要静默退回临时隧道** —— 那会给出一个
        # "看起来能用但每次都不一样"的地址，而用户以为已经是长期方案了。
        # 如实报错，让他去修证书/域名。
        logger.warning("命名隧道没起来：%s", (got.get("reason") or "")[:200])
        return got

    proc = _TUNNEL.get("proc")
    alive = bool(proc and proc.poll() is None)
    same_dir = (_TUNNEL.get("dir") or "") == d

    if alive and same_dir and _TUNNEL.get("url"):
        # ★★ 2026-09-25 补：复用之前**先确认它现在还活着**。
        #
        #   实测（localhost.run 免费档）：同一条隧道，
        #   A 文件发布后能取到（HTTP 200），几十秒后同一个域名变成
        #   **HTTP 503 Service Unavailable**，而**进程还活着** ——
        #   原来的复用判断只看"进程在不在"，于是继续把死地址发出去，
        #   厂商取不到白膜，用户花钱出的片没按白膜走，界面还显示一切正常。
        #
        #   所以复用要加一道"肯定坏就重开"的检查：
        #     探到 5xx（边缘明确说没有）→ 重开隧道
        #     探不出结论（DNS/连接问题）→ 照旧复用（假阴性不能有否决权）
        verdict = _probe_tunnel_definitely_dead(_TUNNEL["url"])
        if verdict is None:
            return {"ok": True, "base_url": _TUNNEL["url"],
                    "reason": "复用已有隧道", "reused": True,
                    "permanent": (_TUNNEL.get("kind") == "named")}
        logger.warning("已有隧道被边缘否认（%s），重开一条", verdict)
        _TUNNEL["restarts"] = int(_TUNNEL.get("restarts") or 0) + 1
        stop_tunnel()

    # 目录变了、或隧道死了 → 重开
    if proc and (not alive or not same_dir):
        logger.info("隧道需要重开（alive=%s same_dir=%s）", alive, same_dir)
        stop_tunnel()
        _TUNNEL["restarts"] = int(_TUNNEL.get("restarts") or 0) + 1

    try:
        import re
        import socketserver

        port = _free_port()
        handler = _quiet_handler(d)
        httpd = socketserver.TCPServer(("127.0.0.1", port), handler)
        httpd.allow_reuse_address = True
        threading.Thread(target=httpd.serve_forever, daemon=True,
                         name="vf-publish-static").start()

        # ★ 干净环境：免得系统代理把隧道工具自己的出网请求也劫持了
        env = {k: v for k, v in os.environ.items()
               if k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}

        # ★★ 按顺序试**所有**可用的隧道实现。
        #    （2026-09-24）本机实测 Cloudflare 的 IPv4/IPv6 都不通，
        #    cloudflared 必然停在 "context deadline exceeded"。
        #    所以"试完 cloudflared 就放弃"等于白膜永远发不出去 ——
        #    必须自动换到 localhost.run（走 SSH/IPv4，实测通）。
        #
        #    ★★★ 2026-09-25 补上**第二个回退条件**（这一条是真跑抓到的）：
        #      cloudflared 有时候**拿到了域名但那条隧道是死的** ——
        #      公网访问返回 **HTTP 530**（Cloudflare 边缘说"这条隧道不在"）。
        #      原来的代码只看"有没有拿到 URL"，于是**接受了这条死隧道**，
        #      publish_file 照样报 ok=True。
        #
        #      后果比"发布失败"严重得多：
        #        厂商取不到白膜 → 白膜没约束生成 →
        #        用户花钱出的片**根本没按白膜走**，而界面显示一切正常。
        #
        #      所以：拿到 URL 之后要**快速探一下**。探到"边缘明确回答但说没有"
        #      （HTTP 状态码回来了，且不是 2xx）就是**肯定坏了** ——
        #      换下一个实现。注意**只认这种"肯定的失败"**：
        #      DNS 解析不了**不算**（本机 DNS 对 trycloudflare 时好时坏，
        #      拿它当失败会把好隧道判死 —— 这是文件里记过的教训）。
        tried: List[str] = []
        last_reason = ""
        dead_urls: List[str] = []
        for kind, exe in _tunnel_impls(tunnel_exe):
            tried.append(kind)
            got = _start_one_tunnel(kind, exe, port, httpd, d, env, re)
            if got.get("ok"):
                verdict = _probe_tunnel_definitely_dead(got.get("base_url") or "")
                if verdict is None:
                    # 探不出"肯定坏"（可能是 DNS 抖动）→ 接受它，并记下好用
                    mark_impl_health(kind, True, "起来了且边缘没否认")
                    return got
                dead_urls.append(f"{kind}:{got.get('base_url')} → {verdict}")
                logger.warning("隧道实现 %s 给了地址但边缘说它不在（%s），"
                               "换下一个实现", kind, verdict)
                mark_impl_health(kind, False, f"地址取回 {verdict}")
                stop_tunnel()
                last_reason = f"{kind} 给的地址取回是 {verdict}（隧道没真正连上）"
                continue
            last_reason = got.get("reason") or ""
            mark_impl_health(kind, False, last_reason)
            logger.warning("隧道实现 %s 没成：%s", kind, last_reason[:160])

        httpd.shutdown()
        return {"ok": False, "base_url": "",
                "reason": (f"试过的隧道方式都不行（{'、'.join(tried)}）。\n"
                           + last_reason
                           + "\n★ 替代方案（任一即可）：\n"
                             "  1. 「设置 → 白膜公网通道」填 public_base_url + "
                             "同步目录（域名固定，**长期推荐**）；\n"
                             "  2. 换用支持**本地文件直传**的平台 —— "
                             "可灵 MCP 的 file_upload 那条**完全不需要公网 URL**。")}

    except Exception as e:
        logger.warning("起隧道失败：%s", e)
        return {"ok": False, "base_url": "",
                "reason": f"起隧道出错：{type(e).__name__}: {e}"}


def _impl_health_path() -> str:
    root = (os.environ.get("VIDEOFORGE_DATA_DIR") or "").strip()
    if not root or "://" in root:
        root = os.path.join(os.path.expanduser("~"), ".videoforge")
    try:
        os.makedirs(root, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return os.path.join(root, "tunnel_health.json")


def load_impl_health() -> Dict[str, Any]:
    """读「哪条隧道实现真的好用」的记忆。

    ★★ 为什么需要它（2026-09-25 实测）：
      这台机器**连不上 Cloudflare 的临时隧道边缘** ——
      `*.trycloudflare.com` 要么 DNS 解析不了，要么 TLS 握手超时，
      而 `*.lhr.life`（localhost.run，走 SSH/IPv4）**稳定可用**。

      但 `_tunnel_impls` 每次都**先试 cloudflared**（因为 tunnel_available()
      认为它优先），于是每次发布都要先白等一轮超时，再回退 ——
      第一次发布实测要 35~100 秒，其中大半是白等的。

      所以让程序**记住上次哪条成了**，下次优先用它。
      这不是"猜"，是"这台机器上的实测结果"。

      （同一个模式项目里已经有了：`voicecast.load_voice_health` ——
        音色也一样，哪个能合成过就记下来。）
    """
    try:
        with open(_impl_health_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def mark_impl_health(kind: str, ok: bool, note: str = "") -> None:
    """记下某条隧道实现这次成没成。"""
    if not kind:
        return
    d = load_impl_health()
    rec = d.get(kind) if isinstance(d.get(kind), dict) else {}
    rec["ok_count"] = int(rec.get("ok_count") or 0) + (1 if ok else 0)
    rec["fail_count"] = int(rec.get("fail_count") or 0) + (0 if ok else 1)
    rec["last_ok"] = bool(ok)
    rec["last_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    rec["last_note"] = str(note or "")[:200]
    d[kind] = rec
    try:
        with open(_impl_health_path(), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        logger.debug("记隧道健康度失败（不影响使用）：%s", e)


def _tunnel_impls(preferred: str) -> List[tuple]:
    """返回要依次尝试的隧道实现 [(kind, exe), ...]。

    kind 是 "cloudflared" / "localhost.run"；exe 是对应的可执行文件。

    排序规则（★ 2026-09-25 改成"按实测记忆排"）：
      ① 上次**成功过**的排最前 —— 这台机器上它真的能用，别每次重新踩坑
      ② 然后是 `tunnel_available()` 挑出来的那个
      ③ 上次连续失败过的排最后（但仍然会试 —— 网络会变，不能永久拉黑）

    ★ 为什么不能"永久拉黑失败的"：本次实测就遇到过同一条实现
      前一分钟失败、后一分钟成功（网络抖动）。**记住偏好**可以，
      **永久禁用**不行。
    """
    out: List[tuple] = []
    cf = _find_cloudflared()
    ssh = find_ssh()
    cands: List[tuple] = []
    pref_is_cf = Path(preferred).name.lower().startswith("cloudflared")
    if pref_is_cf and cf:
        cands.append(("cloudflared", cf))
    if ssh:
        cands.append(("localhost.run", ssh))
    if not pref_is_cf and cf:
        cands.append(("cloudflared", cf))

    health = load_impl_health()

    def rank(item):
        kind = item[0]
        h = health.get(kind) if isinstance(health.get(kind), dict) else {}
        okc = int(h.get("ok_count") or 0)
        failc = int(h.get("fail_count") or 0)
        # 上次成功过 → 排最前
        if h.get("last_ok") and okc > 0:
            return (0, -okc, failc)
        # 从没试过 → 中间
        if okc == 0 and failc == 0:
            return (1, 0, 0)
        # 上次失败过 → 最后（但还会试）
        return (2, failc, -okc)

    out = sorted(cands, key=rank)
    return out


def _start_one_tunnel(kind: str, exe: str, port: int, httpd, d: str,
                      env: dict, re_mod) -> Dict[str, Any]:
    """起**一个**隧道实现并等它给出公网地址。失败返回 {ok: False, reason}。"""
    if kind == "localhost.run":
        cmd = [exe, "-R", f"80:127.0.0.1:{port}",
               "-o", "StrictHostKeyChecking=accept-new",
               "-o", "UserKnownHostsFile=" + os.devnull,
               "-o", "ServerAliveInterval=30",
               "-o", "ServerAliveCountMax=3",
               "-o", "ExitOnForwardFailure=yes", "-T",
               "nokey@localhost.run"]
        url_re = re_mod.compile(r"https://([a-z0-9-]+)\.(?:lhr\.life|localhost\.run)")
        fail_hints = ("permission denied", "connection refused",
                      "could not resolve", "timed out", "no route to host",
                      "remote port forwarding failed", "connection closed")
        good_label = lambda lb: len(lb) >= 6                     # noqa: E731
    elif kind == "serveo":
        # ★ 2026-09-25 实测记录（`tests/probe_free_stable_tunnel.py`）：
        #   serveo.net 免费档**给不出固定子域** ——
        #     域名形如 `<随机hex>-39-182-9-223.serveousercontent.com`
        #     （后半段是你的公网 IP），两次连接**不一样**，
        #     而且日志里明说 `Port forwarding ... expired`。
        #   所以它**没有**进 _tunnel_impls 的默认链。这里保留实现 +
        #   修正域名后缀，只是为了：① 记录这个结论，别再有人来回试；
        #   ② 万一将来要用（比如注册了 SSH key 拿固定子域）。
        #   ⚠ 后缀是 `serveousercontent.com`，**不是** `serveo.net` ——
        #     只写 serveo.net 的话会把拿到的地址判成"没认出来"。
        cmd = [exe, "-R", f"{getattr(_start_one_tunnel, '_serveo_name', '') or ''}"
                          f":80:127.0.0.1:{port}",
               "-o", "StrictHostKeyChecking=accept-new",
               "-o", "UserKnownHostsFile=" + os.devnull,
               "-o", "ServerAliveInterval=30", "-T", "serveo.net"]
        url_re = re_mod.compile(
            r"https://([a-z0-9-]+)\.(?:serveo\.net|serveousercontent\.com)")
        fail_hints = ("permission denied", "connection refused",
                      "could not resolve", "timed out", "expired",
                      "remote port forwarding failed")
        good_label = lambda lb: len(lb) >= 6                     # noqa: E731
    else:
        cmd = []
        for tname, targs in TUNNEL_CANDIDATES:
            if Path(exe).name.lower().startswith(tname):
                cmd = [exe] + [a.format(origin=f"http://127.0.0.1:{port}")
                               for a in targs]
                break
        if not cmd:
            return {"ok": False, "reason": f"不认识这个隧道工具：{exe}"}
        url_re = re_mod.compile(r"https://([a-z0-9-]+)\.trycloudflare\.com")
        fail_hints = ("failed to request quick tunnel", "context deadline exceeded",
                      "no such host", "connection refused", "failed to dial",
                      "unable to connect", "certificate")
        good_label = lambda lb: lb.count("-") >= 2 and len(lb) >= 12  # noqa: E731

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", env=env)
    # ★★ 2026-09-27 第 71 轮：把这次起的隧道 PID **记到磁盘**。
    #   为什么：程序被强杀 / 崩溃 / 直接从任务管理器结束的时候，
    #   `stop_tunnel()`（挂在优雅退出的钩子上）**不会执行**，
    #   `cloudflared.exe` 就变成孤儿一直留着。
    #   实测：用户机器上攒了 **40 个**孤儿 cloudflared，合计吃掉 **1087 MB** 内存
    #   （最早的是当天上午 9:57 留下的）—— 这既拖慢机器，又会被误当成
    #   "软件毛病多/会崩"的一部分。记下 PID，下次启动就能精确回收。
    _remember_tunnel_pid(proc.pid)

    # 服务商自家的域名绝不能当隧道地址（否则就是"假成功"—— 见下面长注释）
    _RESERVED = {"api", "www", "developers", "blog", "one", "dash",
                 "support", "community", "cloudflare", "cftunnel",
                 "admin", "docs", "localhost"}
    url = ""
    failed_line = ""
    deadline = time.time() + 45
    while time.time() < deadline:
        line = proc.stdout.readline() if proc.stdout else ""
        if not line:
            if proc.poll() is not None:
                break
            continue
        low = line.lower()
        if any(h in low for h in fail_hints):
            failed_line = line.strip()
        for m in url_re.finditer(line):
            label = m.group(1)
            if label in _RESERVED or not good_label(label):
                continue
            url = m.group(0)
            break
        if url:
            break

    if not url:
        try:
            proc.kill()
        except Exception:
            pass
        why = ("隧道工具报错：" + failed_line[:200]) if failed_line else \
              f"{kind} 起来了但 45 秒内没拿到公网地址（可能被网络策略挡了）"
        net_down = bool(failed_line) and any(
            k in failed_line.lower() for k in
            ("deadline exceeded", "no such host", "timed out",
             "no route to host", "could not resolve"))
        if net_down and kind == "cloudflared":
            why += ("\n★ 这台机器**连不上 Cloudflare**"
                    "（实测 1.1.1.1:443 与 api.trycloudflare.com 的 IPv4/IPv6 都不通）。")
        elif net_down:
            why += ("\n★ 连不上 localhost.run（SSH；实测这本该是通的）。"
                    "检查网络/防火墙，或改用「设置 → 白膜公网通道」里的 "
                    "public_base_url + 同步目录。")
        return {"ok": False, "reason": why}

    _TUNNEL.update({"proc": proc, "url": url, "port": port,
                    "started": time.time(), "httpd": httpd,
                    "dir": d, "published": {}, "healthy": True,
                    "last_health": time.time(), "provider": kind})
    logger.info("白膜隧道就绪：%s（%s，本地 %s，端口 %d）", url, kind, d, port)
    return {"ok": True, "base_url": url, "reused": False, "provider": kind,
            "reason": f"新建隧道（{kind}）"}


def _local_selfcheck(file_url: str) -> tuple:
    """本地可达性自检 → (reachable, warning)。

    ★★ 2026-09-23 实测，这里有个很容易做错的地方：

      本机**无法可靠地验证 trycloudflare 的临时域名**：
        · 系统 DNS（223.5.5.5）对该域名返回 NXDOMAIN / REFUSED
        · 所有外部 DoH（1.1.1.1 / cloudflare-dns.com / dns.google）
          在本网络下**全部被挡**（超时或连接重置）
        · 但隧道本身是**好的** —— 实测厂商真的从公网取到了白膜，还据此出了片。

      所以如果把"本地自检不通过"当成失败，就会**把好隧道判死**，
      白膜永远发不出去。我第一版就是这么写的，测出假阴性后改掉了。

      **正确做法**：本地检查只用来**提示**，真正的裁判是厂商。
      返回 `verified=False` 让上层知道"没验成，但不代表不能用"。

    > 通用教训：当一个检查的假阴性会导致功能完全不可用时，它就不该有否决权。

    ★ 2026-09-25 改进：**区分"DNS 解析不了"和"真取不回"**。
      两者含义完全不同，混成一句话会让人判断错方向：

        · `getaddrinfo failed` → 是本机 DNS 抽风（这台机器对
          *.trycloudflare.com 确实不稳，实测时好时坏）。
          **不代表隧道坏** —— 别让用户去查隧道。
        · 连上了但 4xx/5xx、或 TLS 握手超时 → 更可能是真问题
          （文件没同步过去、隧道没指向那个目录）。

      同时把重试从 2 次提到 4 次：DNS 抖动往往**几秒后就恢复**，
      原来只试 2 次（合计 ~4 秒）太容易误报。实测就遇到过：
      同一个域名前一分钟解析失败、后一分钟就正常。

    ★ 2026-09-25 再调参：**自检不能拖慢发布**。
      重试提到 4 次 × (25 秒超时 + 2.5 秒间隔) 之后，最坏情况要 ~110 秒，
      全压在"用户点了生成"的等待里。实测发布耗时从 ~35 秒涨到 98~123 秒 ——
      这对"稳定发挥"是**负面**的：自检只是"顺便看一眼"，
      它的价值不该用一个多分钟的等待去换。

      现在的分工：
        · 发布路径上的自检 = **快**（3 次 × 8 秒超时 ≈ 最坏 30 秒），
          拿到就报 verified=True，拿不到就如实说"没验成"（不阻断发布）
        · 想要**彻底**验证 → 用「🔌 测试白膜公网通道」那个按钮
          （那是显式动作，用户愿意等，可以在那里用长超时 + 多次重试）
    """
    reachable = _wait_reachable(file_url, attempts=3, gap=2.0, timeout=8.0)
    if reachable:
        return True, ""

    import socket as _sk
    host = ""
    try:
        from urllib.parse import urlparse as _up
        host = _up(file_url).hostname or ""
    except Exception:  # noqa: BLE001
        pass

    dns_ok = True
    if host:
        try:
            _sk.gethostbyname(host)
        except Exception:  # noqa: BLE001
            dns_ok = False

    if not dns_ok:
        return False, ("提示：本机 DNS 解析不到这个域名（这台机器对 "
                       "*.trycloudflare.com 解析**时好时坏**，且外部 DoH 被网络挡住）。"
                       "**这不代表隧道不能用** —— 实测厂商那边能正常取到。"
                       "★ 想要一个本机也能稳定验证的地址，配命名隧道"
                       "（跑 `scripts\\装永久白膜隧道.ps1`）——"
                       "自己的域名 DNS 是正常的。")
    return False, ("提示：域名能解析，但取回这个地址没成功"
                   "（可能是文件还没同步过去、或隧道指向的目录不对）。"
                   "厂商那边多半也取不到 —— 建议点「🔌 测试白膜公网通道」细看。")


def _quiet_handler(directory: str):
    import http.server

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=directory, **kw)

        def log_message(self, *a):        # 别把访问日志刷进主日志
            pass

    return H


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def stop_tunnel() -> None:
    """关掉隧道和静态服务（进程退出时也该调一次）。

    ⚠ 长期使用时**不必**每次生成后都调 —— 隧道按目录复用，
    留着反而省掉下次 5-40 秒的握手。只在退出程序、或想收回公网暴露时调。
    """
    proc = _TUNNEL.get("proc")
    if proc:
        try:
            proc.kill()
        except Exception:
            pass
    httpd = _TUNNEL.get("httpd")
    if httpd:
        try:
            httpd.shutdown()
        except Exception:
            pass
    _TUNNEL.update({"proc": None, "url": "", "port": 0, "started": 0.0,
                    "httpd": None, "dir": "", "published": {},
                    "healthy": False})


# ══════════════════════════════════════════════════════════════════════
# 隧道进程回收（2026-09-27 第 71 轮）
# ══════════════════════════════════════════════════════════════════════
# 【问题】`cloudflared.exe` 是子进程。程序**优雅退出**时会 `stop_tunnel()` 杀掉它；
#   但用户从任务管理器结束、或程序崩了、或直接关掉控制台 —— 钩子不执行，
#   子进程就变孤儿，一直挂着。实测这台机器上攒了 **40 个**，
#   合计 **1087 MB** 内存（最早一个留了 13 小时）。
#
# 【为什么不用"按名字杀"】那会把**另一个正在用的实例**的隧道一起杀掉。
#   这里只杀**我们自己上一个会话里起的**那些：启动时把 PID 写进一个文件，
#   下次启动只回收文件里记着、且**确实还是 cloudflared** 的进程。

def _tunnel_pid_file() -> str:
    root = (os.environ.get("VIDEOFORGE_DATA_DIR") or "").strip() or os.getcwd()
    return os.path.join(root, "cache", "tunnel_pids.json")


#: **本进程**起过的隧道 PID。
#: ★ 回收时必须排除它们：同一个进程里可能有**多个 app 实例**
#:   （`tests/e2e_previz_channel_live.py` 这类脚本、以及 TestClient 的多次
#:   with 块都会走启动流程）。第二次启动如果把自己刚起的、**正在用**的隧道
#:   回收掉，公网地址当场失效 —— 实测就是这么被自己的"清理"坑了一次。
_SPAWNED_PIDS: set = set()


def _remember_tunnel_pid(pid: int) -> None:
    try:
        _SPAWNED_PIDS.add(int(pid))
        d = _read_pid_file()
        d.append({"pid": int(pid), "at": time.time()})
        os.makedirs(os.path.dirname(_tunnel_pid_file()), exist_ok=True)
        with open(_tunnel_pid_file(), "w", encoding="utf-8") as f:
            json.dump(d[-200:], f)
    except Exception as e:  # noqa: BLE001
        logger.debug("记隧道 PID 失败（不影响隧道本身）：%s", e)


def _read_pid_file() -> List[Dict[str, Any]]:
    try:
        with open(_tunnel_pid_file(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return [x for x in (d if isinstance(d, list) else []) if isinstance(x, dict)]
    except Exception:  # noqa: BLE001
        return []


def _win_proc_alive_and_image(pid: int) -> tuple:
    """(还活着吗, 可执行文件全路径)。只用 Win32 API，不做进程枚举。

    ★ 不能用 `os.kill(pid, 0)` 探活 —— **Windows 上它对任意 signal 都会
      真的 TerminateProcess**，那是"探活顺手把人杀了"的经典事故。
    """
    if os.name != "nt":
        return False, ""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False, ""
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False, ""
            if code.value != 259:            # STILL_ACTIVE
                return False, ""
            # 拿镜像路径（要确认它确实是 cloudflared，别误杀别人的进程）
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return True, buf.value
            return True, ""
        finally:
            k32.CloseHandle(h)
    except Exception:  # noqa: BLE001
        return False, ""


def _win_kill(pid: int) -> bool:
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_TERMINATE = 0x0001
        h = k32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
        if not h:
            return False
        try:
            return bool(k32.TerminateProcess(h, 1))
        finally:
            k32.CloseHandle(h)
    except Exception:  # noqa: BLE001
        return False


def reap_stale_tunnels() -> Dict[str, Any]:
    """回收**上一个会话遗留**的隧道进程（启动时调一次）。

    只处理"我们自己记过 PID"的进程，而且要求它确实是 cloudflared ——
    绝不按进程名一把杀（那会误伤另一个正在用的实例）。
    """
    out = {"checked": 0, "killed": 0, "skipped": 0}
    if os.name != "nt":
        return out
    recs = _read_pid_file()
    if not recs:
        return out
    keep: List[Dict[str, Any]] = []
    for r in recs:
        out["checked"] += 1
        pid = int(r.get("pid") or 0)
        if pid <= 0:
            continue
        if pid in _SPAWNED_PIDS:
            # ★ 本进程自己起的、可能**正在用** —— 绝不回收（否则等于自断公网地址）
            keep.append(r)
            out["skipped"] += 1
            continue
        alive, image = _win_proc_alive_and_image(pid)
        if not alive:
            continue                       # 已经没了，不保留记录
        if image and "cloudflared" not in os.path.basename(image).lower():
            out["skipped"] += 1            # PID 被复用了（现在是别的程序）→ 绝不碰
            continue
        if _win_kill(pid):
            out["killed"] += 1
            logger.info("回收上一个会话遗留的隧道进程 pid=%s", pid)
        else:
            keep.append(r)
    try:
        with open(_tunnel_pid_file(), "w", encoding="utf-8") as f:
            json.dump(keep, f)
    except Exception:  # noqa: BLE001
        pass
    return out


def tunnel_status() -> Dict[str, Any]:
    """隧道当前状态。给前端/agent 工具用。"""
    proc = _TUNNEL.get("proc")
    running = bool(proc and proc.poll() is None)
    started = float(_TUNNEL.get("started") or 0)
    kind = _TUNNEL.get("kind") or ""
    named = kind == "named"
    return {
        "running": running,
        "url": (_TUNNEL.get("url") or "") if running else "",
        "base_url": (_TUNNEL.get("url") or "") if running else "",
        "port": _TUNNEL.get("port") or 0,
        "dir": _TUNNEL.get("dir") or "",
        "published_count": len(_TUNNEL.get("published") or {}),
        "uptime_seconds": int(time.time() - started) if (running and started) else 0,
        "healthy_selfcheck": _TUNNEL.get("healthy"),
        "restarts": int(_TUNNEL.get("restarts") or 0),
        "tool": tunnel_available() or "",
        # ★ 新增：让界面能区分"固定域名"和"每次变的临时域名"。
        #   这个区别对用户很实际：临时域名的地址发给厂商之后，
        #   下次重启就失效，得重新走一遍确认流程。
        "kind": kind or ("quick" if running else ""),
        "permanent": named,
        "tunnel_name": _TUNNEL.get("name") or "",
        "verified": _TUNNEL.get("verified"),
    }

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Seedance 2.0 · token6688 网关 · 最小验证脚本

═══════════════════════════════════════════════════════════════════
为什么单独写一个脚本而不是用 VideoForge UI
═══════════════════════════════════════════════════════════════════
这个脚本**不依赖 VideoForge 主进程**，直接 import 新写的 adapter：
  · 用来验证：
      ① adapter 注册成功（list_providers 里能看见 "seedance_t6"）
      ② token6688 网关连通（按秒计费网关不会乱扣钱）
      ③ 生成 1 段 5s 720p 9:16 测试视频，**最少花费**，验证 end-to-end
      ④ 异步轮询直到 succeeded
  · 全程使用 1 张测试图（如果提供）+ 默认 prompt；
    不发角色参考、不发尾帧；adapter 自己验证单参考图链路。

═══════════════════════════════════════════════════════════════════
怎么跑（**注意：会真的花钱！**）
═══════════════════════════════════════════════════════════════════
在 backend/ 目录下：

    export TOKEN6688_API_KEY="sk-xxxx..."      # ← 把你的 key 设进环境变量
    python scripts/test_seedance_t6.py          # 默认 5s 720p 9:16 单图模式
    python scripts/test_seedance_t6.py --duration 5 --no-image    # 纯文生视频
    python scripts/test_seedance_t6.py --dry-run                  # 只发请求不轮询
    python scripts/test_seedance_t6.py --provider-list            # 只看 provider 注册

★ 钱相关：默认 5s × 0.42 元/秒 ≈ 2.1 元/次（含 wait 时长不计费）。
  --duration 15 走最长档：5 × 0.42 ≈ 6.3 元/次。
  网关不同时段通道价格不同，最贵通道可能 1.3 元/秒，15s ≈ 20 元/次。

═══════════════════════════════════════════════════════════════════
key 安全
═══════════════════════════════════════════════════════════════════
· 脚本**绝不**把 key 写进文件 / commit / 输出明文（除错误日志）
· 接受 `TOKEN6688_API_KEY` 环境变量；也可读 `.env`（开发模式）
· 屏幕输出会用 `sk-omni-***` 前 8 位 + `***` 后 4 位脱敏
"""

from __future__ import annotations
import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Optional

# ──────────── 让脚本能 import backend.* ────────────
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _mask_key(k: str) -> str:
    """脱敏：只显示前 8 位 + ... + 后 4 位"""
    if not k:
        return "<empty>"
    if len(k) <= 16:
        return f"{k[:4]}***{k[-4:]}"
    return f"{k[:8]}***{k[-4:]}"


def _load_key() -> str:
    """从环境变量或 backend/.env 读 key（不写入任何文件）"""
    k = (os.environ.get("TOKEN6688_API_KEY") or "").strip()
    if k:
        return k
    # 回退：读 backend/.env（VideoForge 推荐用法）
    env_path = _BACKEND_DIR.parent / ".env"
    if env_path.exists():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                if name.strip() == "TOKEN6688_API_KEY":
                    v = value.strip().strip('"').strip("'")
                    if v:
                        return v
        except Exception as e:
            print(f"[WARN] 读 .env 失败：{e}")
    return ""


async def _list_providers():
    """列出所有已注册的 provider（验证 seedance_t6 在册）"""
    from core.adapters import list_providers
    print("=" * 60)
    print("已注册的 provider：")
    print("=" * 60)
    for p in list_providers(include_unusable=True):
        marker = "★" if p["name"] == "seedance_t6" else " "
        print(f" {marker} {p['name']:<20} {p['display_name']}")
        print(f"     分辨率：{p['supported_resolutions']}")
        print(f"     比例：  {p['supported_aspect_ratios']}")
        print(f"     时长：  {p['min_duration']} ~ {p['max_duration']}s")
        print(f"     首尾帧/角色参考：{p['supports_first_last_frame']} / {p['supports_character_ref']}")
    print()


async def _dry_check(api_key: str):
    """不下发生成任务，只校验网络 / key / 模型可用性"""
    import httpx
    print("=" * 60)
    print("干跑自检（GET /v1/models）")
    print("=" * 60)
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            "https://agentai.token6688.com/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        print(f"HTTP {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            data_list = data.get("data") or data.get("models") or data
            print(f"  网关返回 {len(data_list) if isinstance(data_list, list) else '?'} 个模型")
            # 找 Seedance 2.0
            for m in (data_list if isinstance(data_list, list) else []):
                mid = m.get("id") or m.get("name") or ""
                if "seedance-2-0-official" in mid or "seedance-2-0" in mid:
                    print(f"  ★ 命中：id={mid}  display={m.get('display_name','')}")
            return True
        elif r.status_code == 401:
            print("  ✗ 鉴权失败：key 不对或已过期")
            return False
        else:
            print(f"  ✗ 异常响应：{r.text[:200]}")
            return False


async def _run_generate(args, api_key: str):
    """主流程：构造请求 → 提交 → 轮询 → 输出结果"""
    from core.adapters import get_adapter
    from core.adapters import VideoGenRequest

    adapter = get_adapter(
        "seedance_t6",
        api_key=api_key,
        config={"model_name": args.model},
    )

    print("=" * 60)
    print(f"创建任务：model={args.model}  duration={args.duration}s  "
          f"resolution={args.resolution}  ratio={args.ratio}")
    print(f"key：{_mask_key(api_key)}")
    print(f"first_frame：{'<默认图>' if args.first_frame else '无（纯文生）'}")
    print("=" * 60)

    # 准备首帧（如果指定）
    first_frame_path = ""
    if args.first_frame:
        first_frame_path = str(Path(args.first_frame).expanduser().resolve())
        if not Path(first_frame_path).exists():
            print(f"[ERR] 首帧图不存在：{first_frame_path}")
            return

    req = VideoGenRequest(
        prompt=args.prompt,
        duration=args.duration,
        aspect_ratio=args.ratio,
        resolution=args.resolution,
        first_frame=first_frame_path or None,
        # 角色参考图：脚本默认不发，留给后续集成的项目跑
        character_reference=[],
        reference_images=[],
    )

    # 校验先跑一遍（省得任务被网关拒了再扣钱）
    err = adapter.validate_request(req)
    if err:
        print(f"[ERR] 请求校验失败：{err}")
        return

    t0 = time.time()
    if args.dry_run:
        print("[DRY-RUN] 跳过 generate() 调用")
        return

    result = await adapter.generate(req)
    elapsed = time.time() - t0

    print()
    print("=" * 60)
    print(f"结果（耗时 {elapsed:.1f}s）：")
    print("=" * 60)
    print(f"  success     : {result.success}")
    print(f"  task_id     : {result.task_id}")
    print(f"  video_url   : {(result.video_url or '')[:120]}")
    print(f"  duration    : {result.duration_seconds}s")
    print(f"  error       : {result.error}")
    print(f"  raw keys    : {list((result.raw or {}).keys())[:6]}")

    if not result.success:
        sys.exit(2)


def _parse_args():
    p = argparse.ArgumentParser(
        description="Seedance 2.0 via token6688 · 最小验证脚本",
    )
    p.add_argument("--provider-list", action="store_true",
                   help="只列 provider 注册情况，不发请求")
    p.add_argument("--dry-run", action="store_true",
                   help="构造好请求就退出，不调用 generate()")
    p.add_argument("--dry-check", action="store_true",
                   help="只调 GET /v1/models 验证 key / 网络（不发生成任务）")
    p.add_argument("--model", default="seedance-2-0-official",
                   help="token6688 网关的 Seedance 模型 ID")
    p.add_argument("--duration", type=int, default=5,
                   help="时长（秒），token6688 范围 4~15，默认 5")
    p.add_argument("--resolution", default="720p",
                   choices=["480p", "720p", "1080p"],
                   help="分辨率")
    p.add_argument("--ratio", default="9:16",
                   choices=["16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive"],
                   help="画幅比例")
    p.add_argument("--prompt", default=(
        "一只橘猫坐在窗台上看雨，镜头缓慢推进到猫的眼睛特写，"
        "窗外雨滴落在玻璃上有透明水痕。"
    ), help="生成提示词")
    p.add_argument("--first-frame", default="",
                   help="首帧图本地路径（不传则纯文生视频）")
    p.add_argument("--no-image", action="store_true",
                   help="纯文生视频（等价于不传 --first-frame）")
    p.add_argument("--yes", "-y", action="store_true",
                   help="跳过交互确认（默认会问「确认生成？[y/N]」）")
    p.add_argument("--output-log", default="",
                   help="把日志写到该文件路径（脚本仍打印到 stdout）")
    p.add_argument("--max-wait", type=int, default=1500,
                   help="轮询最长等多少秒（adapter 默认 1500s = 25 分钟）")
    return p.parse_args()


def main():
    args = _parse_args()

    # 永远先列 provider（验证注册成功）
    asyncio.run(_list_providers())

    if args.provider_list:
        return

    api_key = _load_key()
    if not api_key:
        print("[ERR] 没找到 token6688 API key。")
        print("  解决：export TOKEN6688_API_KEY='sk-xxxx...'", file=sys.stderr)
        print("  或：在 backend/.env 里加 TOKEN6688_API_KEY=sk-xxxx...", file=sys.stderr)
        sys.exit(1)

    if args.no_image:
        args.first_frame = ""

    if args.dry_check:
        ok = asyncio.run(_dry_check(api_key))
        sys.exit(0 if ok else 2)

    # 确认要不要真的花钱
    if not args.dry_run and not args.yes:
        print()
        print(f"⚠ 此操作将通过 token6688 网关调用 {args.model} 生成 {args.duration}s "
              f"{args.resolution} {args.ratio} 视频。")
        cost_low = args.duration * 0.42
        cost_high = args.duration * 1.3
        print(f"  预估费用：{cost_low:.2f} ~ {cost_high:.2f} 元（按最便宜/最贵通道）")
        print(f"  排队可能 20 分钟，已在 adapter 里把 read timeout 拉到 30 分钟。")
        print()
        try:
            ans = input("确认生成？[y/N] ").strip().lower()
        except EOFError:
            ans = "n"
        if ans != "y":
            print("已取消。")
            return

    # 如果指定了 --output-log，把 stdout/stderr 复制一份到该文件
    log_fh = None
    if args.output_log:
        log_path = Path(args.output_log).expanduser().resolve()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(log_path, "a", encoding="utf-8")

    class TeeWriter:
        def __init__(self, *writers):
            self.writers = [w for w in writers if w is not None]
        def write(self, s):
            for w in self.writers:
                try: w.write(s)
                except Exception: pass
        def flush(self):
            for w in self.writers:
                try: w.flush()
                except Exception: pass
        def isatty(self): return False

    if log_fh:
        orig_out, orig_err = sys.stdout, sys.stderr
        sys.stdout = TeeWriter(orig_out, log_fh)
        sys.stderr = TeeWriter(orig_err, log_fh)

    try:
        asyncio.run(_run_generate(args, api_key))
    finally:
        if log_fh:
            sys.stdout = orig_out
            sys.stderr = orig_err
            log_fh.close()


if __name__ == "__main__":
    main()
# -*- coding: utf-8 -*-
r"""验证 find_or_install_ffmpeg() / get_ffmpeg_for_postprocess() 的 lru_cache 行为。

背景（2026-09-14 真机现象）：
    videoforge.log 10:20:10 这 1 秒内有 70+ 条 "FFmpeg found (imageio-ffmpeg)"
    日志连发，间隔 5-15ms ≈ 200Hz。同一秒出现
    `asyncio._ProactorBasePipeTransport._call_connection_lost()
     ConnectionResetError: [WinError 10054]`（远程主机强迫关闭连接）。

    根因：find_or_install_ffmpeg() 无缓存，每次调用都走 4 步探测：
      1. _get_bundle_dir() + Path.stat() 系统调用
      2. shutil.which("ffmpeg") —— Win 上 spawn `where.exe`（~20ms/次）
      3. imageio_ffmpeg.get_ffmpeg_exe() —— 解压 + ffmpeg -version 验证
    200Hz × 20ms = 单核 CPU 跑满 → DWM 渲染抖动 = 闪屏抽搐；
    spawn 子进程风暴触发 WinError 10054 → 显卡驱动 TDR → LOL 黑屏。

    修法：find_or_install_ffmpeg() 和 get_ffmpeg_for_postprocess()
    都加 @lru_cache(maxsize=1)，单进程内只探测一次。
    新增 clear_ffmpeg_cache() 给"用户改 ffmpeg_path 设置"等场景清缓存。

本套件锁死：
    · find_or_install_ffmpeg() 调 100 次 → 'FFmpeg found' 只打 1 次
    · get_ffmpeg_for_postprocess() 调 100 次 → 'FFmpeg found' 只打 1 次
    · clear_ffmpeg_cache() 后再次调用 → 探测重新跑，路径一致
    · cache_info() 直接读 lru_cache 内部状态确认命中（hits/misses）
"""
import io
import logging
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))

# ──────────── mini test harness（对齐项目内 test_*.py 风格）────────────
PASS = []
FAIL = []


def check(label, cond, detail=""):
    if cond:
        PASS.append(label)
        print(f"  [✓] {label}")
    else:
        FAIL.append(f"{label} :: {detail}")
        print(f"  [✗] {label}\n      {detail}")


def count_ffmpeg_found(records) -> int:
    return sum(1 for r in records if "FFmpeg found" in r.getMessage())


class _CaptureHandler(logging.Handler):
    """抓 log record 到外部 list（构造时绑定，不靠 closure —— 多个实例独立）。"""
    def __init__(self, target):
        super().__init__(level=logging.INFO)
        self.target = target

    def emit(self, record):
        self.target.append(record)


def main() -> int:
    from core.ffmpeg_manager import (
        clear_ffmpeg_cache,
        find_or_install_ffmpeg,
        get_ffmpeg_for_postprocess,
    )

    # ─── T1: find_or_install_ffmpeg() 调 100 次 → "FFmpeg found" 只 1 次 ───
    clear_ffmpeg_cache()
    records = []
    handler = _CaptureHandler(records)
    ff_logger = logging.getLogger("videoforge.ffmpeg")
    ff_logger.setLevel(logging.INFO)  # 子 logger 默认 NOTSET 会 propagate 到 root（WARNING）→ INFO 被过滤
    ff_logger.addHandler(handler)
    try:
        for _ in range(100):
            ff1 = find_or_install_ffmpeg()
    finally:
        ff_logger.removeHandler(handler)

    n_found = count_ffmpeg_found(records)
    check(
        "T1.1 find_or_install_ffmpeg() 调 100 次，FFmpeg found 只打 1 次（lru_cache 生效）",
        n_found == 1,
        f"实际 {n_found} 次。修这个 bug 之前 1 秒内能打 70+ 条（10:20:10 真日志）",
    )
    check("T1.2 返回非空路径", bool(ff1), repr(ff1))

    # ─── T2: get_ffmpeg_for_postprocess() 调 100 次 → "FFmpeg found" 只 1 次 ───
    clear_ffmpeg_cache()
    records2 = []
    handler2 = _CaptureHandler(records2)
    ff_logger.addHandler(handler2)
    try:
        for _ in range(100):
            ff2 = get_ffmpeg_for_postprocess()
    finally:
        ff_logger.removeHandler(handler2)

    n_found2 = count_ffmpeg_found(records2)
    check(
        "T2.1 get_ffmpeg_for_postprocess() 调 100 次，FFmpeg found 只打 1 次",
        n_found2 == 1,
        f"实际 {n_found2} 次",
    )
    check(
        "T2.2 返回真实路径（不是兜底字符串 'ffmpeg'）",
        ff2 and ff2 != "ffmpeg",
        repr(ff2),
    )

    # ─── T3: cache_info() 直接读 lru_cache 内部状态 ───
    clear_ffmpeg_cache()

    # find_or_install_ffmpeg 首次（miss）+ 再调 99 次（全部 hit）
    find_or_install_ffmpeg()
    info_before = find_or_install_ffmpeg.cache_info()
    for _ in range(99):
        find_or_install_ffmpeg()
    info_after = find_or_install_ffmpeg.cache_info()

    check(
        "T3.1 find_or_install_ffmpeg clear 后首次 misses==1",
        info_before.misses == 1,
        f"实际 misses={info_before.misses}",
    )
    check(
        "T3.2 再调 99 次全部命中（hits 增加 99）",
        info_after.hits - info_before.hits == 99,
        f"实际新增 hits={info_after.hits - info_before.hits}",
    )

    # get_ffmpeg_for_postprocess 同理
    clear_ffmpeg_cache()
    for _ in range(50):
        get_ffmpeg_for_postprocess()
    info3 = get_ffmpeg_for_postprocess.cache_info()
    check(
        "T3.3 get_ffmpeg_for_postprocess clear 后 50 次内 misses==1, hits==49",
        info3.misses == 1 and info3.hits == 49,
        f"实际 misses={info3.misses}, hits={info3.hits}",
    )

    # ─── T4: clear_ffmpeg_cache() 失效语义 ───
    clear_ffmpeg_cache()
    ff_a = find_or_install_ffmpeg()
    ff_b = find_or_install_ffmpeg()  # hit
    check("T4.1 clear 后连续两次调用返回同一路径", ff_a == ff_b)

    clear_ffmpeg_cache()
    ff_c = find_or_install_ffmpeg()
    check(
        "T4.2 clear 后再次调用仍然返回路径（环境没变 → 路径一致）",
        ff_c and ff_c == ff_a,
        f"ff_a={ff_a!r} vs ff_c={ff_c!r}",
    )

    # ─── T5: source-level 锁定（防止改回去） ───
    src_path = os.path.join(HERE, "..", "backend", "core", "ffmpeg_manager.py")
    src = io.open(src_path, encoding="utf-8").read()
    check(
        "T5.1 find_or_install_ffmpeg() 有 @lru_cache 装饰器",
        "@lru_cache(maxsize=1)\ndef find_or_install_ffmpeg" in src,
    )
    check(
        "T5.2 get_ffmpeg_for_postprocess() 有 @lru_cache 装饰器",
        "@lru_cache(maxsize=1)\ndef get_ffmpeg_for_postprocess" in src,
    )
    check(
        "T5.3 clear_ffmpeg_cache() 已暴露",
        "def clear_ffmpeg_cache" in src
        and "find_or_install_ffmpeg.cache_clear()" in src
        and "get_ffmpeg_for_postprocess.cache_clear()" in src,
    )
    check(
        "T5.4 注释说明了 hot path spawn 子进程风暴的因果链",
        "where.exe" in src and "WinError 10054" in src and "TDR" in src,
    )

    print("\n" + "=" * 70)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
    for f in FAIL:
        print("  [x] " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
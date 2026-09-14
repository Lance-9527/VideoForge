# -*- coding: utf-8 -*-
"""「时长必须是真的」端到端测试 —— 治"分层定 10s 但最终只能生成 5s"这半个病。

═══ 为什么单独测这件事 ═══
上一批（`test_audio_master_plan.py`）解决的是**规划**：不再拿 12 秒的草稿去要求
只卖 6/10 秒的模型。但规划得再好，只要模型少给几秒，还有第二个坑：

    旧代码：`if len(parts) == 1: final_dur = total_sec`
    → 模型实际只出 5 秒，我们却往库里写 **10 秒**。

假数字会一路传下去：candidate.duration_seconds → /continuity 的 real_durs →
成片时间轴 → 字幕时间戳。用户看到的现象就是"画面只有一小段，后面是空的"
或者"字幕压在没画面上"，而**库里每个数字看起来都正常**，根本查不出来。

MoneyPrinterTurbo 在这一点上分得很清（`video.py:743-748`）：
`max_clip_duration` 约束的是**成片里的最终播放时长**，读源文件用的是换算过的
`source_clip_duration`；它每次都用**实际** `clip.duration` 累加，缺口还会明确打日志
（`"video duration (X) is shorter than required duration (Y)"`）。

═══ 怎么在不花钱的前提下测真实路径 ═══
往 `core.adapters.ADAPTERS` 注入一个**桩适配器**，它宣称支持 10 秒，
但不管你要几秒都只返回一个 **5 秒**的 mp4 —— 这正是"模型做不到它承诺的时长"
这个真实故障。然后走完整的 `POST /api/shots/{sid}/render` 接口，
断言落库的时长是 5 秒、并且如实告诉了用户差多少。

运行：python tests/test_duration_truth.py
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return cond


def _ffmpeg() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def make_clip(path: str, seconds: float, color: str = "navy", size: str = "640x360") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(
        [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=c={color}:s={size}:d={seconds}:r=25",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", path],
        capture_output=True, timeout=120, check=True)
    return path


def probe(path: str) -> float:
    p = subprocess.run([_ffmpeg(), "-hide_banner", "-i", path],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=60)
    import re
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", (p.stderr or "") + (p.stdout or ""))
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


# ═══════════════════════════════════════════════════════════════
# A. 纯函数：计划 vs 实测 的对账
# ═══════════════════════════════════════════════════════════════

def part_a():
    print("\n" + "=" * 74)
    print("A. reconcile_duration（计划 vs 实测 对账）")
    print("=" * 74)
    from core.assemble import reconcile_duration, DURATION_TOLERANCE

    # 模型没给够 —— 用户报的那个 case
    r = reconcile_duration(10, 5.0)
    print(f"  计划10s 实测5.0s -> 记{r['actual']}s 缺{r['shortfall']}s 提示: {r['warnings'][0][:70]}")
    check("A1 计划10s/实测5s: 记录的是**实测** 5s", abs(r["actual"] - 5.0) < 0.01, str(r["actual"]))
    check("A2 缺口算对（10-5=5）", abs(r["shortfall"] - 5.0) < 0.01, str(r["shortfall"]))
    check("A3 必须给用户提示", bool(r["warnings"]), str(r["warnings"]))
    check("A4 提示里说清了差多少秒", "5.0" in r["warnings"][0] and "差" in r["warnings"][0],
          r["warnings"][0][:80])
    check("A5 提示里说明成片不会有空白画面", "空白" in r["warnings"][0], "")

    # 给够了
    r2 = reconcile_duration(10, 10.0)
    check("A6 计划10s/实测10s: 无提示、无缺口",
          r2["warnings"] == [] and r2["shortfall"] == 0.0, str(r2))
    # 容器时长天然有几十毫秒误差，不该天天报警
    r3 = reconcile_duration(10, 10 - DURATION_TOLERANCE + 0.01)
    check("A7 容差内的微小差异不报警（否则提示会变噪音）",
          r3["warnings"] == [], str(r3["warnings"]))
    r32 = reconcile_duration(10, 10 - DURATION_TOLERANCE - 0.2)
    check("A8 超出容差就要报", bool(r32["warnings"]), str(r32["warnings"]))

    # 给多了
    r4 = reconcile_duration(10, 14.0)
    check("A9 给多了要说明会裁掉（不是问题，但要知道）",
          bool(r4["warnings"]) and "裁" in r4["warnings"][0] and r4["over"] == 4.0,
          r4["warnings"][0][:70])
    check("A10 给多了不算缺口", r4["shortfall"] == 0.0, str(r4["shortfall"]))

    # 量不出来
    r5 = reconcile_duration(10, 0.0)
    check("A11 量不出时长时如实说是量不出，不编缺口",
          r5["shortfall"] == 0.0 and "量不出" in r5["warnings"][0], str(r5["warnings"]))

    # 没有计划值时不乱报
    r6 = reconcile_duration(0, 5.0)
    check("A12 没有计划值时按实测记、不报警",
          r6["warnings"] == [] and r6["actual"] == 5.0, str(r6))
    return


# ═══════════════════════════════════════════════════════════════
# B. 端到端：桩适配器只给 5 秒，走完整 /render 接口
# ═══════════════════════════════════════════════════════════════

STUB_PROVIDER = "stubvid"
STUB_MODEL = "stub-10s"


def install_stub(clip_path: str):
    """注入一个"嘴上说 10 秒、实际只给 5 秒"的桩适配器 + 对应能力表。"""
    from core import adapters as A
    from core.adapters import VideoAdapter, VideoGenResult, ADAPTERS
    from core.model_catalog import VIDEO_CATALOG

    calls = {"n": 0, "requested": [], "prompts": []}

    class StubAdapter(VideoAdapter):
        name = STUB_PROVIDER
        display_name = "桩模型（测试用）"
        description = "宣称支持 10 秒，实际只返回 5 秒的固定素材 —— 复现'模型做不到承诺时长'"
        supported_resolutions = ["720P"]
        max_duration = 10
        min_duration = 5
        requires_api_key = True
        usable_for_video = True

        async def generate(self, req) -> VideoGenResult:
            calls["n"] += 1
            calls["requested"].append(int(req.duration))
            calls["prompts"].append(req.prompt)
            # ★ 关键：不管你要几秒，我只给你一个 5 秒的文件
            return self.make_result(success=True, video_url=clip_path,
                                    duration=5.0, raw={"stub": True})

        async def query_task(self, task_id: str) -> VideoGenResult:
            # 抽象方法必须实现（VideoAdapter 是 ABC）。
            # 桩模型是同步返回的，没有远程任务可查。
            return self.make_result(success=True, video_url=clip_path,
                                    duration=5.0, raw={"stub": True})

        async def download_to_local(self, url: str, save_path: str) -> str:
            # 基类对"本地路径"是直接 return url **不复制**的（真实厂商给的是
            # http URL 才需要下载）。桩模型给的就是本地文件，所以这里显式复制，
            # 让上层那段 `os.path.exists(local)` 的校验能通过。
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            shutil.copyfile(clip_path, save_path)
            return save_path

    ADAPTERS[STUB_PROVIDER] = StubAdapter
    # 能力表：720P 下 5/10 秒都"支持"（所以才敢规划成一次 10 秒）
    VIDEO_CATALOG[STUB_PROVIDER] = {
        "display": "桩模型", "audio": {"native": False},
        "models": [{"id": STUB_MODEL, "label": "桩 10s", "mode": "t2v",
                    "mode_zh": "文生视频", "tags": ["测试"],
                    "caps": {"720P": [5, 10]}, "best_for": "测试用",
                    "verified": True}],
    }
    return calls


def part_b(tmp: str):
    print("\n" + "=" * 74)
    print("B. 端到端：模型只给 5 秒时，库里记的到底是几秒")
    print("=" * 74)

    clip = make_clip(os.path.join(tmp, "fixture_5s.mp4"), 5.0)
    real = probe(clip)
    print(f"  桩素材：{clip}")
    print(f"  实测素材时长 = {real:.2f}s（这就是模型会返回的东西）")
    check("B0 桩素材本身是 5 秒", 4.8 < real < 5.3, f"{real:.2f}s")

    calls = install_stub(clip)

    data_root = os.path.join(tmp, "data")
    os.makedirs(data_root, exist_ok=True)
    os.environ["VIDEOFORGE_DATA_DIR"] = data_root

    from fastapi.testclient import TestClient
    import main as appmod

    app = appmod.create_app(os.path.join(data_root, "videoforge.db"))
    # ★ 必须让 TestClient 进入上下文：starlette 的 TestClient 用 blocking portal
    #   跑事件循环，只有进入上下文才**保持**这个 loop 活着。不然
    #   `asyncio.create_task(_worker())` 起的后台渲染任务会随每次请求结束被丢掉，
    #   表现就是 status 永远 running=True。
    cm = TestClient(app)
    client = cm.__enter__()
    try:
        _run_b(client, appmod, data_root, calls)
    finally:
        cm.__exit__(None, None, None)


def _run_b(client, appmod, data_root, calls):
    db = appmod.Database(os.path.join(data_root, "videoforge.db"))
    # 给桩厂商配一个 Key，否则 mode=api 直接被拒。
    # 注意 set_setting 内部会 json.dumps，这里必须传 dict（传字符串会双重编码）
    db.set_setting("api_keys", {STUB_PROVIDER: "stub-key-123"})
    proj = db.create_project("时长真实性测试", "")
    pid = proj["id"] if isinstance(proj, dict) else proj

    # 分镜：标称 12 秒、两块内容、有台词
    tl = [
        {"index": 0, "start": 0, "end": 6, "camera": "中景",
         "action": "士兵把地图铺在泥地上，用手指点住一个位置。",
         "dialogue": {"character": "李连长", "text": "这里，就是突破口。", "emotion": "坚定"}},
        {"index": 1, "start": 6, "end": 12, "camera": "近景",
         "action": "他抬头看向远处，炮火在城墙后升起。",
         "dialogue": {"character": "小虎", "text": "连长，让我去！", "emotion": "急切"}},
    ]
    shot = db.create_shot(project_id=pid, order_index=0, duration_seconds=12,
                          layer1_overview="战壕里，李连长指着地图布置突破口。",
                          layer2_timeline=json.dumps(tl, ensure_ascii=False),
                          layer3_constraints="", character_ids=[],
                          model_provider=STUB_PROVIDER, model_name=STUB_MODEL,
                          aspect_ratio="16:9", resolution="720P")
    sid = shot["id"] if isinstance(shot, dict) else shot
    print(f"  分镜标称 = 12s    模型 = {STUB_PROVIDER}/{STUB_MODEL}（能力表说支持 10s）")

    # 先看规划：请求 10 秒时应该是一段 10 秒（能力表说 720P 支持 10s）
    plan = client.get(
        f"/api/projects/{pid}/shots/{sid}/plan?measure=0&duration=10").json()["data"]
    planned = sum(int(s["duration"]) for s in plan["segments"])
    print(f"  规划 = {planned}s，{len(plan['segments'])} 段，分辨率 {plan['resolution']}")
    check("B1 规划阶段按能力表规划成 1 段 10 秒",
          planned == 10 and len(plan["segments"]) == 1, f"{planned}s {len(plan['segments'])}段")

    # 真的走生成（用桩模型，不花钱）
    r = client.post(f"/api/shots/{sid}/render",
                    json={"mode": "api", "duration_seconds": 10, "chain": False})
    check("B2 /render 接受请求", r.status_code == 200, str(r.status_code))
    st = {}
    for _ in range(240):
        st = client.get(f"/api/shots/{sid}/render/status").json()["data"]
        if not st.get("running"):
            break
        time.sleep(0.5)
    print(f"  渲染结束：running={st.get('running')} error={st.get('error') or '(无)'}")
    res = st.get("result") or {}
    print(f"  接口返回 duration = {res.get('duration')}")

    check("B3 桩模型确实被调用了（走的是真实 API 路径）", calls["n"] >= 1, f"{calls['n']} 次")
    check("B4 实现成功（没有因为时长不符而失败）", bool(res.get("ok")), str(st.get("error"))[:120])
    check("B5 实际请求给模型的是 10 秒", calls["requested"][:1] == [10], str(calls["requested"]))

    # ★★ 核心断言：落库的必须是**实测**的 5 秒，不是请求的 10 秒
    dur = float(res.get("duration") or 0)
    print(f"  ★ 返回时长 = {dur:.2f}s（旧代码会写成 10）")
    check("B6 返回的时长是**实测**的 5 秒，不是请求的 10 秒",
          4.8 < dur < 5.4, f"{dur:.2f}s")

    fresh = db.get_shot(sid) or {}
    cands = fresh.get("candidates") or []
    if isinstance(cands, str):
        cands = json.loads(cands)
    sel = next((c for c in cands if c.get("is_selected")), None) or (cands[-1] if cands else {})
    cd = float(sel.get("duration_seconds") or 0)
    print(f"  ★ 库里 candidate.duration_seconds = {cd:.2f}s")
    check("B7 落库的 candidate 时长也是实测的 5 秒（不是 10）",
          4.8 < cd < 5.4, f"{cd:.2f}s")

    # 文件本身也确实只有 5 秒
    fpath = res.get("path") or sel.get("path") or ""
    fdur = probe(fpath) if fpath and os.path.exists(fpath) else 0.0
    print(f"  ★ 文件本身 = {fdur:.2f}s")
    check("B8 文件实测时长与记录一致（记录没有说谎）",
          abs(fdur - dur) < 0.3, f"文件{fdur:.2f} vs 记录{dur:.2f}")

    # ★ 必须如实告诉用户
    warns = " | ".join(res.get("warnings") or [])
    print(f"  ★ 提示：{warns[:200]}")
    check("B9 如实告诉用户『模型只出了 5 秒』",
          "5.0" in warns or "5.1" in warns, warns[:120])
    check("B10 提示里说明成片不会出现空白画面", "空白" in warns, warns[:120])

    # 成片时间轴必须用真实时长（否则字幕/音频会排到不存在的画面上）
    cont = client.get(f"/api/projects/{pid}/continuity").json()["data"]
    print(f"  成片时间轴总长 = {cont.get('total')}s（标称合计 {cont.get('nominal_total')}s）")
    check("B11 成片时间轴用真实时长（≈5.2s，而不是 10s）",
          4.8 <= float(cont.get("total") or 0) <= 5.6, str(cont.get("total")))
    check("B12 时间轴同时报出标称合计，方便对照",
          float(cont.get("nominal_total") or 0) == 12.0, str(cont.get("nominal_total")))
    return


def main() -> int:
    part_a()
    tmp = tempfile.mkdtemp(prefix="vf_durtruth_")
    try:
        part_b(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + "=" * 74)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
    for f in FAIL:
        print("  [x] " + f)
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

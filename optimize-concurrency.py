"""性能优化 ①：出图/白膜**并发化** + 逐个回报进度。

实测背景（2026-10-22，用户反馈"出一组图片卡了至少 10 分钟"）：
  · 出图是**串行**的：\`for kind, obj in batch: await _gen_asset_image(...)\`
    一批 6 张 = 6 × 单张耗时（单张 12~60 秒，Qwen-Image 慢的时候更久），
    而且**中途不回传任何进度**（只在最后一次性更新）⇒ 用户只看到"卡住"。
  · 白膜也是串行：每镜一次 Blender 进程（360 帧 ≈ 54 秒），4 镜 ≈ 3.6 分钟。

改法：
  · 出图：asyncio.Semaphore(3) + gather（并发 3：Qwen-Image 侧不至于被限流，
    又能把 6 张从 6T 压到 ~2T）；**每张完成立刻回报进度**。
  · 白膜：并发 2（Blender 是 CPU/内存大户，2 路是安全值），同样逐个回报。
"""
import shutil
import time
from pathlib import Path

STAMP = time.strftime('%Y%m%d-%H%M%S')
p = Path(r'D:\VideoForge-dev\backend\core\agent_tools_stages.py')
text = p.read_text(encoding='utf-8')
shutil.copyfile(p, p.with_suffix(p.suffix + '.bak-' + STAMP))

# ── ① 出图：串行 → 并发（3 路）─────────────────────────────────────────
old_looks = """    made, failed = [], []
    for kind, obj in batch:
        r = await _gen_asset_image(db, pid, kind, obj, params)
        if r.get("ok"):
            made.append({"kind": kind, "name": obj.get("name"),
                         "path": r.get("path")})
        else:
            failed.append({"kind": kind, "name": obj.get("name"),
                           "error": r.get("error")})

    left = len(todo) - len(batch)"""
new_looks = """    # ★ 2026-10-22：**并发出图**（原来串行，6 张要 6×单张耗时，用户反馈"卡了 10 分钟"）。
    #   并发度 3：Qwen-Image 侧不易被限流，又能把批次耗时压到 ~1/3。
    #   逐张完成即回报进度（progress 回调），让用户看到"第 2/6 张完成"而不是干等。
    import asyncio as _aio

    made, failed = [], []
    _done = {"n": 0}
    _total = len(batch)
    _lock = _aio.Lock()

    async def _one(kind: str, obj: Dict[str, Any]) -> None:
        r = await _gen_asset_image(db, pid, kind, obj, params)
        async with _lock:
            _done["n"] += 1
            if r.get("ok"):
                made.append({"kind": kind, "name": obj.get("name"),
                             "path": r.get("path")})
            else:
                failed.append({"kind": kind, "name": obj.get("name"),
                               "error": r.get("error")})
            _n = _done["n"]
        # 进度回报：一张完成就报一次
        try:
            _prog = params.get("_progress")
            if callable(_prog):
                _prog(_n / max(1, _total),
                      f"出图 {_n}/{_total}"
                      + (f"（{obj.get('name')}）" if obj.get("name") else ""))
        except Exception:  # noqa: BLE001
            pass

    _sem = _aio.Semaphore(3)

    async def _guarded(kind: str, obj: Dict[str, Any]) -> None:
        async with _sem:
            await _one(kind, obj)

    await _aio.gather(*[_guarded(k, o) for k, o in batch])

    left = len(todo) - len(batch)"""
if old_looks in text:
    text = text.replace(old_looks, new_looks, 1)
    print('① 出图已并发化（3 路 + 逐张回报进度）')
else:
    print('① 未匹配出图串行段')

# ── ② 白膜：串行 → 并发（2 路）────────────────────────────────────────
old_previz = """    made, failed = [], []
    for s in batch:
        try:
            r = await _dispatch("render_previz", {"project_id": pid,
                                                  "shot_id": s.get("id")})
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if r.get("ok"):"""
new_previz = """    # ★ 2026-10-22：**白膜并发**（原来串行，每镜一次 Blender 进程 ≈ 54 秒）。
    #   并发度 2：Blender 是 CPU/内存大户，2 路在普通机器上安全，且把 4 镜从 ~3.6 分钟压到 ~1.8 分钟。
    import asyncio as _aio2

    made, failed = [], []
    _pdone = {"n": 0}
    _plock = _aio2.Lock()
    _psem = _aio2.Semaphore(2)

    async def _render_one(s: Dict[str, Any]) -> None:
        try:
            r = await _dispatch("render_previz", {"project_id": pid,
                                                  "shot_id": s.get("id")})
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        async with _plock:
            _pdone["n"] += 1
            try:
                _prog = params.get("_progress")
                if callable(_prog):
                    _prog(_pdone["n"] / max(1, len(batch)),
                           f"白膜 {_pdone['n']}/{len(batch)}")
            except Exception:  # noqa: BLE001
                pass
        return r

    async def _guarded_previz(s: Dict[str, Any]) -> Dict[str, Any]:
        async with _psem:
            return await _render_one(s)

    _results = await _aio2.gather(*[_guarded_previz(s) for s in batch])
    for _r in _results:
        r = _r if isinstance(_r, dict) else {"ok": False, "error": "白膜渲染无返回"}
        if r.get("ok"):"""
if old_previz in text:
    text = text.replace(old_previz, new_previz, 1)
    print('② 白膜已并发化（2 路 + 逐镜回报进度）')
else:
    print('② 未匹配白膜串行段')

p.write_text(text, encoding='utf-8')
import py_compile
py_compile.compile(str(p), doraise=True)
print('语法 OK')

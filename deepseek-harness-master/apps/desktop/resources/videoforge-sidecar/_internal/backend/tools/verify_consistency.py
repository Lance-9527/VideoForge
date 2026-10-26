#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""白膜 / 480p 成片一致性验收 CLI（只读，不联网、不调用付费 API）。

用法（在 VideoForge 仓库根目录运行）：

    python -X utf8 backend/tools/verify_consistency.py \
        --spec  <spec.json> --index 1 \
        --whitebox <白膜.mp4> --generated <480p.mp4> [--delivery <交付.mp4>]

可选：
    --script <剧本.txt>        项目剧本全文（让契约能从文字里读到 arc_degrees / no_zoom 意图）
    --text "<一句话>"          直接给意图文字（优先级低于 --script）
    --camera-samples <json>   渲染器逐帧相机采样（有则契约用「实测机位」而不是 spec 声明值）
    --reference-image <path>  白膜之外的参考图（只登记，不做语义判断）
    --thresholds '{"silhouette_iou_min":0.45}'   覆盖阈值
    --compact                 单行 JSON（默认缩进 2）

输出：一份 JSON（stdout）。退出码 0=全绿；2=有 failures；3=工作性错误（文件/ffmpeg）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

try:
    from core.shot_contract import build_contract  # type: ignore
except ImportError:  # pragma: no cover - 独立脚本运行
    from shot_contract import build_contract  # type: ignore

from core.shot_consistency import (  # noqa: E402
    ConsistencyError,
    evaluate_delivery,
    evaluate_generated,
    evaluate_whitebox,
)


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _script_text(args: argparse.Namespace) -> str:
    if args.script:
        return Path(args.script).read_text(encoding="utf-8", errors="replace")
    return args.text or ""


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="VideoForge 白膜/成片一致性验收（契约→白膜→480p→交付）")
    parser.add_argument("--spec", required=True, help="白膜 spec.json（含 shots[]）")
    parser.add_argument("--index", type=int, default=1, help="第几镜，1 起")
    parser.add_argument("--whitebox", help="白膜 mp4")
    parser.add_argument("--generated", help="480p 成片 mp4")
    parser.add_argument("--delivery", help="交付成片 mp4（可选，单独跑 C 组）")
    parser.add_argument("--script", help="剧本文本文件（可选）")
    parser.add_argument("--text", help="意图文字（可选）")
    parser.add_argument("--camera-samples", help="camera-samples.json（可选）")
    parser.add_argument("--reference-image", help="白膜之外的参考图（只登记，不做语义）")
    parser.add_argument("--thresholds", help="阈值覆盖，JSON 字符串")
    parser.add_argument("--compact", action="store_true", help="单行 JSON")
    args = parser.parse_args(argv)

    overrides: Optional[Dict[str, float]] = None
    if args.thresholds:
        try:
            overrides = {str(k): float(v) for k, v in json.loads(args.thresholds).items()}
        except (ValueError, TypeError) as exc:
            print(json.dumps({"ok": False, "error": f"--thresholds 解析失败: {exc}"},
                             ensure_ascii=False), file=sys.stdout)
            return 3

    try:
        spec = _load_json(args.spec)
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": f"spec 读取失败: {exc}"}, ensure_ascii=False))
        return 3
    shots = (spec or {}).get("shots") or []
    index = int(args.index)
    if not shots:
        print(json.dumps({"ok": False, "error": "spec 里没有 shots[]"}, ensure_ascii=False))
        return 3
    if not 1 <= index <= len(shots):
        print(json.dumps({"ok": False, "error": f"--index 越界：1..{len(shots)}"},
                         ensure_ascii=False))
        return 3
    shot = shots[index - 1]

    camera_samples = None
    if args.camera_samples:
        try:
            camera_samples = _load_json(args.camera_samples)
        except (OSError, ValueError) as exc:
            print(json.dumps({"ok": False, "error": f"camera-samples 读取失败: {exc}"},
                             ensure_ascii=False))
            return 3

    out: Dict[str, Any] = {
        "spec": str(Path(args.spec).resolve()),
        "index": index,
        "shot_note": shot.get("note"),
    }
    try:
        contract = build_contract(shot, shot, _script_text(args),
                                  camera_samples=camera_samples)
        out["contract"] = {k: contract.get(k) for k in (
            "duration", "fps", "movement", "movement_zh", "arc_degrees",
            "arc_degrees_intent", "radius_m", "radius_drift_ratio", "lens_mm",
            "lens_declared_mm", "lens_rewritten", "camera_height_m", "look_at",
            "subject_height_ratio", "subject_on_surface", "subject_contact_ok",
            "no_zoom_confirmed", "conflicts", "thresholds")}
        out["whitebox_spec"] = evaluate_whitebox(shot, contract, thresholds=overrides)
        if args.whitebox or args.generated:
            if not (args.whitebox and args.generated):
                out["generated"] = {
                    "ok": False,
                    "metrics": {},
                    "failures": [{"name": "cli_args", "want": "--whitebox 与 --generated 成对",
                                  "got": "", "note": ""}],
                    "limitations": [],
                }
            else:
                out["generated"] = evaluate_generated(
                    args.whitebox, args.generated, contract, thresholds=overrides)
        if args.delivery:
            out["delivery"] = evaluate_delivery(
                args.delivery, contract, thresholds=overrides,
                reference_image=args.reference_image)
        elif args.reference_image:
            out["delivery_limitation"] = (
                "提供了参考图但没给 --delivery；参考图只登记、不做语义判断")
    except ConsistencyError as exc:
        out = {"ok": False, "error": str(exc), "spec": out.get("spec"), "index": index}
        print(json.dumps(out, ensure_ascii=False, indent=None if args.compact else 2))
        return 3

    checks = [out[k] for k in ("whitebox_spec", "generated", "delivery") if k in out]
    all_ok = all(bool(c.get("ok")) for c in checks) if checks else False
    out["ok"] = all_ok
    print(json.dumps(out, ensure_ascii=False, indent=None if args.compact else 2))
    return 0 if all_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())

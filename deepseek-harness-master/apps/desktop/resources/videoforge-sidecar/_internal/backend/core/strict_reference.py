"""Conservative, full-timeline visual-reference verification. Never generates media.

This verifies visible structure against the actual final file. It cannot recover
3D world coordinates, name objects, or prove arbitrary semantic equivalence.
Unknown is a separate state and must not be treated as verified delivery.
"""
from __future__ import annotations

from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

import numpy as np

SCHEMA = "videoforge.strict-reference/1"
MAX_ANALYSIS_WIDTH = 960
EDGE_DISTANCE_PX = 1
MAX_EDGE_MISMATCH_RATIO = 0.03
MAX_LOCAL_EDGE_MISMATCH_RATIO = 0.15
MIN_LOCAL_BAD_EDGE_PIXELS = 12
MIN_EDGE_PIXELS = 30
TIMESTAMP_EPSILON = 0.0000011  # ffprobe prints timestamps to six decimal places.


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _process_options() -> dict:
    options: dict[str, Any] = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    try:
        from .proc import child_env
        options["env"] = child_env()
    except ImportError:
        pass
    return options


def _executable(name: str) -> str:
    return os.environ.get("VIDEOFORGE_" + name.upper()) or shutil.which(name) or name


def _probe(path: Path) -> dict:
    command = [_executable("ffprobe"), "-v", "error", "-count_frames", "-show_streams", "-show_frames",
               "-show_entries", "stream=codec_type,width,height,avg_frame_rate,r_frame_rate,duration,nb_read_frames:"
               "frame=media_type,best_effort_timestamp_time,pkt_duration_time", "-of", "json", str(path)]
    result = subprocess.run(command, capture_output=True, timeout=180, **_process_options())
    if result.returncode:
        raise ValueError("无法探测视频流与逐帧时间戳")
    data = json.loads(result.stdout)
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise ValueError("没有视频流")
    fps = Fraction(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0")
    width, height = int(video["width"]), int(video["height"])
    timestamps = [float(f["best_effort_timestamp_time"]) for f in data.get("frames", [])
                  if f.get("media_type") == "video" and "best_effort_timestamp_time" in f]
    count = int(video.get("nb_read_frames") or len(timestamps))
    if fps <= 0 or count < 1 or width < 2 or height < 2 or len(timestamps) != count:
        raise ValueError("帧率/实际解码帧数/逐帧时间戳不完整")
    return {"width": width, "height": height, "fps": float(fps), "fps_fraction": str(fps),
            "frames": count, "duration": count / float(fps), "stream_duration": video.get("duration"),
            "aspect_fraction": str(Fraction(width, height)), "timestamps": timestamps,
            "has_audio": any(s.get("codec_type") == "audio" for s in streams)}


def _failure(code: str, *, frame: int | None = None, **details) -> dict:
    return {"code": code, **({"frame": frame} if frame is not None else {}), **details}


def _blur(gray: np.ndarray) -> np.ndarray:
    padded = np.pad(gray, 1, mode="edge")
    return sum(padded[y:y + gray.shape[0], x:x + gray.shape[1]] for y in range(3) for x in range(3)) / 9


def _edges(gray: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    smooth = _blur(gray)
    p = np.pad(smooth, 1, mode="edge")
    gx = p[:-2, 2:] + 2 * p[1:-1, 2:] + p[2:, 2:] - p[:-2, :-2] - 2 * p[1:-1, :-2] - p[2:, :-2]
    gy = p[2:, :-2] + 2 * p[2:, 1:-1] + p[2:, 2:] - p[:-2, :-2] - 2 * p[:-2, 1:-1] - p[:-2, 2:]
    magnitude = np.hypot(gx, gy)
    threshold = max(10.0, float(np.percentile(magnitude, 99)) * 0.12)
    mask = magnitude > threshold
    mask[:2] = mask[-2:] = False
    mask[:, :2] = mask[:, -2:] = False
    return magnitude, mask


def _dilate(mask: np.ndarray) -> np.ndarray:
    padded = np.pad(mask, EDGE_DISTANCE_PX)
    return np.logical_or.reduce([padded[y:y + mask.shape[0], x:x + mask.shape[1]]
                                 for y in range(3) for x in range(3)])


def _subject(gray: np.ndarray) -> dict | None:
    # Auxiliary measurement only: a lit face is not a semantic object silhouette.
    from .shot_consistency import _segment_subject
    ys = np.linspace(0, gray.shape[0] - 1, min(180, gray.shape[0])).astype(int)
    xs = np.linspace(0, gray.shape[1] - 1, min(320, gray.shape[1])).astype(int)
    image = gray[np.ix_(ys, xs)]
    segment = _segment_subject(image)
    if not segment or segment["p99"] - segment["median"] < 12 or segment["fill"] < 0.80:
        return None
    x0, y0, x1, y1 = segment["bbox"]
    return {"bbox": [x0 / len(xs), y0 / len(ys), (x1 + 1) / len(xs), (y1 + 1) / len(ys)],
            "center": [segment["cx"] / len(xs), segment["cy"] / len(ys)],
            "width": segment["width_ratio"], "height": segment["height_ratio"],
            "method": "lit_component_auxiliary", "semantic_identity_verified": False}


class _Decoder:
    def __init__(self, path: Path, width: int, height: int):
        self.width, self.height = width, height
        command = [_executable("ffmpeg"), "-v", "error", "-i", str(path), "-map", "0:v:0", "-an",
                   "-vf", f"scale={width}:{height}:flags=area", "-vsync", "0", "-pix_fmt", "rgb24",
                   "-f", "rawvideo", "pipe:1"]
        self.child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **_process_options())

    def frame(self) -> np.ndarray | None:
        need = self.width * self.height * 3
        chunks, received = [], 0
        while received < need:
            chunk = self.child.stdout.read(need - received)
            if not chunk:
                if not received:
                    return None
                raise ValueError("解码得到不完整帧")
            chunks.append(chunk)
            received += len(chunk)
        return np.frombuffer(b"".join(chunks), dtype=np.uint8).reshape(self.height, self.width, 3)

    def close(self):
        if self.child.poll() is None:
            self.child.kill()
        self.child.wait(timeout=10)
        self.child.stdout.close()


def _compare(index: int, left: np.ndarray, right: np.ndarray, previous: tuple | None) -> tuple[dict, list, bool, tuple]:
    gray_l, gray_r = left.astype(np.float32).mean(axis=2), right.astype(np.float32).mean(axis=2)
    mag_l, edge_l = _edges(gray_l)
    mag_r, edge_r = _edges(gray_r)
    count_l, count_r = int(edge_l.sum()), int(edge_r.sum())
    missing = edge_l & ~_dilate(edge_r)
    extra = edge_r & ~_dilate(edge_l)
    missing_ratio = float(missing.sum()) / max(1, count_l)
    extra_ratio = float(extra.sum()) / max(1, count_r)
    exact = bool(np.array_equal(left, right))
    contrast_l = float(np.percentile(gray_l, 99) - np.percentile(gray_l, 1))
    contrast_r = float(np.percentile(gray_r, 99) - np.percentile(gray_r, 1))
    # A dense near-photometric transport is stronger evidence than an unstable
    # lit-face segmenter. This has no spatial transform: only fit global affine
    # exposure, then bound the residual of the actual same-index pixel pairs.
    mean_l, mean_r = float(gray_l.mean()), float(gray_r.mean())
    variance = float(np.mean((gray_l - mean_l) ** 2))
    gain = float(np.mean((gray_l - mean_l) * (gray_r - mean_r))) / max(variance, 1e-9)
    offset = mean_r - gain * mean_l
    residual = np.abs(gray_r - (gray_l * gain + offset))
    p99, p999 = [float(v) for v in np.percentile(residual, [99, 99.9])]
    near_transport = bool(min(contrast_l, contrast_r) >= 12 and 0.5 <= gain <= 2
                          and float(residual.mean()) <= 1.5 and p99 <= 4 and p999 <= 8)
    confident = exact or min(count_l, count_r) >= MIN_EDGE_PIXELS and min(contrast_l, contrast_r) >= 12
    row = {"frame": index, "exact_decoded_pixels": exact, "edge_pixels_reference": count_l,
           "edge_pixels_candidate": count_r, "missing_edge_ratio": missing_ratio, "extra_edge_ratio": extra_ratio,
           "confidence": "high" if confident else "low", "local_bad_tiles": [],
           "near_photometric_transport": near_transport, "photometric_residual_p99": p99,
           "photometric_residual_p999": p999, "photometric_gain": gain, "photometric_offset": offset}
    failures = []
    if missing_ratio > MAX_EDGE_MISMATCH_RATIO or extra_ratio > MAX_EDGE_MISMATCH_RATIO:
        failures.append(_failure("visible_structure_mismatch", frame=index,
                                 missing_ratio=missing_ratio, extra_ratio=extra_ratio))
    for ty, rows in enumerate(np.array_split(np.arange(edge_l.shape[0]), 6)):
        for tx, columns in enumerate(np.array_split(np.arange(edge_l.shape[1]), 8)):
            if not len(rows) or not len(columns):
                continue
            selection = np.ix_(rows, columns)
            unmatched = int(missing[selection].sum() + extra[selection].sum())
            total = max(1, int(edge_l[selection].sum() + edge_r[selection].sum()))
            if unmatched > MIN_LOCAL_BAD_EDGE_PIXELS and unmatched / total > MAX_LOCAL_EDGE_MISMATCH_RATIO:
                row["local_bad_tiles"].append({"x": tx, "y": ty, "unmatched": unmatched, "ratio": unmatched / total})
    if row["local_bad_tiles"]:
        failures.append(_failure("local_structure_mismatch", frame=index, tiles=row["local_bad_tiles"]))
    subject_l, subject_r = _subject(gray_l), _subject(gray_r)
    row["subject_reference"], row["subject_candidate"] = subject_l, subject_r
    if subject_l and subject_r:
        center_delta = float(np.linalg.norm(np.array(subject_l["center"]) - np.array(subject_r["center"])))
        size_delta = max(abs(subject_l["width"] - subject_r["width"]), abs(subject_l["height"] - subject_r["height"]))
        row.update(subject_center_delta=center_delta, subject_size_delta=size_delta)
        if (center_delta > 0.015 or size_delta > 0.02) and not exact and not near_transport:
            failures.append(_failure("subject_measurement_mismatch", frame=index, center_delta=center_delta,
                                     size_delta=size_delta, note="受光区域辅助测量，不能单独证明语义或完整物体轮廓"))
    elif not exact:
        row["subject_measurement"] = "unverified"
        # Structure itself can verify a near-exact transport with unchanged contours;
        # ambiguous cross-material segmentation must stay unverified.
        if not near_transport and (missing_ratio > 0.005 or extra_ratio > 0.005):
            confident = False
            row["confidence"] = "low"
    feature_l = _blur(gray_l)[::4, ::4]
    feature_r = _blur(gray_r)[::4, ::4]
    # Normalize affine illumination, then compare temporal content change at the same index.
    def normalize(feature):
        return (feature - feature.mean()) / max(1.0, float(feature.std()))
    norm_l, norm_r = normalize(feature_l), normalize(feature_r)
    if previous is not None:
        prev_l, prev_r = previous
        step_l = float(np.mean(np.abs(norm_l - prev_l)))
        step_r = float(np.mean(np.abs(norm_r - prev_r)))
        row.update(reference_step=step_l, candidate_step=step_r)
        if step_l > 0.025 and step_r < max(0.001, step_l * 0.05):
            failures.append(_failure("unexpected_hold_or_repeated_frame", frame=index,
                                     reference_step=step_l, candidate_step=step_r))
    row["failures"] = [f["code"] for f in failures]
    return row, failures, confident, (norm_l, norm_r)


def evaluate_reference(reference: str, candidate: str, *, contract: dict | None = None,
                       output_report: str | None = None) -> dict:
    """Compare every same-index frame of final media; unknown never returns ok=True.

    Contract supports duration (seconds), fps (number or rational), aspect_ratio
    ("16:9" or numeric), resolution ([width,height]), and silent (bool). No crop,
    alignment, retiming, model-name exception, or permissive threshold override.
    """
    contract = dict(contract or {})
    result = {"schema": SCHEMA, "ok": False, "status": "unverified", "failures": [], "per_frame": [],
              "metrics": {}, "provenance": {"verifier_sha256": _sha(Path(__file__)), "contract": contract},
              "limitations": ["只验证可见结构与时间对应，不恢复三维相机世界坐标，不识别物体名称。",
                               "材质纹理、阴影和低对比画面无法与几何变化可靠分离时会保守拒收或标记未验证。",
                               "边缘容差为分析画布1像素；不会以全片平均分掩盖坏帧。"]}
    paths = [Path(reference).resolve(), Path(candidate).resolve()]
    decoders = []
    try:
        for label, path in zip(("reference", "candidate"), paths):
            if not path.is_file():
                raise ValueError(label + " 文件不存在")
            result["provenance"][label] = {"path": str(path), "sha256": _sha(path), "bytes": path.stat().st_size}
        left, right = [_probe(path) for path in paths]
        result["provenance"]["probe_reference"], result["provenance"]["probe_candidate"] = left, right
        for field in ("frames", "fps_fraction", "aspect_fraction"):
            if left[field] != right[field]:
                result["failures"].append(_failure("media_" + field + "_mismatch", reference=left[field], candidate=right[field]))
        expected_fps = Fraction(str(contract.get("fps", left["fps_fraction"])))
        if expected_fps <= 0:
            raise ValueError("合同帧率无效")
        duration = contract.get("duration")
        expected_count = Fraction(str(duration)) * expected_fps if duration is not None else Fraction(left["frames"])
        if expected_count.denominator != 1:
            result["failures"].append(_failure("contract_fractional_frame", expected=str(expected_count)))
        for label, probe in (("reference", left), ("candidate", right)):
            if Fraction(probe["fps_fraction"]) != expected_fps or probe["frames"] != expected_count:
                result["failures"].append(_failure("contract_timeline_mismatch", media=label, expected_frames=str(expected_count),
                                                   expected_fps=str(expected_fps), frames=probe["frames"], fps=probe["fps_fraction"]))
            timestamps = probe["timestamps"]
            errors = [i for i, value in enumerate(timestamps) if abs(value - i / float(expected_fps)) > TIMESTAMP_EPSILON]
            if errors:
                result["failures"].append(_failure("noncanonical_frame_timestamps", media=label, frames=errors))
            if probe["stream_duration"] is not None and abs(float(probe["stream_duration"]) - float(expected_count / expected_fps)) > TIMESTAMP_EPSILON:
                result["failures"].append(_failure("stream_duration_mismatch", media=label, duration=probe["stream_duration"]))
        aspect = contract.get("aspect_ratio")
        if aspect is not None:
            expected_aspect = Fraction(str(aspect).replace(":", "/")).limit_denominator(10000)
            if any(Fraction(p["aspect_fraction"]) != expected_aspect for p in (left, right)):
                result["failures"].append(_failure("contract_aspect_mismatch", expected=str(expected_aspect)))
        if contract.get("resolution") and list(contract["resolution"]) != [right["width"], right["height"]]:
            result["failures"].append(_failure("contract_resolution_mismatch", expected=contract["resolution"],
                                               candidate=[right["width"], right["height"]]))
        if contract.get("silent") is True and right["has_audio"]:
            result["failures"].append(_failure("unexpected_audio_track"))
        if result["failures"]:
            result["status"] = "failed"
        else:
            width = min(left["width"], right["width"], MAX_ANALYSIS_WIDTH)
            ratio = Fraction(left["aspect_fraction"])
            width = max(ratio.numerator, width // ratio.numerator * ratio.numerator)
            height = width * ratio.denominator // ratio.numerator
            result["metrics"]["analysis_resolution"] = [width, height]
            result["metrics"]["analysis_transform"] = "equal-aspect common downsample only; no alignment/crop/retime"
            decoders = [_Decoder(path, width, height) for path in paths]
            previous, low_confidence = None, []
            for index in range(left["frames"]):
                a, b = [decoder.frame() for decoder in decoders]
                if a is None or b is None:
                    raise ValueError("解码帧数少于探测帧数")
                row, failures, confident, previous = _compare(index, a, b, previous)
                row["time_seconds"] = index / float(expected_fps)
                result["per_frame"].append(row)
                result["failures"].extend(failures)
                if not confident:
                    low_confidence.append(index)
            if any(decoder.frame() is not None for decoder in decoders):
                raise ValueError("解码帧数多于探测帧数")
            bad_frames = sorted({f["frame"] for f in result["failures"] if "frame" in f})
            result["metrics"].update(frames_compared=left["frames"], bad_frames=bad_frames,
                                      bad_frame_ratio=len(bad_frames) / left["frames"], low_confidence_frames=low_confidence,
                                      max_missing_edge_ratio=max(r["missing_edge_ratio"] for r in result["per_frame"]),
                                      max_extra_edge_ratio=max(r["extra_edge_ratio"] for r in result["per_frame"]))
            result["status"] = "failed" if result["failures"] else "unverified" if low_confidence else "verified"
            result["ok"] = result["status"] == "verified"
    except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError, ZeroDivisionError) as error:
        result["failures"].append(_failure("verification_unavailable", message=str(error)))
        result["status"], result["ok"] = "unverified", False
    finally:
        for decoder in decoders:
            decoder.close()
    if output_report:
        report = Path(output_report).resolve()
        report.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
        if report.exists():
            if report.read_text(encoding="utf-8") != serialized:
                raise FileExistsError("验收记录已存在且内容不同；不会覆盖历史证据")
        else:
            with report.open("x", encoding="utf-8") as stream:
                stream.write(serialized)
    return result

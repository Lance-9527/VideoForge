"""Measured Seedance reference delivery. Rejected media is retained, never selected here."""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
from datetime import datetime
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import threading
import uuid

_ACCOUNT_LOCK = threading.RLock()


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def requires_reference_delivery(shot: dict, settings: dict | None = None,
                                provider: str = '', model_name: str = '') -> bool:
    """Use the same explicit promo-reference scope as the pre-spend quality gate."""
    settings = settings or {}
    provider = provider or shot.get('model_provider') or ''
    model_name = model_name or shot.get('model_name') or ''
    disabled = settings.get('previz_as_reference')
    return (str(provider).lower() == 'seedance_t6'
            and str(model_name).lower() == 'seedance-2-0-promo'
            and not (disabled is False or str(disabled).strip().lower() in ('0', 'false', 'off', 'no')))


def prepare_reference_spend(db, shot: dict, duration: float, resolution: str,
                            *, provider: str = '', model_name: str = '') -> dict | None:
    settings = db.get_all_settings() if db is not None else {}
    if not requires_reference_delivery(shot, settings, provider, model_name):
        return None
    if db is None:
        raise ValueError('没有可核验的项目预算，未提交付费任务')
    from core.budget import assert_spend_allowed, check_budget
    from core.shot_segments import _estimate_segment_cost_yuan
    provider = provider or shot.get('model_provider') or ''
    estimate = float(_estimate_segment_cost_yuan(provider, float(duration), resolution))
    assert_spend_allowed(db, provider=provider)
    check_budget(str(shot.get('project_id') or ''), estimate, db=db, provider=provider)
    return {'estimate_yuan': estimate, 'estimated': True, 'request_id': uuid.uuid4().hex,
            'provider': provider, 'model': model_name or shot.get('model_name') or ''}


def record_reference_spend(db, shot: dict, prepared: dict | None, task_id: str = '') -> dict | None:
    """Charge a successful paid generation even when download/verification later fails."""
    if prepared is None:
        return None
    project = str(shot.get('project_id') or '')
    if db is None or not project:
        raise ValueError('付费任务已返回但无法写入项目费用，需人工核查，禁止自动重试')
    identity = '|'.join((str(prepared['provider']), str(prepared['model']), str(task_id or prepared['request_id'])))
    key = 'reference_spend_task_' + hashlib.sha256(identity.encode()).hexdigest()
    with _ACCOUNT_LOCK:
        with db.transaction() if hasattr(db, 'transaction') else nullcontext():
            existing = db.get_setting(key)
            if isinstance(existing, dict):
                return {**existing, 'reused_charge': True}
            amount = float(prepared['estimate_yuan'])
            cost_key = 'project_cost_yuan_' + project
            total = round(float(db.get_setting(cost_key) or 0) + amount, 2)
            receipt = {'task_id': str(task_id), 'request_id': prepared['request_id'],
                       'project_id': project, 'provider': prepared['provider'], 'model': prepared['model'],
                       'amount_yuan': amount, 'estimated': True, 'project_total_yuan': total,
                       'recorded_at': datetime.now().isoformat(timespec='seconds'),
                       'note': '生成成功即计保守估算费用；验收拒绝不代表服务商未收费。'}
            db.set_setting(cost_key, total)
            db.set_setting(key, receipt)
            return receipt


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def _reference_window(reference: Path, target_duration: float, start: float,
                      fps: int, folder: Path, ffmpeg_path: str, ffprobe_path: str) -> Path:
    from core.video_delivery import _probe
    meta = _probe(reference, ffprobe_path or os.environ.get('VIDEOFORGE_FFPROBE') or shutil.which('ffprobe') or 'ffprobe')
    if abs(meta['fps'] - fps) > 1e-6:
        raise ValueError('白膜帧率与交付契约不同；不能靠重采样参考掩盖时序差异')
    first, count = round(start * fps), round(target_duration * fps)
    if start < 0 or abs(first / fps - start) > 1e-6 or abs(count / fps - target_duration) > 1e-6:
        raise ValueError('白膜分段边界必须落在原始完整帧上')
    if first + count > meta['frames']:
        raise ValueError('白膜参考不足本段所需完整帧；不会循环、补帧或改速')
    if first == 0 and count == meta['frames']:
        return reference
    fingerprint = hashlib.sha256(f'{_sha(reference)}|{first}|{count}'.encode()).hexdigest()[:20]
    target = folder / ('reference-window-' + fingerprint + '.mp4')
    if target.exists():
        cached = _probe(target, ffprobe_path or shutil.which('ffprobe') or 'ffprobe')
        if cached['frames'] != count or abs(cached['fps'] - fps) > 1e-6:
            raise ValueError('已有白膜时间窗文件校验失败；拒绝覆盖')
        return target
    command = [ffmpeg_path or os.environ.get('VIDEOFORGE_FFMPEG') or shutil.which('ffmpeg') or 'ffmpeg',
               '-n', '-hide_banner', '-loglevel', 'error', '-i', str(reference), '-map', '0:v:0',
               '-vf', f'trim=start_frame={first}:end_frame={first + count},setpts=PTS-STARTPTS',
               '-frames:v', str(count), '-an', '-c:v', 'libx264', '-crf', '0', '-pix_fmt', 'yuv420p', str(target)]
    options = {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)}
    try:
        from core.proc import child_env
        options['env'] = child_env()
    except ImportError:
        pass
    result = subprocess.run(command, capture_output=True, timeout=180, **options)
    if result.returncode:
        raise ValueError('原始白膜时间窗提取失败；未改写参考或提交新生成任务')
    return target


async def finalize_reference_candidate(raw_path: str, reference_path: str, target_duration: float,
                                       *, aspect_ratio: str = '16:9', fps: int = 24,
                                       silent: bool = False, contract: dict | None = None,
                                       output_path: str | None = None, ffmpeg_path: str = '',
                                       ffprobe_path: str = '', reference_start: float = 0.0,
                                       source_metadata: dict | None = None) -> dict:
    """Conform only media specifications, then inspect the actual final file, frame by frame.

    This function never selects candidates, writes project state, performs remote
    calls, retries generation, or substitutes a local render for a Seedance result.
    """
    raw = Path(raw_path).resolve()
    receipt_path = raw.with_name(raw.stem + '.reference-' + uuid.uuid4().hex + '.json')
    source = dict(source_metadata or {})
    result = {'schema': 'videoforge.reference-delivery/1', 'ok': False, 'status': 'rejected',
              'source': 'seedance', 'provider': source.get('provider'), 'model': source.get('model'),
              'task_id': source.get('task_id') or '', 'task_ids': source.get('task_ids') or [],
              'source_metadata': source, 'source_verification': 'adapter_task_record',
              'raw_path': str(raw), 'reference_path': str(Path(reference_path).resolve()),
              'report_path': str(receipt_path), 'requires_user_review': True, 'no_fallback': True,
              'paid_generation_may_have_been_charged': True}
    try:
        if not raw.is_file():
            raise ValueError('平台生成原片不存在，不能交付')
        result['raw_sha256'] = _sha(raw)
        reference = Path(reference_path).resolve()
        if not reference.is_file():
            raise ValueError('缺少本次给定的白膜视频，不能证明一致')
        result['reference_sha256'] = _sha(reference)
        if source.get('provider') != 'seedance_t6' or source.get('model') != 'seedance-2-0-promo' or not (source.get('task_id') or source.get('task_ids')):
            result['status'] = 'unverified'
            raise ValueError('缺少可核验的 Seedance 2.0 特价按秒任务来源；不能冒充已生成')
        expected = dict(contract or {})
        expected.update(duration=float(target_duration), fps=int(fps), aspect_ratio=aspect_ratio, silent=bool(silent))
        window = await asyncio.to_thread(_reference_window, reference, float(target_duration), float(reference_start),
                                         int(fps), raw.parent, ffmpeg_path, ffprobe_path)
        result['reference_window'] = {'path': str(window), 'sha256': _sha(window),
                                      'start_seconds': reference_start, 'frames': round(target_duration * fps)}
        from core.video_delivery import conform_reference_delivery
        conformed = await conform_reference_delivery(str(raw), target_duration=float(target_duration),
            aspect_ratio=aspect_ratio, fps=fps, silent=silent, output_path=output_path,
            ffmpeg_path=ffmpeg_path, ffprobe_path=ffprobe_path)
        result['conform'] = conformed
        candidate = Path(conformed['path']).resolve()
        result.update(path=str(candidate), candidate_sha256=_sha(candidate), duration=conformed['duration'])
        from core.strict_reference import evaluate_reference
        result['validator_sha256'] = _sha(Path(__file__).with_name('strict_reference.py'))
        audit = await asyncio.to_thread(evaluate_reference, str(window), str(candidate), contract=expected)
        result['audit'] = audit
        result['ok'] = audit.get('ok') is True and audit.get('status') == 'verified'
        result['status'] = 'passed' if result['ok'] else audit.get('status') or 'rejected'
        if result['ok']:
            result['requires_user_review'] = False
        else:
            result['error'] = '480p 视频与给定白膜的逐帧一致性未通过；保留原片与报告，未自动选中。'
    except Exception as error:
        result['error'] = str(error)
    _write(receipt_path, result)
    return result


def verified_reference_receipt(candidate_path: str, reference_path: str, delivery: dict | None,
                                *, expected_contract: dict | None = None) -> dict | None:
    """Reuse a prior verdict only for the current bytes and current verifier implementation."""
    try:
        if expected_contract is None:
            return None  # Current shot requirements must be known, not inferred from an old verdict.
        path = Path((delivery or {}).get('report_path') or '')
        if not path.is_file():
            return None
        receipt = json.loads(path.read_text(encoding='utf-8'))
        source = receipt.get('source_metadata') or {}
        audit = receipt.get('audit') or {}
        audited_contract = (audit.get('provenance') or {}).get('contract') or {}
        if any(audited_contract.get(key) != value for key, value in expected_contract.items()):
            return None
        candidate, reference = Path(candidate_path).resolve(), Path(reference_path).resolve()
        if not (receipt.get('schema') == 'videoforge.reference-delivery/1' and receipt.get('ok') is True
                and receipt.get('status') == 'passed' and audit.get('ok') is True and audit.get('status') == 'verified'
                and source.get('provider') == 'seedance_t6' and source.get('model') == 'seedance-2-0-promo'
                and (source.get('task_id') or source.get('task_ids'))):
            return None
        if not candidate.is_file() or not reference.is_file():
            return None
        actual = _sha(candidate)
        if (receipt.get('candidate_sha256') != actual or receipt.get('reference_sha256') != _sha(reference)
                or ((audit.get('provenance') or {}).get('candidate') or {}).get('sha256') != actual
                or receipt.get('validator_sha256') != _sha(Path(__file__).with_name('strict_reference.py'))):
            return None
        if str(candidate) != str(Path(receipt.get('path') or '').resolve()):
            return None
        return receipt
    except (OSError, ValueError, TypeError):
        return None

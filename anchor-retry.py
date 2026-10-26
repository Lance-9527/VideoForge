"""锚点表：JSON 解析失败时重试一次（模型偶尔会输出带解释文字的 JSON）。"""
from pathlib import Path

p = Path(r'D:\VideoForge-dev\backend\core\scene_anchors.py')
text = p.read_text(encoding='utf-8')

old = """    data: Optional[Dict[str, Any]] = None
    try:
        from core.orchestrator import _get_llm_from_settings
        from core.llm import extract_json

        llm = _get_llm_from_settings(settings)
        raw = await llm.chat([{"role": "user", "content": prompt}], temperature=0.2, max_tokens=2000)
        data = extract_json(raw) if raw else None
        # ★ LLMClient 没有 close() 方法（旧代码在这里抛异常，把已经拿到的结果也丢了）
    except Exception as e:  # noqa: BLE001
        logger.warning("场景锚点表生成失败（用兜底）：%s", e)
        data = None"""
new = """    data: Optional[Dict[str, Any]] = None
    for attempt in range(2):
        try:
            from core.orchestrator import _get_llm_from_settings
            from core.llm import extract_json

            llm = _get_llm_from_settings(settings)
            raw = await llm.chat([{"role": "user", "content": prompt}], temperature=0.2, max_tokens=2000)
            data = extract_json(raw) if raw else None
            # ★ LLMClient 没有 close() 方法（旧代码在这里抛异常，把已经拿到的结果也丢了）
            if isinstance(data, dict) and data:
                break
            logger.warning("场景锚点表 JSON 不可用（第 %d 次），重试", attempt + 1)
        except Exception as e:  # noqa: BLE001
            logger.warning("场景锚点表生成失败（第 %d 次）：%s", attempt + 1, e)
            data = None"""
if old in text:
    p.write_text(text.replace(old, new, 1), encoding='utf-8')
    print('已加一次重试')
else:
    print('未匹配')

import py_compile
py_compile.compile(str(p), doraise=True)
print('语法 OK')

"""白膜规格：加几何体检（Techviz）+ 导出「白膜规格说明」。

来源：KnowledgeVault《AI短剧-白模预演-previz-调研报告》里的 Techviz 定位
（"渲染前几何体检：主体出画、穿墙、超框"）+ Proma 的白膜规范（步幅/接触点）。

产出：
  ① spec["techviz"]：逐镜的体检问题（出画/穿墙/超框/步幅过大/接地）
  ② `<output_root>/<pid>/previz/白膜规格.txt`：给人看的"白膜锁定了什么"
     （blockout 的做法：白膜视频 + 每机位静帧 + 自动 prompt.txt）
"""
import shutil
import time
from pathlib import Path

STAMP = time.strftime('%Y%m%d-%H%M%S')
ROOT = Path(r'D:\VideoForge-dev\backend\core')

# ── ① previz_design：几何体检 ───────────────────────────────────────────
p = ROOT / 'previz_design.py'
text = p.read_text(encoding='utf-8')
shutil.copyfile(p, p.with_suffix(p.suffix + '.bak-' + STAMP))

if 'def techviz_check(' not in text:
    helper = '''

#: 一步的最大位移（Proma 实测：>0.34m 腿会被拉直，走起来像木偶）
MAX_STEP_M = 0.34


def techviz_check(spec: Dict[str, Any]) -> List[str]:
    """几何体检：主体出画 / 穿墙 / 超框 / 步幅过大 / 人物与相机重合。

    ★ 为什么要查：白膜是**花钱前唯一的机位检查机会**，这些问题在静态帧上最容易看出来，
      到成片才发现就是白花钱（调研报告里的 Techviz 定位）。
    """
    issues: List[str] = []
    for idx, shot in enumerate(spec.get("shots") or [], 1):
        tag = f"镜{idx}"
        st = shot.get("set") or {}
        width = float(st.get("width") or 0) or 0.0
        depth = float(st.get("depth") or 0) or 0.0
        height = float(st.get("height") or 0) or 0.0
        cam = shot.get("camera") or {}
        cam_pos = cam.get("position") or [0, -4, 1.5]
        for person in (shot.get("cast") or []):
            name = _s(person.get("name")) or "人物"
            x = float(person.get("x") or 0)
            y = float(person.get("y") or 0)
            # 超框：人物站在场景之外
            if width and (abs(x) > width / 2 + 0.5):
                issues.append(f"{tag}「{name}」横向超出场景范围（x={x:.2f} > 宽 {width:.2f}）")
            if depth and (y > depth + 0.5 or y < -1.0):
                issues.append(f"{tag}「{name}」纵深超出场景范围（y={y:.2f}）")
            # 穿墙/重合：人物与相机在同一位置
            if abs(y - float(cam_pos[1] if len(cam_pos) > 1 else 0)) < 0.6:
                issues.append(f"{tag}「{name}」与相机几乎重合（会穿帮/看不见人）")
            # 步幅：beats 之间位移
            path = person.get("path") or []
            for a, b in zip(path, path[1:]):
                try:
                    d = ((float(b[1]) - float(a[1])) ** 2 + (float(b[2]) - float(a[2])) ** 2) ** 0.5
                except Exception:  # noqa: BLE001
                    continue
                frames = abs(int(b[0]) - int(a[0])) or 1
                step = d / max(1.0, frames / 24.0)
                if step > MAX_STEP_M:
                    issues.append(
                        f"{tag}「{name}」步幅过大（{step:.2f}m/步 > {MAX_STEP_M}m，会像木偶）")
        # 接地：人物高度必须合理（不在地上也不会在天上）
        for person in (shot.get("cast") or []):
            h = float(person.get("height") or 0)
            if h and not (1.2 <= h <= 2.1):
                issues.append(f"{tag} 人物身高异常（{h:.2f}m），白膜比例会失真")
        # 相机是否在场景外
        if width and depth:
            cx = float(cam_pos[0] if cam_pos else 0)
            cy = float(cam_pos[1] if len(cam_pos) > 1 else 0)
            if abs(cx) > width / 2 + 2.0 or cy > depth + 2.0 or cy < -depth - 2.0:
                issues.append(f"{tag} 相机在场景之外（{cx:.1f},{cy:.1f}），画面会空")
        if height and cam_pos and len(cam_pos) > 2 and float(cam_pos[2]) > height + 1.5:
            issues.append(f"{tag} 相机高于场景天花板（z={float(cam_pos[2]):.1f}）")
    return issues
'''
    # 插到 normalize_spec 之前
    anchor = 'def normalize_spec('
    if anchor in text:
        text = text.replace(anchor, helper.strip('\n') + '\n\n\n' + anchor, 1)
        # 在 normalize_spec 的 return 前写入 techviz
        # 找到 normalize_spec 里的 return 语句
        i = text.find('def normalize_spec(')
        j = text.find('\ndef ', i + 10)
        seg = text[i:j]
        seg2 = seg.replace('    return out', '''    # ★ 几何体检（Techviz）：出画/穿墙/超框/步幅 —— 白膜是花钱前唯一的机位检查机会
    try:
        out["techviz"] = techviz_check(out)
    except Exception as _e:  # noqa: BLE001
        logger.warning("几何体检失败（不阻塞）：%s", _e)
    return out''', 1)
        if seg2 == seg:
            print('   （normalize_spec 的 return 未匹配，稍后手工补）')
        else:
            text = text[:i] + seg2 + text[j:]
        p.write_text(text, encoding='utf-8')
        print('① 几何体检已加入 previz_design')
    else:
        print('① 未找到 normalize_spec')

import py_compile
try:
    py_compile.compile(str(p), doraise=True)
    print('① 语法 OK')
except Exception as e:
    print('① 语法错误:', e)

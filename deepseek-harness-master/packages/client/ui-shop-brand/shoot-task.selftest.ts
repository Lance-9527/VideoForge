/**
 * 拍摄任务映射的自测（用 tsx 直接跑 TS 源码，不做字符串剥离）。
 *
 * 验的是「电商镜头表 → splat 机位序列」这条接缝：
 *   节拍（抛需求/给方案/展示细节/促单）→ yaw/pitch/zoom + 是否环绕。
 *
 * 用法：npx tsx packages/client/ui-shop-brand/shoot-task.selftest.ts
 */
import { planShotCameras } from './src/client/shoot-task.ts'

const shots = [
  { scene_number: 1, title: '抛需求｜轻薄防晒衣 UPF50+', beat: '抛需求', duration_seconds: 3 },
  { scene_number: 2, title: '给方案｜轻薄防晒衣 UPF50+', beat: '给方案', duration_seconds: 12 },
  { scene_number: 3, title: '验信任｜轻薄防晒衣 UPF50+', beat: '验信任', duration_seconds: 10.5 },
  { scene_number: 4, title: '催下单｜轻薄防晒衣 UPF50+', beat: '催下单', duration_seconds: 4.5 },
  { scene_number: 5, title: '钩子｜婴儿奶瓶', beat: '钩子', duration_seconds: 3 },
  { scene_number: 6, title: '痛点｜婴儿奶瓶', beat: '痛点', duration_seconds: 8 },
  { scene_number: 7, title: '演示｜婴儿奶瓶', beat: '演示', duration_seconds: 12 },
  { scene_number: 8, title: '证据｜婴儿奶瓶', beat: '证据', duration_seconds: 10 },
  { scene_number: 9, title: 'CTA｜婴儿奶瓶', beat: 'CTA', duration_seconds: 4 },
]

const plan = planShotCameras(shots)
console.log('镜头表 → 机位序列：')
for (const camera of plan) {
  console.log(
    `  #${camera.shotNumber} ${camera.label}\n` +
    `     yaw=${camera.yaw}° pitch=${camera.pitch}° zoom=${camera.zoom} 环绕=${camera.orbit}`,
  )
}

const problems: string[] = []
if (plan.length !== shots.length) { problems.push(`机位数 ${plan.length} ≠ 镜头数 ${shots.length}`) }
const byBeat = Object.fromEntries(plan.map((c) => [c.label.split('｜')[0], c]))
if (byBeat['抛需求']!.zoom >= 1) { problems.push('抛需求 应该是中景（zoom < 1）') }
if (byBeat['给方案']!.zoom >= byBeat['抛需求']!.zoom) { problems.push('给方案 应该比抛需求更近') }
if (byBeat['验信任']!.zoom >= byBeat['给方案']!.zoom) { problems.push('验信任 应该最贴（讲证据）') }
if (byBeat['演示']!.orbit !== true) { problems.push('演示 应该走环绕（splat 的独门优势）') }
if (byBeat['CTA']!.orbit !== false) { problems.push('CTA 不该环绕') }
if (byBeat['钩子'] === undefined || byBeat['CTA'] === undefined) { problems.push('品类五段没被识别') }
// 同一套节拍体系内不能出现重复机位（默认四段与品类五段互斥，但保险起见都查）
const groups = [
  ['抛需求', '给方案', '验信任', '催下单'],
  ['钩子', '痛点', '演示', '证据', 'CTA'],
]
for (const group of groups) {
  const inGroup = plan.filter((c) => group.includes(c.label.split('｜')[0] ?? ''))
  const distinct = new Set(inGroup.map((c) => `${c.yaw}/${c.pitch}/${c.zoom}/${c.orbit}`))
  if (distinct.size !== inGroup.length) { problems.push(`体系 [${group.join('/')}] 内机位有重复`) }
}

console.log(problems.length === 0 ? 'SHOOT_TASK_OK' : `问题:\n  - ${problems.join('\n  - ')}`)
process.exit(problems.length === 0 ? 0 : 1)

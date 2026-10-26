// @vf/vf-extra · 自测（不需要 DSH；--live 时额外打真 sidecar）
//
//   node selftest.mjs          → 纯逻辑
//   node selftest.mjs --live   → 纯逻辑 + 对 http://127.0.0.1:8765 真实只读/免费接口
//
// ★ 为什么不 import lib/index.js：它 import '@deepseek-ai/dsh-tools'，
//   那个包只有宿主能解析（外部插件 bare import 由宿主解析，见 README）。
//   所以这里只测 lib/core.js 的纯函数，另外自己实现一份最小 fetch 校验 HTTP 契约。

import {
  TOOL_SPECS,
  buildRequest,
  summarize,
  asList,
  DEFAULT_BASE_URL,
} from './lib/core.js'

let pass = 0
let fail = 0
const fails = []

function ok(name, cond, detail = '') {
  if (cond) {
    pass += 1
    console.log('  PASS  ' + name + (detail ? '  ' + detail : ''))
  } else {
    fail += 1
    fails.push(name + (detail ? ' — ' + detail : ''))
    console.log('  FAIL  ' + name + (detail ? '  ' + detail : ''))
  }
}

console.log('='.repeat(70))
console.log('1. 工具清单')
console.log('='.repeat(70))
ok('工具数 = 6', TOOL_SPECS.length === 6, `实际 ${TOOL_SPECS.length}`)
const names = TOOL_SPECS.map((t) => t.name)
for (const n of ['vf_remake', 'vf_transcribe', 'vf_ecom_plan', 'vf_ecom_audit',
  'vf_ecom_points', 'vf_ecom_label']) {
  ok('存在 ' + n, names.includes(n))
}
ok('每个都有 description（>20 字）', TOOL_SPECS.every((t) => (t.description || '').length > 20))
ok('每个都有 parameters', TOOL_SPECS.every((t) => t.parameters && typeof t.parameters === 'object'))

console.log()
console.log('='.repeat(70))
console.log('2. buildRequest 契约')
console.log('='.repeat(70))
{
  const r = buildRequest('vf_remake', { video_path: 'D:\\a.mp4' })
  ok('remake 默认 dry_run=true（不花钱）', r.body.dry_run === true)
  ok('remake 路径正确', r.path === '/api/remake/analyze')
  ok('remake 默认三个开关为 true',
    r.body.use_ocr === true && r.body.use_whisper === true && r.body.use_siliconflow === true)
}
{
  const r = buildRequest('vf_remake', { video_path: 'x.mp4', dry_run: false, max_shots: 12 })
  ok('remake 可关 dry_run', r.body.dry_run === false && r.body.max_shots === 12)
}
{
  const r = buildRequest('vf_ecom_plan', {
    product: '防晒衣', platform: 'douyin_shop_main', category: 'apparel',
    selling_points: '质量特别好、UPF50+', duration_sec: 25,
  })
  ok('plan 路径正确', r.path === '/api/ecommerce/plan')
  ok('卖点顿号字符串被拆成数组', Array.isArray(r.body.selling_points) && r.body.selling_points.length === 2,
    JSON.stringify(r.body.selling_points))
  ok('plan 默认 ai_label=plan（只给方案）', r.body.ai_label === 'plan')
  ok('duration 转成数字', r.body.duration_sec === 25)
}
{
  const r = buildRequest('vf_ecom_audit', { text: '全网最低价' })
  ok('audit 路径正确', r.path === '/api/ecommerce/audit' && r.body.text === '全网最低价')
}
{
  const r = buildRequest('vf_ecom_points', { selling_points: ['质量特别好'] })
  ok('points 路径正确', r.path === '/api/ecommerce/selling-points')
}
{
  const r = buildRequest('vf_ecom_label', {})
  ok('label 默认 apply=false（不动文件）', r.body.apply === false)
  ok('label 默认 mode=start_and_badge', r.body.mode === 'start_and_badge')
  ok('label 默认 start_text', r.body.start_text.includes('AI'))
}
{
  let threw = false
  try { buildRequest('vf_nope', {}) } catch { threw = true }
  ok('未知工具抛错', threw)
}
ok('asList 处理 null', asList(null).length === 0)
ok('asList 处理换行', asList('a\nb').length === 2)

console.log()
console.log('='.repeat(70))
console.log('3. summarize（给模型看的一句话）')
console.log('='.repeat(70))
{
  const s = summarize('vf_remake', { dry_run: true, estimated_shots: 24, est: { est_cost_yuan: 0.07, est_seconds: 30 } })
  ok('remake 估算摘要', s.includes('24') && s.includes('0.07') && s.includes('dry_run=false'), s)
}
{
  const s = summarize('vf_remake', { shots: 24, style_profile_label: '生活流电影感', pacing: { mean_shot: 2.38 }, dialogue_stats: { lines: 105 }, spec_path: 'X.json' })
  ok('remake 完成摘要', s.includes('24') && s.includes('2.38') && s.includes('105'), s)
}
{
  const s = summarize('vf_ecom_plan', { product: '防晒衣', platform: { name_zh: '抖店主图' }, category: { name_zh: '服饰' }, target_duration: 22, scenes: [1, 2, 3], compliance: { errors: 0, warnings: 1 }, ai_label: { choice: 'off' } })
  ok('plan 摘要含合规与标识选择', s.includes('0 错误') && s.includes('off'), s)
}
{
  const s = summarize('vf_ecom_audit', { score: 20, errors: 6, warnings: 3, hits: [{ term: '国家级' }, { term: '最低价' }] })
  ok('audit 摘要含命中词', s.includes('国家级'), s)
}
{
  const s = summarize('vf_ecom_label', { checklist: {}, plan: { steps: [1, 2, 3, 4], font: 'simhei' } })
  ok('label 方案摘要', s.includes('4') && s.includes('apply=true'), s)
}
for (const n of ['vf_transcribe', 'vf_ecom_points', 'vf_ecom_label']) {
  ok(n + ' 摘要非空', summarize(n, {}).length > 3)
}

// ─────────────────────────── live（--live） ───────────────────────────

if (process.argv.includes('--live')) {
  const base = (process.env.VF_BASE_URL || DEFAULT_BASE_URL).replace(/\/+$/, '')
  console.log()
  console.log('='.repeat(70))
  console.log('4. live sidecar: ' + base)
  console.log('='.repeat(70))

  async function post(path, body) {
    const resp = await fetch(base + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    const j = await resp.json()
    if (String(j.code) !== '000000') throw new Error(j.message || 'code=' + j.code)
    return j.data
  }

  try {
    const h = await (await fetch(base + '/api/health')).json()
    ok('sidecar health', h && h.data && h.data.status === 'ok', h && h.data ? h.data.status : '?')
  } catch (e) {
    ok('sidecar health', false, String(e))
  }

  try {
    const d = await post('/api/ecommerce/audit', { text: '全网最低价，国家级品质' })
    ok('audit 真接口命中违禁词', d.errors >= 2, `errors=${d.errors}`)
  } catch (e) {
    ok('audit 真接口', false, String(e))
  }

  try {
    const d = await post('/api/ecommerce/selling-points', { selling_points: ['质量特别好', 'UPF50+'] })
    const kinds = (d.items || []).map((x) => x.kind)
    ok('points 真接口：形容词被改写', kinds.includes('table') || kinds.includes('quality'), kinds.join(','))
    ok('points 真接口：数字不动', kinds.includes('already_measurable'), kinds.join(','))
  } catch (e) {
    ok('points 真接口', false, String(e))
  }

  try {
    const d = await post('/api/ecommerce/plan', {
      product: '测试防晒衣', platform: 'douyin_shop_main', category: 'apparel',
      selling_points: ['UPF50+'], duration_sec: 20, ai_label: 'off',
    })
    ok('plan 真接口出场景', (d.scenes || []).length > 0, `${(d.scenes || []).length} 场`)
    ok('plan 真接口 total==target',
      Math.abs((d.scenes || []).reduce((s, x) => s + x.duration_seconds, 0) - d.target_duration) <= 0.6)
    ok('plan 真接口 ai_label=off 有法律提示', !!(d.ai_label && d.ai_label.warning), (d.ai_label || {}).choice)
  } catch (e) {
    ok('plan 真接口', false, String(e))
  }

  try {
    const d = await post('/api/ecommerce/ai-label', {})
    ok('label 真接口出方案（不动文件）', !!(d.plan && d.plan.steps && d.plan.steps.length >= 3),
      `${(d.plan && d.plan.steps || []).length} 步`)
  } catch (e) {
    ok('label 真接口', false, String(e))
  }
}

console.log()
console.log('='.repeat(70))
console.log(`汇总：PASS=${pass}  FAIL=${fail}`)
console.log('='.repeat(70))
if (fail) {
  fails.forEach((f) => console.log('  FAILED: ' + f))
  process.exit(1)
}
console.log('全部通过')

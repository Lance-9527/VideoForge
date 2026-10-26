// VideoForge · 增量能力工具（2026-10-24 加入 @deepseek-ai/dsh-vf-video）
//
// 为什么放在这里而不是独立 bundle：
//   打包版的 profile bundle 解析对新增包不生效（试过 profile node_modules junction、
//   DSH_HOME/node_modules junction、cordis.patch.yml 的 file:// 插入，都没被 apply）。
//   而本包（dsh-vf-video）是从 app.asar 里被必定加载的 —— 把工具挂在这，路径最短、最可靠。
//
// 为什么用一个独立文件而不是直接写进 index.js：
//   index.js 已经有 335 行、承载 9 步流水线；新增能力独立成模块，出问题能整段摘掉，
//   也不会让原有注册逻辑的 diff 变大。
//
// 六个工具全是 Python sidecar 已有 HTTP 接口的薄壳：
//   /api/remake/analyze · /api/remake/transcribe
//   /api/ecommerce/plan · audit · selling-points · ai-label
//
// ★ 宿主约束（从 vf-video 的踩坑记录抄来，必须遵守）：
//   ① defineTool 的产物不要 spread（会 fiber failed）—— 本文件只出 spec，注册在 index.js 的 wrap 里做；
//   ② 每个 spec 必须带 output: { schema, render } —— 缺了宿主直接 TypeError，全部工具消失。

import { appendFileSync, mkdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

// ★ 模块级信标：证明本文件真的被 import 了（排障用，失败不影响功能）
try {
  const dir = join(homedir(), 'AppData', 'Local', 'VideoForge')
  mkdirSync(dir, { recursive: true })
  appendFileSync(join(dir, 'vf-extra-boot.log'),
    `${new Date().toISOString()} pid=${process.pid} phase=vf-extra-tools-import\n`, 'utf8')
} catch { /* ignore */ }

const LONG_TIMEOUT_MS = 30 * 60 * 1000
const QUICK_TIMEOUT_MS = 120 * 1000

function outputOf() {
  return {
    schema: { type: 'json' },
    render: (_args, value) => [{ type: 'text', text: String((value && value.__summary) || '完成。') }],
  }
}

function asList(v) {
  if (Array.isArray(v)) return v.map((x) => String(x)).filter((x) => x.trim())
  if (typeof v === 'string' && v.trim()) {
    return v.split(/[\n、,，;；]+/).map((x) => x.trim()).filter(Boolean)
  }
  return []
}

function summarize(name, d) {
  const r = d || {}
  switch (name) {
    case 'vf_remake':
      if (r.dry_run) {
        const est = r.est || {}
        return `估算：约 ${r.estimated_shots} 个镜头，预计花费 ¥${est.est_cost_yuan || 0}，`
          + `预计 ${est.est_seconds || '?'} 秒。确认后传 dry_run=false 真跑。`
      }
      return `复刻完成：${r.shots} 镜，风格档 ${r.style_profile_label || r.style_profile}，`
        + `平均镜头 ${(r.pacing || {}).mean_shot}s，台词 ${(r.dialogue_stats || {}).lines} 行。`
        + (r.spec_path ? `规格：${r.spec_path}` : '')
    case 'vf_transcribe':
      return `台词提取完成：${(r.lines || []).length} 行，花费 ¥${r.cost_yuan || 0}。`
    case 'vf_ecom_plan': {
      const c = r.compliance || {}
      return `方案已生成：${r.product} · ${(r.platform || {}).name_zh} · ${(r.category || {}).name_zh} · `
        + `${r.target_duration}s · ${(r.scenes || []).length} 场；`
        + `合规 ${c.errors || 0} 错误 / ${c.warnings || 0} 待举证；`
        + `AI 标识选择=${(r.ai_label || {}).choice}。`
    }
    case 'vf_ecom_audit':
      return `合规体检：得分 ${r.score}，${r.errors} 错误 / ${r.warnings} 待举证。`
        + ((r.hits || []).length
          ? ` 命中：${(r.hits || []).slice(0, 6).map((h) => h.term).join('、')}`
          : ' 未命中违禁词。')
    case 'vf_ecom_points':
      return `卖点改写：${r.count} 条，改写 ${r.changed} 条`
        + ((r.still_vague || []).length ? `；仍需人工补事实：${r.still_vague.join('、')}` : '。')
    case 'vf_ecom_label':
      if (r.checklist) {
        return `AI 标识方案（未动文件）：共 ${((r.plan || {}).steps || []).length} 步，`
          + `字体 ${(r.plan || {}).font || '?'}。要真烧传 apply=true。`
      }
      return `AI 标识已烧进画面：${(r.layers_done || []).join('/')} → ${r.out_path}；仍需你做 L4「发布时主动声明」。`
    default:
      return '完成。'
  }
}

/**
 * 构造 6 个工具 spec。
 * @param {{fetchJson: Function}} deps  vf-video 的 fetchJson（已解 {code,message,data} 信封）
 * @returns {Array<object>} spec 数组（交给 index.js 的 wrap 注册）
 */
export function makeVfExtraSpecs(deps) {
  const { fetchJson } = deps

  const call = async (name, path, body, timeoutMs) => {
    const data = await fetchJson(path, { method: 'POST', body, timeoutMs })
    return {
      ...(data && typeof data === 'object' ? data : { data }),
      __summary: summarize(name, data),
    }
  }

  const mk = (name, description, parameters, timeoutMs, path, buildBody) => ({
    name,
    description: `[vf-extra] ${description}`,
    parameters,
    output: outputOf(),
    isConcurrencySafe: true,
    timeoutMs,
    execute: async (args) => call(name, path, buildBody(args || {}), timeoutMs),
  })

  const listParam = (desc) => ({ type: 'array', description: desc, items: { type: 'string' } })

  return [
    mk('vf_remake',
      '**视频复刻**：把参考视频拆成可照着做的施工图 —— 镜头表 + 景别 + 运镜 + 色调 + 构图 + 字幕版式 + 台词 + 节奏指标。'
      + '复刻的是**手法**（节奏/景别/运镜/色调/台词密度），不是内容。'
      + '★ 默认 dry_run=true：只报镜头数与花费，不花钱不动文件；确认后再 dry_run=false 真跑（几分钟）。',
      {
        video_path: { type: 'string', description: '参考视频绝对路径' },
        dry_run: { type: 'boolean', description: 'true=只估算（默认）；false=真跑' },
        max_shots: { type: 'number', description: '最多分析多少镜（0=全部）' },
        use_ocr: { type: 'boolean', description: '是否 OCR 硬字幕（默认 true）' },
        use_whisper: { type: 'boolean', description: '本地 whisper 出时间戳（免费，默认 true）' },
        use_siliconflow: { type: 'boolean', description: '硅基流动校对台词（花钱，默认 true）' },
      },
      LONG_TIMEOUT_MS, '/api/remake/analyze',
      (a) => ({
        video_path: String(a.video_path || '').trim(),
        dry_run: a.dry_run === undefined ? true : !!a.dry_run,
        use_ocr: a.use_ocr === undefined ? true : !!a.use_ocr,
        use_whisper: a.use_whisper === undefined ? true : !!a.use_whisper,
        use_siliconflow: a.use_siliconflow === undefined ? true : !!a.use_siliconflow,
        max_shots: Number(a.max_shots || 0),
      })),

    mk('vf_transcribe',
      '从视频/音频提取**台词**：本地 whisper 出带时间戳分句 + 硅基流动整段校对。'
      + '台词只用于理解剧情意图，**绝不写进画面提示词**。',
      {
        video_path: { type: 'string', description: '视频或音频绝对路径' },
        whisper_size: { type: 'string', description: 'tiny / base / small / medium（默认 small）' },
        use_siliconflow: { type: 'boolean', description: '是否硅基流动校对（花钱，默认 true）' },
      },
      LONG_TIMEOUT_MS, '/api/remake/transcribe',
      (a) => ({
        video_path: String(a.video_path || '').trim(),
        whisper_size: String(a.whisper_size || 'small'),
        use_siliconflow: a.use_siliconflow === undefined ? true : !!a.use_siliconflow,
      })),

    mk('vf_ecom_plan',
      '**电商商品视频方案**：商品 + 平台 + 品类 → 按品类转化漏斗排分镜，时长精确切到目标秒数。'
      + '附带平台规格核对、合规体检、卖点改写、AI 标识选择、批量投稿风险。纯本地不花钱。'
      + '★ 品类选错会明显跑偏（比如防晒衣被套成美妆），要么选对，要么不传走通用四段。',
      {
        product: { type: 'string', description: '商品名（必填）' },
        platform: { type: 'string', description: '平台 key，如 douyin_shop_main / taobao_main / jd_main' },
        category: { type: 'string', description: 'beauty / food / apparel / tech3c / home / baby / daily' },
        selling_points: listParam('卖点数组（尽量给可验证的事实）'),
        duration_sec: { type: 'number', description: '目标时长秒（不传用平台推荐值）' },
        price_line: { type: 'string', description: '价格/优惠（须与挂车实际成交价一致）' },
        ai_label: { type: 'string', description: 'AI 标识：off / plan（默认）/ apply' },
      },
      QUICK_TIMEOUT_MS, '/api/ecommerce/plan',
      (a) => ({
        product: String(a.product || '').trim(),
        platform: String(a.platform || '').trim(),
        category: String(a.category || '').trim(),
        selling_points: asList(a.selling_points),
        duration_sec: Number(a.duration_sec || 0),
        price_line: String(a.price_line || ''),
        ai_label: String(a.ai_label || 'plan'),
      })),

    mk('vf_ecom_audit',
      '**电商文案合规体检**：广告法绝对化用语 / 化妆品医疗暗示 / 食品保健功效 / 三品一械 / 平台红线。'
      + '分 error（命中即拒）与 warn（需举证）。★ 只查文案不查画面，且不是法律意见。',
      { text: { type: 'string', description: '要检查的口播稿/字幕（必填）' } },
      QUICK_TIMEOUT_MS, '/api/ecommerce/audit',
      (a) => ({ text: String(a.text || '') })),

    mk('vf_ecom_points',
      '**卖点 → 可拍摄画面**：把"质量特别好"这类空泛形容词改写成能拍的动作或能读的数字。'
      + '已含数字的不动；"是主张但没数字"的提示补数字。纯本地免费。',
      { selling_points: listParam('卖点数组（必填）') },
      QUICK_TIMEOUT_MS, '/api/ecommerce/selling-points',
      (a) => ({ selling_points: asList(a.selling_points) })),

    mk('vf_ecom_label',
      '**AI 内容标识**（《AI 生成合成内容标识办法》+ GB 45438-2025）。'
      + '不传 video_path 或 apply=false：只出清单与方案，不动文件。'
      + 'apply=true：真烧起始提示 + 全程角标进画面、写元数据与旁挂 JSON（会重编码）。'
      + 'L4「发布时主动声明」只能用户自己做。★ 不提供去标能力。',
      {
        video_path: { type: 'string', description: '要打标识的视频路径（不传只出方案）' },
        apply: { type: 'boolean', description: 'true=真烧进画面' },
        mode: { type: 'string', description: 'start_and_badge（默认）/ start_only / badge_only' },
        start_seconds: { type: 'number', description: '起始提示持续秒数（默认 3）' },
        start_text: { type: 'string', description: '起始提示文字' },
        badge_text: { type: 'string', description: '角标文字' },
      },
      LONG_TIMEOUT_MS, '/api/ecommerce/ai-label',
      (a) => ({
        video_path: String(a.video_path || '').trim(),
        apply: !!a.apply,
        mode: String(a.mode || 'start_and_badge'),
        start_seconds: a.start_seconds === undefined ? 3 : Number(a.start_seconds),
        start_text: String(a.start_text || '本视频由 AI 生成'),
        badge_text: String(a.badge_text || 'AI 生成'),
      })),
  ]
}

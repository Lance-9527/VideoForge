import { defineTool } from '@deepseek-ai/dsh-tools'
import { makeToolSpecs, makeRerunStageSpec, makeSkipStageSpec } from './lib/core.js'

const noop = async () => ({})
const specs = makeToolSpecs({
  fetchJson: noop, baseUrl: 'http://127.0.0.1:8765', startJob: () => 'j1',
  pollMs: 10, openStream: async function* () {}, stageKeys: ['script','assets','looks','shots','previz','draft','final','compose'],
})
specs.stageTools.forEach((s, i) => { defineTool(s); console.log('defineTool OK', s.name) })
const rerun = makeRerunStageSpec(specs.stageTools)
defineTool(rerun)
console.log('defineTool OK', rerun.name)
// ★ 2026-10-26：跳过门面也要过宿主的 schema 校验（不然插件载入期就 failed，工具全没了）。
const skip = makeSkipStageSpec(noop, ['script', 'previz', 'draft'])
defineTool(skip)
console.log('defineTool OK', skip.name)
console.log('HOST_SCHEMA_OK')

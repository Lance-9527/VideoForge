/* ============================================================================
 * VideoForge · 创作工具页（2026-10-24 新增）
 * ============================================================================
 * 为什么要有这个页面
 * ----------------------------------------------------------------------------
 * 「视频复刻」和「电商商品视频」两个功能点后端早在 2026-10-24 就做完了
 * （core/video_remake.py / core/ecommerce_video.py + 9 条 REST 路由 + 8 个 agent 工具），
 * 但**前端一个入口都没有** —— 用户只能靠对话让 agent 去调，界面上看不见。
 * 这个文件补的就是这一层：把两个功能做成能点的界面。
 *
 * 设计上的三条自我约束
 * ----------------------------------------------------------------------------
 * ① **不假装懂**：平台规格、品类节拍、合规词表全部来自后端 rules.json，
 *    这里只渲染，不在前端再抄一份数字（否则两处会漂移）。
 * ② **花钱/长耗时操作必须用户点**：复刻真跑会切镜+抽帧+OCR+ASR（几分钟），
 *    ASR 还会花几分钱；所以默认是「估算」，真跑要点按钮。
 * ③ **AI 标识是可选项**（Lance 2026-10-24 要求）：三档 off/plan/apply 让用户选，
 *    但选 off 时必须把法律风险显示出来 —— 尊重选择，不隐瞒后果。
 * ========================================================================== */

(function () {
  'use strict';

  // ── 小工具：安全转义（后端返回的文案可能含 < > &，直接塞 innerHTML 会炸） ──
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function el(id) { return document.getElementById(id); }

  function setBusy(btn, busy, textIdle, textBusy) {
    if (!btn) return;
    btn.disabled = !!busy;
    btn.textContent = busy ? (textBusy || '处理中…') : (textIdle || btn.textContent);
  }

  function toast(msg, type) {
    try {
      if (type === 'error') window.Toast.error(msg);
      else if (type === 'success') window.Toast.success(msg);
      else window.Toast.info(msg);
    } catch (e) { console.log('[tools]', msg); }
  }

  function fmtSec(s) {
    const n = Number(s) || 0;
    return (Math.round(n * 10) / 10) + 's';
  }

  // ══════════════════════════════════════════════════════════════
  // 状态（切页时保留，避免来回切就丢结果）
  // ══════════════════════════════════════════════════════════════
  const S = {
    tab: 'remake',          // remake | ecommerce
    remake: null,           // 上次复刻结果
    plan: null,             // 上次电商方案
    cache: { platforms: null, categories: null, remakeStatus: null, ecomStatus: null },
  };

  // ══════════════════════════════════════════════════════════════
  // 页面骨架（只建一次；之后只更新结果区）
  // ══════════════════════════════════════════════════════════════
  function ensureSkeleton() {
    if (el('toolsPageBody')) return;
    const host = document.querySelector('.page[data-page="tools"]');
    if (!host) return;
    host.innerHTML = `
      <header class="page-header">
        <h2>🧰 创作工具</h2>
        <div class="header-actions">
          <button class="secondary-btn" id="toolsRefreshBtn" title="重新读后端自检状态">⟳ 刷新自检</button>
        </div>
      </header>
      <div class="tools-wrap" style="padding:16px 20px 40px; max-width:1180px;">
        <div class="tools-tabs" style="display:flex;gap:8px;margin-bottom:14px;">
          <button class="primary-btn" data-tools-tab="remake">🎬 视频复刻</button>
          <button class="secondary-btn" data-tools-tab="ecommerce">🛒 电商商品视频</button>
        </div>
        <div id="toolsSelfCheck" class="dim" style="font-size:12px;margin-bottom:14px;"></div>
        <div id="toolsPageBody"></div>
      </div>`;

    host.querySelectorAll('[data-tools-tab]').forEach(function (b) {
      b.addEventListener('click', function () {
        S.tab = b.dataset.toolsTab;
        host.querySelectorAll('[data-tools-tab]').forEach(function (x) {
          x.className = (x === b) ? 'primary-btn' : 'secondary-btn';
        });
        renderTools();
      });
    });
    const rf = el('toolsRefreshBtn');
    if (rf) rf.addEventListener('click', function () { S.cache = { platforms: null, categories: null, remakeStatus: null, ecomStatus: null }; loadSelfCheck(true); });
  }

  // ══════════════════════════════════════════════════════════════
  // 自检条
  // ══════════════════════════════════════════════════════════════
  async function loadSelfCheck(force) {
    if (!force && S.cache.remakeStatus && S.cache.ecomStatus) {
      paintSelfCheck();
      return;
    }
    try {
      const [rs, es] = await Promise.all([
        window.API.tools.remakeStatus().catch(function (e) { return { error: String(e) }; }),
        window.API.tools.ecommerceStatus().catch(function (e) { return { error: String(e) }; }),
      ]);
      S.cache.remakeStatus = rs || {};
      S.cache.ecomStatus = es || {};
    } catch (e) {
      S.cache.remakeStatus = { error: String(e) };
      S.cache.ecomStatus = { error: String(e) };
    }
    paintSelfCheck();
  }

  function paintSelfCheck() {
    const box = el('toolsSelfCheck');
    if (!box) return;
    const rs = S.cache.remakeStatus || {};
    const es = S.cache.ecomStatus || {};
    const bits = [];
    bits.push(rs.error
      ? '<span style="color:#e07a5f">复刻自检失败：' + esc(rs.error) + '</span>'
      : '复刻：ffmpeg ' + (rs.ffmpeg ? '✓' : '✗') +
        ' · ASR Key ' + (rs.asr && rs.asr.configured ? '✓' : '✗') +
        ' · 风格档 ' + ((rs.style_profiles && Object.keys(rs.style_profiles).length) || 0));
    bits.push(es.error
      ? '<span style="color:#e07a5f">电商自检失败：' + esc(es.error) + '</span>'
      : '电商：平台 ' + ((es.platforms && es.platforms.length) || 0) +
        ' · 品类 ' + ((es.categories && es.categories.length) || 0) +
        ' · 合规组 ' + ((es.compliance_lists && es.compliance_lists.length) || 0));
    box.innerHTML = bits.join('<span class="dim">　|　</span>');
  }

  // ══════════════════════════════════════════════════════════════
  // 主渲染
  // ══════════════════════════════════════════════════════════════
  function renderTools() {
    ensureSkeleton();
    loadSelfCheck(false);
    const body = el('toolsPageBody');
    if (!body) return;
    if (S.tab === 'ecommerce') renderEcommerce(body);
    else renderRemake(body);
  }

  // ──────────────────────────────────────────────────────────────
  // 视频复刻
  // ──────────────────────────────────────────────────────────────
  function renderRemake(body) {
    body.innerHTML = `
      <div class="setup-card" style="border:1px solid rgba(255,255,255,0.10);border-radius:12px;padding:16px;margin-bottom:16px;">
        <div class="dim" style="font-size:12px;margin-bottom:8px;">
          给一段参考视频 → 拆成可照着做的施工图（镜头表 / 景别 / 运镜 / 色调 / 构图 / 台词 / 节奏）。
          <b>复刻的是手法，不是内容</b>：人物与剧情要重新设计。
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px;">
          <input id="remakePath" class="modal-input" style="flex:1 1 420px;min-width:260px;"
                 placeholder="参考视频的绝对路径，例如 D:\\videos\\ref.mp4">
          <button class="secondary-btn" id="remakeEstimateBtn">① 估算（不花钱）</button>
          <button class="primary-btn" id="remakeRunBtn">② 开始复刻</button>
        </div>
        <div style="display:flex;gap:16px;flex-wrap:wrap;font-size:12px;" class="dim">
          <label><input type="checkbox" id="remakeOptOcr" checked> OCR 硬字幕</label>
          <label><input type="checkbox" id="remakeOptWhisper" checked> 本地 whisper 出时间戳（免费）</label>
          <label><input type="checkbox" id="remakeOptSilicon" checked> 硅基流动校对台词（花钱，约 ¥0.07/3分钟）</label>
          <label>最多分析 <input id="remakeMaxShots" type="number" min="0" max="200" value="24"
                 style="width:64px;" class="modal-input"> 镜（0=全部）</label>
        </div>
      </div>
      <div id="remakeOut"></div>`;

    const est = el('remakeEstimateBtn');
    const run = el('remakeRunBtn');
    if (est) est.addEventListener('click', function () { doRemake(true); });
    if (run) run.addEventListener('click', function () { doRemake(false); });
    if (S.remake) paintRemake(S.remake);
  }

  async function doRemake(dry) {
    const path = (el('remakePath') || {}).value || '';
    if (!path.trim()) { toast('先填参考视频路径', 'error'); return; }
    const btn = dry ? el('remakeEstimateBtn') : el('remakeRunBtn');
    const idle = btn ? btn.textContent : '';
    setBusy(btn, true, idle, dry ? '估算中…' : '复刻中（可能要几分钟）…');
    const out = el('remakeOut');
    if (out && !dry) out.innerHTML = '<div class="dim">正在切镜 / 抽帧 / 量化画面 / 提取台词…（长片可能要几分钟，别关页面）</div>';
    try {
      const payload = {
        video_path: path.trim(),
        dry_run: !!dry,
        use_ocr: !!(el('remakeOptOcr') || {}).checked,
        use_whisper: !!(el('remakeOptWhisper') || {}).checked,
        use_siliconflow: !!(el('remakeOptSilicon') || {}).checked,
        max_shots: Number((el('remakeMaxShots') || {}).value || 0),
      };
      const r = await window.API.tools.remakeAnalyze(payload);
      if (dry) {
        if (out) out.innerHTML =
          '<div style="border:1px solid rgba(255,255,255,0.10);border-radius:10px;padding:14px;">' +
          '<b>估算结果</b>（没有动文件、没花钱）<br>' +
          '<span class="dim">镜头数约 </span><b>' + esc((r.estimated_shots || r.estimated_shots === 0 ? r.estimated_shots : '?')) + '</b>' +
          '<span class="dim"> · 预计花费 </span><b>¥' + esc((r.est && r.est.est_cost_yuan) || r.estimated_cost_yuan || 0) + '</b>' +
          '<span class="dim"> · 预计耗时 </span><b>' + esc(r.estimated_minutes || (r.est && r.est.est_seconds) || '?') + (r.estimated_minutes ? ' 分钟' : ' 秒') + '</b>' +
          (r.source ? '<br><span class="dim">' + esc(r.source.width + '×' + r.source.height + ' · ' + (r.source.duration || 0) + 's · ' + (r.source.aspect || '')) + '</span>' : '') +
          '<br><span class="dim" style="font-size:12px;">确认没问题就点「② 开始复刻」。</span></div>';
        toast('估算完成', 'success');
      } else {
        S.remake = r;
        paintRemake(r);
        toast('复刻完成，规格已落盘', 'success');
      }
    } catch (e) {
      if (out) out.innerHTML = '<div style="color:#e07a5f">失败：' + esc(String(e && e.message || e)) + '</div>';
      toast('复刻失败：' + String(e && e.message || e), 'error');
    } finally {
      setBusy(btn, false, idle);
    }
  }

  function paintRemake(r) {
    const out = el('remakeOut');
    if (!out) return;
    if (!r || !r.ok === false) { /* 兼容 */ }
    const pac = r.pacing || {};
    const ds = r.dialogue_stats || {};
    const asr = r.asr || {};
    const gates = pac.gates || [];
    const colorOf = { pass: '#7ac130', fail: '#e07a5f', unknown: '#8a8a8a', skipped: '#8a8a8a' };
    const gateHtml = gates.map(function (g) {
      return '<span style="display:inline-block;margin:0 10px 6px 0;font-size:12px;">' +
        '<span style="color:' + (colorOf[g.state] || '#8a8a8a') + '">●</span> ' +
        esc(g.title) + ' <b>' + esc(g.value) + '</b> ' +
        '<span class="dim">[' + esc(g.state) + ']</span></span>';
    }).join('');

    const shots = r.shots_preview || [];
    const shotHtml = shots.map(function (s) {
      const dlg = (s.dialogue || []).map(function (d) { return esc(d); }).join(' / ');
      return '<div style="border:1px solid rgba(255,255,255,0.08);border-radius:8px;padding:8px 10px;margin-bottom:6px;">' +
        '<div style="font-size:12px;"><b>#' + esc(s.index) + '</b> ' +
        '<span class="dim">' + esc(fmtSec(s.start)) + '–' + esc(fmtSec(s.end)) + ' (' + esc(fmtSec(s.duration)) + ')</span> ' +
        '<span class="badge">' + esc(s.shot_size || '?') + '</span> ' +
        '<span class="badge">' + esc(s.camera_motion || '?') + '</span> ' +
        (s.palette ? '<span class="badge">' + esc(s.palette) + '</span>' : '') +
        '</div>' +
        (s.description ? '<div class="dim" style="font-size:12px;">' + esc(s.description) + '</div>' : '') +
        (dlg ? '<div style="font-size:12px;">🎙 ' + dlg + '</div>' : '') +
        (s.prompt ? '<div class="dim" style="font-size:11px;margin-top:2px;">prompt: ' + esc(s.prompt) + '</div>' : '') +
        '</div>';
    }).join('');

    out.innerHTML =
      '<div style="display:flex;gap:20px;flex-wrap:wrap;margin-bottom:10px;font-size:13px;">' +
        '<div><span class="dim">镜头</span> <b>' + esc(r.shots) + '</b></div>' +
        '<div><span class="dim">平均/中位/最长</span> <b>' + esc(fmtSec(pac.mean_shot)) + ' / ' + esc(fmtSec(pac.median_shot)) + ' / ' + esc(fmtSec(pac.max_shot)) + '</b></div>' +
        '<div><span class="dim">切点/分钟</span> <b>' + esc(pac.cuts_per_min) + '</b></div>' +
        '<div><span class="dim">节奏</span> <b>' + esc(pac.pace_class) + '</b></div>' +
        '<div><span class="dim">风格档</span> <b>' + esc(r.style_profile_label || r.style_profile) + '</b></div>' +
      '</div>' +
      '<div style="margin-bottom:10px;">' + gateHtml + '</div>' +
      '<div class="dim" style="font-size:12px;margin-bottom:10px;">' +
        '台词 ' + esc(ds.lines || 0) + ' 行 · 字数 ' + esc(ds.chars || 0) +
        ' · ' + esc(ds.chars_per_sec || 0) + ' 字/秒 · 句长变异系数 ' + esc(ds.line_len_cv || 0) +
        (asr.ok ? '　ASR ✓（whisper ' + esc(asr.whisper_model) + '，花费 ¥' + esc(asr.cost_yuan) + '）'
                : '　ASR ✗ ' + esc(asr.error || '')) +
      '</div>' +
      (r.spec_path ? '<div class="dim" style="font-size:12px;margin-bottom:10px;">规格文件：<code>' + esc(r.spec_path) + '</code></div>' : '') +
      ((r.warnings || []).length ? '<div class="dim" style="font-size:12px;color:#e0b25f;margin-bottom:10px;">⚠ ' + (r.warnings || []).map(esc).join('<br>⚠ ') + '</div>' : '') +
      '<div style="margin:12px 0 6px;font-size:13px;"><b>分镜预览</b><span class="dim">（最多 12 镜）</span></div>' +
      shotHtml +
      (r.compliance ? '<div class="dim" style="font-size:12px;margin-top:12px;border-top:1px solid rgba(255,255,255,0.08);padding-top:10px;">' +
        '<b>复刻边界</b><br>可复刻：' + esc((r.compliance.can_replicate || []).join('、')) + '<br>' +
        '不可复刻：' + esc((r.compliance.must_not_replicate || []).join('、')) + '</div>' : '');
  }

  // ──────────────────────────────────────────────────────────────
  // 电商商品视频
  // ──────────────────────────────────────────────────────────────
  async function ensurePlatforms() {
    if (S.cache.platforms && S.cache.categories) return;
    try {
      const r = await window.API.tools.ecommercePlatforms();
      S.cache.platforms = r.platforms || [];
      S.cache.categories = r.categories || [];
    } catch (e) {
      S.cache.platforms = [];
      S.cache.categories = [];
    }
  }

  function renderEcommerce(body) {
    ensurePlatforms().then(function () {
      const plats = S.cache.platforms || [];
      const cats = S.cache.categories || [];
      const pOpts = plats.map(function (p) {
        return '<option value="' + esc(p.id) + '">' + esc(p.name) + '</option>';
      }).join('');
      // ★ 品类**给一个空选项**（2026-10-24 实测踩到）：
      //   原来默认选中第一项（美妆/护肤），用户填「防晒衣」却得到「素颜特写」
      //   这种明显不对的镜头。品类是结构模板的开关，必须让用户自己选；
      //   不选就走规则里的通用四段（default_beats），比选错强。
      const cOpts = '<option value="">（不选，用通用四段）</option>' +
        cats.map(function (c) {
          return '<option value="' + esc(c.id) + '">' + esc(c.name) + '</option>';
        }).join('');

      body.innerHTML = `
        <div style="border:1px solid rgba(255,255,255,0.10);border-radius:12px;padding:16px;margin-bottom:16px;">
          <div class="dim" style="font-size:12px;margin-bottom:10px;">
            商品 → 视频方案。按品类转化漏斗排段（各品类有各自的必须/禁止项），
            时长精确切到目标秒数（能直接过剧本体检）。<b>纯本地、不花钱。</b>
          </div>
          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;margin-bottom:10px;">
            <label style="font-size:12px;">商品名<input id="ecomProduct" class="modal-input" style="width:100%;" placeholder="轻薄防晒衣"></label>
            <label style="font-size:12px;">平台<select id="ecomPlatform" class="modal-input" style="width:100%;">${pOpts}</select></label>
            <label style="font-size:12px;">品类<select id="ecomCategory" class="modal-input" style="width:100%;">${cOpts}</select></label>
            <label style="font-size:12px;">目标时长(s)<input id="ecomDuration" type="number" min="0" class="modal-input" style="width:100%;" placeholder="不填用平台推荐值"></label>
            <label style="font-size:12px;">价格/优惠<input id="ecomPrice" class="modal-input" style="width:100%;" placeholder="到手 89 元"></label>
          </div>
          <label style="font-size:12px;display:block;margin-bottom:10px;">卖点（一行一个，或用顿号分隔）
            <textarea id="ecomPoints" class="modal-input" rows="3" style="width:100%;"
              placeholder="UPF50+&#10;重量 90g&#10;可收纳进巴掌"></textarea>
          </label>
          <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
            <button class="secondary-btn" id="ecomRewriteBtn">✍ 卖点改成可拍摄</button>
            <button class="primary-btn" id="ecomPlanBtn">生成方案</button>
            <span class="dim" style="font-size:12px;">AI 标识：</span>
            <label style="font-size:12px;"><input type="radio" name="ecomAiLabel" value="off"> 不加</label>
            <label style="font-size:12px;"><input type="radio" name="ecomAiLabel" value="plan" checked> 只给方案</label>
            <label style="font-size:12px;"><input type="radio" name="ecomAiLabel" value="apply"> 真烧进画面</label>
          </div>
        </div>

        <div style="border:1px solid rgba(255,255,255,0.10);border-radius:12px;padding:16px;margin-bottom:16px;">
          <div class="dim" style="font-size:12px;margin-bottom:8px;">文案合规体检（广告法绝对化用语 / 化妆品医疗暗示 / 食品保健 / 三品一械 / 平台红线）</div>
          <textarea id="ecomAuditText" class="modal-input" rows="3" style="width:100%;margin-bottom:8px;"
            placeholder="把要发的口播稿/字幕贴进来…"></textarea>
          <button class="secondary-btn" id="ecomAuditBtn">体检这段文案</button>
        </div>

        <div id="ecomOut"></div>
        <div id="ecomAuditOut"></div>`;

      const rb = el('ecomRewriteBtn');
      if (rb) rb.addEventListener('click', doRewritePoints);
      const pb = el('ecomPlanBtn');
      if (pb) pb.addEventListener('click', doPlan);
      const ab = el('ecomAuditBtn');
      if (ab) ab.addEventListener('click', doAudit);
      if (S.plan) paintPlan(S.plan);
    });
  }

  function pointsOf() {
    const t = (el('ecomPoints') || {}).value || '';
    return t.split(/[\n、,，;；]+/).map(function (x) { return x.trim(); }).filter(Boolean);
  }

  function aiLabelChoice() {
    const r = document.querySelector('input[name="ecomAiLabel"]:checked');
    return r ? r.value : 'plan';
  }

  async function doRewritePoints() {
    const pts = pointsOf();
    if (!pts.length) { toast('先填卖点', 'error'); return; }
    const btn = el('ecomRewriteBtn');
    const idle = btn ? btn.textContent : '';
    setBusy(btn, true, idle, '改写着…');
    try {
      const r = await window.API.tools.ecommerceSellingPoints(pts);
      const items = r.items || [];
      const kindZh = {
        table: '命中对照表', already_measurable: '已有数字（不改）',
        claim_needs_proof: '主张缺数字', unknown: '判断不了',
        quality: '质量类', value: '划算类', effect: '效果类',
        social: '口碑类', feel: '触感类',
      };
      el('ecomAuditOut').innerHTML =
        '<div style="border:1px solid rgba(255,255,255,0.10);border-radius:10px;padding:14px;">' +
        '<b>卖点改写</b> <span class="dim">（方向建议，落地要换成你真能拍/能测的内容）</span><div style="margin-top:8px;">' +
        items.map(function (x) {
          const zh = kindZh[x.kind] || x.kind;
          const color = x.changed ? '#7ac130' : (x.shootable ? '#8a8a8a' : '#e0b25f');
          return '<div style="margin-bottom:8px;font-size:12px;">' +
            '<span style="color:' + color + '">●</span> <b>' + esc(x.original) + '</b>' +
            ' <span class="dim">[' + esc(zh) + ']</span><br>' +
            '<span style="margin-left:14px;">→ ' + esc(x.rewritten) + '</span>' +
            (x.why ? '<br><span class="dim" style="margin-left:14px;font-size:11px;">' + esc(x.why) + '</span>' : '') +
            '</div>';
        }).join('') +
        ((r.still_vague || []).length ? '<div style="color:#e0b25f;font-size:12px;margin-top:6px;">仍需人工补事实：' + (r.still_vague || []).map(esc).join('、') + '</div>' : '') +
        '</div></div>';
      toast('卖点改写完成', 'success');
    } catch (e) {
      toast('改写失败：' + String(e && e.message || e), 'error');
    } finally {
      setBusy(btn, false, idle);
    }
  }

  async function doPlan() {
    const prod = (el('ecomProduct') || {}).value || '';
    if (!prod.trim()) { toast('先填商品名', 'error'); return; }
    const btn = el('ecomPlanBtn');
    const idle = btn ? btn.textContent : '';
    setBusy(btn, true, idle, '生成中…');
    try {
      const r = await window.API.tools.ecommercePlan({
        product: prod.trim(),
        platform: (el('ecomPlatform') || {}).value || '',
        category: (el('ecomCategory') || {}).value || '',
        selling_points: pointsOf(),
        duration_sec: Number((el('ecomDuration') || {}).value || 0),
        price_line: (el('ecomPrice') || {}).value || '',
        ai_label: aiLabelChoice(),
      });
      S.plan = r;
      paintPlan(r);
      toast('方案已生成', 'success');
    } catch (e) {
      el('ecomOut').innerHTML = '<div style="color:#e07a5f">失败：' + esc(String(e && e.message || e)) + '</div>';
      toast('生成失败：' + String(e && e.message || e), 'error');
    } finally {
      setBusy(btn, false, idle);
    }
  }

  function paintPlan(r) {
    const out = el('ecomOut');
    if (!out) return;
    const comp = r.compliance || {};
    const lab = r.ai_label || {};
    const pc = r.platform_check || {};
    const beats = r.beats || [];
    const scenes = r.scenes || [];

    // 时间轴
    const maxEnd = Math.max.apply(null, beats.map(function (b) { return b.end || 0; }).concat([1]));
    const bar = beats.map(function (b) {
      const w = ((b.end - b.start) / maxEnd * 100) || 0;
      return '<div title="' + esc(b.beat + ' ' + b.start + '–' + b.end + 's') + '" ' +
        'style="width:' + w + '%;background:rgba(122,193,48,0.22);border-right:1px solid rgba(0,0,0,0.4);' +
        'font-size:10px;padding:2px 3px;overflow:hidden;white-space:nowrap;">' + esc(b.beat) + '</div>';
    }).join('');

    // 合规
    const hits = (comp.hits || []);
    const hitHtml = hits.length
      ? hits.map(function (h) {
          return '<div style="font-size:12px;color:' + (h.severity === 'error' ? '#e07a5f' : '#e0b25f') + ';">' +
            (h.severity === 'error' ? '✗' : '⚠') + ' 「' + esc(h.term) + '」 ' + esc(h.why || '') + '</div>';
        }).join('')
      : '<div class="dim" style="font-size:12px;">未命中违禁词 ✓</div>';

    // AI 标识
    const labColor = lab.choice === 'off' ? '#e07a5f' : (lab.choice === 'apply' ? '#7ac130' : '#8a8a8a');
    const labHtml =
      '<div style="font-size:12px;color:' + labColor + ';">● 选择：<b>' +
      esc({ off: '不加', plan: '只给方案', apply: '出片后真烧' }[lab.choice] || lab.choice) + '</b> — ' +
      esc(lab.text || '') + '</div>' +
      (lab.warning ? '<div style="font-size:12px;color:#e0b25f;margin-top:4px;">' + esc(lab.warning) + '</div>' : '') +
      (lab.next_step ? '<div class="dim" style="font-size:11px;margin-top:4px;">下一步：' + esc(lab.next_step) + '</div>' : '') +
      ((lab.manual_steps || []).length ? '<div class="dim" style="font-size:11px;margin-top:4px;">⚠ 还需你做：' + (lab.manual_steps || []).map(function (m) { return esc(m.kind); }).join('、') + '</div>' : '');

    const rw = r.selling_point_rewrite || {};
    const scenesHtml = scenes.map(function (s) {
      const dlg = (s.dialogues || []).map(function (d) { return esc(d.character || '') + '：' + esc(d.text || ''); }).join(' / ');
      return '<div style="border:1px solid rgba(255,255,255,0.08);border-radius:8px;padding:8px 10px;margin-bottom:6px;">' +
        '<div style="font-size:12px;"><b>#' + esc(s.scene_number) + ' ' + esc(s.title) + '</b> ' +
        '<span class="dim">' + esc(fmtSec(s.duration_seconds)) + '</span> ' +
        '<span class="badge">' + esc(s.beat) + '</span></div>' +
        '<div class="dim" style="font-size:12px;">📷 ' + esc((s.actions || []).join('；')) + '</div>' +
        (dlg ? '<div style="font-size:12px;">🎙 ' + dlg + '</div>' : '') +
        (((s.must_not || []).length) ? '<div class="dim" style="font-size:11px;color:#e0b25f;">✗ 不许：' + (s.must_not || []).map(esc).join('、') + '</div>' : '') +
        '</div>';
    }).join('');

    out.innerHTML =
      '<div style="display:flex;gap:20px;flex-wrap:wrap;margin-bottom:10px;font-size:13px;">' +
        '<div><span class="dim">商品</span> <b>' + esc(r.product) + '</b></div>' +
        '<div><span class="dim">平台</span> <b>' + esc(r.platform && r.platform.name_zh) + '</b></div>' +
        '<div><span class="dim">品类</span> <b>' + esc(r.category && r.category.name_zh) + '</b></div>' +
        '<div><span class="dim">时长</span> <b>' + esc(fmtSec(r.target_duration)) + '</b></div>' +
        '<div><span class="dim">合规</span> <b style="color:' + (comp.errors ? '#e07a5f' : '#7ac130') + '">' +
          (comp.errors || 0) + ' 错误 / ' + (comp.warnings || 0) + ' 待举证</b></div>' +
      '</div>' +
      '<div style="display:flex;height:26px;border-radius:6px;overflow:hidden;margin-bottom:12px;">' + bar + '</div>' +
      ((pc.checks || []).length ? '<div class="dim" style="font-size:12px;margin-bottom:10px;">平台核对：' +
        (pc.checks || []).map(function (c) {
          const col = c.level === 'pass' ? '#7ac130' : (c.level === 'fail' ? '#e07a5f' : '#8a8a8a');
          return '<span style="color:' + col + '">●</span> ' + esc(c.title) + ' <span class="dim">' + esc(c.detail) + '</span>';
        }).join('　') + '</div>' : '') +
      '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px;">' +
        '<div style="border:1px solid rgba(255,255,255,0.10);border-radius:10px;padding:12px;">' +
          '<b style="font-size:13px;">合规体检</b>' + hitHtml + '</div>' +
        '<div style="border:1px solid rgba(255,255,255,0.10);border-radius:10px;padding:12px;">' +
          '<b style="font-size:13px;">AI 内容标识（可选项）</b>' + labHtml + '</div>' +
      '</div>' +
      (rw.changed ? '<div class="dim" style="font-size:12px;margin-bottom:10px;color:#7ac130;">✍ 已把 ' + esc(rw.changed) + ' 个空泛卖点改写成可拍摄表述（原话见 selling_points_raw）</div>' : '') +
      ((r.warnings || []).length ? '<div class="dim" style="font-size:12px;color:#e0b25f;margin-bottom:10px;">⚠ ' + (r.warnings || []).map(esc).join('<br>⚠ ') + '</div>' : '') +
      '<div style="margin:12px 0 6px;font-size:13px;"><b>分镜（' + scenes.length + ' 场）</b></div>' + scenesHtml +
      '<div class="dim" style="font-size:11px;margin-top:10px;">scenes 用的是 VideoForge 剧本 schema，可直接进剧本体检 / 分镜规划。</div>';
  }

  async function doAudit() {
    const t = (el('ecomAuditText') || {}).value || '';
    if (!t.trim()) { toast('先贴要检查的文案', 'error'); return; }
    const btn = el('ecomAuditBtn');
    const idle = btn ? btn.textContent : '';
    setBusy(btn, true, idle, '体检中…');
    try {
      const r = await window.API.tools.ecommerceAudit(t);
      const hits = r.hits || [];
      el('ecomAuditOut').innerHTML =
        '<div style="border:1px solid rgba(255,255,255,0.10);border-radius:10px;padding:14px;margin-top:12px;">' +
        '<b>合规体检</b>　<span class="dim">得分</span> <b style="color:' +
        (r.errors ? '#e07a5f' : '#7ac130') + '">' + esc(r.score) + '</b>' +
        '　<span class="dim">' + esc(r.errors) + ' 错误 / ' + esc(r.warnings) + ' 待举证</span>' +
        (hits.length
          ? '<div style="margin-top:8px;">' + hits.map(function (h) {
              return '<div style="font-size:12px;color:' + (h.severity === 'error' ? '#e07a5f' : '#e0b25f') + ';">' +
                (h.severity === 'error' ? '✗' : '⚠') + ' 「' + esc(h.term) + '」' +
                ' <span class="dim">[' + esc(h.category) + ']</span> ' + esc(h.why || '') + '</div>';
            }).join('') + '</div>'
          : '<div class="dim" style="font-size:12px;margin-top:6px;">未命中违禁词 ✓</div>') +
        '<div class="dim" style="font-size:11px;margin-top:8px;">errors 必须改；warnings 是「需举证」不是「必须删」——商标含「最」、限定时空的销量事实都合法。</div>' +
        '</div>';
      toast('体检完成', 'success');
    } catch (e) {
      toast('体检失败：' + String(e && e.message || e), 'error');
    } finally {
      setBusy(btn, false, idle);
    }
  }

  // ══════════════════════════════════════════════════════════════
  // 导出 + 自动挂到导航
  // ══════════════════════════════════════════════════════════════
  window.renderTools = renderTools;

  // 导航切到 tools 时调 renderTools（app.js 的 switch 里已加 case）
  // 这里再兜一层：万一 app.js 的 case 没生效，页面也不会空白。
  document.addEventListener('DOMContentLoaded', function () {
    try {
      if (window.State && window.State.currentPage === 'tools') renderTools();
    } catch (e) { /* 忽略 */ }
  });

  console.log('[VideoForge] 创作工具页已加载（视频复刻 / 电商商品视频）');
})();

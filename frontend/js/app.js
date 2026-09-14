/**
 * VideoForge · 主应用（v2 - 对话优先）
 */

const State = {
  currentProject: null,
  projects: [],
  videoProviders: [],
  settings: {},
  currentPage: 'chat',
  sceneDetails: {},        // sceneNumber -> detail obj
};

const ProviderMap = {};

/* ============================================================
 * 自绘弹窗系统的必要性：
 *   WebView2 / pywebview 不实现 window.prompt() / confirm()，
 *   调用会静默返回 null（用户看不到任何输入框 → 「点了没反应」）。
 *   因此本应用禁止使用原生 prompt/confirm/alert，一律走 Modal。
 * ============================================================ */
const Modal = {
  _resolve: null,

  el() { return document.getElementById('appModal'); },

  /** 打开弹窗；html 为表单区内容 */
  open({ title, html, okText = '确定', cancelText = '取消', onOk, wide = false, bind }) {
    const m = this.el();
    if (!m) {
      // 极端兜底：连弹窗骨架都没有时，用原生输入（浏览器调试场景）
      const v = window.prompt && window.prompt(title + '\n（弹窗组件缺失，临时降级）');
      return Promise.resolve(v ? { _fallback: v } : null);
    }
    this.close();
    m.classList.add('show');
    m.querySelector('.modal-box').classList.toggle('wide', !!wide);
    m.querySelector('#appModalTitle').innerHTML = title;
    m.querySelector('#appModalBody').innerHTML = html || '';
    m.querySelector('#appModalOk').textContent = okText;
    m.querySelector('#appModalCancel').textContent = cancelText;
    m.querySelector('#appModalOk').style.display = okText === null ? 'none' : '';
    setTimeout(() => {
      const first = m.querySelector('#appModalBody input, #appModalBody textarea, #appModalBody select');
      if (first) try { first.focus(); } catch (e) {}
    }, 30);
    // 字段联动（例如：换厂商 → 自动带出该厂商默认模型名）
    if (typeof bind === 'function') { try { bind(m.querySelector('#appModalBody')); } catch (e) { console.warn(e); } }

    return new Promise(resolve => {
      this._resolve = resolve;
      const done = (val) => {
        const r = this._resolve; this._resolve = null;
        this.close();
        if (r) r(val);
      };
      m.querySelector('#appModalOk').onclick = async () => {
        let payload = this.readFields();
        payload = payload || {};
        if (onOk) {
          let ret;
          try { ret = await onOk(payload, done); }
          catch (e) { Toast.error('操作失败: ' + e.message); return; }
          if (ret === false) return;        // 校验未通过，保持弹窗
          if (ret !== undefined && ret !== true) return done(ret);
        }
        done(payload);
      };
      m.querySelector('#appModalCancel').onclick = () => done(null);
      m.querySelector('#appModalClose').onclick = () => done(null);
      m.onclick = (e) => { if (e.target === m) done(null); };
      m.querySelectorAll('[data-modal-cancel]').forEach(b => b.onclick = () => done(null));
    });
  },

  close() {
    const m = this.el();
    if (m) m.classList.remove('show');
  },

  /** 读取弹窗内所有具名字段（data-field） */
  readFields() {
    const m = this.el();
    if (!m) return null;
    const out = {};
    m.querySelectorAll('#appModalBody [data-field]').forEach(f => {
      out[f.dataset.field] = f.type === 'checkbox' ? f.checked : f.value.trim();
    });
    return out;
  },

  /** 确认框 → Promise<boolean> */
  async confirm(message, { title = '请确认', okText = '确定', danger = false } = {}) {
    const r = await this.open({
      title: `<span class="${danger ? 'danger-text' : ''}">${title}</span>`,
      html: `<div class="modal-text">${message}</div>`,
      okText,
    });
    return r !== null;
  },

  /** 表单框 → Promise<fields|null>；fields: [{key,label,type,value,placeholder,options,hint,rows}] */
  async form(title, fields, { okText = '确定', wide = false, desc = '', bind } = {}) {
    const html = (desc ? `<div class="modal-text">${desc}</div>` : '') + fields.map(f => {
      const label = `<label class="modal-label">${f.label}${f.hint ? `<span class="modal-hint">${f.hint}</span>` : ''}</label>`;
      if (f.type === 'select') {
        return `<div class="modal-field">${label}<select data-field="${f.key}" class="modal-input">
          ${(f.options || []).map(o => {
            const val = typeof o === 'string' ? o : o.value;
            const text = typeof o === 'string' ? o : o.label;
            return `<option value="${escapeHtml(val)}" ${val === f.value ? 'selected' : ''}>${escapeHtml(text)}</option>`;
          }).join('')}
        </select></div>`;
      }
      if (f.type === 'textarea') {
        return `<div class="modal-field">${label}<textarea data-field="${f.key}" class="modal-input" rows="${f.rows || 3}" placeholder="${escapeHtml(f.placeholder || '')}">${escapeHtml(f.value || '')}</textarea></div>`;
      }
      if (f.type === 'checkbox') {
        return `<div class="modal-field modal-check"><label><input type="checkbox" data-field="${f.key}" ${f.value ? 'checked' : ''}> ${f.label}</label></div>`;
      }
      return `<div class="modal-field">${label}<input data-field="${f.key}" class="modal-input" type="${f.type || 'text'}" value="${escapeHtml(f.value ?? '')}" placeholder="${escapeHtml(f.placeholder || '')}"></div>`;
    }).join('');
    return await this.open({ title, html, okText, wide, bind });
  },
};

async function init() {
  // 主题
  const savedTheme = localStorage.getItem('theme') || 'dark';
  document.documentElement.setAttribute('data-theme', savedTheme);

  // 视频 provider
  try {
    const data = await API.listProviders();
    State.videoProviders = data.providers || [];
    data.providers.forEach(p => { ProviderMap[p.name] = p; });
  } catch (e) {
    Toast.error('加载视频模型失败: ' + e.message);
  }

  // 设置
  try {
    State.settings = await API.settings.get();
  } catch (e) {
    console.error('Load settings failed:', e);
  }

  // 项目
  await loadProjects();

  // 健康检查
  try {
    const health = await API.health();
    const el = document.getElementById('ffmpegStatus');
    if (el) {
      el.textContent = `FFmpeg: ${health.ffmpeg_available ? 'OK' : 'MISSING'}`;
      el.classList.toggle('dim', !health.ffmpeg_available);
    }
  } catch (e) {
    console.error('Health check failed:', e);
  }

  // 初始化 Chat
  await Chat.init();

  bindEvents();
  // 绑定聊天输入框 Enter 发送 + 自动增高
  if (typeof window.bindChatInputKeys === 'function') {
    window.bindChatInputKeys();
  }
  navigateTo('chat');

  // 深链接支持：?page=script&project=<id>（便于调试、分享与外部唤起）
  const params = new URLSearchParams(location.search);
  const pageParam = params.get('page');
  const projectParam = params.get('project');
  if (projectParam) {
    try { await selectProject(projectParam, { navigate: false }); } catch (e) { /* 忽略 */ }
  }
  if (pageParam) navigateTo(pageParam);

  // 开发自检：?selftest=enter —— 模拟在对话框输入并按 Enter，验证"Enter 发送"链路
  // 结果写入 document.title 与页面横幅（便于无头浏览器截图/抓取核对）
  if (params.get('selftest') === 'enter') {
    const banner = (text, ok) => {
      let el = document.getElementById('selftestBanner');
      if (!el) {
        el = document.createElement('div');
        el.id = 'selftestBanner';
        el.style.cssText = 'position:fixed;left:50%;top:12px;transform:translateX(-50%);z-index:99999;'
          + 'padding:14px 26px;border-radius:10px;font:600 20px/1.4 system-ui;color:#fff;'
          + 'box-shadow:0 10px 30px rgba(0,0,0,.4);background:' + (ok ? '#2f9e44' : '#c92a2a');
        document.body.appendChild(el);
      }
      el.textContent = text;
      document.title = text;
    };
    setTimeout(() => {
      try {
        const input = document.getElementById('chatInput');
        if (!input) { banner('SELFTEST-ERROR 找不到 #chatInput', false); return; }
        const before = document.querySelectorAll('#chatMessages .msg').length;
        input.value = '自检：Enter 发送链路测试';
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
        setTimeout(() => {
          const after = document.querySelectorAll('#chatMessages .msg').length;
          const cleared = input.value === '';
          const pass = after > before && cleared;
          banner(pass ? `SELFTEST-PASS 消息 ${before}→${after} 输入框已清空`
                      : `SELFTEST-FAIL before=${before} after=${after} cleared=${cleared}`, pass);
        }, 600);
      } catch (e) {
        banner('SELFTEST-ERROR ' + e.message, false);
      }
    }, 1200);
  }

  // 开发自检：?selftest=library —— 打开资产库导入弹窗（角色），验证渲染
  if (params.get('selftest') === 'library') {
    setTimeout(async () => {
      try {
        await openLibraryModal('characters');
        setTimeout(() => {
          const n = document.querySelectorAll('#libraryList .library-item').length;
          const cnt = document.getElementById('libraryCount')?.textContent || '';
          const banner = document.createElement('div');
          banner.style.cssText = 'position:fixed;left:50%;top:10px;transform:translateX(-50%);z-index:99999;'
            + 'padding:12px 22px;border-radius:10px;font:600 18px/1.4 system-ui;color:#fff;'
            + 'box-shadow:0 10px 30px rgba(0,0,0,.4);background:' + (n > 0 ? '#2f9e44' : '#c92a2a');
          banner.textContent = `LIBRARY-SELFTEST 资产条目=${n} ${cnt}`;
          document.body.appendChild(banner);
        }, 900);
      } catch (e) {
        console.error(e);
      }
    }, 1500);
  }

  // 开发自检：?selftest=batch3 —— 覆盖本轮三个问题（图片实例 / 生成弹窗 / 换模型弹窗）
  //   加 &view=chars | imgmodal | model 可停在某一步用于截图；不带 view 则自动跑完并输出结果
  if (params.get('selftest') === 'batch3') {
    const view = params.get('view') || 'all';
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    const banner = (text, ok) => {
      let el = document.getElementById('selftestBanner');
      if (!el) { el = document.createElement('div'); el.id = 'selftestBanner'; document.body.appendChild(el); }
      el.style.cssText = 'position:fixed;left:50%;top:12px;transform:translateX(-50%);z-index:99999;'
        + 'padding:14px 26px;border-radius:10px;font:600 19px/1.5 system-ui;color:#fff;max-width:92vw;'
        + 'box-shadow:0 10px 30px rgba(0,0,0,.4);background:' + (ok ? '#2f9e44' : '#c92a2a');
      el.textContent = text;
      document.title = text;
    };
    const results = [];
    const rec = (k, v, pass) => { results.push({ k, v, pass }); return pass; };

    (async () => {
      try {
        const pid = params.get('pid');
        const proj = (pid && State.projects.find(p => p.id === pid))
          || State.projects.find(p => (p.name || p.title || '').includes('抗日'))
          || State.projects[0];
        if (!proj) return banner('BATCH3-ERROR 没有可用项目', false);
        await selectProject(proj.id, { navigate: false });
        navigateTo('assets');   // 注意：角色页的 page id 是 assets（不是 characters）
        await sleep(600);
        await renderCharacters();
        await sleep(2500);   // 等图片真正解码（local-image 走 HTTP）

        rec('通用弹窗组件存在', !!Modal.el(), !!Modal.el());

        const imgs = Array.from(document.querySelectorAll('#characterGrid .asset-card img'));
        const loaded = imgs.filter(i => i.complete && i.naturalWidth > 0);
        const allLocalApi = imgs.length > 0 && imgs.every(i => (i.getAttribute('src') || '').startsWith('/api/local-image'));
        rec('角色卡 <img> 数量', imgs.length, imgs.length > 0);
        rec('图片真实解码成功', loaded.length, loaded.length > 0);
        rec('图片全部走 /api/local-image', allLocalApi, allLocalApi);
        const genBtns = document.querySelectorAll('#characterGrid [data-action="genimg"]').length;
        rec('每张角色卡都有生成按钮', genBtns, genBtns === State.characters.length && genBtns > 0);

        if (view === 'chars') {
          banner(`CHARS ${State.characters.length} 角色 / ${loaded.length} 张图已解码`, loaded.length > 0);
          return;
        }

        const genBtn = document.querySelector('#characterGrid [data-action="genimg"]');
        if (!genBtn) return banner('BATCH3-ERROR 找不到生成按钮', false);
        genBtn.click();
        await sleep(900);
        const modal = Modal.el();
        const shown = !!modal && modal.classList.contains('show');
        const fields = Array.from(document.querySelectorAll('#appModalBody [data-field]')).map(f => f.dataset.field);
        const need = ['provider', 'model', 'size', 'prompt'];
        rec('生成形象弹窗已弹出', shown, shown);
        rec('弹窗字段齐全', fields.join(','), need.every(n => fields.includes(n)));
        const provSel = document.querySelector('#appModalBody [data-field="provider"]');
        rec('图像模型可下拉选择', provSel && provSel.options.length,
          provSel ? Array.from(provSel.options).map(o => o.text).join(' | ') : '无');
        const pv = document.querySelector('#appModalBody [data-field="prompt"]');
        rec('提示词已按角色预填', !!(pv && pv.value && pv.value.length > 10), pv ? pv.value.slice(0, 34) + '…' : '空');

        if (view === 'imgmodal') {
          banner(`IMG-MODAL 字段=${fields.join('/')} 模型=${provSel ? provSel.options.length : 0}个`, shown);
          return;
        }
        Modal.close();
        await sleep(400);

        // 换视频模型弹窗（问题①：原来用 prompt()，WebView2 中根本不弹窗）
        navigateTo('storyboard');
        await sleep(800);
        await renderStoryboard();
        await sleep(900);
        const mBtn = document.querySelector('#shotList [data-action="model"]');
        if (!mBtn) {
          rec('分镜存在（可测换模型）', false, false);
        } else {
          mBtn.click();
          await sleep(900);
          const mShown = !!modal && modal.classList.contains('show');
          rec('换模型弹窗已弹出', mShown, mShown);
          const mSel = document.querySelector('#appModalBody [data-field="model_provider"]');
          const nOpt = mSel ? mSel.options.length : 0;
          rec('厂商下拉可选数量', nOpt, mShown && nOpt >= 2);
          let selected = '';
          if (mSel && nOpt) {
            mSel.value = mSel.options[Math.min(1, nOpt - 1)].value;
            selected = mSel.selectedOptions[0] ? mSel.selectedOptions[0].text : mSel.value;
          }
          rec('可把下拉切到指定厂商', selected, !!selected);

          if (view === 'model') {
            banner(`MODEL-MODAL 厂商 ${nOpt} 个，已选中「${selected}」`, mShown && nOpt >= 2);
            return;
          }
          Modal.close();
        }
        await sleep(600);
        const bad = results.filter(r => !r.pass);
        banner(`BATCH3-${bad.length ? 'FAIL ' + bad.map(b => b.k + '=' + b.v).join('; ')
          : 'PASS ' + results.length + ' 项全通过'}`, bad.length === 0);
        console.table(results);
      } catch (e) {
        banner('BATCH3-ERROR ' + e.message, false);
        console.error(e);
      }
    })();
  }
  // 开发自检：?selftest=batch4 —— 生成页 / 后期页的按钮与元素是否真的就位
  if (params.get('selftest') === 'batch4') {
    const view = params.get('view') || 'all';
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    const banner = (text, ok) => {
      let el = document.getElementById('selftestBanner');
      if (!el) { el = document.createElement('div'); el.id = 'selftestBanner'; document.body.appendChild(el); }
      el.style.cssText = 'position:fixed;left:50%;top:12px;transform:translateX(-50%);z-index:99999;'
        + 'padding:14px 26px;border-radius:10px;font:600 19px/1.5 system-ui;color:#fff;max-width:92vw;'
        + 'box-shadow:0 10px 30px rgba(0,0,0,.4);background:' + (ok ? '#2f9e44' : '#c92a2a');
      el.textContent = text; document.title = text;
    };
    const results = [];
    const rec = (k, v, pass) => { results.push({ k, v, pass }); return pass; };
    (async () => {
      try {
        const pid = params.get('pid');
        const proj = (pid && State.projects.find(p => p.id === pid))
          || State.projects[0];
        if (!proj) return banner('BATCH4-ERROR 没有可用项目', false);
        await selectProject(proj.id, { navigate: false });

        // ── 生成页 ──
        navigateTo('generate');
        await sleep(2800);
        const need = ['genVoice', 'genDuration', 'genAspect', 'genImageProvider',
                      'genWithVoice', 'genAutoImages', 'genAllBtn', 'genRefreshBtn'];
        const missing = need.filter(id => !document.getElementById(id));
        rec('生成页工具栏元素齐全', missing.join(',') || 'ok', missing.length === 0);
        const durSel = document.getElementById('genDuration');
        const has5 = !!(durSel && Array.from(durSel.options).some(o => o.value === '5'));
        rec('时长可选 5 秒', has5 ? '有' : '无', has5);
        const cards = document.querySelectorAll('#genShotList .gen-card').length;
        rec('分镜卡片渲染', cards, cards > 0);
        const genBtns = document.querySelectorAll('#genShotList [data-act="gen"]').length;
        rec('每张卡都有生成按钮', genBtns, genBtns === cards && cards > 0);
        const voiceOpts = (document.getElementById('genVoice') || {}).options;
        const nv = voiceOpts ? voiceOpts.length : 0;
        rec('音色下拉已填充', nv, nv > 1);

        if (view === 'generate') {
          banner(`GEN-PAGE 卡片=${cards} 生成按钮=${genBtns} 音色=${nv}`, missing.length === 0 && cards > 0);
          return;
        }

        // ── 后期页 ──
        navigateTo('postprocess');
        await sleep(3000);
        const ppNeed = ['stitchBtn', 'refreshOutputsBtn', 'exportSource', 'exportAspect',
                        'exportAspectBtn', 'voiceSelect', 'voiceTarget', 'applyVoiceBtn',
                        'bgmTarget', 'bgmFile', 'addBgmBtn', 'outputDirInput',
                        'browseOutputDirBtn', 'saveOutputDirBtn', 'openOutputDirBtn',
                        'composeStartBtn'];
        const ppMissing = ppNeed.filter(id => !document.getElementById(id));
        rec('后期页元素齐全', ppMissing.join(',') || 'ok', ppMissing.length === 0);
        const outItems = document.querySelectorAll('#outputList .output-item').length;
        rec('产出文件列表已渲染', outItems, outItems > 0);
        const srcSel = document.getElementById('exportSource');
        const srcOpts = srcSel ? srcSel.options.length : 0;
        const hasSrc = !!(srcSel && Array.from(srcSel.options).some(o => o.value));
        rec('源文件下拉有真实文件', hasSrc ? 'yes' : 'no', hasSrc);
        const applyEnabled = !(document.getElementById('applyVoiceBtn') || {}).disabled;
        rec('配音按钮可用', applyEnabled ? 'enabled' : 'disabled', applyEnabled);
        const dirVal = (document.getElementById('outputDirInput') || {}).value || '';
        rec('输出目录已回填', dirVal ? 'yes' : 'no', !!dirVal);

        if (view === 'postprocess') {
          banner(`PP-PAGE 产出=${outItems} 源=${srcOpts} 目录=${dirVal ? '已回填' : '空'}`,
                 ppMissing.length === 0 && outItems > 0);
          return;
        }
        const bad = results.filter(r => !r.pass);
        banner(`BATCH4-${bad.length ? 'FAIL ' + bad.map(b => b.k + '=' + b.v).join('; ')
          : 'PASS ' + results.length + ' 项全通过'}`, bad.length === 0);
        console.table(results);
      } catch (e) {
        banner('BATCH4-ERROR ' + e.message, false);
        console.error(e);
      }
    })();
  }
}


// 安全绑定：元素不存在时跳过，绝不因单个元素缺失中断后续所有绑定
function on(id, event, handler) {
  const el = document.getElementById(id);
  if (!el) { console.warn(`[VideoForge] 元素缺失，跳过绑定: #${id}`); return null; }
  el.addEventListener(event, handler);
  return el;
}

function bindEvents() {
  on('themeBtn', 'click', toggleTheme);
  on('aiSettingsBtn', 'click', openSettings);
  on('closeSettingsBtn', 'click', closeSettings);
  on('saveSettingsBtn', 'click', saveSettings);
  on('testLlmBtn', 'click', testLlm);
  // 语音合成连通性测试（真合成一小段，验证 Key 能不能用）
  on('testTtsBtn', 'click', testTtsProvider);
  on('cancelProgressBtn', 'click', () => Progress.hide());

  // 新建短片 → 跳主页
  on('newProjectBtn', 'click', () => navigateTo('home'));

  // 导航
  document.querySelectorAll('.nav-item[data-page]').forEach(btn => {
    btn.addEventListener('click', () => navigateTo(btn.dataset.page));
  });

  // 主页
  on('heroCreateBtn', 'click', createFromHero);
  on('abstractBtn', 'click', abstractAndCreate);

  // 剧本
  on('ideaToOutlineBtn', 'click', ideaToOutline);   // 想法 → 大纲 → 剧本（专业版）
  on('regenScriptBtn', 'click', regenerateScript);
  on('saveScriptBtn', 'click', () => Toast.info('已自动保存'));
  on('importScriptBtn', 'click', openImportScript);
  on('genAllDetailsBtn', 'click', generateAllSceneDetails);
  on('auditScriptBtn', 'click', auditScript);
  on('openSceneDetailBtn', 'click', openSceneDetailModal);
  on('closeSceneDetailBtn', 'click', closeSceneDetailModal);
  on('generateSceneDetailBtn', 'click', generateSceneDetail);

  // 角色 / 场景
  on('addCharacterBtn', 'click', addCharacter);
  on('addSceneBtn', 'click', addScene);
  // 资产库联动：从剧本提取 + 跨剧本导入
  on('charFromScriptBtn', 'click', () => extractFromScript('characters'));
  on('sceneFromScriptBtn', 'click', () => extractFromScript('scenes'));
  // ♻ 细化设定：用 AI 重新设计外貌/服装并拆成固定字段（跨镜头一致的保障）
  on('charRefineBtn', 'click', () => extractFromScript('characters', { force: true }));
  on('sceneRefineBtn', 'click', () => extractFromScript('scenes', { force: true }));
  on('charFromLibraryBtn', 'click', () => openLibraryModal('characters'));
  on('sceneFromLibraryBtn', 'click', () => openLibraryModal('scenes'));
  on('closeLibraryBtn', 'click', closeLibraryModal);
  on('cancelLibraryBtn', 'click', closeLibraryModal);
  on('libraryImportBtn', 'click', importSelectedFromLibrary);
  on('librarySelectAllBtn', 'click', () => {
    const q = Library.query.trim().toLowerCase();
    Library.items
      .filter(it => !q || [it.name, it.description].filter(Boolean).some(v => String(v).toLowerCase().includes(q)))
      .forEach(it => Library.selected.add(it.id));
    renderLibraryList();
  });
  on('libraryClearBtn', 'click', () => { Library.selected.clear(); renderLibraryList(); });
  // 剧本导入弹窗
  on('closeImportScriptBtn', 'click', closeImportScript);
  on('cancelImportScriptBtn', 'click', closeImportScript);
  on('doImportScriptBtn', 'click', doImportScript);
  const libSearch = document.getElementById('librarySearch');
  if (libSearch) {
    libSearch.addEventListener('input', () => { Library.query = libSearch.value; renderLibraryList(); });
  }

  // 分镜
  on('addShotBtn', 'click', addShot);
  on('autoGenShotBtn', 'click', autoGenShots);
  on('relinkShotsBtn', 'click', relinkShots);
  on('refCheckBtn', 'click', refCheckShots);
  on('framesheetBtn', 'click', showFrameSheet);
  on('dubAllBtn', 'click', dubAllShots);

  // 后期（全部接真实现，不再有"只弹提示"的死按钮）
  on('stitchBtn', 'click', stitchProject);
  on('exportAspectBtn', 'click', exportAspectNow);
  on('addBgmBtn', 'click', mixBgmNow);
  on('applyVoiceBtn', 'click', applyVoiceNow);
  on('refreshOutputsBtn', 'click', loadOutputs);
  // 后期流水线（逐步确认版）
  on('pipelineNextBtn', 'click', () => PIPE.next());
  on('pipelineAllBtn', 'click', () => PIPE.runAll());
  on('pipelineResetBtn', 'click', () => PIPE.reset());
  on('pipelineOpenFolderBtn', 'click', async () => {
    if (!State.currentProject) return Toast.warn('请先选择一个项目');
    try {
      const r = await API.compose.reveal(State.currentProject.id);
      const where = (r && (r.path || r.dir)) || '';
      Modal.open({
        title: '📂 已打开输出目录',
        html: `<div class="modal-text">文件资源管理器已打开下面这个位置（没看到就检查任务栏）：</div>
          <pre style="white-space:pre-wrap;word-break:break-all;background:var(--bg-3);padding:10px;border-radius:8px;font-size:12px">${escapeHtml(where)}</pre>
          <div class="modal-text dim">成片文件是 final.mp4。流水线的每一步中间产物在数据目录的 cache/pipeline 里，用上面的「下载这一步的结果」拿。</div>`,
        okText: null, cancelText: '知道了',
      });
    } catch (e) { Toast.error('打开失败：' + e.message); }
  });

  // 输出目录
  on('browseOutputDirBtn', 'click', async () => {
    try {
      Toast.info('请在系统对话框中选择目录…');
      const r = await API.system.pickFolder(document.getElementById('outputDirInput').value);
      if (r && r.path) {
        document.getElementById('outputDirInput').value = r.path;
        Toast.success('已选择：' + r.path);
      } else {
        Toast.info('已取消');
      }
    } catch (e) { Toast.error('打开目录选择框失败：' + e.message); }
  });
  on('saveOutputDirBtn', 'click', async () => {
    const p = document.getElementById('outputDirInput').value.trim();
    if (!p) return Toast.warn('请先填写或选择目录');
    try {
      const r = await API.system.setOutputDir(p);
      Toast.success('输出目录已设为：' + r.output_dir);
      State.settings = await API.settings.get();
      await loadOutputs();
    } catch (e) { Toast.error('设置失败：' + e.message); }
  });
  on('openOutputDirBtn', 'click', async () => {
    if (!State.currentProject) return Toast.warn('请先选择项目');
    try { await API.compose.reveal(State.currentProject.id); Toast.success('已打开输出目录'); }
    catch (e) { Toast.error('打开失败：' + e.message); }
  });

  // 欢迎建议
  document.querySelectorAll('[data-suggestion]').forEach(btn => {
    btn.addEventListener('click', () => {
      document.getElementById('chatInput').value = btn.dataset.suggestion;
      sendChat();
    });
  });

  // ESC
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      closeSettings();
      Progress.hide();
    }
  });
}


function navigateTo(page) {
  State.currentPage = page;
  document.querySelectorAll('.nav-item').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.page === page);
  });
  document.querySelectorAll('.page').forEach(p => {
    p.hidden = (p.dataset.page !== page);
  });
  // 单个页面渲染出错不能拖垮整个应用（此前一处异常会让后续所有交互失效）
  try {
    switch (page) {
      case 'home': renderHome(); break;
      case 'script': renderScript(); break;
      case 'assets': renderCharacters(); break;
      case 'scenes': renderScenes(); break;
      case 'storyboard': renderStoryboard(); break;
      case 'generate': renderGeneratePage(); break;
      case 'postprocess': renderPostprocess(); break;
    }
  } catch (e) {
    console.error('[navigateTo] 页面渲染失败:', page, e);
    if (typeof Toast !== 'undefined' && Toast.error) {
      Toast.error(`「${page}」页渲染出错：${e.message}（其他页面仍可正常使用）`);
    }
  }
}


function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme');
  const next = current === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('theme', next);
}


// ──────────── 项目 ────────────

async function loadProjects() {
  try {
    const data = await API.projects.list();
    State.projects = data.projects || [];
    renderProjectList();
    renderRecentProjects();
  } catch (e) {
    Toast.error('加载项目失败: ' + e.message);
  }
}


function renderProjectList() {
  const list = document.getElementById('projectList');
  if (!State.projects.length) {
    list.innerHTML = '<div class="dim" style="padding: 8px 12px; font-size: 12px;">暂无</div>';
    return;
  }
  list.innerHTML = State.projects.slice(0, 20).map(p => `
    <div class="project-list-item ${State.currentProject?.id === p.id ? 'active' : ''}"
         data-pid="${p.id}">
      <div>${escapeHtml(p.title)}</div>
      <div class="project-status">${formatStatus(p.status)}</div>
    </div>
  `).join('');
  list.querySelectorAll('[data-pid]').forEach(el => {
    el.addEventListener('click', () => selectProject(el.dataset.pid));
  });
}


function renderRecentProjects() {
  const grid = document.getElementById('recentProjects');
  if (!grid) return;
  if (!State.projects.length) {
    grid.innerHTML = `
      <div class="empty-state" style="grid-column: 1 / -1;">
        <div class="empty-state-icon">🎬</div>
        <div class="empty-state-text">还没有项目</div>
        <div class="empty-state-hint">在 AI 对话里告诉我你想做什么</div>
      </div>
    `;
    return;
  }
  grid.innerHTML = State.projects.slice(0, 12).map(p => `
    <div class="project-card" data-pid="${p.id}">
      <div class="project-card-title">${escapeHtml(p.title)}</div>
      <div class="project-card-desc">${escapeHtml(p.description || '无描述')}</div>
      <div class="project-card-meta">
        <span class="project-status-badge ${p.status}">${formatStatus(p.status)}</span>
        <span>${formatTime(p.updated_at)}</span>
      </div>
    </div>
  `).join('');
  grid.querySelectorAll('.project-card').forEach(el => {
    el.addEventListener('click', () => {
      selectProject(el.dataset.pid);
      navigateTo('script');
    });
  });
}


async function selectProject(pid, opts = {}) {
  if (!pid) return;
  // 立刻高亮 + 切页（不等后端，避免"卡着不动"的观感）
  const cached = State.projects.find(p => p.id === pid);
  if (cached) State.currentProject = cached;
  renderProjectList();
  if (opts.navigate !== false) navigateTo('script');
  try {
    const project = await API.projects.get(pid);
    if (project && project.id) {
      State.currentProject = project;
      updateModelPill();
      renderProjectList();
      if (State.currentPage === 'script') await renderScript();
      Toast.success(`已打开：${project.title || '项目'}`);
    }
  } catch (e) {
    Toast.error(`打开项目失败：${e.message || e}（已显示缓存内容）`);
  }
}


function updateModelPill() {
  // 统一 AI 状态显示（之前有两个 pill，现在合并）
  const aiText = document.getElementById('aiPillText');
  const aiStatus = document.getElementById('aiPillStatus');
  if (!aiText) return;

  const llmName = Chat.currentProvider || '未配';
  const videoName = State.currentProject?.settings?.default_model_provider;
  const llmKeys = State.settings.llm_api_keys || {};
  const videoKeys = State.settings.api_keys || {};
  const hasLlm = !!llmKeys[Chat.currentProvider];
  const hasVideo = !!(videoName && videoKeys[videoName]);

  aiText.textContent = llmName === '未配' ? '设置 AI' : llmName;

  if (aiStatus) {
    const dots = [hasLlm ? '🤖' : null, hasVideo ? '🎬' : null].filter(Boolean).join('');
    aiStatus.textContent = dots || '⚠';
    aiStatus.title = hasLlm && hasVideo
      ? `LLM + 视频模型 都已配置`
      : `点击设置 LLM 和视频模型`;
  }
}


// ──────────── 主页 / 剧本 / 角色 / 场景 / 分镜 / 生成 / 后期 ────────────
// 保持原 app.js 的逻辑（这里简化版）

function renderHome() { /* 由 renderRecentProjects 渲染 */ }

async function createFromHero() {
  const prompt = document.getElementById('heroPrompt').value.trim();
  if (!prompt) return Toast.warn('请先描述');
  const duration = parseInt(document.getElementById('heroDuration').value);
  const style = document.getElementById('heroStyle').value;
  Progress.show('创建项目...');
  try {
    const project = await API.projects.create({
      title: prompt.slice(0, 30),
      description: prompt,
      settings: {
        default_aspect_ratio: document.getElementById('heroAspect').value,
        default_duration_per_shot: 10,
        // ★★ 总时长必须**存进项目设置**（2026-09-14 用户实测：主页选了 5 秒，
        //   生成出来却是 60 秒）。原因是这里以前只把秒数发给了"这一次"生成请求，
        //   没有落库；而剧本页/分镜页读的是 `settings.default_total_duration || 60`
        //   —— 键不存在就一律 60。现在两处都读同一个值。
        default_total_duration: duration,
        default_model_provider: State.settings.default_model_provider || 'kling',
        default_model_name: State.settings.default_model_name || 'kling-1.6',
        default_resolution: '1080p',
        style,
      },
    });
    Progress.hide();
    Toast.success('项目创建成功，正在进入...');
    await selectProject(project.id);
    await loadProjects();
    navigateTo('script');

    // 异步生成剧本（不阻塞 UI）
    Progress.show('AI 生成剧本...');
    try {
      await API.scripts.generate(project.id, {
        user_prompt: prompt,
        total_duration_seconds: duration,
        style,
        auto_split_scenes: true,
      });
      Progress.hide();
      Toast.success('剧本已生成');
      await renderScript();
    } catch (e) {
      Progress.hide();
      const msg = e.message || '';
      if (msg.includes('LLM') || msg.includes('api_key') || msg.includes('key') || msg.includes('API')) {
        Toast.warn('项目已创建，但剧本生成需要 LLM key。点右上角设置 → 配置 DeepSeek/通义千问/MiniMax 等');
      } else {
        Toast.warn(`剧本生成失败：${msg}（可点击右上角设置配置 LLM key）`);
      }
    }
  } catch (e) {
    Progress.hide();
    Toast.error('创建项目失败: ' + (e.message || String(e)));
  }
}

async function abstractAndCreate() {
  const source = document.getElementById('abstractInput').value.trim();
  if (!source) return Toast.warn('请输入');
  Progress.show('抽象化...');
  try {
    const project = await API.projects.create({
      title: source.slice(0, 30),
      description: source,
      settings: {
        default_aspect_ratio: '16:9',
        default_duration_per_shot: 10,
        default_model_provider: 'kling',
        default_model_name: 'kling-1.6',
        style: 'cinematic',
      },
    });
    Progress.update(0.5, '调用 LLM...');
    await API.abstract(project.id, source);
    Progress.hide();
    await selectProject(project.id);
    await loadProjects();
    navigateTo('assets');
    Toast.success('抽象化完成');
  } catch (e) {
    Progress.hide();
    Toast.error('抽象化失败: ' + e.message);
  }
}

// 其他页面渲染占位（保持向后兼容）
async function renderScript() {
  const metaBar = document.getElementById('scriptMeta');
  if (!State.currentProject) {
    document.getElementById('scriptUserPrompt').value = '';
    document.getElementById('scriptOutline').value = '';
    document.getElementById('scriptScenes').innerHTML = '<div class="empty-state">请先在左侧「项目」中选择一个项目，或在主页创建新项目</div>';
    if (metaBar) metaBar.hidden = true;
    return;
  }
  document.getElementById('scriptUserPrompt').value = State.currentProject.description || '';
  try {
    const script = await API.scripts.get(State.currentProject.id);
    State.script = script || null;

    // 大纲：后端已渲染为专业编剧体文本；此处兜底处理历史数据与解析异常
    let outlineText = script?.outline || '';
    const parseErr = !!(script && script._parse_error);
    const scenes = Array.isArray(script?.scenes) ? script.scenes : [];
    const hasScenes = scenes.length > 0;
    if (parseErr && !hasScenes) {
      // AI 输出格式异常：不再显示内部错误标题，改为展示原始内容 + 明确提示
      const rawText = String(script?._raw || '').trim();
      outlineText = '⚠️ AI 本次返回的内容格式异常（已自动尝试修复仍失败）\n'
        + '以下是模型原始输出，可复制后手动整理，或点击「重新生成」再试一次：\n\n'
        + (rawText || '(模型未返回内容)');
    }
    if (outlineText && typeof outlineText === 'object') {
      outlineText = JSON.stringify(outlineText, null, 2);
    }
    document.getElementById('scriptOutline').value = outlineText;

    // 剧本头信息（标题 / 梗概 / 风格 / 场次数）
    if (metaBar) {
      const rawTitle = script?.title || (script?.outline_json && script.outline_json.title) || '';
      const titleIsBad = /解析失败|格式异常|未命名剧本/.test(rawTitle);
      const title = titleIsBad ? '' : rawTitle;
      const logline = script?.logline || (script?.outline_json && script.outline_json.logline) || '';
      const style = script?.style || (State.currentProject.settings?.style) || '';
      if (title || logline || hasScenes || parseErr) {
        metaBar.hidden = false;
        document.getElementById('scriptTitle').textContent = title ? `《${title}》` : '（剧本待生成）';
        document.getElementById('scriptLogline').textContent = logline || '';
        const total = scenes.reduce((n, s) => n + (parseInt(s.duration_seconds) || 0), 0);
        // ★ 目标 vs 实际：把"你要多少秒 / 剧本实际多少秒"并排显示。
        //   用户实测「选 5 秒给了 60 秒」之所以刺眼，就是因为界面上只有
        //   "总时长 60s" 一个数，看不出它跟自己的选择对不上。
        const want = parseInt(State.currentProject?.settings?.default_total_duration
          || (document.getElementById('scriptDuration') || {}).value || 0);
        const mismatch = want && total && Math.abs(total - want) > 0;
        document.getElementById('scriptTags').innerHTML = [
          parseErr && !hasScenes ? '<span class="s-tag warn">⚠ AI 输出格式异常</span>' : '',
          scenes.length ? `<span class="s-tag">${scenes.length} 场</span>` : '',
          total ? `<span class="s-tag${mismatch ? ' warn' : ' ok'}">总时长 ${total}s${
            want ? (mismatch ? `（你要的是 ${want}s）` : ` = 目标 ${want}s ✓`) : ''}</span>` : '',
          style ? `<span class="s-tag">${escapeHtml(style)}</span>` : '',
        ].filter(Boolean).join('');
        // 提示条：对不上就直说怎么处理
        const hint = document.getElementById('scriptDurationHint');
        if (hint && want) {
          hint.innerHTML = mismatch
            ? `⚠ 当前剧本 ${total}s ≠ 你选的 ${want}s（旧剧本或 AI 没照做）——`
              + `点「重新生成」会按 ${want}s 强制重排`
            : `目标 ${want}s · 已对齐（后端强制，不再靠 AI 自觉）`;
          hint.style.color = mismatch ? 'var(--warning, #d80)' : 'var(--text-secondary)';
        }
      } else {
        metaBar.hidden = true;
      }
    }

    // 场次卡片
    document.getElementById('scriptScenes').innerHTML = scenes.length
      ? scenes.map((s, i) => `
        <div class="scene-item">
          <div class="scene-item-title"><span class="shot-index">${s.scene_number || i + 1}</span>${escapeHtml(s.title || '')}
            ${s.duration_seconds ? `<span class="scene-dur">${s.duration_seconds}s</span>` : ''}</div>
          <div class="scene-item-meta">📍 ${escapeHtml(s.location || '')}${s.mood ? ` · 💭 ${escapeHtml(s.mood)}` : ''}</div>
          <div class="scene-item-content">
            ${s.characters?.length ? `<strong>人物：</strong>${(Array.isArray(s.characters) ? s.characters : [s.characters]).map(c => escapeHtml(c)).join('、')}<br>` : ''}
            ${s.actions?.length ? `<strong>动作：</strong>${(Array.isArray(s.actions) ? s.actions : [s.actions]).map(a => escapeHtml(a)).join(' → ')}<br>` : ''}
            ${s.dialogues?.length ? `<strong>对白：</strong>${s.dialogues.map(d => `${escapeHtml(d.character || '')}「${escapeHtml(d.text || '')}」`).join(' · ')}<br>` : ''}
            ${s.mood ? `<strong>情绪：</strong>${escapeHtml(s.mood)}` : ''}
          </div>
        </div>`).join('')
      : '<div class="empty-state"><div class="empty-state-icon">📝</div><div class="empty-state-text">剧本尚未生成</div><div class="empty-state-hint">点击右上角「💡 想法生成大纲」，AI 会按专业编剧格式生成完整剧本</div></div>';
  } catch (e) {
    Toast.error('加载剧本失败: ' + (e.message || e));
  }
}

/**
 * 剧本生成统一入口（「💡 想法生成大纲」与「重新生成」共用）
 * 统一处理：前置校验 → 按钮 loading → 进度提示 → 渲染结果 → 友好错误分类
 */
async function runScriptGeneration(opts = {}) {
  const { buttonId = null, progressTitle = 'AI 正在生成专业剧本…', successPrefix = '剧本已生成' } = opts;

  if (!State.currentProject) {
    Toast.warn('请先在左侧「项目」中选择一个项目，或到主页创建新项目');
    return false;
  }
  const idea = (document.getElementById('scriptUserPrompt').value || '').trim()
    || State.currentProject.description || '';
  if (!idea) {
    Toast.warn('请先填写「用户原始想法」，再生成剧本');
    document.getElementById('scriptUserPrompt')?.focus();
    return false;
  }

  // ★★ 总时长：**先读项目设置**（主页选的那个），没有才退默认值。
  //   界面上这个下拉是可改的，改完会跟着这次生成一起发出去，并且后端会把它
  //   写回项目设置 —— 这样"我选了 5 秒"和"生成出来的就是 5 秒"是同一件事。
  const durSel = document.getElementById('scriptDuration');
  if (durSel) {
    const saved = parseInt(State.currentProject.settings?.default_total_duration || 0);
    if (saved && [...durSel.options].some(o => parseInt(o.value) === saved)) {
      durSel.value = String(saved);
    }
  }
  const totalDuration = parseInt((durSel && durSel.value)
    || State.currentProject.settings?.default_total_duration || 60) || 60;
  const style = State.currentProject.settings?.style || 'cinematic';
  // 生成时按所选秒数；顺手把选择落回项目设置（下次进来还是它）
  if (durSel && State.currentProject.settings) {
    State.currentProject.settings.default_total_duration = totalDuration;
  }

  const btn = buttonId ? document.getElementById(buttonId) : null;
  const btnLabel = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '生成中…'; }

  Progress.show(progressTitle);
  const outlineEl = document.getElementById('scriptOutline');
  if (outlineEl) outlineEl.placeholder = '⏳ AI 正在创作：拆分场次、按镜头编写动作与对白…';

  try {
    const gen = await API.scripts.generate(State.currentProject.id, {
      user_prompt: idea,
      total_duration_seconds: totalDuration,
      style,
      auto_split_scenes: true,
    }, { timeout: 300000 });
    // ★ 把后端的"强制时长"结果如实报出来（目标 / 实际 / 被裁掉的场次）。
    //   静默改数最招人烦：用户选了 5 秒，就该知道那 5 秒是怎么来的、
    //   以及 AI 多写的场次去哪了。
    const dur = (gen && gen.duration) || {};
    if (dur.note) {
      const dropped = (dur.dropped || []);
      await Modal.open({
        title: `⏱ 时长已按 ${dur.target}s 强制对齐${dur.ok ? ' ✓' : ''}`,
        html: `
          <div class="modal-text">你要的时长：<b>${dur.target}s</b>　
            剧本实际：<b>${dur.actual}s</b>　
            场次：<b>${dur.scene_count}</b> 场${
              dur.before_total ? `（AI 原本写了 ${dur.before_total}s）` : ''}</div>
          <div class="modal-text">${escapeHtml(dur.note || '')}</div>
          ${dropped.length ? `<div class="modal-text"><b>没排进来的场次</b>（时长放不下）：</div>
            <div style="max-height:30vh;overflow:auto">${dropped.map(x =>
              `<div class="dim">· ${escapeHtml(x.title || '')}${x.duration_seconds ? `（AI 写了 ${x.duration_seconds}s）` : ''}</div>`
            ).join('')}</div>` : ''}
          <div class="dim" style="margin-top:.5em">秒数由后端**强制**重排（不再只靠提示词请求 AI），
            所以不会再出现"选 5 秒生成 60 秒"。</div>`,
        okText: null, cancelText: '知道了',
      });
    }
    Progress.hide();
    await renderScript();

    const n = (State.script?.scenes || []).length;
    if (State.script && State.script._parse_error && n === 0) {
      Toast.warn('AI 返回格式异常，已保留原始输出（见大纲区）。可再点一次「重新生成」');
    } else {
      Toast.success(`${successPrefix}：${n} 个场次`);
    }
    document.querySelector('.script-content')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    return true;
  } catch (e) {
    Progress.hide();
    const msg = String(e.message || e);
    if (/429|用量|quota|balance|余额|insufficient/i.test(msg)) {
      Toast.error(`LLM 额度不足：${msg.slice(0, 80)}。请到「设置」更换有额度的 API Key`);
    } else if (/超时|timeout/i.test(msg)) {
      Toast.error('生成超时（LLM 响应过慢）。可稍后重试，或换用更快的模型');
    } else if (/未识别|base_url|Key|key/i.test(msg)) {
      Toast.error(`请到「设置」配置可用的 LLM Provider 与 API Key：${msg.slice(0, 80)}`);
    } else {
      Toast.error(`剧本生成失败：${msg.slice(0, 120)}`);
    }
    return false;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = btnLabel || '重新生成'; }
    if (outlineEl) outlineEl.placeholder = '点击右上角「💡 想法生成大纲」→ AI 按编剧格式输出：标题 / 一句话梗概 / 逐场次（地点·人物·按镜头动作·对白·情绪）';
  }
}

/** 💡 想法生成大纲 */
async function ideaToOutline() {
  return runScriptGeneration({
    buttonId: 'ideaToOutlineBtn',
    progressTitle: 'AI 正在生成专业剧本…',
    successPrefix: '剧本已生成',
  });
}

/** 重新生成（与「想法生成大纲」同一流程，入口不同） */
async function regenerateScript() {
  return runScriptGeneration({
    buttonId: 'regenScriptBtn',
    progressTitle: 'AI 正在重新生成剧本…',
    successPrefix: '剧本已重新生成',
  });
}

// ──────── 资产库：跨剧本复用 + 从剧本提取（角色 / 场景）────────

const Library = {
  kind: null,          // 'characters' | 'scenes'
  items: [],
  selected: new Set(),
  query: '',
};

/** 从当前项目剧本中提取人物/地点，自动创建角色/场景 */
async function extractFromScript(kind, opts) {
  if (!State.currentProject) return Toast.warn('请先选择或创建一个项目');
  const isChar = kind === 'characters';
  const kindName = isChar ? '角色' : '场景';
  const force = !!(opts && opts.force);
  if (force) {
    const total = isChar ? (State.characters || []).length : (State.scenes || []).length;
    if (!await Modal.confirm(
      `重新提取会<b>用 AI 新设计的设定覆盖</b>现有 ${total} 个${kindName}里那些占位/空的内容。` +
      `<br><br>你手写过的内容不会被覆盖（脚本里没写外貌时 AI 会按题材/年代补全，` +
      `并拆成「脸型/肤色/眼型/发型/体型/上装/下装/鞋/配饰/识别锚点」等固定字段）。` +
      `<br><br>继续吗？`, { title: `♻ 重新提取${kindName}`, okText: '开始', danger: true })) return;
  }
  const btn = document.getElementById(isChar ? 'charFromScriptBtn' : 'sceneFromScriptBtn');
  if (btn) btn.disabled = true;
  Progress.show(force ? `正在重新提取${kindName}（AI 重新设计设定，约 1-2 分钟）…`
                      : `正在从剧本提取${kindName}…`);
  try {
    const r = isChar
      ? await API.characters.fromScript(State.currentProject.id, force)
      : await API.scenes.fromScript(State.currentProject.id, force);
    Progress.hide();
    const n = r?.count ?? 0, total = r?.total_in_script ?? 0, skipped = r?.skipped ?? 0;
    const upd = r?.updated_count ?? 0;
    if (n > 0 || upd > 0) {
      Toast.success(`已从剧本提取 ${n} 个${kindName}` +
        (upd ? `，更新 ${upd} 个已有设定` : '') +
        (skipped ? `（${skipped} 个已存在，跳过）` : ''));
    } else {
      Toast.info(total ? `剧本中的 ${total} 个${isChar ? '人物' : '地点'}都已存在，无需重复创建`
                       : `剧本中未找到可提取的${isChar ? '人物' : '地点'}`);
    }
    if (r?.warning) Toast.warn(String(r.warning).slice(0, 200));
    if (isChar) await renderCharacters(); else await renderScenes();
  } catch (e) {
    Progress.hide();
    Toast.error(`提取失败：${e.message || e}`);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function _libraryItemDesc(it, kind) {
  if (kind === 'characters') {
    return [it.description, it.costume_main].filter(Boolean).join(' · ') || '（无描述）';
  }
  return [it.location_type, it.lighting, it.description].filter(Boolean).join(' · ') || '（无描述）';
}

/** 打开资产库导入弹窗 */
async function openLibraryModal(kind) {
  if (!State.currentProject) return Toast.warn('请先选择或创建一个项目');
  Library.kind = kind;
  Library.selected = new Set();
  Library.query = '';
  const isChar = kind === 'characters';
  document.getElementById('libraryModalTitle').textContent = isChar ? '📚 从资产库导入角色' : '📚 从资产库导入场景';
  document.getElementById('librarySearch').value = '';
  document.getElementById('libraryStatus').textContent = '';
  document.getElementById('libraryModal').style.display = 'flex';
  document.getElementById('libraryList').innerHTML = '<div class="empty-state"><div class="empty-state-text">加载中…</div></div>';
  try {
    const r = isChar
      ? await API.library.characters(State.currentProject.id)
      : await API.library.scenes(State.currentProject.id);
    Library.items = ((isChar ? r.characters : r.scenes) || []);
    renderLibraryList();
  } catch (e) {
    document.getElementById('libraryList').innerHTML =
      `<div class="empty-state"><div class="empty-state-icon">⚠</div><div class="empty-state-text">加载失败：${escapeHtml(e.message || String(e))}</div></div>`;
  }
}

function closeLibraryModal() {
  document.getElementById('libraryModal').style.display = 'none';
}

function renderLibraryList() {
  const listEl = document.getElementById('libraryList');
  const isChar = Library.kind === 'characters';
  const q = Library.query.trim().toLowerCase();
  const shown = Library.items.filter(it => {
    if (!q) return true;
    return [it.name, it.description, it.costume_main, it.location_type]
      .filter(Boolean).some(v => String(v).toLowerCase().includes(q));
  });

  if (!Library.items.length) {
    listEl.innerHTML = `<div class="empty-state">
      <div class="empty-state-icon">📦</div>
      <div class="empty-state-text">资产库还是空的</div>
      <div class="empty-state-hint">在其他剧本里创建过${isChar ? '角色' : '场景'}后，就能在这里选择性复用。</div>
    </div>`;
    document.getElementById('libraryCount').textContent = '已选 0';
    return;
  }

  listEl.innerHTML = shown.map(it => {
    const checked = Library.selected.has(it.id);
    const src = it.source_project_title ? escapeHtml(String(it.source_project_title).slice(0, 24)) : '未知项目';
    return `
      <label class="library-item ${checked ? 'checked' : ''}" data-id="${it.id}">
        <input type="checkbox" ${checked ? 'checked' : ''}>
        <div class="library-item-body">
          <div class="library-item-title">${escapeHtml(it.name || '(未命名)')}</div>
          <div class="library-item-desc">${escapeHtml(_libraryItemDesc(it, Library.kind))}</div>
          <div class="library-item-src">来源：${src}</div>
        </div>
      </label>`;
  }).join('') || `<div class="empty-state"><div class="empty-state-text">没有匹配的${isChar ? '角色' : '场景'}</div></div>`;

  listEl.querySelectorAll('.library-item').forEach(el => {
    el.addEventListener('click', (ev) => {
      if (ev.target.tagName !== 'INPUT') ev.preventDefault();
      const id = el.dataset.id;
      if (Library.selected.has(id)) Library.selected.delete(id);
      else Library.selected.add(id);
      el.classList.toggle('checked', Library.selected.has(id));
      el.querySelector('input').checked = Library.selected.has(id);
      updateLibraryCount();
    });
  });
  updateLibraryCount();
}

function updateLibraryCount() {
  const el = document.getElementById('libraryCount');
  if (el) el.textContent = `已选 ${Library.selected.size}`;
}

/** 把选中的资产导入当前项目 */
async function importSelectedFromLibrary() {
  if (!State.currentProject) return;
  const ids = Array.from(Library.selected);
  const isChar = Library.kind === 'characters';
  const kindName = isChar ? '角色' : '场景';
  const statusEl = document.getElementById('libraryStatus');
  if (!ids.length) {
    statusEl.textContent = `请先勾选要导入的${kindName}`;
    statusEl.style.color = 'var(--warn, #e0a800)';
    return;
  }
  const btn = document.getElementById('libraryImportBtn');
  if (btn) { btn.disabled = true; btn.textContent = '导入中…'; }
  statusEl.textContent = `正在导入 ${ids.length} 个${kindName}…`;
  statusEl.style.color = 'var(--text-secondary)';
  try {
    const r = isChar
      ? await API.characters.importFromLibrary(State.currentProject.id, ids)
      : await API.scenes.importFromLibrary(State.currentProject.id, ids);
    const n = r?.count ?? ids.length;
    statusEl.textContent = `✓ 已导入 ${n} 个${kindName}`;
    statusEl.style.color = 'var(--success, #2f9e44)';
    Toast.success(`已导入 ${n} 个${kindName}（可在本项目继续编辑）`);
    if (isChar) await renderCharacters(); else await renderScenes();
    setTimeout(closeLibraryModal, 600);
  } catch (e) {
    statusEl.textContent = `✗ 导入失败：${e.message || e}`;
    statusEl.style.color = 'var(--error, #c92a2a)';
    Toast.error(`导入失败：${e.message || e}`);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '导入选中'; }
  }
}



// ──────── 剧本细节生成（按 Skill 手册场次模板） ──────────

async function openSceneDetailModal() {
  if (!State.currentProject) {
    Toast.warn('请先创建项目');
    return;
  }
  document.getElementById('sceneDetailModal').style.display = 'flex';
  await populateSceneSelect();
  // 加载已保存的细节列表（如果有）
  await loadAllSceneDetails();
}

function closeSceneDetailModal() {
  document.getElementById('sceneDetailModal').style.display = 'none';
}

async function populateSceneSelect() {
  const sel = document.getElementById('sceneDetailSelect');
  sel.innerHTML = '<option>加载中…</option>';
  try {
    const script = await API.scripts.get(State.currentProject.id);
    const scenes = script?.scenes || [];
    if (!scenes.length) {
      sel.innerHTML = '<option>暂无场次（先生成剧本）</option>';
      return;
    }
    sel.innerHTML = scenes.map(s =>
      `<option value="${s.scene_number}">场次 ${s.scene_number}: ${escapeHtml(s.title || '')} (${s.duration_seconds || 0}s)</option>`
    ).join('');
  } catch (e) {
    sel.innerHTML = '<option>加载失败</option>';
  }
}

async function loadAllSceneDetails() {
  // 拉取已生成的细节（不影响当前选中）
  try {
    const d = await API.scripts.listSceneDetails(State.currentProject.id);
    State.sceneDetails = (d.details || []).reduce((m, x) => {
      m[x.scene_number] = x.detail;
      return m;
    }, {});
  } catch (e) {
    State.sceneDetails = {};
  }
}

async function generateSceneDetail() {
  if (!State.currentProject) return;
  const sel = document.getElementById('sceneDetailSelect');
  const sceneNumber = parseInt(sel.value);
  if (!sceneNumber || isNaN(sceneNumber)) {
    Toast.warn('请先选择场次');
    return;
  }
  const resultEl = document.getElementById('sceneDetailResult');
  resultEl.innerHTML = '<div class="loading-text" style="padding: 40px; text-align: center;">⏳ AI 正在按 Skill 手册模板生成场次细节（按秒动作、对白、光影）…</div>';
  try {
    const d = await API.scripts.generateSceneDetail(State.currentProject.id, sceneNumber);
    State.sceneDetails[sceneNumber] = d.detail || d;
    renderSceneDetail(d.detail || d);
    Toast.success(`场次 ${sceneNumber} 细节已生成`);
  } catch (e) {
    resultEl.innerHTML = `<div class="empty-state"><div class="empty-state-icon">⚠</div><div class="empty-state-text">生成失败：${escapeHtml(e.message)}</div></div>`;
    Toast.error('生成失败: ' + e.message);
  }
}

function renderSceneDetail(detail) {
  const el = document.getElementById('sceneDetailResult');
  if (!detail || detail.parse_error) {
    el.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon">⚠</div>
        <div class="empty-state-text">解析失败，原始输出：</div>
        <pre style="text-align:left;background:var(--bg-3);padding:12px;border-radius:8px;margin-top:12px;white-space:pre-wrap;font-size:12px;max-height:300px;overflow:auto;">${escapeHtml(detail?.raw_text || '(空)')}</pre>
      </div>`;
    return;
  }

  const headerMeta = [];
  if (detail.location) headerMeta.push(`📍 ${escapeHtml(detail.location)}`);
  if (detail.duration_seconds) headerMeta.push(`⏱ ${detail.duration_seconds}s`);
  if (detail.mood) headerMeta.push(`💭 ${escapeHtml(detail.mood)}`);
  if (detail.lighting) headerMeta.push(`💡 ${escapeHtml(detail.lighting)}`);

  const timelineRows = (detail.timeline || []).map(t => {
    const tags = [];
    if (t.expression) tags.push(`<span class="ts-tag">${escapeHtml(t.expression)}</span>`);
    if (t.camera) tags.push(`<span class="ts-tag">📷 ${escapeHtml(t.camera)}</span>`);
    (t.props_used || []).forEach(p => tags.push(`<span class="ts-tag">🔧 ${escapeHtml(p)}</span>`));
    if (t.visual_focus) tags.push(`<span class="ts-tag">👁 ${escapeHtml(t.visual_focus)}</span>`);
    return `
      <div class="timeline-row">
        <div class="timeline-time">[${t.start}-${t.end}s]</div>
        <div class="timeline-content">
          <div class="ts-action">${escapeHtml(t.action || '')}</div>
          <div class="timeline-tags">${tags.join('')}</div>
        </div>
      </div>`;
  }).join('');

  const dialogueLines = (detail.dialogues || []).map(d => `
    <div class="dialogue-line">
      <span class="dialogue-speaker">${escapeHtml(d.character || '')}:</span>
      <span>${escapeHtml(d.text || '')}</span>
      ${d.emotion ? `<span class="dialogue-emotion">(${escapeHtml(d.emotion)}${d.timing ? ' · ' + escapeHtml(d.timing) : ''})</span>` : ''}
    </div>`).join('');

  const characters = (detail.characters || []).map(c =>
    `<span class="ts-tag" style="background:var(--accent-soft);color:var(--accent);">${escapeHtml(c)}</span>`
  ).join(' ');

  const keyProps = (detail.key_props || []).map(p =>
    `<span class="ts-tag">${escapeHtml(p)}</span>`
  ).join(' ');

  el.innerHTML = `
    <div class="detail-header">
      <div class="detail-title">场次 ${detail.scene_number || ''}：${escapeHtml(detail.title || '')}</div>
      <div class="detail-meta">${headerMeta.join('')}</div>
    </div>

    ${characters ? `
    <div class="detail-section">
      <h4>👥 人物</h4>
      <div class="timeline-tags">${characters}</div>
    </div>` : ''}

    <div class="detail-section">
      <h4>🎬 动作（按秒拆分）</h4>
      ${timelineRows || '<div class="dim">无</div>'}
    </div>

    <div class="detail-section">
      <h4>💬 对白</h4>
      ${dialogueLines || '<div class="dim">无对白</div>'}
    </div>

    ${keyProps ? `
    <div class="detail-section">
      <h4>🔑 关键道具</h4>
      <div class="timeline-tags">${keyProps}</div>
    </div>` : ''}

    <div style="margin-top: 16px; font-size: 12px; color: var(--text-tertiary);">
      ✓ 已保存到项目（重新打开会保留）
    </div>
  `;
}

/* 本地图片统一走后端 /api/local-image（WebView2 中 file:// 会被拦成白图） */
function assetImg(path, alt) {
  if (!path) return '<div class="placeholder">暂无图片<br><span>点下方「AI 生成形象」</span></div>';
  return `<img src="${API.images.localUrl(path)}" alt="${escapeHtml(alt || '')}" loading="lazy"
    onerror="this.parentNode.innerHTML='<div class=&quot;placeholder&quot;>图片加载失败</div>'">`;
}

/** 生成角色形象 / 场景概念图（provider + 模型 + 可编辑提示词） */
async function openImageGen(kind, id, view) {
  const isChar = kind === 'character';
  const item = isChar
    ? (State.characters || []).find(c => c.id === id)
    : (State.scenes || []).find(s => s.id === id);
  if (!item) return Toast.error('未找到该资产，请刷新后重试');

  let providers = [], defaultProvider = 'minimax';
  try {
    const d = await API.images.providers();
    providers = d.providers || [];
    defaultProvider = d.default_provider || (providers[0] && providers[0].name) || 'minimax';
  } catch (e) {
    return Toast.error('读取图像模型失败: ' + e.message);
  }
  if (!providers.length) return Toast.error('没有可用的图像生成模型');

  const chosen = providers.find(p => p.name === defaultProvider) || providers[0];
  const defaultPrompt = isChar
    ? `角色设定图：${item.name}。${item.description || ''} 服装：${item.costume_main || '符合角色身份'}。正面半身像，单人，简洁背景，电影级光影，高清写实`
    : `场景概念图：${item.name}。${item.description || ''} 光线：${item.lighting || 'natural'}，时间：${item.time_of_day || 'day'}。无人物，广角电影感，高清写实`;

  // 角色额外给「风格」与「视图」——四视图是跨镜头保持同一张脸的关键（借鉴 Pavo）
  const charFields = isChar ? [
    {
      key: 'style', label: '画面风格', type: 'select', value: 'cinematic',
      options: [
        { value: 'cinematic', label: '电影级写实' },
        { value: 'documentary', label: '纪实摄影' },
        { value: 'anime', label: '日式动画' },
        { value: 'chinese_ink', label: '中国水墨' },
        { value: 'cyberpunk', label: '赛博朋克' },
        { value: '3d_render', label: '3D 渲染' },
      ],
    },
    {
      key: 'view', label: '视图', type: 'select', value: view || 'bust',
      hint: '四视图用于让不同镜头保持同一张脸',
      options: [
        { value: 'bust', label: '半身像（推荐做封面）' },
        { value: 'full', label: '全身像' },
        { value: 'fourview', label: '四视图设定稿（正/侧/背/特写）' },
      ],
    },
  ] : [];

  const r = await Modal.form(
    isChar ? `🎨 生成角色形象 · ${escapeHtml(item.name)}` : `🎨 生成场景图 · ${escapeHtml(item.name)}`,
    [
      {
        key: 'provider',
        label: '图像模型',
        type: 'select',
        value: chosen.name,
        options: providers.map(p => ({ value: p.name, label: `${p.display_name || p.name} · ${p.default_model || ''}` })),
      },
      { key: 'model', label: '模型名', type: 'text', value: chosen.default_model || '', hint: '留空用默认', placeholder: chosen.default_model || '' },
      {
        key: 'size', label: '尺寸', type: 'select', value: chosen.default_size || '1024x1024',
        options: (chosen.sizes && chosen.sizes.length ? chosen.sizes : ['1024x1024', '1280x720', '720x1280']),
      },
      ...charFields,
      { key: 'prompt', label: '提示词（留空则用角色卡自动拼装）', type: 'textarea', rows: 5, value: '', hint: '清空即可让系统按「剧本设定」自动生成提示词' },
    ],
    {
      wide: true,
      okText: '开始生成（约 10-30 秒）',
      desc: '生成会真实调用图像模型并计费。若提示<p class="inline-warn">内容审核不通过</p>，请把提示词里的敏感词（战争、真实人物、血腥等）换成中性描述后重试。',
      bind: (body) => {
        const sel = body.querySelector('[data-field="provider"]');
        const nameInput = body.querySelector('[data-field="model"]');
        const sizeSel = body.querySelector('[data-field="size"]');
        if (!sel) return;
        sel.addEventListener('change', () => {
          const p = providers.find(x => x.name === sel.value);
          if (!p) return;
          if (nameInput) { nameInput.value = p.default_model || ''; nameInput.placeholder = p.default_model || ''; }
          if (sizeSel && p.sizes && p.sizes.length) {
            sizeSel.innerHTML = p.sizes.map(s =>
              `<option value="${escapeHtml(s)}"${s === (p.default_size || p.sizes[0]) ? ' selected' : ''}>${escapeHtml(s)}</option>`).join('');
          }
        });
      },
    }
  );
  if (!r) return;
  if (isChar && !r.prompt) {
    Toast.info('未填提示词 → 将用该角色的「剧本设定」自动拼装提示词');
  }

  try {
    Progress.show('正在生成图片…（约 10-30 秒）');
    const res = isChar
      ? await API.images.generateCharacter(State.currentProject.id, id, r)
      : await API.images.generateScene(State.currentProject.id, id, r);
    Progress.hide();
    Toast.success(`✅ 生成成功：${res.filename} (${Math.round((res.size_bytes || 0) / 1024)} KB) · ${res.provider}/${res.model}`);
    if (isChar) await renderCharacters(); else await renderScenes();
  } catch (e) {
    Progress.hide();
    const msg = e.message || '';
    Modal.open({
      title: '❌ 图片生成失败',
      html: `<div class="modal-text">${escapeHtml(msg)}</div>
        <div class="modal-text" style="margin-top:10px;color:var(--text-tertiary)">
        常见原因：① 未配置该厂商 API Key（设置 → 对话 API Keys）② 提示词触发内容审核 ③ 账号额度用尽 ④ 网络无法访问该厂商接口。
        到「设置 → 环境诊断」可一键检查密钥与网络。</div>`,
      okText: null,
      cancelText: '知道了',
    });
  }
}

/* ══════════════════════════════════════════════════════════════
 * 全图查看（灯箱）+ 角色配音色
 * 用户原话："我需要能真实点击角色看到角色全貌而不是半截图片"
 * 两个原因一起修：① 卡片改用 object-fit:contain（不再裁掉一半）
 *                ② 点图开灯箱看原图
 * ══════════════════════════════════════════════════════════════ */
function openLightbox(src, opts) {
  if (!src) return;
  const o = opts || {};
  const old = document.getElementById('vfLightbox');
  if (old) old.remove();
  const el = document.createElement('div');
  el.className = 'lightbox';
  el.id = 'vfLightbox';
  el.innerHTML = `
    <button class="lightbox-close" title="关闭（Esc）">✕</button>
    <img src="${src}" alt="${escapeHtml(o.title || '')}">
    <div class="lightbox-bar">
      <span>${escapeHtml(o.title || '')}</span>
      ${o.subtitle ? `<span class="dim">${escapeHtml(o.subtitle)}</span>` : ''}
      <a href="${src}" download="${escapeHtml(o.filename || 'image.png')}">⬇ 下载原图</a>
      ${o.extra || ''}
    </div>
    ${o.tip ? `<div class="lightbox-tip">${escapeHtml(o.tip)}</div>` : ''}`;
  document.body.appendChild(el);
  const close = () => { el.remove(); document.removeEventListener('keydown', onKey); };
  const onKey = (e) => { if (e.key === 'Escape') close(); };
  el.querySelector('.lightbox-close').addEventListener('click', close);
  el.addEventListener('click', (e) => { if (e.target === el) close(); });
  document.addEventListener('keydown', onKey);
  if (typeof o.onMount === 'function') o.onMount(el, close);
}

/** 点资产卡片上的图片 → 看全图 */
function bindAssetImageZoom(root) {
  (root || document).querySelectorAll('.asset-card-image').forEach(box => {
    const img = box.querySelector('img');
    if (!img || box.dataset.zoomBound) return;
    box.dataset.zoomBound = '1';
    box.addEventListener('click', (e) => {
      if (e.target.closest('button')) return;
      const card = box.closest('.asset-card');
      const t = card ? (card.querySelector('.asset-card-title') || {}).textContent || '' : '';
      openLightbox(img.getAttribute('src'), {
        title: t.trim(), filename: 'asset.png',
        tip: '点空白处或按 Esc 关闭',
      });
    });
  });
}

/** 从角色卡里读它被分配的音色 */
function charVoice(c) {
  let rf = c && c.reference_features;
  if (typeof rf === 'string') { try { rf = JSON.parse(rf); } catch (e) { rf = {}; } }
  const v = (rf && rf.voice) || {};
  return v.voice_id || charExtra(c, 'voice_id') || '';
}

/** 给角色配一个专属音色（多角色不同声音 → 对话才像对话） */
async function castCharacterVoice(cid) {
  const c = (State.characters || []).find(x => x.id === cid);
  if (!c) return;
  if (!GEN.voices.length) {
    try { GEN.voices = ((await API.voice.voices()) || {}).voices || []; } catch (e) {}
  }
  const usable = GEN.voices.filter(v => v.usable !== false);
  const nope = GEN.voices.length - usable.length;
  const cur = charVoice(c);
  const r = await Modal.form(`🎙 给「${c.name}」配音色`, [
    {
      key: 'voice_id', label: '这个角色说话用哪个声音', type: 'select',
      value: cur || (usable[0] || {}).voice_id || '',
      hint: '每个角色配不同声音，成片里的对话才像对话',
      options: usable.map(v => ({
        value: v.voice_id,
        label: `${v.display_name}｜${v.provider_display || v.provider || ''}`,
      })),
    },
    {
      key: 'gender', label: '角色性别（没手选音色时按它自动兜底）', type: 'select',
      value: c.gender || 'unknown',
      options: [{ value: 'male', label: '男' }, { value: 'female', label: '女' },
                { value: 'unknown', label: '未指定' }],
    },
    { key: 'locked', label: '锁定（自动选角时不覆盖）', type: 'checkbox',
      value: !!((c.reference_features || {}).voice || {}).locked },
  ], {
    okText: '保存', wide: true,
    desc: nope > 0 ? `有 ${nope} 个音色因未配 API Key 被过滤掉了。` : '',
  });
  if (!r || !r.voice_id) return;
  const v = GEN.voices.find(x => x.voice_id === r.voice_id) || {};
  let rf = c.reference_features;
  if (typeof rf === 'string') { try { rf = JSON.parse(rf); } catch (e) { rf = {}; } }
  rf = (rf && typeof rf === 'object') ? Object.assign({}, rf) : {};
  rf.voice = { voice_id: r.voice_id, voice_name: v.display_name || '',
               provider: v.provider || '', gender: v.gender || r.gender || '',
               locked: !!r.locked };
  try {
    await API.characters.update(cid, { reference_features: rf, gender: r.gender });
    Toast.success(`「${c.name}」的声音已设为 ${v.display_name || r.voice_id}`);
    await renderCharacters();
  } catch (e) { Toast.error('保存失败：' + e.message); }
}

/**
 * 用户自带参考图 → 让 AI 按剧情需要改造。
 *
 * 用户原话："支持用户自己提供图片 AI 参考后根据剧情需要进行实际修改"
 *
 * ★ 「这张图当什么用」必须由用户显式三选一 —— 这是参考项目文档里的空白：
 *   Pavo 只有 keyframe_url 与 reference_image_url 两个字段，但**没说谁优先、冲突怎么办**。
 *   实际使用中这三种用法效果完全不同：
 *     首帧构图   → 锁死画面构图与机位（视频从这里开始动）
 *     身份参考   → 只锁人物长相/服装，构图自由（跨镜头不换脸）
 *     风格参考   → 只锁画风/色调，人物与构图都自由
 *   混在一起做，用户就会得到"我要它改衣服，结果连脸都换了"这种结果。
 */
async function openReferenceRefine(kind, id) {
  const isChar = kind === 'character';
  const item = isChar ? (State.characters || []).find(c => c.id === id)
                      : (State.scenes || []).find(s => s.id === id);
  if (!item) return Toast.error('未找到该资产');

  const r = await Modal.form(`🖼 参考图改造 · ${item.name}`, [
    {
      key: 'file', label: '你的参考图（jpg/png）', type: 'file',
      hint: '上传后交给 AI 作为参考，按下面的要求改造',
    },
    {
      key: 'role', label: '这张图当什么用（三选一，效果完全不同）', type: 'select',
      value: isChar ? 'identity' : 'composition',
      options: isChar ? [
        { value: 'identity', label: '👤 身份参考：锁定长相与服装，构图交给 AI' },
        { value: 'composition', label: '🎬 首帧构图：锁死画面构图，视频从这里开始动' },
        { value: 'style', label: '🎨 风格参考：只锁画风色调，人和景都自由' },
      ] : [
        { value: 'composition', label: '🎬 场景构图：锁定空间结构' },
        { value: 'style', label: '🎨 风格参考：只锁画风色调' },
        { value: 'identity', label: '🏞 环境参考：锁定地貌与建筑质感' },
      ],
    },
    {
      key: 'change', label: '要改什么（按剧情需要写）', type: 'textarea', rows: 3,
      placeholder: isChar
        ? '例如：把外套换成1940年代的灰蓝粗布军装，加绑腿；脸和发型保持不变'
        : '例如：改成傍晚，加一层薄雾，把白墙换成夯土墙',
      hint: '写"改什么、保留什么"，比只写"改什么"效果稳得多',
    },
    {
      key: 'keep', label: '必须保留什么（逗号分隔）', type: 'text',
      placeholder: '例如：脸型，发型，眼神',
    },
  ], { okText: '上传并改造', wide: true });

  if (!r || !r.file) return Toast.warn('请选择一张参考图');
  const file = r.file;
  if (!/\.(jpe?g|png|webp)$/i.test(file.name || '')) {
    return Toast.warn('请用 jpg / png / webp 图片');
  }

  try {
    Progress.show('上传参考图…');
    const up = await API.images.uploadReference(kind, id, file);
    Progress.hide();
    const path = up.path || up.image_path || '';

    // ★ 把"这张图当什么用"记下来 —— 这是参考项目文档里的空白，
    //   不记的话后续生成就分不清"锁构图"还是"锁长相"，效果会飘。
    try {
      let rf = item.reference_features;
      if (typeof rf === 'string') { try { rf = JSON.parse(rf); } catch (e) { rf = {}; } }
      rf = (rf && typeof rf === 'object') ? Object.assign({}, rf) : {};
      rf.reference_role = r.role;
      rf.reference_instruction = r.change || '';
      if (isChar) await API.characters.update(id, { reference_features: rf });
      else await API.scenes.update(id, { reference_features: rf });
    } catch (e) { /* 记录失败不影响改造本身 */ }

    const roleLabel = { identity: '身份参考', composition: '首帧构图', style: '风格参考' }[r.role];
    const keep = (r.keep || '').split(/[，,]/).map(x => x.trim()).filter(Boolean);
    const instruction = (r.change || '').trim() ||
      (isChar ? '把人物形象细化得更符合剧本设定' : '把环境细节细化得更符合剧本设定');

    if (!await Modal.confirm(
      `<b>这张图会作为底图，交给图像编辑模型在原图上修改</b>（不是重画一张）。<br><br>
       用法：${roleLabel}<br>
       要改：<code>${escapeHtml(instruction)}</code><br>
       ${keep.length ? '必须保留：' + escapeHtml(keep.join('、')) + '<br>' : ''}
       <br>修改后旧图会自动备份，随时可以退回。<br><br>继续吗？`,
      { title: '🖼 在原图上修改', okText: '开始修改' })) return;

    Progress.show('正在按参考图修改…（约 20-60 秒）');
    let res;
    try {
      res = await API.images.refine(State.currentProject.id, kind, id, {
        instruction, keep, base_image: path,
        provider: 'siliconflow',
        model: 'Qwen/Qwen-Image-Edit-2509',
      });
    } catch (e) {
      Progress.hide();
      return Modal.open({
        title: '⚠ 图像编辑不可用',
        html: `<div class="modal-text">${escapeHtml(e.message || String(e))}</div>
          <div class="modal-text dim" style="margin-top:8px">
          图像编辑需要一个「能接受参考图」的模型。目前推荐用硅基流动的
          Qwen-Image-Edit（到「设置 → 🔊 语音合成 API Keys」填 SiliconFlow Key 即可，
          图像与语音共用同一个 Key）。<br>
          也可以退而用「🎨 AI 生成形象」——那是重新画一张，长相与构图会变。</div>`,
        okText: null, cancelText: '知道了',
      });
    }
    Progress.hide();
    if (!res.ok) return Toast.error('修改失败：' + (res.error || ''));
    if (isChar) await renderCharacters(); else await renderScenes();
    // 直接给用户看前后对比
    openLightbox(res.url, {
      title: `${item.name} · 修改结果`,
      subtitle: `${res.provider} / ${res.model} · ${res.elapsed}s`,
      filename: 'refined.png',
      tip: '这就是"在原图上按要求修改"的结果。不满意可以再改一次；旧图已备份。',
    });
    Toast.success(`✅ 已按要求修改（${res.elapsed}s，原图已备份）`);
  } catch (e) {
    Progress.hide();
    Toast.error('改造失败：' + (e.message || e));
  }
}

/**
 * 视频参考：上传一段视频 → 从里面挑出有代表性的画面当参考。
 *
 * 用户原话："支持用户自己提供图片或者视频做 AI 参考后根据剧情需要进行实际修改"
 *
 * 为什么是"抽帧"而不是"把视频喂给模型"：
 *   主流图像/视频模型接受的是**图片**（首帧/身份/风格参考），
 *   几乎没有能直接吃视频的（Pavo 文档没有，MPT 更不涉及）。
 *   所以正确落地就是：视频 → 挑画面 → 当参考图 → 再让 AI 按剧情改。
 */
async function openVideoReference(kind, id) {
  const isChar = kind === 'character';
  const item = isChar ? (State.characters || []).find(c => c.id === id)
                      : (State.scenes || []).find(s => s.id === id);
  if (!item) return Toast.error('未找到该资产');

  const r = await Modal.form(
    `🎞 视频参考 · ${item.name}`,
    [{
      key: 'file', label: '你的参考视频（mp4/mov/webm）', type: 'file',
      hint: '会上传后自动抽出若干有代表性的画面，你挑一张用',
    }],
    { okText: '上传并抽帧', wide: true,
      desc: '适合"我有一段喜欢的片子，想做出这种质感的场景/人物"。'
          + '抽出的画面会成为该资产的形象基准，之后可以直接用「🖼 在原图上修改」继续调。' });

  if (!r || !r.file) return Toast.warn('请选择一个视频文件');
  const f = r.file;
  if (!/\.(mp4|mov|webm|mkv|avi|m4v)$/i.test(f.name || '')) {
    return Toast.warn('请上传视频文件（mp4 / mov / webm）');
  }

  try {
    Progress.show('上传视频并抽帧…（视频越大越慢）');
    const res = await API.images.uploadVideoRef(State.currentProject.id, kind, id, f);
    Progress.hide();

    if (!res.ok || !(res.frames || []).length) {
      return Modal.open({
        title: '⚠ 没能从视频里取到画面',
        html: `<div class="modal-text">${escapeHtml(res.error || '未知原因')}</div>
          <div class="modal-text dim" style="margin-top:8px">
          常见原因：视频损坏 / 时长读不出 / 画面几乎没有变化。<br>
          可以换个视频，或者直接用「🖼 参考图改造」传一张图。</div>`,
        okText: null, cancelText: '知道了',
      });
    }

    const frames = res.frames;
    Modal.open({
      title: `🎞 挑一张画面 · 共 ${frames.length} 张`,
      wide: true,
      html: `
        <div class="modal-text">
          视频 ${escapeHtml(String(res.duration))} 秒，
          识别到 ${res.scene_points || 0} 个画面切换点。
          <b>挑一张作为这个${isChar ? '角色' : '场景'}的形象基准</b>：
        </div>
        <div class="vref-grid">
          ${frames.map((fr, i) => `
            <div class="vref-item" data-i="${i}">
              <img src="${fr.url}" loading="lazy" alt="候选 ${i + 1}">
              <div class="vref-meta">${fr.time}s
                ${fr.source === 'scene' ? '<span class="s-tag ok">切换点</span>' : ''}</div>
            </div>`).join('')}
        </div>
        <div class="modal-text dim" style="margin-top:10px">
          也可以自己填一个时间点让系统现取一帧：
          <input type="number" id="vrefTime" step="0.1" min="0"
                 max="${res.duration || 999}" placeholder="例如 12.5"
                 style="width:100px;margin-left:6px"> 秒
          <button class="secondary-btn" id="vrefPickTime" style="margin-left:6px">取这一帧</button>
        </div>`,
      okText: null, cancelText: '关闭',
      bind: (body) => {
        body.querySelectorAll('.vref-item').forEach(box => {
          box.addEventListener('click', async () => {
            const fr = frames[Number(box.dataset.i)];
            await applyFrame(fr.path, null);
          });
        });
        const btn = body.querySelector('#vrefPickTime');
        if (btn) btn.addEventListener('click', async () => {
          const t = parseFloat((body.querySelector('#vrefTime') || {}).value);
          if (isNaN(t)) return Toast.warn('请填一个时间点（秒）');
          await applyFrame(null, null, t);
        });
      },
    });

    async function applyFrame(path, _close, time) {
      try {
        Progress.show('设定形象基准…');
        const rr = await API.images.useFrame(State.currentProject.id, kind, id,
                                             time != null ? { time } : { frame_path: path });
        Progress.hide();
        Modal.close();
        Toast.success('✅ ' + (rr.note || '已设定'));
        if (isChar) await renderCharacters(); else await renderScenes();
      } catch (e) {
        Progress.hide();
        Toast.error('设定失败：' + e.message);
      }
    }
  } catch (e) {
    Progress.hide();
    Toast.error('上传失败：' + (e.message || e));
  }
}

async function renderCharacters() {
  if (!State.currentProject) {
    document.getElementById('characterGrid').innerHTML = '<div class="empty-state" style="grid-column:1/-1;">请先选择项目</div>';
    return;
  }
  try {
    const data = await API.characters.list(State.currentProject.id);
    State.characters = data.characters || [];
  } catch (e) { return; }
  const grid = document.getElementById('characterGrid');
  if (!State.characters.length) {
    grid.innerHTML = `
      <div class="empty-state" style="grid-column:1/-1;">
        <div class="empty-state-icon">👤</div>
        <div class="empty-state-text">还没有角色</div>
        <div class="empty-state-hint">
          两种来源：① 剧本生成后点「📖 从剧本提取」自动生成角色 →
          ② 点「📚 从资产库导入」复用其他剧本里已有（含已生成形象）的角色
        </div>
      </div>`;
    return;
  }
  grid.innerHTML = State.characters.map(c => {
    const sl = charSlots(c);
    const n = Object.keys(sl).length;
    const slotChips = n
      ? `<div class="char-slots" title="这些字段会逐字写进每个镜头的提示词，保证长相不漂移">${
          CHAR_FACE_SLOTS.concat(CHAR_COSTUME_SLOTS)
            .filter(([k]) => sl[k])
            .slice(0, 6)
            .map(([k, label]) => {
              const v = Array.isArray(sl[k]) ? sl[k].join('、') : sl[k];
              return `<span class="char-slot-chip">${escapeHtml(label.split('（')[0])}：${
                escapeHtml(String(v).slice(0, 14))}</span>`;
            }).join('')
        }${n > 6 ? `<span class="char-slot-chip dim">还有 ${n - 6} 项…</span>` : ''}</div>`
      : '';
    return `
    <div class="asset-card" data-cid="${c.id}">
      <div class="asset-card-image">${assetImg(c.reference_image_path, c.name)}</div>
      <div class="asset-card-body">
        <div class="asset-card-title">${escapeHtml(c.name)}
          ${c.reference_image_path ? '<span class="s-tag ok">已有形象</span>' : '<span class="s-tag warn">待生成</span>'}
          ${n ? `<span class="s-tag ok" title="已拆成固定字段，跨镜头一致">槽位 ${n}</span>`
              : '<span class="s-tag" title="点「编辑」把外貌/服装拆成固定字段，跨镜头长相更稳">未拆槽位</span>'}
        </div>
        <div class="asset-card-desc">${escapeHtml(c.description || c.costume_main || '（无描述）')}</div>
        ${slotChips}
        <div class="asset-card-actions">
          <button class="primary-btn" data-action="genimg">🎨 AI 生成形象</button>
          <button data-action="gen4v" title="生成四视图设定稿：正面/侧面/背面/特写并排，用于跨镜头锁定长相">🧍 生成四视图</button>
          <button data-action="voice" title="给这个角色指定配音音色（多角色不同声音，对话才像对话）">
            🎙 ${charVoice(c) ? '改音色' : '配音色'}</button>
          <button data-action="refine" title="上传自己的图片作为参考，让 AI 按剧情需要改造">🖼 参考图改造</button>
          <button data-action="videoref" title="上传一段视频，从里面挑一张有代表性的画面当形象基准">🎞 视频参考</button>
          <button data-action="edit">编辑</button>
          <button data-action="upload">上传参考图</button>
          <button data-action="delete" style="color:var(--error)">删除</button>
        </div>
        ${charVoice(c) ? `<div class="dim" style="font-size:11px;margin-top:6px">
          🔊 配音：${escapeHtml((GEN.voices.find(x => x.voice_id === charVoice(c)) || {}).display_name || charVoice(c))}</div>` : ''}
      </div>
    </div>`;
  }).join('');
  grid.querySelectorAll('.asset-card').forEach(card => {
    const cid = card.dataset.cid;
    card.querySelector('[data-action="genimg"]').addEventListener('click', () => openImageGen('character', cid));
    const b4 = card.querySelector('[data-action="gen4v"]');
    if (b4) b4.addEventListener('click', () => openImageGen('character', cid, 'fourview'));
    const bv = card.querySelector('[data-action="voice"]');
    if (bv) bv.addEventListener('click', () => castCharacterVoice(cid));
    const br = card.querySelector('[data-action="refine"]');
    if (br) br.addEventListener('click', () => openReferenceRefine('character', cid));
    const bvr = card.querySelector('[data-action="videoref"]');
    if (bvr) bvr.addEventListener('click', () => openVideoReference('character', cid));
    card.querySelector('[data-action="edit"]').addEventListener('click', () => editCharacter(cid));
    card.querySelector('[data-action="upload"]').addEventListener('click', () => uploadCharacterRef(cid));
    card.querySelector('[data-action="delete"]').addEventListener('click', () => deleteCharacter(cid));
  });
  bindAssetImageZoom(grid);
}

// 角色卡的「固定槽位」——和 backend/core/assets.py 的 CHARACTER_SCHEMA 一一对应。
// 为什么在界面上逐个列出来：这些槽位会**逐字**出现在每个镜头的视频提示词里，
// 是跨镜头长相一致的唯一保障。用户能看见、能改，才谈得上"精准"。
const CHAR_FACE_SLOTS = [
  ['face_shape', '脸型与骨相', '方脸，颧骨明显，下颌线硬朗'],
  ['skin', '肤色与肤质', '偏黄，风吹日晒的粗糙感'],
  ['eyes', '眼型与眼神', '细长眼，眼神锐利'],
  ['hair', '发型+长度+颜色', '短寸黑发，两鬓已花白'],
  ['distinctive', '最能认出他的特征（没有就写「无」）', '左眉骨一道旧疤'],
  ['height_build', '身高体型与姿态', '中等身高，精瘦，肩背挺直'],
];
const CHAR_COSTUME_SLOTS = [
  ['costume_top', '上装（颜色+材质+款式）', '洗得发白的灰蓝粗布对襟上衣'],
  ['costume_bottom', '下装（颜色+材质+款式）', '深灰粗布长裤，裤脚扎进绑腿'],
  ['costume_shoes', '鞋（颜色+款式）', '黑色旧布鞋，鞋面有补丁'],
  ['costume_accessories', '配饰/装备（用、分隔）', '布制绑腿、帆布挎包'],
  ['costume_palette', '服装主色（用、分隔）', '灰蓝、深灰、黑'],
  ['signature', '识别锚点（跨镜头必须保持不变的唯一特征）', '左眉骨旧疤 + 洗白的灰蓝上衣'],
];

/** 从 reference_features 里取一个普通字段（可能是 JSON 字符串）。 */
function charExtra(c, key) {
  let rf = c && c.reference_features;
  if (typeof rf === 'string') { try { rf = JSON.parse(rf); } catch (e) { rf = {}; } }
  return (rf && typeof rf === 'object' && rf[key]) || '';
}

/** 从 reference_features 里取出固定槽位（可能是 JSON 字符串）。 */
function charSlots(c) {
  return charExtra(c, 'slots') || {};
}

/** 产出角色的槽位表单字段（编辑/新建共用）。 */
function characterSlotFields(c) {
  const sl = charSlots(c || {});
  const listToText = (v) => Array.isArray(v) ? v.join('、') : (v || '');
  return CHAR_FACE_SLOTS.concat(CHAR_COSTUME_SLOTS).map(([k, label, ph]) => ({
    key: k, label, type: 'text', placeholder: ph,
    value: k === 'costume_accessories' || k === 'costume_palette'
      ? listToText(sl[k]) : (sl[k] || ''),
  }));
}

/** 把表单里的槽位收成一个 slots 对象。 */
function collectCharSlots(r) {
  const slots = {};
  for (const [k] of CHAR_FACE_SLOTS.concat(CHAR_COSTUME_SLOTS)) {
    const v = (r[k] || '').trim();
    if (!v) continue;
    slots[k] = (k === 'costume_accessories' || k === 'costume_palette')
      ? v.split(/[、,，]/).map(x => x.trim()).filter(Boolean)
      : v;
  }
  return slots;
}

async function editCharacter(cid) {
  const c = (State.characters || []).find(x => x.id === cid);
  if (!c) return;
  const r = await Modal.form(`编辑角色 · ${escapeHtml(c.name)}`, [
    { key: 'name', label: '角色名', type: 'text', value: c.name },
    { key: 'age', label: '年龄/年龄段', type: 'text', value: c.age || '', placeholder: '40 岁上下' },
    {
      key: '_face', label: '— 面部（下面每一项都会逐字写进每个镜头的提示词）—',
      type: 'text', value: '', hint: '这些字段是跨镜头长相一致的保障，越具体越好',
    },
    ...characterSlotFields(c),
    { key: 'description', label: '外貌整段描述（不填会自动用上面的槽位合成）',
      type: 'textarea', rows: 3, value: c.description || '' },
    { key: 'costume_main', label: '服装整段描述（不填会自动用上面的槽位合成）',
      type: 'textarea', rows: 2, value: c.costume_main || '' },
    { key: 'personality', label: '性格与说话风格', type: 'text',
      value: charExtra(c, 'personality') || '' },
  ], { okText: '保存', wide: true });
  if (!r || !r.name) return;
  const slots = collectCharSlots(r);
  const payload = {
    name: r.name,
    age: r.age || '',
    description: r.description || '',
    costume_main: r.costume_main || '',
  };
  // 槽位存进 reference_features.slots（保留已有 personality 等字段）
  let rf = c.reference_features;
  if (typeof rf === 'string') { try { rf = JSON.parse(rf); } catch (e) { rf = {}; } }
  rf = (rf && typeof rf === 'object') ? Object.assign({}, rf) : {};
  rf.slots = slots;
  if (r.personality) rf.personality = r.personality;
  payload.reference_features = rf;
  try {
    await API.characters.update(cid, payload);
    Toast.success(`已保存（${Object.keys(slots).length} 个槽位）`);
    await renderCharacters();
  } catch (e) { Toast.error('保存失败: ' + e.message); }
}

async function addCharacter() {
  if (!State.currentProject) return Toast.warn('请先选择项目');
  const r = await Modal.form('新建角色', [
    { key: 'name', label: '角色名', type: 'text', placeholder: '例如：林昭', value: '' },
    { key: 'age', label: '年龄/年龄段', type: 'text', placeholder: '27 岁', value: '' },
    { key: 'description', label: '外貌整段描述', type: 'textarea', rows: 2,
      placeholder: '例如：二十七岁，瘦高，短发，眼神锐利', value: '' },
    { key: 'costume_main', label: '服装整段描述', type: 'text',
      placeholder: '例如：深灰中山装', value: '' },
    ...characterSlotFields({}),
  ], { okText: '创建', wide: true });
  if (!r) return;
  if (!r.name) return Toast.warn('角色名不能为空');
  const slots = collectCharSlots(r);
  try {
    await API.characters.create({
      project_id: State.currentProject.id,
      name: r.name, age: r.age || '',
      description: r.description || '', costume_main: r.costume_main || '',
      reference_features: Object.keys(slots).length ? { slots } : null,
    });
    Toast.success('已创建');
    await renderCharacters();
  } catch (e) {
    Toast.error('失败: ' + e.message);
  }
}

async function uploadCharacterRef(cid) {
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = 'image/*';
  input.onchange = async () => {
    if (!input.files[0]) return;
    try {
      Progress.show('上传中...');
      await API.characters.uploadReference(cid, input.files[0]);
      Progress.hide();
      await renderCharacters();
      Toast.success('上传完成');
    } catch (e) {
      Progress.hide();
      Toast.error('失败: ' + e.message);
    }
  };
  input.click();
}

async function deleteCharacter(cid) {
  const c = (State.characters || []).find(x => x.id === cid);
  if (!await Modal.confirm(`确认删除角色「${escapeHtml(c?.name || '')}」？该操作不可撤销。`, { title: '删除角色', okText: '删除', danger: true })) return;
  try { await API.characters.delete(cid); Toast.success('已删除'); await renderCharacters(); }
  catch (e) { Toast.error('失败: ' + e.message); }
}

const LIGHTING_ZH = { natural: '自然光', golden_hour: '黄金时刻', blue_hour: '蓝调时刻', night: '夜景', neon: '霓虹', studio: '棚拍', candle: '烛光', overcast: '阴天' };
const TIMEOFDAY_ZH = { day: '白天', night: '夜晚', dawn: '黎明', dusk: '黄昏', noon: '正午' };
const LOCATION_ZH = { indoor: '室内', outdoor: '室外', urban: '城市', rural: '乡村', fantasy: '幻想' };

async function renderScenes() {
  if (!State.currentProject) {
    document.getElementById('sceneGrid').innerHTML = '<div class="empty-state" style="grid-column:1/-1;">请先选择项目</div>';
    return;
  }
  try {
    const data = await API.scenes.list(State.currentProject.id);
    State.scenes = data.scenes || [];
  } catch (e) { return; }
  const grid = document.getElementById('sceneGrid');
  if (!State.scenes.length) {
    grid.innerHTML = `
      <div class="empty-state" style="grid-column:1/-1;">
        <div class="empty-state-icon">🏙</div>
        <div class="empty-state-text">还没有场景</div>
        <div class="empty-state-hint">
          场景决定分镜的环境与光线。两种来源：①「📖 从剧本提取」按剧本场次自动建立 →
          ②「📚 从资产库导入」复用其他剧本已有（含已生成场景图）的场景。
        </div>
      </div>`;
    return;
  }
  grid.innerHTML = State.scenes.map(s => {
    const meta = [
      LOCATION_ZH[s.location_type] || s.location_type,
      LIGHTING_ZH[s.lighting] || s.lighting,
      TIMEOFDAY_ZH[s.time_of_day] || s.time_of_day,
    ].filter(Boolean).join(' · ');
    return `
    <div class="asset-card" data-sid="${s.id}">
      <div class="asset-card-image">${assetImg(s.reference_image_path, s.name)}</div>
      <div class="asset-card-body">
        <div class="asset-card-title">${escapeHtml(s.name)}
          ${s.reference_image_path ? '<span class="s-tag ok">已有场景图</span>' : '<span class="s-tag warn">待生成</span>'}
        </div>
        <div class="asset-card-desc">📍 ${escapeHtml(meta)}<br>${escapeHtml(s.description || '（无描述）')}</div>
        <div class="asset-card-actions">
          <button class="primary-btn" data-action="genimg">🎨 AI 生成场景图</button>
          <button data-action="genvideo" data-scene-video="${s.id}">🎬 生成场景视频</button>
          <button data-action="refine" title="上传自己的图片作为底图，让 AI 在原图上按要求修改">🖼 参考图改造</button>
          <button data-action="videoref" title="上传一段视频，从里面挑一张有代表性的画面当场景基准">🎞 视频参考</button>
          <button data-action="upload">📤 上传图片/视频</button>
          <button data-action="edit">编辑</button>
          <button data-action="delete" style="color:var(--error)">删除</button>
        </div>
      </div>
    </div>`;
  }).join('');
  grid.querySelectorAll('.asset-card').forEach(card => {
    const sid = card.dataset.sid;
    card.querySelector('[data-action="genimg"]').addEventListener('click', () => openImageGen('scene', sid));
    card.querySelector('[data-action="genvideo"]').addEventListener('click', () => genSceneVideo(sid));
    const rf = card.querySelector('[data-action="refine"]');
    if (rf) rf.addEventListener('click', () => openReferenceRefine('scene', sid));
    const vr = card.querySelector('[data-action="videoref"]');
    if (vr) vr.addEventListener('click', () => openVideoReference('scene', sid));
    const up = card.querySelector('[data-action="upload"]');
    if (up) up.addEventListener('click', () => uploadSceneAsset(sid));
    card.querySelector('[data-action="edit"]').addEventListener('click', () => editScene(sid));
    card.querySelector('[data-action="delete"]').addEventListener('click', () => deleteScene(sid));
  });
  bindAssetImageZoom(grid);
}

async function genSceneVideo(sid) {
  const s = (State.scenes || []).find(x => x.id === sid);
  if (!s) return;
  const r = await Modal.form(`🎬 生成场景视频 · ${escapeHtml(s.name)}`, [
    {
      key: 'mode', label: '生成方式', type: 'select', value: 'auto',
      options: [
        { value: 'auto', label: '自动（有 Key 走视频模型，失败自动回退本地运镜）' },
        { value: 'api', label: '只用真实视频模型（需要 Key）' },
        { value: 'local', label: '只用本地运镜合成（不需要 Key）' },
      ],
    },
    { key: 'duration_seconds', label: '时长（秒）', type: 'text', value: '5', hint: '最小 5 秒；各模型上限不同' },
    {
      key: 'aspect_ratio', label: '画面比例', type: 'select', value: '16:9',
      options: ['16:9', '9:16', '1:1'],
    },
    { key: 'prompt', label: '提示词（留空则按剧本场景设定自动拼装）', type: 'textarea', rows: 4, value: '' },
  ], { okText: '开始生成', wide: true });
  if (!r) return;

  const btn = document.querySelector(`[data-scene-video="${sid}"]`);
  const old = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '生成中…'; }
  try {
    const res = await API.images.generateSceneVideo(State.currentProject.id, sid, {
      mode: r.mode, duration_seconds: Math.max(5, parseInt(r.duration_seconds, 10) || 5),
      aspect_ratio: r.aspect_ratio, prompt: r.prompt,
    });
    const src = res.source === 'api' ? `真实模型 ${res.provider}/${res.model}` : '本地运镜合成';
    Toast.success(`✅ 场景视频完成（${(res.duration || 0).toFixed(1)}s · ${src}）`);
    if (res.warnings && res.warnings.length) res.warnings.forEach(w => Toast.warn(w));
    // 直接弹播放器预览
    Modal.open({
      title: '🎬 场景视频',
      html: `<video controls autoplay style="width:100%;max-height:420px;background:#000;border-radius:10px"
               src="${res.url}"></video>
             <div class="modal-text" style="margin-top:10px;color:var(--text-tertiary)">
               ${escapeHtml(src)} · ${(res.duration || 0).toFixed(1)} 秒<br>
               <a href="${res.url}" download>⬇ 下载</a></div>`,
      okText: null, cancelText: '关闭', wide: true,
    });
  } catch (e) {
    Modal.open({
      title: '❌ 场景视频生成失败',
      html: `<div class="modal-text">${escapeHtml(e.message || '')}</div>
        <div class="modal-text" style="margin-top:10px;color:var(--text-tertiary)">
        可以改用「本地运镜合成」（不需要任何 Key），或到「设置」检查视频模型的 Key 与模型名。</div>`,
      okText: null, cancelText: '知道了',
    });
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = old || '🎬 生成场景视频'; }
  }
}

/* 一键生成全部场次细节（逐场 LLM，可能需要几分钟；给长超时 + 明确进度） */

/**
 * 剧本体检：按**用户提供的那份成品剧本**（《40集农村微短剧剧本：寒潮抢收玉米》）
 * 量出来的写法逐条检查，不是泛泛地夸"很专业"。
 *
 * 为什么要做成按钮而不是只改进提示词：
 *   提示词只是"请求"，模型可以不听。用户的要求原话是
 *   「剧本上可以多向它学习，方法再融入到我们剧本生成里面」——
 *   方法只有**能被检验**，才算真的融进去了。
 */
async function auditScript() {
  if (!State.currentProject) return Toast.warn('请先选择项目');
  const btn = document.getElementById('auditScriptBtn');
  const old = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '体检中…'; }
  try {
    const r = await API.scripts.audit(State.currentProject.id);
    const lvl = { error: '❌', warn: '⚠', ok: '✅' };
    const rows = (r.checks || []).map(c => `
      <tr><td style="padding:.25em .5em .25em 0;vertical-align:top">${lvl[c.level] || '·'}</td>
        <td style="padding:.25em 0"><b>${escapeHtml(c.title)}</b>
          ${c.detail ? `<div class="dim">${escapeHtml(c.detail)}</div>` : ''}
          ${c.evidence ? `<div class="dim" style="font-size:.85em">${escapeHtml(c.evidence)}</div>` : ''}
        </td></tr>`).join('');
    const s = r.stats || {};
    await Modal.open({
      title: `🔍 剧本体检 · ${r.score} 分（${r.errors} 个硬问题 / ${r.warnings} 个建议）`,
      html: `
        <div class="modal-text">${escapeHtml(r.note || '')}</div>
        <div class="modal-text">场次 <b>${s.scenes || 0}</b> ·
          总时长 <b>${s.total_seconds || 0}s</b>${r.target_duration ? `（目标 ${r.target_duration}s）` : ''} ·
          台词 <b>${s.lines_total || 0}</b> 句（每场均 ${s.lines_avg || 0}）</div>
        <table style="width:100%;border-collapse:collapse">${rows}</table>
        <div class="dim" style="margin-top:.5em">这把尺子是从你给的那份成品剧本
          《40集农村微短剧剧本：寒潮抢收玉米》里**量**出来的（标题 4–10 字钩子、
          每场 1–3 句台词、每场 32–83 字、前史动机、反派行为方式、群像摇摆、金句收尾）。
          硬问题（❌）会影响成片，建议（⚠）是"离那个水平还差一点"。</div>`,
      okText: null, cancelText: '知道了', wide: true,
    });
  } catch (e) {
    Toast.error('剧本体检失败：' + (e.message || e));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = old || '🔍 剧本体检'; }
  }
}

async function generateAllSceneDetails() {
  if (!State.currentProject) return Toast.warn('请先选择项目');
  const n = (State.script?.scenes || []).length;
  if (!n) return Toast.warn('还没有剧本场次');
  if (!await Modal.confirm(
    `将为全部 <b>${n}</b> 个场次生成逐秒细节（含动作/表情/镜头/光影）。<br>
     这一步会逐场调用 AI，<b>可能需要几分钟</b>，期间可以继续用其他页面。<br>
     已生成过的场次会自动跳过。<br><br>
     细节越细，后面的角色/场景/分镜就越精确 —— 这是成片质感的基础。`,
    { title: '生成全部剧本细节', okText: '开始生成' })) return;
  const btn = document.getElementById('genAllDetailsBtn');
  const old = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '生成中…'; }
  Progress.show(`正在逐场生成细节（共 ${n} 场，可能需要几分钟）…`);
  try {
    const r = await API.scripts.generateAllSceneDetails(State.currentProject.id);
    Progress.hide();
    const okN = (r.generated || []).length, sk = (r.skipped || []).length, bad = (r.failed || []).length;
    Toast.success(`✅ 细节生成完成：新生成 ${okN} 场 · 跳过 ${sk} 场${bad ? ` · 失败 ${bad} 场` : ''}`);
    if (bad) {
      Modal.open({
        title: '⚠ 部分场次失败',
        html: `<div class="modal-text">${(r.failed || []).map(f =>
          `第 ${f.scene_number} 场：${escapeHtml(f.error)}`).join('<br>')}</div>`,
        okText: null, cancelText: '知道了',
      });
    }
    await renderScript();
  } catch (e) {
    Progress.hide();
    Toast.error('生成失败：' + (e.message || e));
  } finally {
    btn.disabled = false; btn.textContent = old || '⚡ 一键生成全部细节';
  }
}

/* ══════════════════════════════════════════════════════════════
 * 剧本导入：文档（txt/md/json/docx/pdf/rtf）或粘贴文字 → 结构化剧本
 * ══════════════════════════════════════════════════════════════ */
function openImportScript() {
  if (!State.currentProject) return Toast.warn('请先选择项目');
  const m = document.getElementById('importScriptModal');
  if (!m) return Toast.error('导入弹窗缺失');
  m.style.display = 'flex';
  document.getElementById('importScriptStatus').textContent = '';
  document.getElementById('importScriptProgress').style.display = 'none';
  m.onclick = (e) => { if (e.target === m) closeImportScript(); };
}

function closeImportScript() {
  const m = document.getElementById('importScriptModal');
  if (m) m.style.display = 'none';
}

async function doImportScript() {
  if (!State.currentProject) return;
  const f = (document.getElementById('importScriptFile') || {}).files || [];
  const text = (document.getElementById('importScriptText') || {}).value.trim();
  const struct = (document.getElementById('importScriptStructurize') || {}).checked !== false;
  if (!f.length && !text) return Toast.warn('请选择文档或粘贴剧本文字');

  const btn = document.getElementById('doImportScriptBtn');
  const bar = document.getElementById('importScriptBar');
  const ptxt = document.getElementById('importScriptProgressText');
  const stat = document.getElementById('importScriptStatus');
  btn.disabled = true; const old = btn.textContent; btn.textContent = '导入中…';
  document.getElementById('importScriptProgress').style.display = '';
  bar.style.width = '15%';
  ptxt.textContent = '正在解析文档…';
  stat.textContent = struct ? '正在用 AI 结构化剧本（可能要 1-2 分钟）…' : '正在切分场次…';

  try {
    let res;
    if (f.length) {
      res = await API.scriptImport.file(State.currentProject.id, f[0], { structurize: struct });
    } else {
      res = await API.scriptImport.paste(State.currentProject.id, text, struct);
    }
    bar.style.width = '100%';
    ptxt.textContent = `完成：${res.scenes} 个场次`;
    const mode = res.llm_structurized ? 'AI 结构化' : '原文切分';
    Toast.success(`✅ 导入成功（${mode} · ${res.scenes} 场 · ${res.chars} 字）`);
    if (res.warning) Toast.warn(res.warning);
    if (res.note) Toast.info(res.note);
    stat.textContent = '导入完成';
    closeImportScript();
    await renderScript();
  } catch (e) {
    stat.textContent = '';
    ptxt.textContent = '失败';
    bar.style.background = 'var(--error)';
    Toast.error('导入失败：' + (e.message || e));
  } finally {
    btn.disabled = false; btn.textContent = old;
  }
}

async function uploadSceneAsset(sid) {
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = 'image/*,video/*';
  input.onchange = async () => {
    if (!input.files[0]) return;
    try {
      Progress.show('上传中…');
      const r = await API.scenes.uploadSceneReference(sid, input.files[0]);
      Progress.hide();
      Toast.success(`已上传场景${r.kind === 'video' ? '视频' : '图片'}素材`);
      await renderScenes();
    } catch (e) { Progress.hide(); Toast.error('上传失败：' + e.message); }
  };
  input.click();
}

// 场景的「固定槽位」——和 backend/core/assets.py 的 SCENE_SCHEMA 对应。
// 同一地点的这些字段会逐字进每个镜头的提示词，场景才不会一个镜头一个样。
const SCENE_SLOTS = [
  ['terrain', '地形/地面', '碎石土路，两侧枯草与残雪'],
  ['architecture', '建筑/构筑物', '半塌的土坯院墙，木门已歪斜'],
  ['furnishings', '陈设/植被/固定物件', '院中一口枯井、两棵光秃的槐树'],
  ['atmosphere', '氛围', '清冷、萧瑟，空气里像有雪味'],
  ['color_tone', '色调', '冷灰蓝为主，唯一暖色是窗内油灯光'],
];

function sceneSlotFields(s) {
  const sl = charSlots(s || {});
  return SCENE_SLOTS.map(([k, label, ph]) => ({
    key: k, label, type: 'text', placeholder: ph, value: sl[k] || '',
  }));
}

function collectSceneSlots(r) {
  const slots = {};
  for (const [k] of SCENE_SLOTS) {
    const v = (r[k] || '').trim();
    if (v) slots[k] = v;
  }
  return slots;
}

async function editScene(sid) {
  const s = (State.scenes || []).find(x => x.id === sid);
  if (!s) return;
  const r = await Modal.form(`编辑场景 · ${escapeHtml(s.name)}`, [
    { key: 'name', label: '场景名', type: 'text', value: s.name },
    {
      key: 'location_type', label: '内外景', type: 'select', value: s.location_type,
      options: [{ value: 'indoor', label: '室内' }, { value: 'outdoor', label: '室外' }],
    },
    {
      key: 'lighting', label: '光线', type: 'select', value: s.lighting,
      options: Object.keys(LIGHTING_ZH).map(k => ({ value: k, label: LIGHTING_ZH[k] })),
    },
    {
      key: 'time_of_day', label: '时间', type: 'select', value: s.time_of_day,
      options: Object.keys(TIMEOFDAY_ZH).map(k => ({ value: k, label: TIMEOFDAY_ZH[k] })),
    },
    { key: '_slots', label: '— 环境细节（下面每一项都会逐字写进每个镜头的提示词）—',
      type: 'text', value: '', hint: '同一地点在这些字段上保持一致，换镜头才不会变样' },
    ...sceneSlotFields(s),
    { key: 'description', label: '环境整段描述（不填会自动用上面的槽位合成）',
      type: 'textarea', rows: 3, value: s.description || '' },
  ], { okText: '保存', wide: true });
  if (!r || !r.name) return;
  const slots = collectSceneSlots(r);
  let rf = s.reference_features;
  if (typeof rf === 'string') { try { rf = JSON.parse(rf); } catch (e) { rf = {}; } }
  rf = (rf && typeof rf === 'object') ? Object.assign({}, rf) : {};
  rf.slots = slots;
  try {
    await API.scenes.update(sid, {
      name: r.name, location_type: r.location_type, lighting: r.lighting,
      time_of_day: r.time_of_day, description: r.description || '',
      reference_features: rf,
    });
    Toast.success(`已保存（${Object.keys(slots).length} 个槽位）`);
    await renderScenes();
  } catch (e) { Toast.error('保存失败: ' + e.message); }
}

async function deleteScene(sid) {
  const s = (State.scenes || []).find(x => x.id === sid);
  if (!await Modal.confirm(`确认删除场景「${escapeHtml(s?.name || '')}」？`, { title: '删除场景', okText: '删除', danger: true })) return;
  try { await API.scenes.delete(sid); Toast.success('已删除'); await renderScenes(); }
  catch (e) { Toast.error('失败: ' + e.message); }
}

async function addScene() {
  if (!State.currentProject) return Toast.warn('请先选择项目');
  const r = await Modal.form('新建场景', [
    { key: 'name', label: '场景名', type: 'text', placeholder: '例如：废弃厂房', value: '' },
    {
      key: 'location_type', label: '内外景', type: 'select', value: 'outdoor',
      options: [{ value: 'outdoor', label: '室外' }, { value: 'indoor', label: '室内' }],
    },
    {
      key: 'lighting', label: '光线', type: 'select', value: 'natural',
      options: Object.keys(LIGHTING_ZH).map(k => ({ value: k, label: LIGHTING_ZH[k] })),
    },
    {
      key: 'time_of_day', label: '时间', type: 'select', value: 'day',
      options: Object.keys(TIMEOFDAY_ZH).map(k => ({ value: k, label: TIMEOFDAY_ZH[k] })),
    },
    { key: 'description', label: '场景描述', type: 'textarea', rows: 3, placeholder: '例如：锈迹斑斑的钢架，地面积水反射天光', value: '' },
    ...sceneSlotFields({}),
  ], { okText: '创建', wide: true });
  if (!r) return;
  if (!r.name) return Toast.warn('场景名不能为空');
  const slots = collectSceneSlots(r);
  try {
    await API.scenes.create({
      project_id: State.currentProject.id, space_scale: 'medium', weather: 'clear',
      name: r.name, location_type: r.location_type, lighting: r.lighting,
      time_of_day: r.time_of_day, description: r.description || '',
      reference_features: Object.keys(slots).length ? { slots } : null,
    });
    Toast.success('已创建');
    await renderScenes();
  } catch (e) { Toast.error('失败: ' + e.message); }
}

async function renderStoryboard() {
  if (!State.currentProject) {
    document.getElementById('shotList').innerHTML = '<div class="empty-state">请先选择项目</div>';
    return;
  }
  try {
    const data = await API.shots.list(State.currentProject.id);
    State.shots = data.shots || [];
  } catch (e) { return; }
  const list = document.getElementById('shotList');
  if (!State.shots.length) {
    list.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon">🎞</div>
        <div class="empty-state-text">还没有分镜</div>
        <div class="empty-state-hint">
          联动流程：① 到「剧本」页点「💡 想法生成大纲」生成剧本 →
          ② 回到本页点「⚡ 自动从剧本生成」把每个场次转成分镜 →
          ③ 到「后期」页点 <b>「🎬 一键成片」</b>（自动配音 + 画面 + 字幕，<b>不需要任何付费视频模型 Key</b>）<br>
          <span class="dim">「生成」页的 11 个视频模型是可选的高阶玩法，需要自己配厂商 Key；不配也能出片。</span>
        </div>
        <button class="primary-btn" style="margin-top:12px" onclick="document.querySelector('.nav-item[data-page=&quot;script&quot;]').click()">→ 先去剧本页生成剧本</button>
      </div>`;
    return;
  }
  list.innerHTML = State.shots.map((shot, i) => {
    // 把「这个分镜用了哪些角色/场景」显性化 —— 让用户看得见
    // 剧本 → 角色/场景 → 分镜 → 生成 是一条真实的数据链，而不是各玩各的
    let cids = shot.character_ids;
    if (typeof cids === 'string') { try { cids = JSON.parse(cids); } catch (e) { cids = []; } }
    const cnames = (Array.isArray(cids) ? cids : [])
      .map(id => (State.characters || []).find(c => c.id === id))
      .filter(Boolean).map(c => c.name);
    const scene = shot.scene_id ? (State.scenes || []).find(s => s.id === shot.scene_id) : null;
    const linkTags = [
      scene ? `<span class="s-tag ok" title="本分镜使用的场景">📍 ${escapeHtml(scene.name)}</span>` : '',
      cnames.length ? `<span class="s-tag ok" title="本分镜使用的角色">👤 ${escapeHtml(cnames.join('、'))}</span>` : '',
      (!scene && !cnames.length) ? `<span class="s-tag warn" title="该分镜还没有关联角色/场景，生成时只能靠 AI 现编">未关联角色/场景</span>` : '',
    ].filter(Boolean).join(' ');
    return `
    <div class="shot-card" data-sid="${shot.id}">
      <div class="shot-header">
        <div class="shot-title">
          <span class="shot-index">${i+1}</span>
          <span>分镜 ${i+1}</span>
          <span class="badge">${shot.duration_seconds}s</span>
          <span class="badge accent">${ProviderMap[shot.model_provider]?.display_name || shot.model_provider}</span>
        </div>
        <div class="shot-actions">
          <button class="secondary-btn" data-action="audit" title="花钱之前先机器审查这条提示词：镜头运动是不是写了两个、动作塞不塞得进这一镜、有没有主体、对白有没有被写成画面文字">🔍 体检</button>
          <button class="secondary-btn" data-action="model">换模型</button>
          <button class="primary-btn" data-action="gen">${shot.status==='completed'?'重生成':'生成'}</button>
          <button class="danger-btn" data-action="del">删除</button>
        </div>
      </div>
      ${linkTags ? `<div class="shot-linkage">${linkTags}</div>` : ''}
      <div class="shot-layers-bar">
        <span class="dim">参与生成的层：</span>
        <label title="第1层：场面概述"><input type="checkbox" checked data-layer="l1" data-lsid="${shot.id}"> 第1层</label>
        <label title="第2层：逐秒动作/表情/镜头（信息量最大）"><input type="checkbox" checked data-layer="l2" data-lsid="${shot.id}"> 第2层</label>
        <label title="第3层：约束条件（走负面提示）"><input type="checkbox" checked data-layer="l3" data-lsid="${shot.id}"> 第3层</label>
        <button class="secondary-btn" data-action="editlayer" data-lsid="${shot.id}" style="margin-left:auto">✏️ 编辑各层提示词</button>
      </div>
      <div class="shot-section"><label>第 1 层</label>
        <textarea class="inline-edit" rows="2" data-field="layer1">${escapeHtml(shot.layer1_overview||'')}</textarea>
      </div>
      ${shot.layer2_timeline?.length ? `
      <div class="shot-section"><label>第 2 层</label>
        <div class="shot-timeline">
          ${shot.layer2_timeline.map(t => `
            <div class="timeline-item">
              <div class="timeline-time">[${t.start}-${t.end}s]</div>
              <div class="timeline-action"><strong>${escapeHtml(t.action||'')}</strong></div>
            </div>`).join('')}
        </div>
      </div>` : ''}
      <div class="shot-section">
        <span class="gen-status ${shot.status}">${formatGenStatus(shot.status)}${shot.progress ? ` · ${Math.round(shot.progress*100)}%` : ''}</span>
      </div>
    </div>
  `;
  }).join('');

  list.querySelectorAll('.shot-card').forEach(card => {
    const sid = card.dataset.sid;
    card.querySelector('[data-action="model"]').addEventListener('click', () => editShotModel(sid));
    const ab = card.querySelector('[data-action="audit"]');
    if (ab) ab.addEventListener('click', () => auditShotPrompt(sid, { interactive: true }));
    card.querySelector('[data-action="gen"]').addEventListener('click', () => generateShot(sid));
    card.querySelector('[data-action="del"]').addEventListener('click', () => deleteShot(sid));
    const el = card.querySelector('[data-action="editlayer"]');
    if (el) el.addEventListener('click', () => editShotLayers(sid));
    card.querySelectorAll('[data-layer]').forEach(cb => {
      cb.addEventListener('change', () => {
        GEN.layers[sid] = GEN.layers[sid] || {};
        GEN.layers[sid][cb.dataset.layer] = cb.checked;
      });
    });
  });
}

/**
 * 联络表：把所有分镜已选中的候选片段抽帧拼成一张图，一次看完。
 *
 * 为什么需要它：有些问题**自动判据抓不住**（我试过三种都失败）。
 * 真实例子：一个镜头末尾约 1.7 秒整体倒过来；两个项目的"5 个镜头"
 * 其实是同一张图，只是每镜换了压在上面的文字。
 * 这类问题只有**看一眼**才能发现，所以这里给的是入口，不是一个不可靠的红灯。
 */
async function showFrameSheet() {
  if (!State.currentProject) return Toast.warn('请先选择项目');
  Progress.show('正在抽帧拼图…（每镜 3 帧，片段多时稍慢）');
  try {
    const d = await API.shots.framesheet(State.currentProject.id, { per: 3, cols: 6 });
    Progress.hide();
    if (!d || !d.url) return Toast.error('没有拿到联络表');
    const miss = (d.missing || []).length;
    await Modal.open({
      title: `🎞 抽帧看一眼 · ${d.cells} 格（${d.cols}×${d.rows}）`,
      html: `
        <div class="modal-text">${escapeHtml(d.note || '')}
          ${miss ? ` · <span class="dim">${miss} 个分镜还没有选中片段，未纳入</span>` : ''}</div>
        <div style="max-height:60vh;overflow:auto;margin:.6em 0">
          <img src="${d.url}" style="width:100%;display:block" alt="contact sheet">
        </div>
        <details><summary class="dim">每一格对应哪个分镜/时间点</summary>
          <div class="dim" style="max-height:22vh;overflow:auto;white-space:pre-line">${escapeHtml((d.legend || []).join('\n'))}</div>
        </details>
        <div class="dim" style="margin-top:.5em">看什么：① 有没有哪个镜头的画面**方向不对/突然翻转**；
          ② 有没有几个镜头**其实是同一张图**（成片会像卡住）；
          ③ 画面跟分镜描述对不对得上。</div>`,
      okText: null, cancelText: '知道了', wide: true,
    });
  } catch (e) {
    Progress.hide();
    Toast.error('抽帧失败：' + (e.message || e));
  }
}

/**
 * 一键配音：整片每个镜头都按角色卡 + 剧本配好音，并把音轨挂进这一镜。
 *
 * 与"生成时自动配音"的区别：这条**不碰视频模型** —— 对**已经生成好的素材**
 * 也有效、零视频成本。它做的是整片一次性选角（可解释）+ 逐句韵律 + 挂轨。
 */
async function dubAllShots() {
  if (!State.currentProject) return Toast.warn('请先选择项目');
  Progress.show('正在整片选角并逐句配音…（镜头多时较慢，但不调用视频模型）');
  try {
    const d = await API.shots.dubAll(State.currentProject.id, { select: true });
    Progress.hide();
    const ct = (d && d.catalog) || {};
    const rows = Object.keys((d && d.cast) || {}).map(n => {
      const v = d.cast[n] || '';
      const why = ((d.reasons || {})[n] || '');
      return `<tr><td style="padding:.25em .6em .25em 0"><b>${escapeHtml(n)}</b></td>
        <td style="padding:.25em 0"><code>${escapeHtml(String(v).split(':').slice(-1)[0])}</code>
        <div class="dim" style="font-size:.85em">${escapeHtml(why)}</div></td></tr>`;
    }).join('');
    const shots = (d.shots || []).map(s => {
      const ok = s.ok ? '✅' : '—';
      const rec = s.recovered && s.recovered.added
        ? `<span class="s-tag ok">📜 从剧本「${escapeHtml(s.recovered.scene || s.recovered.from || '')}」回填 ${s.recovered.added} 句</span> ` : '';
      const w = (s.warnings || []).map(x => `<div class="dim">↳ ${escapeHtml(String(x))}</div>`).join('');
      return `<div style="margin:.3em 0">${ok} 分镜 ${s.index}：${s.lines || 0} 句${s.selected ? '（已设为选中）' : ''} ${rec}${w}</div>`;
    }).join('');
    // ★ 这一次**实际用的配音型号**（用户要求"真正的去选择配音模型"，
    //   那就得看得见它有没有生效）。
    const tts = (d && d.tts) || {};
    const modelLine = Object.keys(tts.models || {}).length
      ? `<div class="modal-text">🎙 本次配音模型：` + Object.entries(tts.models)
          .map(([k, v]) => `${escapeHtml(k)} = <code>${escapeHtml(v)}</code>`).join(' · ')
        + `　<span class="dim">（在「设置 → 🎙 配音模型」改）</span></div>`
      : '';
    await Modal.open({
      title: `🎙 一键配音 · ${escapeHtml(d.summary || '')}`,
      html: `
        <div class="modal-text">音色目录：<b>${ct.size || 0}</b> 个可用音色 ——
          ${escapeHtml(ct.note || '')}</div>
        ${modelLine}
        <div class="modal-text"><b>整片选角（每个角色一把嗓子，理由在下面）</b></div>
        <table style="width:100%;border-collapse:collapse">${rows || '<tr><td class="dim">（没有角色卡，用了默认音色）</td></tr>'}</table>
        ${(d.collisions || []).length ? `<div class="modal-text danger-text">⚠ 有角色共用同一把嗓子：${escapeHtml(JSON.stringify(d.collisions))}</div>` : ''}
        <div class="modal-text" style="margin-top:.6em"><b>逐镜结果</b></div>
        <div style="max-height:34vh;overflow:auto">${shots}</div>
        ${(d.warnings || []).length ? `<div class="modal-text danger-text">${(d.warnings || []).map(x => escapeHtml(String(x))).join('<br>')}</div>` : ''}
        <div class="dim" style="margin-top:.5em">没有重新生成画面 —— 只把音轨挂进了每个镜头。
          想让角色"说话方式"更贴合，可以在「角色」页补 personality（例如
          "说话语气强硬且带煽动性"），选角会据此换音色。</div>`,
      okText: null, cancelText: '知道了', wide: true,
    });
    await renderStoryboard();
  } catch (e) {
    Progress.hide();
    Toast.error('一键配音失败：' + (e.message || e));
  }
}

/**
 * 分镜**关联体检**：哪些镜头引用了已删除的角色、或文字提到某角色却没关联它。
 *
 * 为什么必须做（2026-09-14 用户实测「我用了 seedance 2.5，关联了场景+角色，
 * 还是没出现相关的人物」）：
 *   真实原因是**角色卡被删/重建**，分镜上留着悬空 ID，我们静默丢掉 →
 *   提示词里少了主体 → 模型**自己编了一个**（当天编出来的是个发光壮汉），
 *   而界面上一点提示都没有。参考方法论的 `MISSING_REFERENCE` 就是拦这个的。
 */
async function refCheckShots() {
  if (!State.currentProject) return Toast.warn('请先选择项目');
  Progress.show('正在体检分镜的角色/场景关联…');
  try {
    const d = await API.shots.refCheck(State.currentProject.id);
    Progress.hide();
    const bad = (d.shots || []).filter(s => s.severity !== 'ok');
    if (!bad.length) {
      Toast.success(`🩺 关联体检通过：${d.total} 个镜头的角色/场景都在`);
      return;
    }
    const rows = bad.map(s => `
      <div style="margin:.4em 0">
        <b>分镜 ${s.order_index}</b>
        <span class="s-tag ${s.severity === 'error' ? 'bad' : 'warn'}">${
          s.severity === 'error' ? '会生成错的人' : '注意'}</span>
        <div class="dim">${escapeHtml(s.summary || '')}</div>
        ${(s.issues || []).map(i => `<div class="dim">↳ ${escapeHtml(i.message)}</div>`).join('')}
      </div>`).join('');
    const canFix = bad.some(s => s.severity === 'error');
    const go = await Modal.open({
      title: `🩺 关联体检：${d.error_shots} 个分镜有硬问题`,
      html: `<div class="modal-text">这些镜头**生成出来的人物多半不是你要的** ——
        模型看不到没关联上的角色，就会自己编一个。</div>
        <div style="max-height:40vh;overflow:auto">${rows}</div>
        <div class="dim">点「一键补关联」会按剧本重新关联角色/场景（不重建分镜、不丢已生成的视频）。</div>`,
      okText: canFix ? '🔗 一键补关联' : null,
      cancelText: '知道了', wide: true,
    });
    if (go && canFix) await relinkShots();
  } catch (e) {
    Progress.hide();
    Toast.error('关联体检失败：' + (e.message || e));
  }
}

/* 给已有分镜补关联（不重建分镜、不丢已生成的视频） */
async function relinkShots() {
  if (!State.currentProject) return Toast.warn('请先选择项目');
  // 先把角色/场景资产拉进 State，否则标签显示不出名字
  try {
    const [c, s] = await Promise.all([
      API.characters.list(State.currentProject.id),
      API.scenes.list(State.currentProject.id),
    ]);
    State.characters = c.characters || [];
    State.scenes = s.scenes || [];
  } catch (e) { /* 忽略 */ }
  if (!State.characters.length && !State.scenes.length) {
    return Modal.open({
      title: '⚠ 还没有角色/场景资产',
      html: `<div class="modal-text">这个项目还没有从剧本提取过角色和场景，所以分镜没有东西可以关联。</div>
        <div class="modal-text">请先依次到：<b>「角色」页点「📝 从剧本提取」</b> →
        <b>「场景」页点「📝 从剧本提取」</b>，再回来点本按钮。</div>`,
      okText: null, cancelText: '知道了',
    });
  }
  Progress.show('正在关联角色/场景…');
  try {
    const r = await API.shots.relink(State.currentProject.id);
    Progress.hide();
    const d = r.detail || [];
    const sc = d.filter(x => x.scene).length, ch = d.filter(x => x.characters > 0).length;
    Toast.success(`✅ 已关联：场景 ${sc}/${r.total} · 角色 ${ch}/${r.total}（更新 ${r.updated} 条）`);
    if (r.hint) Toast.warn(r.hint);
    if (sc < r.total || ch < r.total) {
      Toast.info('还有分镜没匹配上，通常是因为角色/场景资产还没从剧本提取，或名字差异较大');
    }
    await renderStoryboard();
  } catch (e) {
    Progress.hide();
    Toast.error('关联失败：' + (e.message || e));
  }
}

async function addShot() {
  if (!State.currentProject) return Toast.warn('请先选择');
  const r = await Modal.form('新建分镜', [
    { key: 'duration', label: '时长（秒）', type: 'text', value: '10', hint: '各厂商上限不同，超限会自动截断' },
  ], { okText: '创建' });
  if (!r) return;
  const duration = parseInt(r.duration) || 10;
  try {
    await API.shots.create({
      project_id: State.currentProject.id, order_index: State.shots?.length || 0,
      duration_seconds: duration,
      model_provider: State.settings.default_model_provider || 'kling',
      model_name: State.settings.default_model_name || 'kling-1.6',
      aspect_ratio: State.currentProject.settings?.default_aspect_ratio || '16:9',
      resolution: '1080p',
      character_ids: (State.characters || []).map(c => c.id),
    });
    await renderStoryboard();
    Toast.success('分镜已添加');
  } catch (e) { Toast.error('失败: ' + e.message); }
}

async function autoGenShots() {
  if (!State.currentProject) return;
  try {
    const script = await API.scripts.get(State.currentProject.id);
    if (!script || !script.scenes?.length) return Toast.warn('请先生成剧本');
    Progress.show('从剧本生成...');
    for (const scene of script.scenes) {
      const tlp = script.three_layer_prompts?.[`scene_${scene.scene_number}`] || {};
      await API.shots.create({
        project_id: State.currentProject.id,
        order_index: scene.scene_number - 1,
        duration_seconds: scene.duration_seconds || 10,
        layer1_overview: tlp.layer1_overview || '',
        layer2_timeline: tlp.layer2_timeline || [],
        layer3_constraints: tlp.layer3_constraints || {},
        character_ids: (State.characters || []).map(c => c.id),
        model_provider: State.settings.default_model_provider || 'kling',
        model_name: State.settings.default_model_name || 'kling-1.6',
        aspect_ratio: State.currentProject.settings?.default_aspect_ratio || '16:9',
        resolution: '1080p',
      });
    }
    Progress.hide();
    await renderStoryboard();
    Toast.success(`已生成 ${script.scenes.length} 个分镜`);
  } catch (e) { Progress.hide(); Toast.error('失败: ' + e.message); }
}

/* 换视频生成模型：自绘弹窗（原 prompt 版在 WebView2 中不弹窗 → 表现为「无法选中模型」） */
const DEFAULT_MODELS = {
  kling: 'kling-1.6', wanx: 'wanx2.1-t2v-turbo', minimax: 'MiniMax-Hailuo-02',
  hailuo: 'MiniMax-Hailuo-02', jimeng: 'jimeng-video-3.0', seedance: 'doubao-seedance-1-0-pro',
  cogvideox: 'cogvideox', luma: 'ray-2', pika: 'pika-2.2', runway: 'gen4_turbo', sora: 'sora-2',
  vidu: 'viduq1', hunyuan: 'hunyuan-video',
};

function providerOptions() {
  return (State.videoProviders || []).map(p => ({
    value: p.name,
    label: `${p.display_name || p.name}${p.requires_api_key ? '' : '（本地/免密钥）'}${p.is_local ? ' · 本地' : ''}`,
  }));
}

async function editShotModel(sid, opts) {
  // opts.after：改完之后刷新哪个列表。默认刷「分镜」页；「生成」页传 loadGenShots。
  const after = (opts && opts.after) || renderStoryboard;
  const shot = (State.shots || []).find(s => s.id === sid);
  if (!providerOptions().length) {
    return Toast.error('视频模型列表为空，请检查后端或到设置页配置模型密钥');
  }
  const cur = shot?.model_provider || providerOptions()[0].value;
  let catalogModels = [];
  try {
    const cat = await API.videoModels.catalog('video', cur);
    catalogModels = (((cat || {}).catalog || {}).models) || [];
  } catch (e) { /* 忽略 */ }

  // 把能力矩阵渲染成"用户看得懂"的一行
  const capText = (m) => {
    const caps = m.caps || {};
    const parts = Object.keys(caps).map(r => `${r} 可出 ${caps[r].join('/')}秒`);
    return parts.length ? parts.join(' · ') : '（能力未标注）';
  };
  let unavailable = [];
  try {
    const cat0 = await API.videoModels.catalog('video', cur);
    unavailable = (((cat0 || {}).catalog || {}).unavailable) || [];
  } catch (e) { /* 忽略 */ }
  const modeText = (m) => (m.mode === 'i2v' ? '【图生视频·需先有场景图】' : '');

  const r = await Modal.form('更换视频生成模型', [
    { key: 'model_provider', label: '模型厂商', type: 'select', options: providerOptions(), value: cur },
    {
      key: 'model_name', label: '模型版本（后面标注了清晰度与可用时长）', type: 'select',
      value: shot?.model_name || DEFAULT_MODELS[cur] || '',
      hint: '清晰度越高，能出的时长往往越短',
      options: (catalogModels.length
        ? catalogModels.map(m => ({ value: m.id, label: `${m.label}${modeText(m)}｜${capText(m)}` }))
        : [{ value: DEFAULT_MODELS[cur] || '', label: DEFAULT_MODELS[cur] || '（默认）' }]),
    },
    {
      key: 'model_name_custom', label: '自定义模型名 / 接入点 ID', type: 'text',
      value: '', hint: '留空则用上面选的版本；火山方舟可填 ep-2024xxxx-xxxxx',
      placeholder: 'ep-xxxxxxxx 或官方模型 ID',
    },
    {
      key: 'advice', label: '这个版本能做什么（自动显示）', type: 'textarea', rows: 4,
      value: '', hint: '',
    },
  ], {
    desc: `当前分镜「${shot?.layer1_overview?.slice(0, 40) || sid}」`,
    okText: '应用',
    wide: true,
    bind: async (body) => {
      const sel = body.querySelector('[data-field="model_provider"]');
      const verSel = body.querySelector('[data-field="model_name"]');
      const custom = body.querySelector('[data-field="model_name_custom"]');
      const advice = body.querySelector('[data-field="advice"]');
      const showAdvice = (models, mid) => {
        const m = (models || []).find(x => x.id === mid);
        if (!advice) return;
        advice.value = m
          ? `版本：${m.label}\n模式：${m.mode_zh || '文生视频'}${m.mode === 'i2v' ? '（⚠ 必须先用场景图/角色图生成过图片，否则一定失败）' : ''}\n可用组合：${capText(m)}\n适合：${m.best_for || '—'}` +
            (m.tags && m.tags.length ? `\n标签：${m.tags.join('、')}` : '') +
            (m.verified === false ? '\n注意：该能力来自官方文档，未在本机实测' : '\n该能力已在本机用你的账号实测')
          : '（自定义模型：能力未知。若生成失败，请到「设置」点「🔌 测试视频模型连通性」看厂商原始报错）';
      };
      const reload = async () => {
        const p = sel.value;
        let models = [];
        try {
          const cat = await API.videoModels.catalog('video', p);
          models = (((cat || {}).catalog || {}).models) || [];
        } catch (e) { /* 忽略 */ }
        verSel.innerHTML = (models.length
          ? models.map(m => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.label + modeText(m) + '｜' + capText(m))}</option>`).join('')
          : `<option value="${DEFAULT_MODELS[p] || ''}">${DEFAULT_MODELS[p] || '（默认）'}</option>`)
          + (unavailable.length ? `<optgroup label="—— 以下版本已实测不可用，不要选 ——">`
            + unavailable.map(u => `<option value="${escapeHtml(u.id)}" disabled>${escapeHtml(u.id)}（${escapeHtml(u.reason)}）</option>`).join('')
            + '</optgroup>' : '')
          + '<option value="__custom__">（自定义，填下方输入框）</option>';
        try {
          const d = await API.videoModels.discover(p);
          if (d && d.ok && (d.video_models || []).length) {
            const have = new Set(models.map(m => m.id));
            const extra = d.video_models.filter(m => !have.has(m.id));
            if (extra.length) {
              const og = document.createElement('optgroup');
              og.label = '—— 你账号里的模型（实时拉取）——';
              extra.forEach(m => {
                const o = document.createElement('option');
                o.value = m.id;
                o.textContent = m.id + (m.usable ? '' : '（已停用）');
                og.appendChild(o);
              });
              verSel.insertBefore(og, verSel.querySelector('option[value="__custom__"]'));
            }
          }
        } catch (e) { /* 该厂商不支持列清单 */ }
        showAdvice(models, verSel.value);
        verSel.addEventListener('change', () => showAdvice(models, verSel.value));
      };
      sel.addEventListener('change', reload);
      if (!verSel.options.length) await reload();
      if (custom) custom.addEventListener('input', () => {
        if (custom.value.trim()) { verSel.value = '__custom__'; showAdvice([], ''); }
      });
    },
  });
  if (!r) return;
  if (!r.model_provider) return Toast.warn('未选择模型厂商');
  const nm = (r.model_name_custom || '').trim()
    || (r.model_name && r.model_name !== '__custom__' ? r.model_name : '')
    || DEFAULT_MODELS[r.model_provider] || '';
  try {
    Progress.show('切换模型中...');
    await API.shots.update(sid, { model_provider: r.model_provider, model_name: nm });
    Progress.hide();
    Toast.success(`已切换为 ${ProviderMap[r.model_provider]?.display_name || r.model_provider} · ${nm}`);
    await after();
  } catch (e) {
    Progress.hide();
    Toast.error('切换失败: ' + e.message);
  }
}

/** 「生成」页专用：把这一张卡片的模型改掉（复用分镜页那套带能力标注的选择器）。 */
function changeGenShotModel(sid) {
  return editShotModel(sid, { after: loadGenShots });
}

/**
 * 提示词体检：按「视频提示词契约」机器审查这条分镜的提示词。
 *
 * 为什么要有这个按钮：用户的抱怨是"有的视频太扯了"。真实的失败模式是
 * 提示词里同时要求两个镜头运动、要求的动作根本塞不进这一镜的时长、
 * 只有抽象形容没有主体、把对白写成了画面里可见的文字 —— 这些**在花钱之前**
 * 就能查出来，但过去没有任何地方告诉用户，只能等生成完对着废片猜。
 *
 * 这里只做"把问题摊开"，不擅自改用户的提示词。
 */
async function auditShotPrompt(sid, { interactive = false } = {}) {
  let a = null;
  try {
    a = await API.shots.promptAudit(sid);
  } catch (e) {
    if (interactive) Toast.error('体检失败：' + (e.message || e));
    return null;
  }
  if (!a) return null;
  const score = (typeof a.score === 'number') ? a.score : null;
  const errs = a.errors || [];
  const warns = a.warnings || [];
  if (!interactive) return a;

  const line = (x, kind) => {
    const code = x.code ? `<code>${escapeHtml(x.code)}</code>` : '';
    const fix = x.fix ? `<div class="dim" style="margin-left:1.2em">↳ ${escapeHtml(x.fix)}</div>` : '';
    return `<div style="margin:.35em 0"><span class="${kind}">${kind === 'danger-text' ? '✗' : '⚠'}</span> ${code} ${escapeHtml(x.message || '')}${fix}</div>`;
  };
  const head = score === null ? '' :
    `<div class="modal-text">体检分数：<b class="${score >= 90 ? '' : 'danger-text'}">${score}</b> / 100
      ${a.valid_for_generation === false ? ' · <span class="danger-text">不建议直接生成</span>' : ' · 可以生成'}</div>`;
  const body = (errs.length || warns.length)
    ? `${head}
       ${errs.length ? `<div class="modal-text"><b>必须改（会白花钱）</b></div>${errs.map(x => line(x, 'danger-text')).join('')}` : ''}
       ${warns.length ? `<div class="modal-text"><b>建议改（不一定废片，但会更稳）</b></div>${warns.map(x => line(x, 'warn-text')).join('')}` : ''}`
    : `${head}<div class="modal-text">没有查出问题：镜头运动只有一个、动作放得进这一镜的时长、
       主体和景别都写清了、对白没有被写成画面文字。</div>`;

  await Modal.open({
    title: '🔍 提示词体检',
    html: body,
    okText: null,
    cancelText: '知道了',
    wide: true,
  });
  return a;
}

async function generateShot(sid) {
  // 与「生成」页同一条链路：优先真实模型，失败自动回退本地合成。
  // 分镜页还支持**逐层勾选**：只把勾选的层送进视频模型提示词。
  const layers = (typeof GEN !== 'undefined' && GEN.layers && GEN.layers[sid]) || null;
  const card = document.querySelector(`.shot-card[data-sid="${sid}"]`);
  const btn = card ? card.querySelector('[data-action="gen"]') : null;
  const oldText = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '生成中…'; }
  if (card && !card.querySelector('.shot-gen-progress')) {
    const d = document.createElement('div');
    d.className = 'pp-compose-progress shot-gen-progress';
    d.innerHTML = '<div class="pp-bar"><div class="pp-bar-fill"></div></div>'
      + '<div class="pp-progress-text">提交…</div>';
    const head = card.querySelector('.shot-linkage') || card.querySelector('.shot-header');
    if (head && head.nextSibling) card.insertBefore(d, head.nextSibling);
    else card.appendChild(d);
  }
  const setP = (pct, txt) => {
    if (!card) return;
    const bar = card.querySelector('.shot-gen-progress .pp-bar-fill');
    const t = card.querySelector('.shot-gen-progress .pp-progress-text');
    if (bar) bar.style.width = Math.max(0, Math.min(100, pct)) + '%';
    if (t) t.textContent = `${pct.toFixed(0)}% · ${txt || ''}`;
  };
  try {
    // ★ 花钱之前先体检一次。有 error 就把选择权交给用户：
    //   要么先去改提示词，要么明确知道会白花钱还是坚持生成。
    //   体检本身失败（比如接口异常）绝不能挡住正常生成。
    let force = false;
    const audit = await auditShotPrompt(sid, { interactive: false });
    if (audit && audit.error_count > 0) {
      if (btn) { btn.disabled = false; btn.textContent = oldText; }
      const errs = (audit.errors || []).slice(0, 5).map(x =>
        `<div style="margin:.3em 0"><code>${escapeHtml(x.code || '')}</code> ${escapeHtml(x.message || '')}</div>`).join('');
      const go = await Modal.confirm(
        `<div>这条提示词体检出 <b class="danger-text">${audit.error_count}</b> 处硬伤，` +
        `直接提交给视频模型很可能白花钱：</div>${errs}` +
        `<div class="dim" style="margin-top:.6em">分数 ${audit.score}/100。` +
        `点「先改提示词」回去改；确实要生成也可以点「仍然生成」。</div>`,
        { title: '🔍 提示词体检未通过', okText: '仍然生成', cancelText: '先改提示词', danger: true });
      if (!go) return;
      force = true;
    }
    const r = await API.shots.render(sid, { mode: 'auto', auto_images: true, layers, force,
                                            ref_priority: refPriority() });
    if (!r.started) { Toast.warn(r.message || '无法启动'); if (btn) { btn.disabled = false; btn.textContent = oldText; } return; }
    const t0 = Date.now();
    const timer = setInterval(async () => {
      try {
        const st = await API.shots.renderStatus(sid);
        setP(st.percent || 0, st.message || '');
        if (st.running) {
          if (Date.now() - t0 > 20 * 60 * 1000) { clearInterval(timer); Toast.warn('超时，稍后刷新查看'); }
          return;
        }
        clearInterval(timer);
        if (btn) { btn.disabled = false; btn.textContent = oldText; }
        const res = st.result || {};
        if (res.ok) {
          const src = res.source === 'api' ? `真实模型 ${res.provider || ''}` : '本地合成';
          Toast.success(`✅ 分镜生成完成（${(res.duration || 0).toFixed(1)}s · ${src}）`);
          (res.warnings || []).forEach(w => Toast.warn(w));
        } else {
          Toast.error('生成失败：' + (st.error || (res.errors || []).join('；') || '未知错误'));
        }
        await renderStoryboard();
      } catch (e) {
        clearInterval(timer);
        if (btn) { btn.disabled = false; btn.textContent = oldText; }
        Toast.error('查询进度失败：' + e.message);
      }
    }, 1500);
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = oldText; }
    Toast.error('提交失败：' + e.message);
  }
}

/* 逐层编辑分镜提示词（模块化：每层可单独改） */
async function editShotLayers(sid) {
  const s = (State.shots || []).find(x => x.id === sid);
  if (!s) return;
  let tl = s.layer2_timeline;
  if (typeof tl === 'string') { try { tl = JSON.parse(tl); } catch (e) { tl = []; } }
  const tlText = (Array.isArray(tl) ? tl : []).map(t =>
    typeof t === 'object'
      ? `[${t.start}-${t.end}s] ${t.action || ''}${t.dialogue ? ' | 台词:' + t.dialogue : ''}${t.camera ? ' | 镜头:' + t.camera : ''}`
      : String(t)).join('\n');
  let cons = s.layer3_constraints;
  if (typeof cons === 'string') { try { cons = JSON.parse(cons); } catch (e) { cons = {}; } }

  const r = await Modal.form(`✏️ 编辑分镜各层 · 分镜 ${(s.order_index || 0) + 1}`, [
    { key: 'l1', label: '第 1 层 · 场面概述', type: 'textarea', rows: 3, value: s.layer1_overview || '',
      hint: '整场戏的概括，决定整体氛围' },
    { key: 'l2', label: '第 2 层 · 逐秒动作（每行一条，信息量最大）', type: 'textarea', rows: 8, value: tlText,
      hint: '格式：[起-止s] 动作 | 台词:xxx | 镜头:xxx' },
    { key: 'l3', label: '第 3 层 · 约束（不要出现的东西，逗号分隔）', type: 'textarea', rows: 2,
      value: ((cons && (cons.must_not_appear || cons.must_not_happen)) || []).join('，'),
      hint: '会作为负面提示传给模型' },
  ], { okText: '保存', wide: true });
  if (!r) return;

  // 第2层：把文本解析回结构（尽量保留原来的时间码，解析不出的行按顺序均分）
  const lines = String(r.l2 || '').split('\n').map(x => x.trim()).filter(Boolean);
  const re = /^\[\s*([\d.]+)\s*[-–~]\s*([\d.]+)\s*s?\s*\]\s*(.*)$/;
  const dur = s.duration_seconds || 10;
  const newTl = lines.map((ln, i) => {
    const m = ln.match(re);
    const body = m ? m[3] : ln;
    const seg = dur / Math.max(1, lines.length);
    const parts = body.split('|').map(x => x.trim());
    const action = parts[0] || '';
    let dialogue = '', camera = '';
    parts.slice(1).forEach(p => {
      if (p.startsWith('台词:')) dialogue = p.slice(3).trim();
      else if (p.startsWith('镜头:')) camera = p.slice(3).trim();
    });
    return {
      start: m ? parseFloat(m[1]) : +(i * seg).toFixed(1),
      end: m ? parseFloat(m[2]) : +((i + 1) * seg).toFixed(1),
      action, dialogue, camera,
      expression: (Array.isArray(tl) && tl[i] && tl[i].expression) || '',
    };
  });
  const mustNot = String(r.l3 || '').split(/[，,、\n]/).map(x => x.trim()).filter(Boolean);
  try {
    await API.shots.update(sid, {
      layer1_overview: r.l1 || '',
      layer2_timeline: newTl,
      layer3_constraints: { ...(typeof cons === 'object' && cons ? cons : {}), must_not_appear: mustNot },
    });
    Toast.success('已保存各层修改');
    await renderStoryboard();
  } catch (e) { Toast.error('保存失败：' + e.message); }
}

async function deleteShot(sid) {
  if (!await Modal.confirm('确认删除该分镜？已生成的视频文件不会一并删除。', { title: '删除分镜', okText: '删除', danger: true })) return;
  try { await API.shots.delete(sid); await renderStoryboard(); }
  catch (e) { Toast.error('失败: ' + e.message); }
}

/* ══════════════════════════════════════════════════════════════
 * 「生成」页 —— 单分镜出片
 * 借鉴 MPT 的核心思路：不调文生视频大模型，用「画面 + 配音 + 运镜 + 字幕」合成。
 * 因此**不需要任何付费视频模型 Key 也能出片**；配了 Key 则可切到真实模型。
 * ══════════════════════════════════════════════════════════════ */
const GEN = {
  voices: [],
  imageProviders: [],
  pollers: {},
  renderAllTimer: null,
  layers: {},          // sid -> {l1,l2,l3} 每层是否参与生成
};

/**
 * 「首帧参考」：模型一般只吃**一张**首帧图，所以这三件事不可能同时保证 ——
 * 跟上一镜衔接 / 场景跟场景图一致 / 人物跟角色图一致。
 *
 * 以前这个取舍是写死的（串联 > 场景图），用户看不到也改不了，
 * 于是"我关联了场景图/角色图，出片却不像"变成一个没有解释的谜。
 * 现在把这个取舍摊到界面上，并且**在结果里如实报出这一镜到底用了哪张图**。
 */
function refPriority() {
  const el = document.getElementById('refPrioritySel');
  return (el && el.value) || 'auto';
}

function genSettings() {
  return {
    voice_id: (document.getElementById('genVoice') || {}).value || 'edge:zh-CN-XiaoxiaoNeural',
    duration_seconds: (() => {
      const v = (document.getElementById('genDuration') || {}).value;
      return v && v !== 'auto' ? parseInt(v, 10) : null;
    })(),
    with_voice: (document.getElementById('genWithVoice') || {}).checked !== false,
    auto_images: (document.getElementById('genAutoImages') || {}).checked !== false,
    image_provider: (document.getElementById('genImageProvider') || {}).value || 'minimax',
    image_size: (document.getElementById('genAspect') || {}).value === '9:16' ? '720x1280' : '1280x720',
    // 生成方式：auto=有 Key 走真实模型、失败回退本地；api=只用真实模型（看真实报错）；local=只用本地
    mode: (document.getElementById('genMode') || {}).value || 'auto',
    model_provider: (document.getElementById('genModelProvider') || {}).value || undefined,
  };
}

function candidateVideoUrl(shot, forDownload) {
  let cands = shot.candidates;
  if (typeof cands === 'string') { try { cands = JSON.parse(cands); } catch (e) { cands = []; } }
  if (!Array.isArray(cands) || !cands.length) return '';
  const sel = cands.find(c => c && c.is_selected) || cands[cands.length - 1];
  if (!sel) return '';
  if (forDownload) return API.shots.mediaDownloadUrl(sel.path);
  return sel.url || API.shots.mediaUrl(sel.path) || '';
}

/* ══════════════════════════════════════════════════════════════
 * 「生成」页工具栏：音色 / 图像模型 两个下拉的真实化
 *
 * 这里曾经有一个**真 bug**：这两个下拉只在"缓存为空"时才填充，
 * 而每次切到生成页都会重建 innerHTML（下拉被清空）——
 * 于是第二次进生成页它们就永远停在「加载中…」/ 空白。
 * 用户看到的就是"配音音色和图像模型都是摆设"。
 * 现在统一按「下拉是不是空的」来判断，并且记住用户上次的选择。
 * ══════════════════════════════════════════════════════════════ */

const GEN_PREFS = {
  key: 'vf_gen_prefs',
  load() {
    try { return JSON.parse(localStorage.getItem(this.key) || '{}') || {}; }
    catch (e) { return {}; }
  },
  save(patch) {
    try {
      localStorage.setItem(this.key, JSON.stringify(Object.assign(this.load(), patch)));
    } catch (e) { /* 隐私模式等，忽略 */ }
  },
};

function voiceLabel(v) {
  const warn = v.usable === false ? ' ⚠ 无Key' : '';
  const free = v.requires_api_key ? '' : ' · 免费';
  return `${v.display_name}${free}${warn}`;
}

function fillVoiceSelect(vsel) {
  const prefs = GEN_PREFS.load();
  const byProv = {};
  for (const v of GEN.voices) (byProv[v.provider || 'other'] = byProv[v.provider || 'other'] || []).push(v);
  const order = ['edge', 'siliconflow', 'silent'];
  const keys = Object.keys(byProv).sort((a, b) => {
    const ia = order.indexOf(a), ib = order.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });
  const parts = [];
  for (const p of keys) {
    const list = byProv[p];
    const disp = list[0].provider_display || p;
    const noKey = list[0].usable === false;
    parts.push(`<optgroup label="${escapeHtml(disp)}${noKey ? '（⚠ 未配 Key，这些音色会失败）' : ''}">`);
    for (const v of list) {
      parts.push(`<option value="${escapeHtml(v.voice_id)}"${v.usable === false ? ' data-nokey="1"' : ''}>${escapeHtml(voiceLabel(v))}</option>`);
    }
    parts.push('</optgroup>');
  }
  vsel.innerHTML = parts.join('') || '<option value="edge:zh-CN-XiaoxiaoNeural">晓晓 · 温柔女声</option>';
  // 恢复上次选择；没有就用默认（优先免费可用的）
  const want = prefs.voice_id || GEN.voiceId || '';
  if (want && [...vsel.options].some(o => o.value === want)) vsel.value = want;
  else {
    const firstUsable = [...vsel.options].find(o => !o.dataset.nokey);
    if (firstUsable) vsel.value = firstUsable.value;
  }
  GEN.voiceId = vsel.value;
}

function imageProviderLabel(p) {
  const key = p.usable === false ? ' ⚠ 无Key' : ' ✅已配Key';
  const best = p.best_for ? `｜${p.best_for}` : '';
  return `${p.display_name}${key}${best}`;
}

function fillImageSelect(isel) {
  const prefs = GEN_PREFS.load();
  isel.innerHTML = GEN.imageProviders.map(p =>
    `<option value="${escapeHtml(p.name)}"${p.usable === false ? ' data-nokey="1"' : ''}>${escapeHtml(imageProviderLabel(p))}</option>`
  ).join('') || '<option value="minimax">MiniMax 图像</option>';
  const want = prefs.image_provider || GEN.imageProvider || '';
  if (want && [...isel.options].some(o => o.value === want)) isel.value = want;
  else {
    const firstUsable = [...isel.options].find(o => !o.dataset.nokey);
    if (firstUsable) isel.value = firstUsable.value;
  }
  GEN.imageProvider = isel.value;
}

/** 把音色/图像模型的实际作用讲清楚 + 提供试听/试生成 */
function renderGenToolHints() {
  const vsel = document.getElementById('genVoice');
  const isel = document.getElementById('genImageProvider');
  const box = document.getElementById('genToolHints');
  if (!box) return;
  const withVoice = (document.getElementById('genWithVoice') || {}).checked !== false;
  const autoImg = (document.getElementById('genAutoImages') || {}).checked !== false;
  const shots = State.shots || [];
  const noImage = shots.filter(s => !candidateVideoUrl(s) && !_shotHasImage(s)).length;

  const v = GEN.voices.find(x => x.voice_id === (vsel || {}).value);
  const p = GEN.imageProviders.find(x => x.name === (isel || {}).value);
  const rows = [];

  // ── 音色 ──
  if (!withVoice) {
    rows.push(`<span class="s-tag">未启用</span><b>配音音色</b>：上面「AI 配音」没勾，
      选什么音色都不会有声音。勾上它就生效。`);
  } else if (!v) {
    rows.push(`<span class="s-tag warn">待选</span><b>配音音色</b>：加载中…`);
  } else if (v.usable === false) {
    rows.push(`<span class="s-tag bad">不可用</span><b>配音音色</b>：${escapeHtml(v.display_name)} 属于
      「${escapeHtml(v.provider_display)}」，那家还没配 API Key，用它配音会失败、片子变静音。
      建议换 Edge TTS 的免费音色（标着「免费」的那些）。`);
  } else {
    const sample = shots.length ? narrationSample(shots[0]) : '';
    rows.push(`<span class="s-tag ok">已选</span><b>配音音色</b>：${escapeHtml(v.display_name)}
      <button class="secondary-btn" id="genVoicePreviewBtn" style="margin-left:6px">▶ 试听</button>
      <button class="secondary-btn" id="genVoicePreviewAllBtn" title="用这个音色朗读第 1 个分镜真正要读的文本">▶ 试听本片第一句</button>
      ${sample ? `<div class="dim" style="margin-top:4px">会朗读的内容（取第 1 个分镜）：「${escapeHtml(sample)}…」</div>` : ''}`);
  }

  // ── 图像模型 ──
  if (!autoImg) {
    rows.push(`<span class="s-tag">未启用</span><b>图像模型</b>：上面「缺画面时 AI 生成」没勾，
      没有画面的分镜会直接出<b>文字卡</b>，不会调用图像模型。`);
  } else if (!p) {
    rows.push(`<span class="s-tag warn">待选</span><b>图像模型</b>：加载中…`);
  } else {
    const keyTag = p.usable === false
      ? '<span class="s-tag bad">不可用</span>'
      : '<span class="s-tag ok">可用</span>';
    const usage = shots.length
      ? (noImage > 0
        ? `当前有 <b>${noImage}</b> 个分镜没有现成画面，生成时会用这个模型补画面。`
        : `当前 ${shots.length} 个分镜都已有画面 —— <b>这个模型暂时不会被用到</b>；`
          + `只有"没有画面的分镜"才走它。`)
      : '（还没有分镜）';
    rows.push(`${keyTag}<b>图像模型</b>：${escapeHtml(p.display_name)}｜尺寸 ${escapeHtml(p.default_size || '')}
      <button class="secondary-btn" id="genImageTestBtn" style="margin-left:6px">🧪 试生成一张</button>
      <div class="dim" style="margin-top:4px">${usage}${p.note ? ' ' + escapeHtml(p.note) : ''}</div>`);
  }

  box.innerHTML = rows.map(r => `<div class="gen-tool-hint">${r}</div>`).join('');
  box.querySelectorAll('.gen-tool-hint').forEach(el => { el.style.display = 'flex'; });

  // 试听
  const pv = document.getElementById('genVoicePreviewBtn');
  const pvAll = document.getElementById('genVoicePreviewAllBtn');
  if (pv) pv.addEventListener('click', () => previewGenVoice('你好，这是音色试听。'));
  if (pvAll) pvAll.addEventListener('click', () => {
    const s = (State.shots || [])[0];
    const t = s ? narrationSample(s, 40) : '';
    previewGenVoice(t || '你好，这是音色试听。');
  });

  // 试生成一张
  const tb = document.getElementById('genImageTestBtn');
  if (tb) tb.addEventListener('click', async () => {
    if (!p) return;
    if (p.usable === false) {
      return Modal.open({
        title: '⚠ 这个图像模型还不能用',
        html: `<div class="modal-text">${escapeHtml(p.unusable_reason || '没有配置 API Key')}</div>`,
        okText: null, cancelText: '知道了',
      });
    }
    if (!await Modal.confirm(
      `会用「${escapeHtml(p.display_name)}」生成<b>一张测试图</b>（约 5-20 秒，会计费，金额很小）。<br><br>
       测试图放在缓存目录，不会写入你的角色/场景资产。<br>继续吗？`,
      { title: '🧪 测试图像模型', okText: '生成' })) return;
    tb.disabled = true; const old = tb.textContent; tb.textContent = '生成中…';
    try {
      const r = await API.images.test({ provider: p.name, size: p.default_size || '1024x1024' });
      if (r.ok) {
        Modal.open({
          title: '✅ 图像模型可用',
          html: `<div class="modal-text">${escapeHtml(p.display_name)} · ${escapeHtml(r.model || '')}
            · ${r.elapsed}s · ${escapeHtml(r.size || '')}</div>
            <img src="${r.url}" style="width:100%;max-height:340px;object-fit:contain;
              border-radius:8px;background:var(--bg-3);margin-top:10px">`,
          okText: null, cancelText: '知道了',
        });
      } else {
        Modal.open({
          title: '❌ 图像模型不可用',
          html: `<div class="modal-text">${escapeHtml(r.error || '未知错误')}</div>
            <div class="modal-text dim" style="margin-top:8px">常见原因：Key 未配 / Key 无效 /
              账号额度用尽 / 提示词触发内容审核 / 网络无法访问该厂商接口。</div>`,
          okText: null, cancelText: '知道了',
        });
      }
    } catch (e) {
      Toast.error('测试失败：' + e.message);
    } finally { tb.disabled = false; tb.textContent = old; }
  });
}

/** 某个分镜是否已经有现成画面（分镜自带 / 场景图 / 角色图） */
function _shotHasImage(s) {
  if (!s) return false;
  if (s.image_path || s.reference_image_path || s.first_frame_path) return true;
  const sc = (State.scenes || []).find(x => x.id === s.scene_id);
  if (sc && sc.reference_image_path) return true;
  for (const cid of (s.character_ids || s.characters || [])) {
    const c = (State.characters || []).find(x => x.id === cid);
    if (c && c.reference_image_path) return true;
  }
  return false;
}

/** 取一个分镜"实际会被朗读"的文本（与后端 narration_for_shot 同规则，取第一句） */
function narrationSample(shot, max) {
  const lim = max || 28;
  let tl = shot && shot.layer2_timeline;
  if (typeof tl === 'string') { try { tl = JSON.parse(tl); } catch (e) { tl = []; } }
  const dl = [];
  if (Array.isArray(tl)) {
    for (const it of tl) {
      if (it && typeof it === 'object') {
        for (const k of ['dialogue', 'line', 'speech', 'voiceover', 'narration', 'subtitle']) {
          if (it[k] && String(it[k]).trim()) { dl.push(String(it[k]).trim()); break; }
        }
      }
    }
  }
  let t = dl.join(' ');
  if (!t) t = String((shot && shot.layer1_overview) || '');
  t = t.replace(/\[\s*\d+(\.\d+)?\s*[-–~]\s*\d+(\.\d+)?\s*s?\s*\]/g, '')
       .replace(/[*#`>]+/g, '').replace(/\s+/g, ' ').trim();
  return t.length > lim ? t.slice(0, lim) : t;
}

async function previewGenVoice(text) {
  const vsel = document.getElementById('genVoice');
  if (!vsel || !vsel.value) return Toast.warn('请先选择音色');
  const v = (GEN.voices || []).find(x => x.voice_id === vsel.value);
  if (v && v.usable === false) {
    return Modal.open({
      title: '⚠ 这个音色还不能用',
      html: `<div class="modal-text">${escapeHtml(v.unusable_reason || '该 provider 没有配置 API Key')}</div>`,
      okText: null, cancelText: '知道了',
    });
  }
  try {
    Progress.show('合成试听…');
    const url = API.voice.previewUrl(vsel.value, text);
    const audio = new Audio(url);
    await audio.play();
    Progress.hide();
    Toast.info('正在播放试听。如果没听到，检查系统音量。');
  } catch (e) {
    Progress.hide();
    Toast.error('试听失败：' + (e.message || e));
  }
}

async function loadGenOptions(force) {
  // 音色：只要下拉是空的就重新填（不是"缓存为空才填"——那正是以前的 bug）
  const vsel = document.getElementById('genVoice');
  if (vsel) {
    if (!GEN.voices.length || force) {
      try {
        const r = await API.voice.voices();
        GEN.voices = (r && r.voices) || [];
      } catch (e) {
        GEN.voices = [{ voice_id: 'edge:zh-CN-XiaoxiaoNeural',
                        display_name: '晓晓 · 温柔女声', provider: 'edge',
                        provider_display: 'Edge TTS', usable: true }];
      }
    }
    if (!vsel.options.length || !vsel.querySelector('optgroup') || force) fillVoiceSelect(vsel);
  }
  // 图像模型：同上
  const isel = document.getElementById('genImageProvider');
  if (isel) {
    if (!GEN.imageProviders.length || force) {
      try {
        const d = await API.images.providers();
        GEN.imageProviders = (d && d.providers) || [];
      } catch (e) {
        GEN.imageProviders = [{ name: 'minimax', display_name: 'MiniMax 图像',
                                default_size: '1024x1024', usable: true }];
      }
    }
    if (!isel.options.length || force) fillImageSelect(isel);
  }
  // 指定视频模型（留空=用分镜自身的模型）
  const msel = document.getElementById('genModelProvider');
  if (msel && !msel.options.length) {
    const provs = (State.videoProviders || []);
    const cfg = State.settings.api_keys || {};
    msel.innerHTML = '<option value="">（用每个分镜自己的模型）</option>' +
      provs.map(p => {
        const has = Object.keys(cfg).some(k => k.toLowerCase() === p.name.toLowerCase() && cfg[k]);
        return `<option value="${p.name}">${escapeHtml(p.display_name || p.name)}${has ? ' ✅已配Key' : ' ⚠无Key'}</option>`;
      }).join('');
    const prefs = GEN_PREFS.load();
    if (prefs.model_provider && [...msel.options].some(o => o.value === prefs.model_provider)) {
      msel.value = prefs.model_provider;
    }
    GEN.imageProviders = [];
    renderGenToolHints();
  }
  renderGenToolHints();
}

/**
 * 「生成」页顶部的"这个模型自带声音吗"提示。
 * 直接回答用户那句"视频模型自带语音字幕，那生成区是不是摆设"——
 * 顺手给一个一键跳过后期②③的入口，把两边打通。
 */
async function wireGenAudioHint() {
  const box = document.getElementById('genAudioHint');
  const sel = document.getElementById('genModelProvider');
  if (!box) return;

  const update = async () => {
    const prov = (sel && sel.value) || '';
    if (!prov) {
      box.innerHTML = '<span class="dim">每个分镜可能用不同模型；到每个分镜卡片的「换模型」里可以看到该模型是否自带声音。</span>';
      return;
    }
    let info = null, disp = prov;
    try {
      const cat = await API.videoModels.catalog('video', prov);
      const entry = (cat || {}).catalog || {};
      info = entry.audio || null;
      disp = entry.display || prov;
    } catch (e) { /* 忽略 */ }
    if (!info) {
      box.innerHTML = `<span class="dim">${escapeHtml(disp)}：是否自带声音未知，实拍一段听一下最准。</span>`;
      return;
    }
    if (info.native === true) {
      box.innerHTML = `
        <span class="s-tag ok">自带声音</span>
        <b>${escapeHtml(disp)}</b>：${escapeHtml(info.note || '')}
        <button class="secondary-btn" id="genSkipVoiceSubBtn"
                style="margin-left:8px">⏭ 去「后期」跳过 ②配音③字幕</button>`;
      const b = document.getElementById('genSkipVoiceSubBtn');
      if (b) b.addEventListener('click', async () => {
        if (!State.currentProject) return Toast.warn('请先选择项目');
        if (!await Modal.confirm(
          '把「后期」流水线的 <b>②配音</b> 和 <b>③字幕</b> 标为跳过？<br><br>' +
          '这样它们不会覆盖模型自带的原声，后面的步骤照常继续。<br>' +
          '随时可以在「后期」页点「↩ 取消跳过」恢复。',
          { title: '⏭ 跳过配音与字幕', okText: '跳过' })) return;
        try {
          Progress.show('设置中…');
          for (const k of ['voice', 'subtitle']) {
            try { await API.pipeline.skip(State.currentProject.id, k, '模型自带对白/字幕'); }
            catch (e) { /* 前面的步骤没跑时会失败，属正常 */ }
          }
          Progress.hide();
          Toast.success('已跳过 ②配音 ③字幕');
          navigateTo('postprocess');
        } catch (e) { Progress.hide(); Toast.error(e.message); }
      });
    } else if (info.native === false) {
      box.innerHTML = `<span class="s-tag">无声画面</span>
        <b>${escapeHtml(disp)}</b>：${escapeHtml(info.note || '')}`;
    } else {
      box.innerHTML = `<span class="s-tag warn">不确定</span>
        <b>${escapeHtml(disp)}</b>：${escapeHtml(info.note || '')}`;
    }
  };

  if (sel) sel.addEventListener('change', update);
  await update();
}

async function renderGeneratePage() {
  const panel = document.getElementById('generatePanel');
  if (!panel) return;
  if (!State.currentProject) {
    panel.innerHTML = '<div class="empty-state">请先在左侧选择一个项目</div>';
    return;
  }
  panel.innerHTML = `
    <div class="gen-toolbar">
      <div class="gen-toolbar-row">
        <label>配音音色 <select id="genVoice" style="min-width:230px"><option value="">加载中…</option></select></label>
        <label>每段时长
          <select id="genDuration">
            <option value="auto">自动（跟配音长度）</option>
            <option value="5" selected>5 秒</option>
            <option value="8">8 秒</option>
            <option value="10">10 秒</option>
            <option value="15">15 秒</option>
            <option value="20">20 秒</option>
            <option value="30">30 秒</option>
            <option value="60">1 分钟</option>
            <option value="120">2 分钟</option>
            <option value="300">5 分钟</option>
            <option value="600">10 分钟</option>
            <option value="1800">30 分钟</option>
          </select>
        </label>
        <label>画面比例
          <select id="genAspect">
            <option value="16:9">16:9 横屏</option>
            <option value="9:16">9:16 竖屏</option>
          </select>
        </label>
        <label>图像模型 <select id="genImageProvider" style="min-width:150px"></select></label>
      </div>
      <div class="gen-toolbar-row">
        <label>生成方式
          <select id="genMode" title="决定用真实视频模型还是本地合成">
            <option value="auto" selected>自动（有 Key 用真实模型，失败回退本地）</option>
            <option value="api">只用真实视频模型（会显示真实报错）</option>
            <option value="local">只用本地合成（不花钱）</option>
          </select>
        </label>
        <label>指定模型 <select id="genModelProvider" style="min-width:180px"></select></label>
        <label><input type="checkbox" id="genWithVoice" checked> AI 配音</label>
        <label><input type="checkbox" id="genAutoImages" checked> 缺画面时 AI 生成</label>
        <button class="primary-btn" id="genAllBtn">⚡ 全部生成</button>
        <button class="secondary-btn" id="genRefreshBtn">↻ 刷新</button>
        <span class="dim" id="genHint">本地合成：不需要任何视频模型 Key。可在「设置」里配 Key 后切换真实模型。</span>
      </div>
      <div class="gen-explain">
        <b>「生成」页和视频模型是什么关系？</b>
        <span>① <b>真实视频模型</b>负责<b>画面</b>——少数模型会自带对白与音效（如 Sora 2）；
          模型自带声音时，把「AI 配音」关掉，再到「后期」把 ②配音 ③字幕 <b>跳过</b>，别叠两套声音。</span>
        <span>② <b>本地合成</b>负责在画面上加<b>配音 + 字幕 + 运镜 + 转场</b>，不需要任何 Key。</span>
        <span>③ <b>「后期」页</b>是把上面产出的分镜片段按你的流水线拼成成片——三者是同一条链，不是三套系统。</span>
        <div class="gen-audio-hint" id="genAudioHint"></div>
      </div>
      <div class="gen-tool-hints" id="genToolHints"></div>
      <div class="pp-controls" style="margin-top:6px">
        <button class="secondary-btn" id="genOpenSettingsBtn"
                title="音色/图像模型要能用，得先配对应的 API Key">⚙ 配语音与图像 Key</button>
        <button class="secondary-btn" id="genRefreshOptionsBtn"
                title="重新拉取音色与图像模型清单（配完 Key 后点它）">↻ 刷新音色/模型</button>
      </div>
      <div class="pp-compose-progress" id="genAllProgress" style="display:none">
        <div class="pp-bar"><div class="pp-bar-fill" id="genAllBar"></div></div>
        <div class="pp-progress-text" id="genAllText"></div>
      </div>
    </div>
    <div id="genSeamPanel" class="seam-panel"></div>
    <div id="genShotList" class="gen-list"></div>`;

  await loadGenOptions();
  renderSeamPanel();
  document.getElementById('genAllBtn').addEventListener('click', genAllShots);
  wireGenAudioHint();
  document.getElementById('genRefreshBtn').addEventListener('click', renderGeneratePage);

  // 选完就记住（切页回来还在），并立刻刷新下面的说明
  const remember = (id, field) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener('change', () => {
      GEN_PREFS.save({ [field]: el.value });
      if (field === 'voice_id') GEN.voiceId = el.value;
      if (field === 'image_provider') GEN.imageProvider = el.value;
      renderGenToolHints();
    });
  };
  remember('genVoice', 'voice_id');
  remember('genImageProvider', 'image_provider');
  remember('genModelProvider', 'model_provider');
  ['genWithVoice', 'genAutoImages'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', () => {
      GEN_PREFS.save({ [id]: el.checked });
      renderGenToolHints();
    });
  });

  const os = document.getElementById('genOpenSettingsBtn');
  if (os) os.addEventListener('click', openSettings);
  const rf = document.getElementById('genRefreshOptionsBtn');
  if (rf) rf.addEventListener('click', async () => {
    rf.disabled = true; const old = rf.textContent; rf.textContent = '刷新中…';
    try {
      await loadGenOptions(true);
      Toast.success(`已刷新：音色 ${GEN.voices.length} 个 · 图像模型 ${GEN.imageProviders.length} 个`);
    } catch (e) { Toast.error('刷新失败：' + e.message); }
    finally { rf.disabled = false; rf.textContent = old; }
  });

  await loadGenShots();
}

async function loadGenShots() {
  const list = document.getElementById('genShotList');
  if (!list) return;
  let shots = [];
  try {
    const data = await API.shots.list(State.currentProject.id);
    shots = data.shots || [];
    State.shots = shots;
  } catch (e) {
    list.innerHTML = `<div class="empty-state">加载分镜失败：${escapeHtml(e.message)}</div>`;
    return;
  }
  if (!shots.length) {
    list.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon">🎬</div>
        <div class="empty-state-text">还没有分镜</div>
        <div class="empty-state-hint">
          ① 到「剧本」页生成剧本 → ② 到「分镜」页点「⚡ 自动从剧本生成」→ ③ 回到本页点「⚡ 全部生成」
        </div>
      </div>`;
    return;
  }
  list.innerHTML = shots.map((s, i) => {
    const url = candidateVideoUrl(s);
    const dlUrl = candidateVideoUrl(s, true);
    const status = s.status === 'completed' ? '<span class="s-tag ok">已生成</span>'
      : s.status === 'generating' ? '<span class="s-tag warn">生成中</span>'
      : s.status === 'failed' ? '<span class="s-tag bad">失败</span>'
      : '<span class="s-tag">待生成</span>';
    const err = s.error_message ? `<div class="gen-err">${escapeHtml(String(s.error_message).slice(0, 200))}</div>` : '';
    // 模型可用性提示：让"改成自己的模型却还是用本地"这种问题一眼可见
    const prov = s.model_provider || '';
    const keyTag = !prov ? ''
      : (s.has_api_key
        ? `<span class="s-tag ok">${escapeHtml(ProviderMap[prov]?.display_name || prov)} ✅有Key</span>`
        : `<span class="s-tag bad">${escapeHtml(ProviderMap[prov]?.display_name || prov)} ⚠无Key→只能本地合成</span>`);
    return `
    <div class="gen-card" data-sid="${s.id}">
      <div class="gen-card-head">
        <div class="gen-card-title">
          <span class="shot-index">${i + 1}</span>
          <span>分镜 ${i + 1}</span> ${status}
          <span class="badge">${s.duration_seconds || 10}s</span>
          ${keyTag}
          <span class="badge accent">${escapeHtml(ProviderMap[s.model_provider]?.display_name || s.model_provider || '')} · ${escapeHtml(s.model_name || '默认')}</span>
        </div>
        <div class="gen-card-actions">
          <button class="primary-btn" data-act="gen">🎬 生成</button>
          <button class="secondary-btn" data-act="plan"
                  title="先算一遍会怎么拍、要调用几次模型、画面怎么接上 —— 不花钱">📋 拍摄计划</button>
          <button class="secondary-btn" data-act="cfgdur">改时长</button>
          <button class="secondary-btn" data-act="cfgmodel"
                  title="换这个分镜用的视频模型（带清晰度/可用时长说明）">换模型</button>
          ${url ? '<button class="secondary-btn" data-act="toggle">▶ 预览</button>' : ''}
          ${url ? `<a class="secondary-btn" href="${dlUrl}" download>⬇ 下载</a>` : ''}
        </div>
      </div>
      <div class="gen-card-body">
        <div class="gen-shot-text">${escapeHtml((s.layer1_overview || '（无第 1 层内容）').slice(0, 160))}</div>
        ${err}
        <div class="gen-video-wrap" style="display:none">
          <video class="gen-video" controls preload="metadata" src="${url || ''}"></video>
        </div>
        <div class="pp-compose-progress gen-progress" style="display:none">
          <div class="pp-bar"><div class="pp-bar-fill"></div></div>
          <div class="pp-progress-text"></div>
        </div>
      </div>
    </div>`;
  }).join('');

  list.querySelectorAll('.gen-card').forEach(card => {
    const sid = card.dataset.sid;
    const genBtn = card.querySelector('[data-act="gen"]');
    genBtn.addEventListener('click', () => genOneShot(sid, card));
    const planBtn = card.querySelector('[data-act="plan"]');
    if (planBtn) planBtn.addEventListener('click', () => showShotPlan(sid, planBtn));
    const durBtn = card.querySelector('[data-act="cfgdur"]');
    if (durBtn) durBtn.addEventListener('click', () => changeShotDuration(sid));
    const modBtn = card.querySelector('[data-act="cfgmodel"]');
    if (modBtn) modBtn.addEventListener('click', () => changeGenShotModel(sid));
    const tg = card.querySelector('[data-act="toggle"]');
    if (tg) tg.addEventListener('click', () => {
      const w = card.querySelector('.gen-video-wrap');
      const v = card.querySelector('.gen-video');
      const show = w.style.display === 'none';
      w.style.display = show ? '' : 'none';
      tg.textContent = show ? '▼ 收起' : '▶ 预览';
      if (show && v && !v.getAttribute('src')) v.setAttribute('src', candidateVideoUrl({ candidates: null }) || '');
    });
  });
}

/**
 * 「📋 拍摄计划」：在**花钱之前**把这一镜会怎么拍摊开给用户看。
 *
 * 为什么必须有这个按钮：用户的原话是
 *   "分层定 10s 但最终只能生成 5s，且生成的视频跟实际描述的完全不相干"
 * 以前这个"对不上"的过程是黑盒 —— 用户点生成，拿到一个不是他要的东西，
 * 也不知道是谁改的、为什么改。这里把三件事直接讲清楚：
 *   ① 你写了几秒（标称）
 *   ② 台词真念完要几秒（**实测**，不是估的）
 *   ③ 所以实际按几秒拍、拆几段、要花几次调用、画面怎么接上
 * 和正式生成走的是同一个 `plan_shot`，所以这份预览不会跟结果不一致。
 */
async function showShotPlan(sid, btn) {
  const s = (State.shots || []).find(x => x.id === sid);
  if (!s) return;
  const old = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ 正在实测配音…'; }
  let d;
  try {
    // project_id 一般就在分镜对象上；万一没有就用当前项目兜底，
    // 免得因为一个字段缺失就整个按钮点了没反应。
    const pid = s.project_id || (State.project && State.project.id) || State.projectId;
    if (!pid) throw new Error('拿不到项目 ID');
    const r = await API.shots.plan(pid, sid);
    d = r.data || r;
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = old; }
    return Modal.open({
      title: '📋 拍摄计划',
      html: `<div class="gen-err">算不出来：${escapeHtml(e.message || String(e))}</div>
             <p class="hint">可以稍后再试。这不影响你直接点「🎬 生成」。</p>`,
      okText: null, cancelText: '知道了',
    });
  }
  if (btn) { btn.disabled = false; btn.textContent = old; }

  const segs = d.segments || [];
  const rows = segs.map((g, i) => {
    const acts = (g.timeline || []).map(a => a.action).filter(Boolean);
    return `<li>
      <b>第 ${i + 1} 段</b> · ${g.duration}s
      <span class="hint">（第 ${g.start}–${g.end} 秒 · ${g.line_count || 0} 句台词）</span>
      ${acts.length ? `<div class="hint">演：${escapeHtml(acts.join('，随后').slice(0, 120))}</div>` : ''}
    </li>`;
  }).join('');

  const calls = segs.length;
  const nominal = d.nominal || s.duration_seconds || 0;
  const audio = Number(d.audio_seconds || 0);
  const effective = d.effective || 0;

  // 三种"时长"的对照表 —— 这三行就是整个问题的答案
  // 第四行是画幅：厂商的"分辨率→画幅"是隐含的（海螺 768P 出的是 1:1 方形！），
  // 花钱之前就得让用户看见，而不是等成片被裁掉 44% 画面才发现。
  const asp = d.aspect || {};
  const aspWant = asp.want || s.aspect_ratio || '16:9';
  const aspActual = asp.actual || '';
  const aspAlts = asp.alternatives || [];
  const aspCell = asp.ok === false
    ? (aspActual
        ? `⚠ 与分镜不一致，成片会裁掉画面${aspAlts.length
            ? `；改用 ${escapeHtml(aspAlts.join('、'))} 可保持画幅` : ''}`
        : '⚠ 画幅不符，成片会裁掉画面')
    : (aspActual
        ? `画幅一致（实测 ${escapeHtml(d.resolution || '')} → ${escapeHtml(aspActual)}）`
        : '该分辨率→画幅还没实测过，生成时会实测并校验');
  const cmp = `
    <table class="plan-cmp">
      <tr><td>分镜上写的</td><td><b>${nominal}s</b></td>
          <td class="hint">这是剧本草稿，不一定做得到</td></tr>
      <tr><td>台词念完实测</td><td><b>${audio > 0 ? audio.toFixed(1) + 's' : '（无台词）'}</b></td>
          <td class="hint">${audio > 0
              ? '真的合成一遍量出来的，不是估算 —— 画面要盖住的是这个数'
              : '这段没有可念的台词，只能按标称时长拍'}</td></tr>
      <tr><td>画幅</td><td><b>${escapeHtml(aspWant)}</b>${aspActual
              ? ` <span class="hint">· ${escapeHtml(String(d.resolution || ''))} 实测出 ${escapeHtml(aspActual)}</span>`
              : ''}</td>
          <td class="hint">${aspCell}</td></tr>
      <tr class="plan-final"><td>实际按这个拍</td><td><b>${effective}s</b></td>
          <td class="hint">分 ${calls} 段 → 要调用模型 <b>${calls} 次</b></td></tr>
    </table>`;

  // 调整说明里带少量 <b> 标记（强调"尾帧接首帧"这类关键动作）。
  // 先整体转义、再只把 <b> 放回来 —— 不能直接 innerHTML 一段后端文本，
  // 万一里面混进了用户可控的字符就是注入。
  const fmtAdj = (a) => escapeHtml(String(a))
    .replace(/&lt;b&gt;/g, '<b>').replace(/&lt;\/b&gt;/g, '</b>');

  const adj = (d.adjustments || []).length
    ? `<h4>为什么这么改</h4><ul class="plan-list">${
        d.adjustments.map(a => `<li>${fmtAdj(a)}</li>`).join('')}</ul>`
    : '';
  const warn = (d.warnings || []).length
    ? `<h4>要注意</h4><ul class="plan-list plan-warn">${
        d.warnings.map(a => `<li>${escapeHtml(a)}</li>`).join('')}</ul>`
    : '';

  return Modal.open({
    title: `📋 拍摄计划 · 分镜 ${(s.order_index || 0) + 1}`,
    wide: true,
    html: `
      ${cmp}
      <h4>分 ${calls} 段怎么拍</h4>
      <ul class="plan-list">${rows || '<li class="hint">这段没有可切的内容</li>'}</ul>
      ${adj}${warn}
      <p class="hint">这份计划和你点「🎬 生成」时用的算法是同一个，
      所以看到的就是将要发生的。<b>看计划不花钱</b>（只合成台词量时长，有缓存）。</p>`,
    okText: '🎬 就按这个生成', cancelText: '再想想',
  }).then(r => {
    if (r) { const c = document.querySelector(`.gen-card[data-sid="${sid}"]`); genOneShot(sid, c); }
  });
}

/**
 * 「成片接缝」体检面板 —— 回答"为什么我的多个分镜拼起来一眼AI"。
 *
 * 用户原话："把多个分镜视频整合成为一个完整的衔接良好丝滑的视频你现在还是做不到"
 *            "别的 agent 一次性生成的视频连贯性很强"
 *
 * 实测出来最大的原因**不在后期**：线上项目 `19636f68` 的 6 个分镜用了
 * 3 种不同模型（video-01 / MiniMax-Hailuo-02 / kling-1.6），
 * 出来的画面一个是 3D 卡通、一个是写实真人 —— 不是同一个世界。
 * 调色和转场只能让接缝不那么刺眼，**补不回画风差异**。
 *
 * 所以这个面板先摊开"你的镜头是不是同一个世界"，再给一键统一。
 * 一键统一**默认只预演**（不偷偷改你的分镜），要真改得再点一次确认。
 */
async function renderSeamPanel() {
  const box = document.getElementById('genSeamPanel');
  if (!box) return;
  const pid = State.project && State.project.id;
  if (!pid) { box.innerHTML = ''; return; }
  box.innerHTML = '<div class="hint">正在体检各分镜的生成条件…</div>';
  let d;
  try {
    const r = await API.projects.consistency(pid);
    d = r.data || r;
  } catch (e) {
    box.innerHTML = `<div class="hint">接缝体检不可用：${escapeHtml(e.message || String(e))}</div>`;
    return;
  }
  if (!d || !d.total_shots) { box.innerHTML = ''; return; }

  const c = d.counts || {};
  const okish = d.consistent;
  const head = okish
    ? `<span class="s-tag ok">生成条件一致</span>
       <span class="hint">${escapeHtml(d.brief || '')}</span>`
    : `<span class="s-tag bad">生成条件不一致</span>
       <span class="hint">${escapeHtml(d.brief || '')}</span>`;

  const devRows = (d.deviations || []).slice(0, 8).map(x => `
    <li>分镜 ${x.index}${x.has_video ? '（已有视频，改了要重新生成）' : ''}：
      <b>${escapeHtml(x.reasons.join('；'))}</b></li>`).join('');
  const more = (d.deviations || []).length > 8
    ? `<li class="hint">…还有 ${(d.deviations || []).length - 8} 个</li>` : '';

  const warn = (d.warnings || []).map(w =>
    `<li>${escapeHtml(String(w))}</li>`).join('');

  box.innerHTML = `
    <div class="seam-head">
      <b>🎞 成片接缝体检</b> ${head}
    </div>
    <div class="seam-counts">
      <span class="s-tag">共 ${d.total_shots} 个分镜</span>
      ${c.fatal ? `<span class="s-tag bad">换了模型 ${c.fatal}</span>` : ''}
      ${c.major ? `<span class="s-tag bad">画幅不同 ${c.major}</span>` : ''}
      ${c.minor ? `<span class="s-tag warn">分辨率不同 ${c.minor}</span>` : ''}
      ${c.ok ? `<span class="s-tag ok">一致 ${c.ok}</span>` : ''}
    </div>
    ${(d.deviations || []).length ? `<ul class="seam-list">${devRows}${more}</ul>` : ''}
    ${warn ? `<ul class="seam-list seam-warn">${warn}</ul>` : ''}
    ${okish ? '' : `<div class="pp-controls" style="margin-top:6px">
      <button class="secondary-btn" id="seamUnifyBtn"
              title="先预演：只算会改什么，不动你的分镜">🧪 预演统一方案</button>
      <span class="hint">统一后，已生成过视频的分镜需要<b>重新生成</b>，
      否则成片里那一段还是旧画风 —— 后期补不回来。</span>
    </div>`}
    <div class="pp-controls" style="margin-top:6px">
      <button class="secondary-btn" id="seamChainBtn"
              title="把还没有视频的分镜按顺序补上（每镜接上一镜尾帧）。分镜写 10 秒而模型单次只给 6 秒时，会自动降一档分辨率或拆段，保证内容装得下">🔗 补全缺视频的分镜（按序 + 尾帧串联）</button>
      <span class="hint">只补<b>还没有视频</b>的分镜，已经出过片的不会被重复调用。
      先预演：只是算清楚<b>会调用模型几次</b>、哪几镜能串上，不生成、不花钱。</span>
    </div>`;

  const btn = document.getElementById('seamUnifyBtn');
  if (btn) btn.addEventListener('click', () => unifyConsistency(pid, true));
  const cbtn = document.getElementById('seamChainBtn');
  if (cbtn) cbtn.addEventListener('click', () => regenChained(pid, true));
}

/**
 * 「按序重生成 + 尾帧串联」的界面流程。
 *
 * 为什么需要它：尾帧串联（上一镜的尾帧当这一镜的首帧）是让多镜头像一镜到底
 * 最直接的一招，而它**只能靠重新生成**实现（已有片段是各自独立生成的，
 * 后期补不上"它们本该连续"）。接口 `POST /projects/{pid}/regen-chained`
 * 早就通了，但用户点不到 —— 这个函数就是把那一步交到手上。
 *
 * ★ 两段式，钱要说清楚：
 *   ① `dryRun=true` 先预演：会调用模型几次、哪几镜会串、哪些厂商没配 Key
 *   ② 用户确认后才 `dryRun=false` 真跑
 *   跑完把**接缝强度 before → after** 直接摊出来（这是"变好了多少"的答案）。
 */
async function regenChained(pid, dryRun) {
  try {
    if (dryRun) {
      // ★ 预演必须用**和真跑一样**的参数（only_missing），
      //   否则预演说"调用 5 次"、真跑只调 2 次 —— 预览跟实际不一致就是在骗人。
      const r = await API.projects.regenChained(pid, true, { onlyMissing: true });
      const d = r.data || r;
      const rows = (d.plan || []).map(p => `<li>
        镜头 ${p.index}：${p.will_chain
          ? '<b>接上一镜尾帧</b>'
          : escapeHtml(String(p.chain_why || '不串联'))}
        ${p.has_key ? '' : ' <span class="s-tag bad">无 API Key</span>'}
        ${p.skip ? ' <span class="s-tag">跳过</span>' : ''}</li>`).join('');
      const ok = await Modal.open({
        title: '🔗 按序重生成 · 预演',
        wide: true,
        html: `
          <p class="hint">下面是**将要发生的事**。预演没有生成任何内容、没有花钱。</p>
          <div class="plan-cmp">
            <table class="plan-cmp">
              <tr><td>会调用真实模型</td><td><b>${d.will_call_model_times} 次</b></td>
                  <td class="hint">每次都可能计费</td></tr>
              <tr><td>用上尾帧串联</td><td><b>${d.will_chain_times} 次</b></td>
                  <td class="hint">同场景内才串；换场景硬切是对的</td></tr>
              <tr class="plan-final"><td>顺序</td><td colspan="2" class="hint">
                  按镜头顺序逐个生成 —— 顺序是前提，上一镜先有视频，尾帧才存在</td></tr>
            </table>
          </div>
          <h4>每个镜头会怎么做</h4>
          <ul class="seam-list">${rows || '<li>没有分镜</li>'}</ul>
          ${(d.providers_without_key || []).length
            ? `<div class="plan-warn">这些厂商没配 API Key，对应镜头会被跳过：
               ${escapeHtml(d.providers_without_key.join('、'))}</div>` : ''}
          <p class="hint">${escapeHtml(String(d.note || ''))}</p>`,
        okText: d.will_call_model_times > 0
          ? `✔ 开始（会调用 ${d.will_call_model_times} 次）` : null,
        cancelText: '取消',
      });
      if (ok) await regenChained(pid, false);
      return;
    }

    // ── 真跑：启动 + 轮询 ──
    // ★ `onlyMissing: true` —— 只补**还没有视频**的分镜。
    //   这正是用户要的场景（"很多分镜的视频根本没生成，得补上"），
    //   而且能省钱：已经出过片的镜头不会被重复调用模型。
    const r0 = await API.projects.regenChained(pid, false, { onlyMissing: true });
    const d0 = r0.data || r0;
    if (d0.started === false) {
      return Modal.open({ title: '🔗 按序重生成',
        html: `<p>${escapeHtml(String(d0.message || '已经在跑了'))}</p>`,
        okText: null, cancelText: '知道了' });
    }
    const box = document.getElementById('genSeamPanel');
    let st = {};
    for (let i = 0; i < 1800; i++) {
      const rs = await API.projects.regenChainedStatus(pid);
      st = rs.data || rs;
      if (box) {
        box.dataset.busy = '1';
        const cur = document.getElementById('seamChainProg');
        const html = `<div id="seamChainProg" class="plan-cmp" style="margin-top:6px">
          <b>🔗 按序重生成中…</b> ${Math.round(st.percent || 0)}%
          <div class="hint">${escapeHtml(String(st.message || ''))}</div></div>`;
        if (cur) cur.outerHTML = html;
        else box.insertAdjacentHTML('beforeend', html);
      }
      if (!st.running) break;
      await new Promise(res => setTimeout(res, 1500));
    }
    const before = (st.seam_before || {}).mean_seam_ratio;
    const after = (st.seam_after || {}).mean_seam_ratio;
    const okN = (st.results || []).filter(x => x.ok).length;
    const chained = (st.results || []).filter(x => x.chained).map(x => x.index);
    const fails = (st.results || []).filter(x => !x.ok && !x.skipped);
    await Modal.open({
      title: '🔗 按序重生成 · 完成',
      wide: true,
      html: `
        <table class="plan-cmp">
          <tr><td>成功生成</td><td><b>${okN}/${(st.results || []).length}</b></td>
              <td class="hint">${fails.length ? `${fails.length} 个失败` : '全部成功'}</td></tr>
          <tr><td>串上尾帧的镜头</td><td><b>${chained.length ? chained.join('、') : '无'}</b></td>
              <td class="hint">同场景才串；换场景硬切是对的</td></tr>
          <tr class="plan-final"><td>接缝强度</td>
              <td><b>${before != null ? before : '—'} → ${after != null ? after : '—'}</b></td>
              <td class="hint">越接近 1 越像一镜到底（1.0 = 接缝处的变化与该片自身运动同量级）</td></tr>
        </table>
        ${fails.length ? `<h4>失败的镜头</h4><ul class="seam-list seam-warn">${
          fails.map(f => `<li>镜头 ${f.index}：${escapeHtml(String(f.error || '').slice(0, 140))}</li>`).join('')
        }</ul>` : ''}
        <p class="hint">接下来点「🎬 一键成片」把这些镜头拼成片 ——
        拼接时会自动量每个接缝的跳变，太硬的会改用溶解摊薄。</p>`,
      okText: null, cancelText: '知道了',
    });
    if (box) delete box.dataset.busy;
    renderGeneratePage();
  } catch (e) {
    Modal.open({ title: '按序重生成失败',
      html: `<div class="gen-err">${escapeHtml(e.message || String(e))}</div>`,
      okText: null, cancelText: '知道了' });
  }
}

async function unifyConsistency(pid, dryRun) {
  try {
    const r = await API.projects.unifyConsistency(pid, dryRun);
    const d = r.data || r;
    const rows = (d.planned || []).map(a => `
      <li>分镜 ${a.index}${a.must_regenerate ? ' <b>（必须重新生成）</b>' : ''}：
        ${escapeHtml(a.reasons.join('；'))}
        → 改成 <b>${escapeHtml(Object.entries(a.patch).map(
          ([k, v]) => `${k === 'model_name' ? '模型' : k === 'resolution' ? '分辨率' : k === 'aspect_ratio' ? '画幅' : k}=${v}`
        ).join('、'))}</b></li>`).join('');
    const note = escapeHtml(String(d.note || ''));
    if (dryRun) {
      const r2 = await Modal.open({
        title: '🧪 统一生成条件 · 预演',
        wide: true,
        html: `<p class="hint">下面是**将要改动**的内容。预演没有改动任何分镜。</p>
               <ul class="seam-list">${rows || '<li>不需要改动</li>'}</ul>
               ${(d.warnings || []).map(w => `<div class="plan-warn">${escapeHtml(String(w))}</div>`).join('')}
               <p class="hint">${note}</p>`,
        okText: (d.planned || []).length ? '✔ 就按这个改' : null,
        cancelText: '取消',
      });
      if (r2 && (d.planned || []).length) await unifyConsistency(pid, false);
      return;
    }
    await Modal.open({
      title: '✅ 已统一生成条件',
      html: `<p>${note}</p>
             <ul class="seam-list">${(d.applied || []).map(a =>
               `<li>分镜 ${a.index} 已更新</li>`).join('')}</ul>
             <p class="hint">标了「必须重新生成」的分镜，请到分镜卡片点「🎬 生成」重出一次，
             否则成片里保留的还是旧画风那一段。</p>`,
      okText: null, cancelText: '知道了',
    });
    renderSeamPanel();
    if (typeof renderGeneratePage === 'function') {
      // 分镜卡上的模型标签也变了，刷新一下
      try { await renderGeneratePage(); } catch (e) {}
    }
  } catch (e) {
    Modal.open({ title: '统一失败', html: `<div class="gen-err">${escapeHtml(e.message || String(e))}</div>`,
                 okText: null, cancelText: '知道了' });
  }
}

async function changeShotDuration(sid) {  const s = (State.shots || []).find(x => x.id === sid);
  const r = await Modal.form('修改该分镜时长', [
    { key: 'duration_seconds', label: '时长（秒）', type: 'text', value: String(s?.duration_seconds || 5),
      hint: '最小 5 秒；各视频模型上限不同，超出会自动截断' },
  ], { okText: '保存' });
  if (!r) return;
  const v = Math.max(5, parseInt(r.duration_seconds, 10) || 5);
  try {
    await API.shots.update(sid, { duration_seconds: v });
    Toast.success(`已改为 ${v} 秒`);
    await renderGeneratePage();
  } catch (e) { Toast.error('保存失败: ' + e.message); }
}

function genProgress(card, pct, text) {
  const wrap = card.querySelector('.gen-progress');
  const bar = card.querySelector('.gen-progress .pp-bar-fill');
  const txt = card.querySelector('.gen-progress .pp-progress-text');
  if (!wrap) return;
  wrap.style.display = '';
  bar.style.width = Math.max(0, Math.min(100, pct)) + '%';
  txt.textContent = `${pct.toFixed(0)}% · ${text || ''}`;
}

async function genOneShot(sid, card) {
  const btn = card.querySelector('[data-act="gen"]');
  if (GEN.pollers[sid]) return;             // 已在轮询 = 正在渲染
  const st0 = genSettings();
  const dur = st0.duration_seconds;
  const mode = st0.mode;
  // ★ 长时长的费用与耗时提示：适配器单次只能出 6~10s，长视频要分段多次调用模型
  if (mode !== 'local' && dur && dur > 10) {
    const segs = Math.ceil(dur / 10);
    if (!await Modal.confirm(
      `你选择的时长是 <b>${dur >= 60 ? (dur / 60) + ' 分钟' : dur + ' 秒'}</b>。<br><br>
       视频模型单次最多生成约 10 秒，所以要拆成 <b>约 ${segs} 段</b>分别调用模型，`
      + `再自动拼接（带交叉溶解转场）。<br>
       <b>这意味着约 ${segs} 次 API 调用</b>，费用与耗时都是单段的 ${segs} 倍左右。<br><br>
       确认继续吗？`,
      { title: '⚠ 长时长会产生多次模型调用', okText: '确认生成', danger: true })) return;
  }
  if (mode === 'local' && dur && dur > 60) {
    Toast.info('本地合成模式：长视频只是图片/片段拉长，不会调用任何付费模型');
  }
  btn.disabled = true;
  btn.textContent = '生成中…';
  genProgress(card, 0, '提交…');
  try {
    const r = await API.shots.render(sid, genSettings());
    if (!r.started) {
      Toast.warn(r.message || '无法启动');
      btn.disabled = false; btn.textContent = '🎬 生成';
      return;
    }
    GEN.pollers[sid] = setInterval(async () => {
      try {
        const st = await API.shots.renderStatus(sid);
        genProgress(card, st.percent || 0, st.message || '');
        if (st.running) return;
        clearInterval(GEN.pollers[sid]); delete GEN.pollers[sid];
        btn.disabled = false; btn.textContent = '🎬 生成';
        const res = st.result || {};
        if (res.ok) {
          const src = res.source === 'api'
            ? `真实模型 ${res.provider || ''}${res.segments > 1 ? `（${res.segments} 段拼接）` : ''}`
            : '本地合成';
          Toast.success(`✅ 分镜生成完成（${(res.duration || 0).toFixed(1)}s · ${src}）`);
          (res.warnings || []).forEach(w => Toast.warn(String(w).slice(0, 300)));
        } else {
          const msg = st.error || (res.errors || []).join('；') || '生成失败';
          Toast.error('生成失败：' + msg);
          genProgress(card, 100, '失败：' + msg);
          card.querySelector('.gen-progress .pp-bar-fill').style.background = 'var(--error)';
        }
        // 刷新列表，让生成的视频立刻出现在卡片上（▶预览 / ⬇下载）
        try { await loadGenShots(); } catch (e) {}
      } catch (e) {
        clearInterval(GEN.pollers[sid]); delete GEN.pollers[sid];
        btn.disabled = false; btn.textContent = '🎬 生成';
        Toast.error('查询进度失败：' + e.message);
      }
    }, 1200);
    // 立即查一次
    setTimeout(async () => {
      try {
        const st = await API.shots.renderStatus(sid);
        genProgress(card, st.percent || 0, st.message || '');
      } catch (e) {}
    }, 400);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = '🎬 生成';
    Toast.error('提交失败：' + e.message);
  }
}

async function genAllShots() {
  if (!State.currentProject) return Toast.warn('请先选择项目');
  const btn = document.getElementById('genAllBtn');
  btn.disabled = true;
  btn.textContent = '批量生成中…';
  document.getElementById('genAllProgress').style.display = '';
  try {
    const r = await API.shots.renderAll(State.currentProject.id, genSettings());
    if (!r.started) { Toast.warn(r.message || '无法启动'); btn.disabled = false; btn.textContent = '⚡ 全部生成'; return; }
    Toast.success(`已开始批量生成 ${r.total} 个分镜`);
    if (GEN.renderAllTimer) clearInterval(GEN.renderAllTimer);
    GEN.renderAllTimer = setInterval(async () => {
      try {
        const st = await API.shots.renderAllStatus(State.currentProject.id);
        document.getElementById('genAllBar').style.width = (st.percent || 0) + '%';
        document.getElementById('genAllText').textContent =
          `${(st.percent || 0).toFixed(0)}% · ${st.message || ''}`;
        if (!st.running) {
          clearInterval(GEN.renderAllTimer); GEN.renderAllTimer = null;
          btn.disabled = false; btn.textContent = '⚡ 全部生成';
          if (st.failed && st.failed.length) Toast.warn(st.message + '（部分失败，见分镜卡片）');
          else Toast.success('✅ 全部分镜生成完成');
          await loadGenShots();
        }
      } catch (e) {
        clearInterval(GEN.renderAllTimer); GEN.renderAllTimer = null;
        btn.disabled = false; btn.textContent = '⚡ 全部生成';
      }
    }, 1500);
  } catch (e) {
    btn.disabled = false; btn.textContent = '⚡ 全部生成';
    Toast.error('批量生成失败：' + e.message);
  }
}

/* ══════════════════════════════════════════════════════════════
 * 后期页：产出文件 / 比例导出 / 配音 / BGM / 输出目录
 * 这四项此前是"死按钮"（只弹一句 Toast），现在全部接了真实现。
 * ══════════════════════════════════════════════════════════════ */
const PP = { outputs: [], busy: false };

function fmtSize(n) {
  if (!n) return '0 B';
  if (n > 1048576) return (n / 1048576).toFixed(2) + ' MB';
  if (n > 1024) return (n / 1024).toFixed(1) + ' KB';
  return n + ' B';
}

async function loadOutputs() {
  if (!State.currentProject) return;
  const list = document.getElementById('outputList');
  const hint = document.getElementById('outputsHint');
  if (list) list.innerHTML = '<div class="dim" style="padding:10px">加载中…</div>';
  try {
    const d = await API.postprocess.outputs(State.currentProject.id);
    PP.outputs = d.outputs || [];
    if (hint) hint.textContent = `${PP.outputs.length} 个文件 · ${d.output_dir}`;
    const dirInput = document.getElementById('outputDirInput');
    if (dirInput && !dirInput.value) dirInput.value = d.root || '';
    const dirHint = document.getElementById('outputDirHint');
    if (dirHint) dirHint.textContent = `默认输出根目录：${d.root || '(未设置)'}`;
    renderOutputs();
    fillOutputSelectors();
  } catch (e) {
    if (list) list.innerHTML = `<div class="dim" style="padding:10px">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

function renderOutputs() {
  const list = document.getElementById('outputList');
  if (!list) return;
  if (!PP.outputs.length) {
    list.innerHTML = `<div class="empty-state" style="padding:18px">
      <div class="empty-state-text">还没有产出文件</div>
      <div class="empty-state-hint">去「生成」页渲染分镜，或在本页点「🎬 一键成片」直接出片。</div>
    </div>`;
    return;
  }
  // ★ 分组显示：让"生成 → 后期"的关系一眼可见（不再像两套互不相干的东西）
  const groups = [
    { key: 'final', title: '🎬 成片', hint: '一键成片 / 拼接产物',
      items: PP.outputs.filter(o => o.is_final || (o.rel || '').includes('final')) },
    { key: 'shots', title: '🎞 分镜片段', hint: '来自「生成」页渲染，可直接拼接成片',
      items: PP.outputs.filter(o => (o.rel || '').startsWith('shots/') && o.kind === 'video') },
    { key: 'derived', title: '🔄 派生件', hint: '比例导出 / 配音 / BGM 产物',
      items: PP.outputs.filter(o => o.kind === 'video' && !o.is_final
                                    && !(o.rel || '').includes('final')
                                    && !(o.rel || '').startsWith('shots/')) },
    { key: 'other', title: '📝 字幕 / 音频', hint: '',
      items: PP.outputs.filter(o => o.kind !== 'video') },
  ].filter(g => g.items.length);

  const rowHtml = (o) => `
    <div class="output-item" data-path="${escapeHtml(o.path)}">
      <div class="output-item-main">
        <span class="output-icon">${o.kind === 'video' ? '🎬' : o.kind === 'subtitle' ? '📝' : '🎵'}</span>
        <div class="output-item-text">
          <div class="output-item-name">${escapeHtml(o.name)}
            ${o.is_final ? '<span class="s-tag ok">成片</span>' : ''}
            ${(o.rel || '').startsWith('shots/api/') ? '<span class="s-tag ok">真实模型</span>' : ''}
            ${((o.rel || '').startsWith('shots/') && !(o.rel || '').startsWith('shots/api/')) ? '<span class="s-tag">本地合成</span>' : ''}
          </div>
          <div class="output-item-meta">${fmtSize(o.size_bytes)}${o.duration ? ' · ' + o.duration.toFixed(1) + 's' : ''} · ${escapeHtml(o.rel)}</div>
        </div>
      </div>
      <div class="output-item-actions">
        ${o.kind === 'video' ? `<button class="secondary-btn" data-oact="play">▶ 播放</button>
        <a class="secondary-btn" href="${o.url}" download="${escapeHtml(o.name)}">⬇ 下载</a>` : ''}
        <button class="secondary-btn" data-oact="reveal">📂 定位</button>
        <button class="danger-btn" data-oact="del">删除</button>
      </div>
      <div class="output-player" style="display:none">
        ${o.kind === 'video' ? `<video controls preload="metadata" src="${o.url}"></video>` : ''}
      </div>
    </div>`;

  list.innerHTML = groups.map(g => `
    <div class="output-group">
      <div class="output-group-head">
        <b>${g.title}</b>
        <span class="dim">${g.items.length} 个${g.hint ? ' · ' + g.hint : ''}</span>
      </div>
      ${g.items.map(rowHtml).join('')}
    </div>`).join('');

  list.querySelectorAll('.output-item').forEach(item => {
    const path = item.dataset.path;
    const play = item.querySelector('[data-oact="play"]');
    if (play) play.addEventListener('click', () => {
      const w = item.querySelector('.output-player');
      const show = w.style.display === 'none';
      w.style.display = show ? '' : 'none';
      play.textContent = show ? '▼ 收起' : '▶ 播放';
    });
    item.querySelector('[data-oact="reveal"]').addEventListener('click', async () => {
      try { await API.compose.reveal(State.currentProject.id); Toast.success('已打开输出目录'); }
      catch (e) { Toast.error('打开失败：' + e.message); }
    });
    item.querySelector('[data-oact="del"]').addEventListener('click', async () => {
      if (!await Modal.confirm(`确认删除「${escapeHtml(item.querySelector('.output-item-name').textContent.trim())}」？`,
        { title: '删除产出文件', okText: '删除', danger: true })) return;
      try {
        await API.postprocess.deleteOutput(State.currentProject.id, path);
        Toast.success('已删除');
        await loadOutputs();
      } catch (e) { Toast.error('删除失败：' + e.message); }
    });
  });
}

function fillOutputSelectors() {
  const vids = PP.outputs.filter(o => o.kind === 'video');
  const opts = vids.map(o =>
    `<option value="${escapeHtml(o.path)}"${o.is_final ? ' selected' : ''}>${escapeHtml(o.rel)}（${fmtSize(o.size_bytes)}）</option>`
  ).join('') || '<option value="">（暂无视频文件）</option>';
  ['exportSource', 'voiceTarget', 'bgmTarget'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = opts;
  });
  const av = document.getElementById('applyVoiceBtn');
  if (av) av.disabled = vids.length === 0;
}

async function exportAspectNow() {
  if (!State.currentProject) return;
  const src = (document.getElementById('exportSource') || {}).value;
  const aspect = (document.getElementById('exportAspect') || {}).value;
  if (!src) return Toast.warn('请先选择一个源视频（先做一键成片或拼接）');
  const btn = document.getElementById('exportAspectBtn');
  btn.disabled = true; const old = btn.textContent; btn.textContent = '导出中…';
  try {
    const r = await API.postprocess.exportAspectOf(State.currentProject.id, src, aspect);
    Toast.success('导出完成：' + r.output_path.split(/[\\/]/).pop());
    await loadOutputs();
  } catch (e) {
    Toast.error('导出失败：' + e.message);
  } finally { btn.disabled = false; btn.textContent = old; }
}

async function mixBgmNow() {
  if (!State.currentProject) return;
  const src = (document.getElementById('bgmTarget') || {}).value;
  const f = (document.getElementById('bgmFile') || {}).files || [];
  if (!src) return Toast.warn('请先选择要混音的视频');
  if (!f.length) return Toast.warn('请选择 BGM 音频文件');
  const btn = document.getElementById('addBgmBtn');
  btn.disabled = true; const old = btn.textContent; btn.textContent = '混音中…';
  try {
    const r = await API.postprocess.mixBgmFile(
      State.currentProject.id, src, f[0],
      parseFloat(document.getElementById('bgmVolume').value) || 0.25,
      parseFloat(document.getElementById('bgmFadeIn').value) || 0,
      parseFloat(document.getElementById('bgmFadeOut').value) || 0);
    Toast.success('BGM 已混入：' + r.output_path.split(/[\\/]/).pop());
    await loadOutputs();
  } catch (e) {
    Toast.error('BGM 混流失败：' + e.message);
  } finally { btn.disabled = false; btn.textContent = old; }
}

async function applyVoiceNow() {
  if (!State.currentProject) return;
  const src = (document.getElementById('voiceTarget') || {}).value;
  const voice = (document.getElementById('voiceSelect') || {}).value;
  const text = (document.getElementById('voiceText') || {}).value.trim();
  if (!src) return Toast.warn('请先选择要配音的视频');
  if (!voice) return Toast.warn('请先选择声音');
  if (!text) return Toast.warn('请输入配音文本');
  const btn = document.getElementById('applyVoiceBtn');
  btn.disabled = true; const old = btn.textContent; btn.textContent = '配音中…';
  try {
    const r = await API.voice.redoVoice(State.currentProject.id, src, voice, text,
      parseFloat(document.getElementById('voiceRate').value) || 1.0);
    Toast.success('配音完成：' + r.output_path.split(/[\\/]/).pop());
    await loadOutputs();
  } catch (e) {
    Toast.error('配音失败：' + e.message);
  } finally { btn.disabled = false; btn.textContent = old; }
}

/* ══════════════════════════════════════════════════════════════
 * 🧱 后期流水线（逐步确认版）
 * 每步跑完停下来给用户看结果：满意→下一步；不满意→重做/改参数。
 * 关键：每步产物都落盘（checkpoint），重做某步只从上一部产物继续，
 *       不会重新调用视频模型 —— 不重复花钱。
 * ══════════════════════════════════════════════════════════════ */
const PIPE = {
  steps: [],
  running: false,
  bgmPath: '',

  async load() {
    if (!State.currentProject) return;
    try {
      const r = await API.pipeline.status(State.currentProject.id);
      this.steps = r.steps || [];
      this.bgmPath = r.bgm_path || '';
    } catch (e) { this.steps = []; }
    this.render();
  },

  nextKey() {
    return (this.steps.find(s => !s.done) || {}).key || '';
  },

  nameOf(key) { return ((this.steps.find(s => s.key === key) || {}).name) || key; },

  render() {
    const box = document.getElementById('pipelineList');
    if (!box) return;
    if (!this.steps.length) {
      box.innerHTML = '<div class="dim" style="padding:10px">加载中…（请先在左侧选择一个项目）</div>';
      return;
    }
    const nextKey = this.nextKey();
    box.innerHTML = this.steps.map((s, i) => {
      const status = s.skipped
        ? '<span class="s-tag">已跳过</span>'
        : s.done
          ? '<span class="s-tag ok">已完成</span>'
          : (s.error ? '<span class="s-tag bad">失败</span>'
            : (s.stale ? '<span class="s-tag warn">需重跑</span>'
              : (s.key === nextKey ? '<span class="s-tag warn">下一步</span>' : '<span class="s-tag">未执行</span>')));
      const fields = s.skipped ? '' : (s.params || []).map(f => {
        const v = (s.params_used && s.params_used[f.k] !== undefined) ? s.params_used[f.k] : f.def;
        if (f.type === 'voice') {
          return `<label>${f.label} <span class="dim">用本页「配音」处选的音色</span></label>`;
        }
        if (f.type === 'checkbox') {
          return `<label><input type="checkbox" ${v !== false ? 'checked' : ''} data-pk="${f.k}" data-pkey="${s.key}"> ${f.label}</label>`;
        }
        if (f.type === 'select') {
          return `<label>${f.label} <select data-pk="${f.k}" data-pkey="${s.key}">
            ${(f.options || []).map(o => `<option value="${o.v}"${String(o.v) === String(v) ? ' selected' : ''}>${o.t}</option>`).join('')}
          </select></label>`;
        }
        return `<label>${f.label} <input type="number" step="${f.step || 1}" value="${v}" data-pk="${f.k}" data-pkey="${s.key}" style="width:88px"></label>`;
      }).join('');
      const bgmRow = (s.key === 'bgm' && !s.skipped)
        ? `<label>BGM 文件
             <input type="file" accept="audio/*" id="pipeBgmFile" style="max-width:220px">
             <span class="dim">${this.bgmPath ? '已选：' + escapeHtml(String(this.bgmPath).split('\\').pop()) : '未选择'}</span>
           </label>` : '';
      const out = s.has_output
        ? `<div class="pipeline-out">产物：${escapeHtml(String(s.note || '') || '已生成')}${s.duration ? ' · ' + Number(s.duration).toFixed(1) + ' 秒' : ''}
             <button class="secondary-btn" data-pact="preview" data-pkey="${s.key}">▶ 看这一步的结果</button></div>`
        : (s.error ? `<div class="pipeline-err">失败原因：${escapeHtml(s.error)}</div>` : '');
      const skipBtn = !s.skippable ? ''
        : (s.skipped
          ? `<button class="secondary-btn" data-pact="unskip" data-pkey="${s.key}">↩ 取消跳过</button>`
          : `<button class="secondary-btn" data-pact="skip" data-pkey="${s.key}"
               title="不做这一步，直接把上一步的画面带到下一步">⏭ 跳过</button>`);
      return `<div class="pipeline-step ${s.done && !s.skipped ? 'done' : ''}${s.skipped ? ' skipped' : ''}" data-sid="${s.key}">
        <div class="pipeline-step-head">
          <b>${i + 1}. ${s.icon} ${escapeHtml(s.name)}</b>
          ${status}
          <span class="pipeline-step-actions">
            ${skipBtn}
            <button class="secondary-btn" data-pact="run" data-pkey="${s.key}">${s.done && !s.skipped ? '🔁 重做这步' : '▶ 跑这步'}</button>
          </span>
        </div>
        <div class="pipeline-desc">${escapeHtml(s.what || '')}</div>
        ${s.why ? `<div class="pipeline-why">💡 ${escapeHtml(s.why)}</div>` : ''}
        ${(fields || bgmRow) ? `<div class="pipeline-fields">${fields}${bgmRow}</div>` : ''}
        ${out}
      </div>`;
    }).join('');

    box.querySelectorAll('[data-pact]').forEach(el => {
      const k = el.dataset.pkey;
      if (el.dataset.pact === 'preview') el.addEventListener('click', () => this.preview(k));
      else if (el.dataset.pact === 'run') el.addEventListener('click', () => this.runStep(k));
      else if (el.dataset.pact === 'skip') el.addEventListener('click', () => this.skip(k));
      else if (el.dataset.pact === 'unskip') el.addEventListener('click', () => this.unskip(k));
    });
    const bgmInput = document.getElementById('pipeBgmFile');
    if (bgmInput) bgmInput.addEventListener('change', async () => {
      if (!bgmInput.files[0]) return;
      try {
        Progress.show('上传 BGM…');
        const r = await API.pipeline.setBgm(State.currentProject.id, bgmInput.files[0]);
        Progress.hide();
        this.bgmPath = r.bgm_path;
        Toast.success('BGM 已设置：' + r.filename);
        await this.load();
      } catch (e) { Progress.hide(); Toast.error('上传失败：' + e.message); }
    });

    const hint = document.getElementById('pipelineHint');
    if (hint) {
      const done = this.steps.filter(s => s.done).length;
      const nx = this.steps.find(s => !s.done) || {};
      hint.textContent = `已完成 ${done}/${this.steps.length} 步 · 下一步：${nx.name || '（全部完成）'}`;
    }
    const nextBtn = document.getElementById('pipelineNextBtn');
    if (nextBtn) {
      const nk = this.nextKey();
      nextBtn.textContent = nk ? `▶ 跑下一步（${this.nameOf(nk)}）` : '✅ 全部完成，点这里出成片';
    }
  },

  collectParams(key) {
    const box = document.getElementById('pipelineList');
    const out = {};
    if (box) {
      box.querySelectorAll(`[data-pkey="${key}"][data-pk]`).forEach(el => {
        out[el.dataset.pk] = el.type === 'checkbox' ? el.checked : el.value;
      });
    }
    if (key === 'bgm' && this.bgmPath) out.bgm_path = this.bgmPath;
    return out;
  },

  setProgress(pct, txt) {
    const w = document.getElementById('pipelineProgress');
    if (w) w.style.display = '';
    const b = document.getElementById('pipelineBar'); if (b) b.style.width = Math.max(0, Math.min(100, pct)) + '%';
    const t = document.getElementById('pipelineText'); if (t) t.textContent = txt || '';
  },

  async runStep(key) {
    if (!State.currentProject) return Toast.warn('请先选择一个项目');
    if (this.running) return Toast.warn('正在执行中');
    this.running = true;
    const nb = document.getElementById('pipelineNextBtn');
    if (nb) nb.disabled = true;
    this.setProgress(2, '提交…');
    try {
      const r = await API.pipeline.run(State.currentProject.id, key, this.collectParams(key));
      if (!r.started) { Toast.warn(r.message || '无法启动'); return; }
      const st = await new Promise((resolve) => {
        const t = setInterval(async () => {
          try {
            const s2 = await API.pipeline.stepStatus(State.currentProject.id);
            this.setProgress(s2.percent || 0, s2.message || '');
            if (!s2.running) { clearInterval(t); resolve(s2); }
          } catch (e) { clearInterval(t); resolve({ error: e.message }); }
        }, 1200);
      });
      const res = (st || {}).result || {};
      await this.load();
      if (res.ok) {
        Toast.success(`✅ ${this.nameOf(key)} 完成${res.note ? '：' + res.note : ''}`);
        (res.warnings || []).forEach(w => Toast.warn(String(w).slice(0, 200)));
        await this.preview(key);
      } else {
        Toast.error(`${this.nameOf(key)} 失败：` + (res.error || (st || {}).error || '未知'));
      }
    } catch (e) {
      Toast.error('提交失败：' + e.message);
    } finally {
      this.running = false;
      const st2 = await API.pipeline.status(State.currentProject.id).catch(() => null);
      if (st2) { this.steps = st2.steps || []; }
      this.render();
      const nb2 = document.getElementById('pipelineNextBtn');
      if (nb2) nb2.disabled = false;
    }
  },

  async preview(key) {
    const s = this.steps.find(x => x.key === key);
    const wrap = document.getElementById('pipelinePreview');
    const player = document.getElementById('pipelinePlayer');
    if (!wrap || !player || !s || !s.has_output) return;
    const url = s.preview_url || `/api/projects/${State.currentProject.id}/pipeline/artifact/${key}`;
    wrap.style.display = '';
    player.setAttribute('src', url);
    player.load();
    document.getElementById('pipelinePreviewMeta').textContent =
      `${s.name} 的结果 · ${s.duration ? Number(s.duration).toFixed(1) + ' 秒 · ' : ''}${s.note || ''}`;
    const dl = document.getElementById('pipelinePreviewDownload');
    if (dl) {
      dl.style.display = '';
      dl.setAttribute('href', s.download_url || (url + '?download=1'));
      dl.setAttribute('download', key + '.mp4');
    }
    wrap.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  },

  async next() {
    const k = this.nextKey();
    if (!k) {
      try {
        Progress.show('正在生成最终成片…');
        const r = await API.pipeline.finalize(State.currentProject.id);
        Progress.hide();
        Toast.success('🎉 成片已生成：' + r.output_path);
        await loadOutputs();
        if (typeof window.vfRefreshFinal === 'function') await window.vfRefreshFinal();
        const pv = document.getElementById('pipelinePreview');
        if (pv) pv.style.display = 'none';
      } catch (e) { Progress.hide(); Toast.error('出成片失败：' + e.message); }
      return;
    }
    await this.runStep(k);
  },

  async runAll() {
    if (!State.currentProject) return Toast.warn('请先选择一个项目');
    if (this.running) return Toast.warn('正在执行中');
    if (!await Modal.confirm('将把剩下的步骤全部跑完（不中途停下）。<br>过程中可随时点「↺ 从头再来」重置。<br><br>继续吗？',
      { title: '⏩ 全部跑完', okText: '开始' })) return;
    const todo = this.steps.filter(x => !x.done).map(x => x.key);
    for (const k of todo) {
      await this.runStep(k);
      const st = await API.pipeline.status(State.currentProject.id).catch(() => null);
      if (st) this.steps = st.steps || [];
      const rec = this.steps.find(x => x.key === k);
      if (rec && !rec.done) { Toast.error(`「${this.nameOf(k)}」没成功，已停下`); return; }
    }
    if (!this.steps.filter(x => !x.done).length) await this.next();
  },

  async reset() {
    if (!State.currentProject) return;
    if (!await Modal.confirm('清空流水线记录、从头再来？<br>（不会删除已生成的分镜视频）',
      { title: '↺ 从头再来', okText: '清空', danger: true })) return;
    try {
      await API.pipeline.reset(State.currentProject.id);
      const pv = document.getElementById('pipelinePreview');
      if (pv) pv.style.display = 'none';
      await this.load();
      Toast.success('已重置');
    } catch (e) { Toast.error('重置失败：' + e.message); }
  },

  async skip(key) {
    if (!State.currentProject) return;
    const s = this.steps.find(x => x.key === key) || {};
    if (!await Modal.confirm(
      `「${escapeHtml(s.name || key)}」这一步不做了，<b>直接把上一步的画面带到下一步</b>。<br><br>` +
      `什么时候该这么做：视频模型自己就出了对白/字幕，或者你本来就不需要这一项。<br>` +
      `跳过后可以在同一条上点「↩ 取消跳过」恢复。`,
      { title: '⏭ 跳过这一步', okText: '跳过' })) return;
    try {
      await API.pipeline.skip(State.currentProject.id, key, '已手动跳过');
      await this.load();
      Toast.success(`已跳过「${this.nameOf(key)}」`);
    } catch (e) { Toast.error('跳过失败：' + e.message); }
  },

  async unskip(key) {
    try {
      await API.pipeline.unskip(State.currentProject.id, key);
      await this.load();
      Toast.success('已取消跳过');
    } catch (e) { Toast.error('取消失败：' + e.message); }
  },
};

function renderPostprocess() {
  const panel = document.querySelector('.postprocess-panel');
  if (!panel) return;
  // ⚠ 绝对不能用 innerHTML 覆盖整个面板：那会把 #outputList / #composeStartBtn /
  //   #exportSource 等节点的 DOM 永久销毁，之后所有按钮都找不到 → "点后期就出 bug"。
  //   这里改成非破坏性的顶部提示条。
  let tip = document.getElementById('ppNeedProject');
  if (!State.currentProject) {
    if (!tip) {
      tip = document.createElement('div');
      tip.id = 'ppNeedProject';
      tip.className = 'pp-need-project';
      tip.innerHTML = '⚠ 请先在左侧选择一个项目，然后这里的按钮才会生效。';
      panel.insertBefore(tip, panel.firstChild);
    }
    tip.style.display = '';
    return;
  }
  if (tip) tip.style.display = 'none';
  loadOutputs();
  PIPE.load().catch(e => console.warn('pipeline load failed', e));
}

async function stitchProject() {
  if (!State.currentProject) return;
  const addSubs = document.getElementById('addSubtitlesChk').checked;
  Progress.show('拼接...');
  try {
    const result = await API.postprocess.stitch(
      State.currentProject.id, null, 'final.mp4', addSubs);
    Progress.hide();
    Toast.success(`完成：${result.output_path}`);
    document.getElementById('outputList').innerHTML = `
      <div class="output-item">
        <span>📹 ${result.output_path.split(/[\\\\/]/).pop()}</span>
        <button onclick="window.open('file://${result.output_path}','_blank')">打开</button>
      </div>`;
  } catch (e) { Progress.hide(); Toast.error('失败: ' + e.message); }
}


// ──────────── 设置 ────────────

function openSettings() {
  // 视频 API keys
  const apiKeys = State.settings.api_keys || {};
  document.querySelectorAll('[data-key]').forEach(input => {
    input.value = apiKeys[input.dataset.key] || '';
  });

  // LLM API keys
  const llmKeys = State.settings.llm_api_keys || {};
  document.querySelectorAll('[data-llm-key]').forEach(input => {
    input.value = llmKeys[input.dataset.llmKey] || '';
  });

  // 语音合成（TTS）API keys —— 以前这里根本没有输入框，
  // 所以 SiliconFlow / MiniMax / 百炼 的音色永远不可能可用。
  const ttsKeys = State.settings.tts_api_keys || {};
  document.querySelectorAll('[data-tts-key]').forEach(input => {
    input.value = ttsKeys[input.dataset.ttsKey] || '';
  });
  loadImageProviderSettings();
  loadTtsModelSettings();

  // LLM provider/model
  const providerSelect = document.getElementById('llmProviderSelect');
  if (providerSelect) {
    providerSelect.value = State.settings.llm_provider || 'deepseek';
    // 填充模型下拉
    populateLlmModels(providerSelect.value);
  }

  // 其他设置
  document.querySelectorAll('[data-llm-setting]').forEach(input => {
    const key = input.dataset.llmSetting;
    if (State.settings[key] !== undefined) {
      input.value = State.settings[key];
    }
  });

  // 显示 providers
  const chips = document.getElementById('availableProviders');
  if (chips) {
    chips.innerHTML = State.videoProviders.map(p => `
      <span class="provider-chip ${apiKeys[p.name] ? 'has-key' : ''}">${p.display_name}</span>
    `).join('');
  }

  // 打开时显示当前已保存状态（让用户一眼看到是否已保存过 key）
  const status = document.getElementById('settingsStatus');
  if (status) {
    const llmCount = Object.keys(llmKeys).length;
    const videoCount = Object.keys(apiKeys).length;
    const ttsCount = Object.keys(ttsKeys).length;
    if (llmCount || videoCount || ttsCount) {
      status.textContent = `已保存：LLM ${llmCount} 个，视频 ${videoCount} 个，语音 ${ttsCount} 个`;
      status.style.color = 'var(--success)';
    } else {
      status.textContent = '尚未保存任何 Key';
      status.style.color = 'var(--text-tertiary)';
    }
  }

  document.getElementById('settingsModal').style.display = 'flex';
}

/** 设置弹窗里的「默认图像模型」下拉：列出可选模型 + Key 状态 */
async function loadImageProviderSettings() {
  const sel = document.getElementById('defaultImageProvider');
  const hint = document.getElementById('imageProviderHint');
  if (!sel) return;
  try {
    const d = await API.images.providers();
    const ps = (d && d.providers) || [];
    sel.innerHTML = '<option value="">（自动：用第一个配了 Key 的）</option>' +
      ps.map(p => `<option value="${escapeHtml(p.name)}">${escapeHtml(p.display_name)}` +
        `${p.usable === false ? ' ⚠ 无Key' : ' ✅已配Key'}</option>`).join('');
    const cur = State.settings.image_provider || d.default_provider || '';
    if (cur && [...sel.options].some(o => o.value === cur)) sel.value = cur;
    if (hint) {
      const usable = ps.filter(p => p.usable !== false);
      hint.innerHTML = usable.length
        ? `可用：${usable.map(p => escapeHtml(p.display_name)).join('、')}。` +
          `只有"没有现成画面的分镜"才会调用它。`
        : `⚠ 三家都没配 Key —— 没有画面的分镜会直接出<b>文字卡</b>。` +
          `MiniMax 复用你已配的对话 Key，通常填了就能用。`;
    }
  } catch (e) {
    sel.innerHTML = '<option value="">（读取失败）</option>';
  }
}

/**
 * 配音模型（TTS 型号）设置。
 *
 * 用户的原话：「最好是可以真正的去选择配音模型（模型新增 minimax M3 模型）」。
 * 以前这里**只有音色、没有型号**：型号写死在适配器里（minimax 一直是
 * speech-02-hd，而 dispatcher 连 config 都没往下传），所以"选模型"是不可能的。
 *
 * 现在的规矩：
 *   · 型号清单由**后端各引擎自己声明**（`/api/tts/models`），前端不抄一份；
 *   · 能选型号的给出下拉（含官方文档依据的说明）；不能选的**如实写明原因**
 *     （Edge 免费引擎没有模型参数；硅基流动的模型跟着音色走），
 *     而不是给一个点了没反应的假开关；
 *   · 顺手澄清一个常见误解：MiniMax 的 M3 是**语言模型**，不是配音模型。
 */
async function loadTtsModelSettings() {
  const box = document.getElementById('ttsModelBox');
  const note = document.getElementById('ttsModelNote');
  if (!box) return;
  try {
    const d = await API.voice.ttsModels();
    const ps = (d && d.providers) || [];
    const saved = State.settings.tts_models || {};
    box.innerHTML = ps.map(p => {
      const cur = saved[p.name] || p.selected_model || p.default_model || '';
      if (!p.supports_model) {
        const why = p.model_note || '这个引擎没有可选的模型参数';
        return `<label>${escapeHtml(p.display_name)}
          <span class="dim" style="font-size:11px">固定型号（不可选）</span>
          <span class="dim" style="font-size:11px">${escapeHtml(why)}</span></label>`;
      }
      const opts = (p.models || []).map(m =>
        `<option value="${escapeHtml(m.id)}"${m.id === cur ? ' selected' : ''}>` +
        `${escapeHtml(m.label || m.id)}</option>`).join('');
      const hit = (p.models || []).find(m => m.id === cur) || {};
      return `<label>${escapeHtml(p.display_name)} 配音型号
        <select data-tts-model="${escapeHtml(p.name)}">${opts}</select>
        <span class="dim" style="font-size:11px">${escapeHtml(hit.note || '')}</span>
        <span class="dim" style="font-size:11px">${
          p.has_api_key ? '✅ 已配 Key' : '⚠ 还没配 Key（选了也用不了）'}</span>
      </label>`;
    }).join('') || '<span class="dim" style="font-size:11px">没有可用的配音引擎</span>';
    if (note && d && d.note) note.textContent = d.note;
  } catch (e) {
    box.innerHTML = `<span class="dim" style="font-size:11px">读取配音模型失败：${escapeHtml(e.message || String(e))}</span>`;
  }
}

/** 测试 TTS provider 的 Key 能不能用（真合成一小段） */
async function testTtsProvider() {
  const sel = document.getElementById('testTtsProvider');
  const btn = document.getElementById('testTtsBtn');
  const out = document.getElementById('ttsTestResult');
  if (!sel) return;
  const provider = sel.value;
  // 先把当前输入框里的 Key 存起来再测，否则用户刚填就点会测到旧值
  await saveSettings({ silent: true });
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = '测试中…';
  if (out) { out.textContent = '正在合成一小段…'; out.style.color = 'var(--text-secondary)'; }
  try {
    const r = await API.voice.test(provider);
    if (r.ok) {
      if (out) {
        out.innerHTML = `✅ 可用（${r.elapsed}s · ${r.chars} 字 → ${r.size_bytes} 字节）`;
        out.style.color = 'var(--success)';
      }
      Modal.open({
        title: '✅ 语音合成可用',
        html: `<div class="modal-text">${escapeHtml(provider)} · 音色
          <code>${escapeHtml(r.voice_id || '')}</code> · ${r.elapsed}s</div>
          <audio controls style="width:100%;margin-top:10px"
                 src="${r.audio_url}"></audio>
          <div class="modal-text dim" style="margin-top:8px">${escapeHtml(r.note || '')}</div>`,
        okText: null, cancelText: '知道了',
      });
    } else {
      if (out) { out.textContent = '❌ ' + (r.error || '不可用'); out.style.color = 'var(--error)'; }
      Modal.open({
        title: '❌ 语音合成不可用',
        html: `<div class="modal-text">${escapeHtml(r.error || '未知错误')}</div>
          ${r.hint ? `<div class="modal-text" style="margin-top:8px">${escapeHtml(r.hint)}</div>` : ''}`,
        okText: null, cancelText: '知道了',
      });
    }
  } catch (e) {
    if (out) { out.textContent = '❌ ' + e.message; out.style.color = 'var(--error)'; }
    Toast.error('测试失败：' + e.message);
  } finally {
    btn.disabled = false; btn.textContent = old;
  }
}


function populateLlmModels(provider) {
  const select = document.getElementById('llmModelSelect');
  if (!select || !Chat.llmProviders.length) return;
  const p = Chat.llmProviders.find(x => x.name === provider);
  if (!p) return;
  select.innerHTML = p.available_models.map(m => `
    <option value="${m}">${m}</option>
  `).join('');
  const cur = State.settings.llm_model;
  if (cur && p.available_models.includes(cur)) select.value = cur;
  else select.value = p.default_model;
}


function closeSettings() {
  document.getElementById('settingsModal').style.display = 'none';
}


async function saveSettings(opts) {
  const silent = !!(opts && opts.silent);
  const apiKeys = {};
  document.querySelectorAll('[data-key]').forEach(input => {
    if (input.value.trim()) apiKeys[input.dataset.key] = input.value.trim();
  });

  const llmKeys = {};
  document.querySelectorAll('[data-llm-key]').forEach(input => {
    if (input.value.trim()) llmKeys[input.dataset.llmKey] = input.value.trim();
  });

  // ★ 语音合成 Key。后端 SettingsUpdate 必须有 tts_api_keys 字段，
  //   否则这里传了也会被 FastAPI 校验静默丢掉。
  const ttsKeys = {};
  document.querySelectorAll('[data-tts-key]').forEach(input => {
    if (input.value.trim()) ttsKeys[input.dataset.ttsKey] = input.value.trim();
  });

  // ★★ 配音模型（用户要求"真正的去选择配音模型"）。
  //   以前界面上**只有音色、没有型号**，型号写死在适配器里
  //   （minimax 一直是 speech-02-hd），用户无从选择。
  const ttsModels = {};
  document.querySelectorAll('[data-tts-model]').forEach(sel => {
    if (sel.value) ttsModels[sel.dataset.ttsModel] = sel.value;
  });

  const provider = document.getElementById('llmProviderSelect')?.value || 'deepseek';
  const model = document.getElementById('llmModelSelect')?.value || '';

  const settings = {
    api_keys: apiKeys,
    llm_api_keys: llmKeys,
    tts_api_keys: ttsKeys,
    tts_models: ttsModels,
    llm_provider: provider,
    llm_model: model,
  };
  const dip = document.getElementById('defaultImageProvider');
  if (dip) settings.image_provider = dip.value || '';
  // 默认视频模型（「剧本 → 分镜」时用它，避免配了 Key 却一直用默认 kling）
  const dvp = document.getElementById('defaultVideoProvider');
  const dvm = document.getElementById('defaultVideoModel');
  if (dvp) settings.default_model_provider = dvp.value || '';
  if (dvm) settings.default_model_name = dvm.value.trim();
  document.querySelectorAll('[data-llm-setting]').forEach(input => {
    settings[input.dataset.llmSetting] = input.value;
  });

  try {
    await API.settings.update(settings);
    State.settings = await API.settings.get();
    Chat.currentProvider = provider;
    Chat.currentModel = model;
    Chat.currentApiKey = llmKeys[provider] || '';
    Chat.updateLlmPill();

    // 保存后让「生成」页的音色/图像下拉重新拉一遍（Key 变了，可用性也变了）
    GEN.voices = [];
    GEN.imageProviders = [];
    loadTtsModelSettings();   // 配音模型：Key 有没配 / 当前选的是哪个，刷新一遍

    // 明确保存反馈：显示已保存的 key 数量
    const llmCount = Object.keys(llmKeys).length;
    const videoCount = Object.keys(apiKeys).length;
    const ttsCount = Object.keys(ttsKeys).length;
    const status = document.getElementById('settingsStatus');
    if (status) {
      status.textContent = `✓ 已保存（LLM ${llmCount} · 视频 ${videoCount} · 语音 ${ttsCount}，当前 LLM: ${provider}）`;
      status.style.color = 'var(--success)';
    }
    if (!silent) {
      Toast.success(`设置已保存：LLM ${llmCount} 个，视频 ${videoCount} 个，语音 ${ttsCount} 个`);
    }
    // 不自动关闭面板，让用户看到保存结果并可点“测试”验证
  } catch (e) {
    const status = document.getElementById('settingsStatus');
    if (status) {
      status.textContent = `✗ 保存失败: ${e.message}`;
      status.style.color = 'var(--error)';
    }
    if (!silent) Toast.error('保存失败: ' + e.message);
    throw e;   // 让 testTtsProvider 知道没存成功
  }
}


async function testLlm() {
  const provider = document.getElementById('llmProviderSelect')?.value || 'deepseek';
  const keyInput = document.querySelector(`[data-llm-key="${provider}"]`);
  const apiKey = keyInput ? keyInput.value.trim() : '';
  const status = document.getElementById('settingsStatus');
  if (status) {
    status.textContent = `测试 ${provider} 中...`;
    status.style.color = 'var(--text-secondary)';
  }
  try {
    const qs = new URLSearchParams({ provider });
    if (apiKey) qs.set('api_key', apiKey);
    const r = await fetch(`/api/chat/test?${qs.toString()}`, { method: 'POST' });
    const d = await r.json();
    if (d.data && d.data.available) {
      const preview = (d.data.response || '').slice(0, 40);
      if (status) {
        status.textContent = `✓ ${provider} 连接成功：${preview}`;
        status.style.color = 'var(--success)';
      }
      Toast.success(`${provider} 连接成功`);
    } else {
      const reason = d.message || (d.data && d.data.reason) || '失败';
      if (status) {
        status.textContent = `✗ ${provider}: ${reason}`;
        status.style.color = 'var(--error)';
      }
      Toast.error(`${provider} 测试失败: ${reason}`);
    }
  } catch (e) {
    if (status) {
      status.textContent = `✗ 测试出错: ${e.message}`;
      status.style.color = 'var(--error)';
    }
    Toast.error('测试出错: ' + e.message);
  }
}


// ──────────── 工具 ────────────

function formatStatus(s) {
  const map = {
    draft: '草稿', scripting: '剧本中', asset_building: '资产构建',
    generating: '生成中', postprocessing: '后期', completed: '完成',
  };
  return map[s] || s;
}

function formatGenStatus(s) {
  const map = {
    pending: '⏸ 等待', generating: '⚡ 生成中',
    completed: '✅ 完成', failed: '❌ 失败',
  };
  return map[s] || s;
}

function formatTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const diff = (new Date() - d) / 1000;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return Math.floor(diff/60) + ' 分钟前';
  if (diff < 86400) return Math.floor(diff/3600) + ' 小时前';
  if (diff < 604800) return Math.floor(diff/86400) + ' 天前';
  return d.toLocaleDateString('zh-CN');
}

window.addEventListener('DOMContentLoaded', init);

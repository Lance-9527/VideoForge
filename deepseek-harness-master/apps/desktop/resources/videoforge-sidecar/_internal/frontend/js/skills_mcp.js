/* ═══════════════════════════════════════════════════════════════════
   VideoForge · 技能（Skill）与 MCP 管理面板

   为什么单独一个文件而不是塞进 app.js：
     app.js 已经 26 万字符，任何改动都很难 review。这块功能自成一体
     （只读写 /api/skills 与 /api/mcp/*），独立文件更好维护，也更容易
     在出问题时单独关掉。

   ★ 两个刻意的设计：
     1) 技能列表只显示「名字 + 一句话描述」——和 AI 看到的一致。
        正文要点「查看全文」才拉，因为正文可能有几万字符。
     2) MCP 的 stdio 类型会**在本机启动子进程**，等同于允许该命令以用户
        身份运行。所以添加按钮旁边有一句明确警告，而且添加后会把真实的
        连接结果（连上了几个工具 / 失败原因）原样显示出来 ——
        不显示"添加成功"就完事。
   ═══════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function setStatus(el, text, kind) {
    if (!el) return;
    el.textContent = text || '';
    el.style.color = kind === 'err' ? 'var(--danger,#e05252)'
                   : kind === 'ok' ? 'var(--ok,#4caf7d)' : '';
  }

  async function req(method, path, body) {
    const opts = { method, headers: { 'Content-Type': 'application/json' } };
    if (body && method !== 'GET') opts.body = JSON.stringify(body);
    const r = await fetch((window.VIDEOFORGE_API || '') + path, opts);
    let data = null;
    try { data = await r.json(); } catch (e) { /* 非 JSON 响应 */ }
    if (!r.ok) {
      // ★ 错误必须说清真实原因：把后端的 message 原样带出来，
      //   不要统一成"操作失败"——那样用户完全没法排查。
      let detail = '';
      if (data) {
        if (typeof data.detail === 'string') detail = data.detail;
        else if (data.detail && data.detail.message) detail = data.detail.message;
        else if (data.message) detail = data.message;
      }
      throw new Error(detail || ('HTTP ' + r.status + ' ' + path));
    }
    // 后端统一包成 {code, message, data}
    return (data && Object.prototype.hasOwnProperty.call(data, 'data')) ? data.data : data;
  }

  /* ───────────────────────── 技能 ───────────────────────── */

  async function loadSkills() {
    const box = $('skillsList');
    const st = $('skillsStatus');
    if (!box) return;
    setStatus(st, '读取中…');
    try {
      const d = await req('GET', '/api/skills?stats=true');
      const skills = (d && d.skills) || [];
      // ★ 两种返回形状都认（2026-09-26）：
      //   · 嵌在 stats 里：{skills: [...], stats: {skills: [{name, uses}]}}
      //   · 平铺在顶层：  {count, skills: [{name, uses, last_used}]}
      //     （= 后端 `SkillRegistry.stats()` 原样返回的形状）
      //   ★ 为什么必须两种都认：这是"哪个技能真被用上了"的**唯一**来源。
      //     认错形状**不会报错** —— 只会让每个技能都显示"未用过"，
      //     也就是**静默显示错误信息**。而 `?stats=true` 的响应形状
      //     目前没有任何测试守着（UI 测试只核对元素 id 与接口路径存在）。
      const stats = (d && d.stats && d.stats.skills) || (d && d.skills) || [];
      const uses = {};
      stats.forEach(s => { uses[s.name] = s.uses || 0; });

      if (!skills.length) {
        box.innerHTML =
          '<div>还没有任何技能。</div>' +
          '<div style="margin-top:6px">在对话里对 AI 说「记住这个流程：…」，' +
          '它就会把做法学成一个技能，下次开新会话也还在。</div>';
      } else {
        box.innerHTML = skills.map(s => {
          const badge = s.source === 'builtin' ? '内置'
                      : s.source === 'learned' ? '学到的' : '外部';
          const rules = s.has_rules
            ? '<span style="color:var(--ok,#4caf7d)">含机器规则</span>' : '';
          const u = uses[s.name] ? ('用过 ' + uses[s.name] + ' 次') : '未用过';
          return '<div style="padding:6px 0;border-bottom:1px solid var(--border-1)">' +
            '<div><b>' + esc(s.name) + '</b> ' +
            '<span class="dim">[' + badge + '] ' + esc(u) + ' ' + rules + '</span></div>' +
            '<div class="dim" style="margin:2px 0 4px">' + esc(s.description || '（无描述）') + '</div>' +
            '<button class="secondary-btn skill-view" data-name="' + esc(s.name) +
            '" style="font-size:11px;padding:2px 8px">查看全文</button>' +
            (s.source === 'learned'
              ? '<button class="secondary-btn skill-del" data-name="' + esc(s.name) +
                '" style="font-size:11px;padding:2px 8px;margin-left:6px">删除</button>'
              : '') +
            '<pre class="skill-body" data-name="' + esc(s.name) +
            '" hidden style="white-space:pre-wrap;max-height:240px;overflow:auto;' +
            'font-size:11px;margin:6px 0 0;background:var(--bg-1);padding:8px;' +
            'border-radius:6px"></pre>' +
            '</div>';
        }).join('');
      }

      const errs = (d && d.errors) || [];
      setStatus(st, skills.length + ' 个技能' +
        (errs.length ? ('，' + errs.length + ' 个目录加载失败') : ''),
        errs.length ? 'err' : 'ok');
      if (errs.length) {
        box.innerHTML += '<div style="margin-top:8px;color:var(--danger,#e05252)">' +
          '⚠ 以下目录没被加载（缺 SKILL.md 或名字不合法）：<br>' +
          errs.map(e => esc(e.path + ' → ' + e.error)).join('<br>') + '</div>';
      }

      box.querySelectorAll('.skill-view').forEach(btn => {
        btn.addEventListener('click', async () => {
          const name = btn.getAttribute('data-name');
          const pre = box.querySelector('.skill-body[data-name="' + name + '"]');
          if (!pre) return;
          if (!pre.hidden) { pre.hidden = true; btn.textContent = '查看全文'; return; }
          btn.textContent = '加载中…';
          try {
            const one = await req('GET', '/api/skills/' + encodeURIComponent(name));
            pre.textContent = (one && one.body) || '（空）';
            pre.hidden = false;
            btn.textContent = '收起';
          } catch (e) {
            pre.textContent = '读取失败：' + e.message;
            pre.hidden = false;
            btn.textContent = '收起';
          }
        });
      });

      box.querySelectorAll('.skill-del').forEach(btn => {
        btn.addEventListener('click', async () => {
          const name = btn.getAttribute('data-name');
          if (!confirm('确定删除学到的技能「' + name + '」？此操作不可撤销。')) return;
          try {
            await req('DELETE', '/api/skills/' + encodeURIComponent(name));
            loadSkills();
          } catch (e) {
            alert('删除失败：' + e.message);
          }
        });
      });
    } catch (e) {
      box.textContent = '读取技能列表失败：' + e.message;
      setStatus(st, '读取失败', 'err');
    }
  }

  /* ───────────────────────── MCP ───────────────────────── */

  async function loadMcp(probe) {
    const box = $('mcpServersList');
    const st = $('mcpStatus');
    if (!box) return;
    setStatus(st, probe ? '正在逐个连接检测…' : '读取中…');
    try {
      const d = await req('GET', '/api/mcp/servers' + (probe ? '?probe=true' : ''));
      const servers = (d && d.servers) || [];
      if (!servers.length) {
        box.innerHTML =
          '<div>还没有配置 MCP 服务器。</div>' +
          '<div class="dim" style="margin-top:6px">点上面的「➕ 添加服务器」。' +
          '常用的比如 filesystem（让 AI 读写指定目录）、sqlite（查数据库）。</div>';
        setStatus(st, '0 台', 'ok');
        return;
      }
      box.innerHTML = servers.map(s => {
        const dot = s.connected ? '🟢' : (s.enabled ? '⚪' : '⏸');
        const state = !s.enabled ? '已禁用'
                    : s.connected ? ('已连接，' + (s.tools || 0) + ' 个工具')
                    : '未连接';
        const how = s.transport === 'stdio'
          ? esc(s.command || '')
          : esc(s.url || '');
        return '<div style="padding:6px 0;border-bottom:1px solid var(--border-1)">' +
          '<div>' + dot + ' <b>' + esc(s.name) + '</b> ' +
          '<span class="dim">[' + esc(s.transport) + '] ' + esc(state) + '</span></div>' +
          '<div class="dim" style="margin:2px 0 4px;word-break:break-all">' + how + '</div>' +
          (s.error ? '<div style="color:var(--danger,#e05252);font-size:11px">' +
                     esc(s.error) + '</div>' : '') +
          '<button class="secondary-btn mcp-tools" data-name="' + esc(s.name) +
          '" style="font-size:11px;padding:2px 8px">看工具</button>' +
          '<button class="secondary-btn mcp-toggle" data-name="' + esc(s.name) +
          '" data-enabled="' + (s.enabled ? '1' : '0') +
          '" style="font-size:11px;padding:2px 8px;margin-left:6px">' +
          (s.enabled ? '禁用' : '启用') + '</button>' +
          '<button class="secondary-btn mcp-del" data-name="' + esc(s.name) +
          '" style="font-size:11px;padding:2px 8px;margin-left:6px">删除</button>' +
          '<pre class="mcp-toolsbox" data-name="' + esc(s.name) +
          '" hidden style="white-space:pre-wrap;max-height:220px;overflow:auto;' +
          'font-size:11px;margin:6px 0 0;background:var(--bg-1);padding:8px;' +
          'border-radius:6px"></pre>' +
          '</div>';
      }).join('');
      setStatus(st, servers.length + ' 台，已连接 ' +
        servers.filter(s => s.connected).length + ' 台', 'ok');

      box.querySelectorAll('.mcp-tools').forEach(btn => {
        btn.addEventListener('click', async () => {
          const name = btn.getAttribute('data-name');
          const pre = box.querySelector('.mcp-toolsbox[data-name="' + name + '"]');
          if (!pre) return;
          if (!pre.hidden) { pre.hidden = true; btn.textContent = '看工具'; return; }
          btn.textContent = '加载中…';
          try {
            const r = await req('GET', '/api/mcp/servers/' + encodeURIComponent(name) + '/tools');
            const list = (r && r.tools) || [];
            pre.textContent = list.length
              ? list.map(t => '· ' + t.name + '\n    ' + (t.description || '（无说明）')).join('\n')
              : '（这台服务器没有暴露任何工具）';
          } catch (e) {
            pre.textContent = '读取失败：' + e.message;
          }
          pre.hidden = false;
          btn.textContent = '收起';
        });
      });

      box.querySelectorAll('.mcp-toggle').forEach(btn => {
        btn.addEventListener('click', async () => {
          const name = btn.getAttribute('data-name');
          const on = btn.getAttribute('data-enabled') === '1';
          try {
            await req('POST', '/api/mcp/servers/' + encodeURIComponent(name) + '/enabled',
                      { enabled: !on });
            loadMcp(false);
          } catch (e) { alert('操作失败：' + e.message); }
        });
      });

      box.querySelectorAll('.mcp-del').forEach(btn => {
        btn.addEventListener('click', async () => {
          const name = btn.getAttribute('data-name');
          if (!confirm('确定删除 MCP 服务器「' + name + '」？')) return;
          try {
            await req('DELETE', '/api/mcp/servers/' + encodeURIComponent(name));
            loadMcp(false);
          } catch (e) { alert('删除失败：' + e.message); }
        });
      });
    } catch (e) {
      box.textContent = '读取 MCP 服务器失败：' + e.message;
      setStatus(st, '读取失败', 'err');
    }
  }

  function splitArgs(text) {
    // 参数按空格切。带引号的整段保留（路径里常有空格）。
    const out = [];
    const re = /"([^"]*)"|'([^']*)'|(\S+)/g;
    let m;
    while ((m = re.exec(text || '')) !== null) {
      out.push(m[1] !== undefined ? m[1] : (m[2] !== undefined ? m[2] : m[3]));
    }
    return out;
  }

  async function saveMcp() {
    const res = $('mcpAddResult');
    const name = ($('mcpNewName').value || '').trim();
    const transport = $('mcpNewTransport').value;
    if (!name) { setStatus(res, '请填名字', 'err'); return; }
    const payload = { name, transport };
    if (transport === 'stdio') {
      payload.command = ($('mcpNewCommand').value || '').trim();
      payload.args = splitArgs($('mcpNewArgs').value || '');
      if (!payload.command) { setStatus(res, 'stdio 必须填命令', 'err'); return; }
    } else {
      payload.url = ($('mcpNewUrl').value || '').trim();
      if (!payload.url) { setStatus(res, 'http 必须填 URL', 'err'); return; }
    }
    setStatus(res, '保存中并测试连接…');
    try {
      const d = await req('POST', '/api/mcp/servers', payload);
      // ★ 拿不到探测结果时**不许说"连不上"**（2026-09-26 修）。
      //   原来写的是 `const p = (d && d.probe) || {}` —— 于是"后端没返回
      //   探测结果"被当成了 `connected = false`，界面上**每次都会**显示
      //   「⚠ 配置已保存，但连不上：未知原因」。那是在**编一个失败**：
      //   用户照着它去查一堆不存在的问题。
      //   现在：没有探测结果就如实说"没有返回连接结果"，让用户点刷新看状态。
      const p = (d && d.probe) || null;
      if (!p) {
        setStatus(res, '✅ 配置已保存（本次没有返回连接结果，可点「刷新」看状态）', 'ok');
      } else if (p.connected) {
        setStatus(res, '✅ 已连接，发现 ' + (p.tool_count || 0) + ' 个工具', 'ok');
      } else {
        // ★ 连不上时不要显示"添加成功"就完事 —— 配置保存了，但必须说清连不上
        setStatus(res, '⚠ 配置已保存，但连不上：' + (p.error || '未知原因'), 'err');
      }
      if (d && d.warning) {
        setStatus(res, (res.textContent || '') + ' ｜ ' + d.warning,
                  p ? (p.connected ? 'ok' : 'err') : '');
      }
      $('mcpAddForm').hidden = true;
      loadMcp(false);
    } catch (e) {
      setStatus(res, '添加失败：' + e.message, 'err');
    }
  }

  /* ───────────────────────── 初始化 ───────────────────────── */

  function init() {
    const r1 = $('refreshSkillsBtn');
    if (r1) r1.addEventListener('click', () => loadSkills());
    const r2 = $('refreshMcpBtn');
    if (r2) r2.addEventListener('click', () => loadMcp(false));
    const r3 = $('probeMcpBtn');
    if (r3) r3.addEventListener('click', () => loadMcp(true));

    const add = $('addMcpBtn');
    if (add) add.addEventListener('click', () => {
      const f = $('mcpAddForm');
      f.hidden = !f.hidden;
      setStatus($('mcpAddResult'), '');
    });
    const cancel = $('cancelMcpBtn');
    if (cancel) cancel.addEventListener('click', () => { $('mcpAddForm').hidden = true; });
    const save = $('saveMcpBtn');
    if (save) save.addEventListener('click', saveMcp);

    const sel = $('mcpNewTransport');
    if (sel) sel.addEventListener('change', () => {
      const isStdio = sel.value === 'stdio';
      $('mcpStdioFields').hidden = !isStdio;
      $('mcpHttpFields').hidden = isStdio;
    });

    // 打开设置面板时才去拉数据 —— 不在页面加载时就请求，
    // 免得每次开 app 都多两个无用请求（还会连 MCP 子进程）。
    const modal = $('settingsModal');
    if (modal) {
      const obs = new MutationObserver(() => {
        // ★ 可见性判据用 `getComputedStyle`，并**同时监听 `class`**（2026-09-26 修）。
        //   原来读的是 `modal.style.display` —— 那只是**内联**样式：
        //   如果弹窗是用 CSS **class** 控制显隐的，这个值恒为空串，
        //   判据形同虚设；而且 `attributeFilter` 里没有 `class`，
        //   "用 class 打开弹窗"根本不会触发这个回调 → 两个面板**永远不加载**
        //   （用户看到的是空白面板，且不报错）。
        let vis = true;
        try { vis = getComputedStyle(modal).display !== 'none'; } catch (e) { vis = true; }
        if (vis && !modal.hidden) {
          if (!loadSkills._done) { loadSkills._done = true; loadSkills(); }
          if (!loadMcp._done) { loadMcp._done = true; loadMcp(false); }
        }
      });
      obs.observe(modal, { attributes: true, attributeFilter: ['style', 'hidden', 'class'] });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.VFSkillsMcp = { loadSkills, loadMcp };
})();

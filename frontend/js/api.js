/**
 * VideoForge · 后端 API 客户端
 *
 * 设计目标：
 * - 统一调用入口
 * - 自动处理响应码（Pavo 风格 code:"000000"）
 * - 错误友好提示
 * - SSE 流订阅
 */

const API_BASE = window.VIDEOFORGE_API || '';  // 同域部署时为空


class APIError extends Error {
  constructor(message, code = 'unknown', httpStatus = 0) {
    super(message);
    this.code = code;
    this.httpStatus = httpStatus;
  }
}


// 从任意错误结构中提取可读消息
function extractErrorMessage(err) {
  if (!err) return null;
  // FastAPI detail 可能是 string/object/array（validation 错误是 array）
  let d = err.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) {
    // Pydantic validation error: [{type, loc, msg, input}, ...]
    return d.map(e => `${(e.loc || []).join('.')}: ${e.msg || JSON.stringify(e)}`).join('; ');
  }
  if (d && typeof d === 'object') {
    if (d.message) return d.message;
    if (d.msg) return d.msg;
    return JSON.stringify(d);
  }
  if (err.message) return err.message;
  return null;
}


async function request(method, path, body = null, options = {}) {
  const url = API_BASE + path;
  const headers = { 'Content-Type': 'application/json', ...options.headers };

  const config = { method, headers };
  if (body && method !== 'GET') {
    config.body = typeof body === 'string' ? body : JSON.stringify(body);
  }

  // 超时保护：默认 20s（读写接口），生成类接口可在 options.timeout 放大
  const timeoutMs = options.timeout || 20000;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  config.signal = controller.signal;

  let response;
  try {
    response = await fetch(url, config);
  } catch (e) {
    clearTimeout(timer);
    if (e.name === 'AbortError') {
      throw new APIError(`请求超时（${Math.round(timeoutMs / 1000)}s）：${path}`, 'timeout', 0);
    }
    throw new APIError(`网络错误: ${e.message}`, 'network', 0);
  }
  clearTimeout(timer);

  if (!response.ok) {
    let detail;
    try {
      const err = await response.json();
      detail = extractErrorMessage(err);
    } catch {
      detail = await response.text();
    }
    throw new APIError(detail || `HTTP ${response.status}`, 'http', response.status);
  }

  const data = await response.json();
  if (data.code && data.code !== '000000') {
    throw new APIError(data.message || '业务错误', data.code, response.status);
  }
  return data.data;
}


async function uploadFile(path, file, onProgress = null) {
  const url = API_BASE + path;
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) {
        onProgress(Math.round(e.loaded / e.total * 100));
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          const data = JSON.parse(xhr.responseText);
          if (data.code && data.code !== '000000') {
            reject(new APIError(data.message || 'Error', data.code));
          } else {
            resolve(data.data);
          }
        } catch (e) {
          reject(new APIError('响应解析失败', 'parse'));
        }
      } else {
        reject(new APIError(`HTTP ${xhr.status}`, 'http', xhr.status));
      }
    };
    xhr.onerror = () => reject(new APIError('网络错误', 'network'));
    const fd = new FormData();
    fd.append('file', file);
    xhr.send(fd);
  });
}


function subscribeTask(taskId, onProgress) {
  // SSE 订阅任务进度
  const url = `${API_BASE}/api/tasks/${taskId}/stream`;
  const es = new EventSource(url);

  es.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      onProgress(data);
      if (data.status === 'completed' || data.status === 'failed') {
        es.close();
      }
    } catch (err) {
      console.error('SSE parse error:', err);
    }
  };

  es.onerror = (err) => {
    console.error('SSE error:', err);
    es.close();
  };

  return es;
}


// ──── API 对象（按业务域分组）────────

const API = {
  // 健康
  health: () => request('GET', '/api/health'),
  listProviders: () => request('GET', '/api/providers'),
  chatProviders: () => request('GET', '/api/chat/providers'),

  // 会话服务端持久化
  listConversations: () => request('GET', '/api/chat/conversations'),
  getConversation: (cid) => request('GET', `/api/chat/conversations/${cid}`),
  upsertConversation: (data) => request('POST', '/api/chat/conversations', data),
  deleteConversation: (cid) => request('DELETE', `/api/chat/conversations/${cid}`),

  // 项目
  projects: {
    list: (limit = 100) => request('GET', `/api/projects?limit=${limit}`),
    get: (id) => request('GET', `/api/projects/${id}`),
    create: (data) => request('POST', '/api/projects', data),
    update: (id, data) => request('PATCH', `/api/projects/${id}`, data),
    delete: (id) => request('DELETE', `/api/projects/${id}`),
    /**
     * 分镜"生成条件"一致性体检。
     *
     * 为什么要这个：多个分镜拼起来"一眼AI"最大的原因不是转场，而是
     * **各镜用了不同模型** —— 画风/色彩科学不同，等于两个片子。
     * 线上实测 9 个项目里 5 个中招。这个接口把"是不是同一个世界"摊开。
     */
    consistency: (id) => request('GET', `/api/projects/${id}/consistency`),
    /**
     * 统一各分镜的生成条件。
     * ⚠ 默认 `dryRun=true` **只预演不落库** —— 改模型会改变出片效果、
     *   还可能要求重新生成（花钱），必须用户点头。
     */
    unifyConsistency: (id, dryRun = true) =>
      request('POST', `/api/projects/${id}/consistency/unify`,
              { dry_run: !!dryRun }, { timeout: 60000 }),
    /**
     * 按镜头顺序用真实模型重新生成，并启用**尾帧串联**。
     *
     * 为什么需要它：尾帧串联（上一镜的尾帧当这一镜的首帧）是让多镜头
     * 看起来像一镜到底最直接的一招，而它**只能靠重新生成**实现。
     * 顺序是前提 —— 上一镜先有视频，尾帧才存在，所以不能并发。
     *
     * ⚠ **这会花钱**：每镜一次真实模型调用。所以
     *   `dryRun` 默认 true，只预演"会调用几次、哪几镜会串"，不生成任何内容。
     */
    regenChained: (id, dryRun = true, opts = {}) =>
      request('POST', `/api/projects/${id}/regen-chained`,
              { dry_run: !!dryRun, only_missing: !!opts.onlyMissing },
              { timeout: 120000 }),
    regenChainedStatus: (id) =>
      request('GET', `/api/projects/${id}/regen-chained/status`, null,
              { timeout: 30000 }),
  },

  // 剧本
  scripts: {
    get: (projectId) => request('GET', `/api/projects/${projectId}/script`),
    /** 剧本体检（按用户提供的成品剧本量出来的写法检查） */
    audit: (projectId) => request('GET', `/api/projects/${projectId}/script/audit`),
    generate: (projectId, data, opts = {}) =>
      request('POST', `/api/projects/${projectId}/script/generate`, data, { timeout: 300000, ...opts }),
    generateSceneDetail: (projectId, sceneNumber) =>
      // ⚠ 这里会调用 LLM 生成整场细节，30~90 秒是常态。
      //   之前用的是默认 20 秒超时 → 必然报「请求超时（20s）」，功能等于不可用。
      request('POST', `/api/projects/${projectId}/script/scenes/${sceneNumber}/generate-detail`,
              null, { timeout: 600000 }),
    /** 批量生成全场次细节（一次请求，避免逐场点） */
    generateAllSceneDetails: (projectId) =>
      request('POST', `/api/projects/${projectId}/script/scene-details/generate-all`,
              null, { timeout: 1800000 }),
    listSceneDetails: (projectId) =>
      request('GET', `/api/projects/${projectId}/script/scene-details`),
    getSceneDetail: (projectId, sceneNumber) =>
      request('GET', `/api/projects/${projectId}/script/scenes/${sceneNumber}/detail`),
  },

  // 角色
  characters: {
    list: (projectId) => request('GET', `/api/projects/${projectId}/characters`),
    create: (data) => request('POST', '/api/characters', data),
    update: (id, data) => request('PATCH', `/api/characters/${id}`, data),
    delete: (id) => request('DELETE', `/api/characters/${id}`),
    uploadReference: (id, file, onProgress) =>
      uploadFile(`/api/characters/${id}/upload-reference`, file, onProgress),
    // force=true：用 AI 新设计的设定覆盖占位/空内容（用户手写的不覆盖）
    // 角色提取要跑两次 LLM（先定美术基调，再设计每个角色），600s 才够
    fromScript: (projectId, force) =>
      request('POST', `/api/projects/${projectId}/characters/from-script?force=${force ? 1 : 0}`,
              null, { timeout: 600000 }),
    importFromLibrary: (projectId, ids) =>
      request('POST', `/api/projects/${projectId}/characters/import`, { ids }, { timeout: 60000 }),
  },

  // 场景
  scenes: {
    list: (projectId) => request('GET', `/api/projects/${projectId}/scenes`),
    create: (data) => request('POST', '/api/scenes', data),
    update: (id, data) => request('PATCH', `/api/scenes/${id}`, data),
    delete: (id) => request('DELETE', `/api/scenes/${id}`),
    fromScript: (projectId, force) =>
      request('POST', `/api/projects/${projectId}/scenes/from-script?force=${force ? 1 : 0}`,
              null, { timeout: 600000 }),
    importFromLibrary: (projectId, ids) =>
      request('POST', `/api/projects/${projectId}/scenes/import`, { ids }, { timeout: 60000 }),
    /**
     * 上传场景素材（图片或视频）。
     *
     * ★ 它原来被放在 `images` 组里，而 `app.js` 是按 `API.scenes.uploadSceneReference`
     *   调的 —— 于是那一步**一点就炸**：`API.scenes.uploadSceneReference is not a function`。
     *   裸 JS 不会在加载时报这种错，只有用户点到才炸。
     *   它的接口路径是 `/api/scenes/{sid}/...`，本来就该在 `scenes` 组里。
     */
    uploadSceneReference: (sid, file) =>
      uploadFile(`/api/scenes/${sid}/upload-reference`, file),
  },

  // 图像生成（角色形象 / 场景概念图）
  images: {
    providers: () => request('GET', '/api/image/providers'),
    test: (payload) => request('POST', '/api/image/test', payload, { timeout: 180000 }),
    generateCharacter: (projectId, cid, data) =>
      request('POST', `/api/projects/${projectId}/characters/${cid}/generate-image`, data, { timeout: 300000 }),
    generateScene: (projectId, sid, data) =>
      request('POST', `/api/projects/${projectId}/scenes/${sid}/generate-image`, data, { timeout: 300000 }),
    /** 给场景生成环境空镜视频（走视频 API；无 Key/失败自动回退本地运镜） */
    generateSceneVideo: (projectId, sid, data) =>
      request('POST', `/api/projects/${projectId}/scenes/${sid}/generate-video`, data, { timeout: 900000 }),
    /** 本地图片走 HTTP 服务（WebView 里 file:// 常被安全策略拦截，直接开 file:// 会白图） */
    localUrl: (p) => (p ? `/api/local-image?path=${encodeURIComponent(p)}` : ''),
    /** 统一入口：给角色或场景上传参考图（返回落盘路径） */
    uploadReference: (kind, id, file, onProgress) =>
      uploadFile(kind === 'character'
        ? `/api/characters/${id}/upload-reference`
        : `/api/scenes/${id}/upload-reference`, file, onProgress),
    /** ★ 真·后期再修改：原图 + 一句话 → 改过的图（指令式图像编辑，不是重画） */
    refine: (projectId, kind, id, payload) =>
      request('POST',
              `/api/projects/${projectId}/${kind === 'character' ? 'characters' : 'scenes'}/${id}/refine-image`,
              payload, { timeout: 600000 }),
    /** ★ 视频参考：上传视频 → 抽出有代表性的画面供挑选 */
    uploadVideoRef: (projectId, kind, id, file) =>
      uploadFile(`/api/projects/${projectId}/${kind === 'character' ? 'characters' : 'scenes'}/${id}/video-reference`,
                 file),
    /** 把挑中的画面（或按时间点现取的一帧）设为形象基准 */
    useFrame: (projectId, kind, id, payload) =>
      request('POST',
              `/api/projects/${projectId}/${kind === 'character' ? 'characters' : 'scenes'}/${id}/use-frame`,
              payload, { timeout: 180000 }),
  },

  // 🧱 可逐步确认的后期流水线
  pipeline: {
    status: (projectId) => request('GET', `/api/projects/${projectId}/pipeline`),
    run: (projectId, step, params) =>
      request('POST', `/api/projects/${projectId}/pipeline/run`, { step, params }, { timeout: 60000 }),
    stepStatus: (projectId) => request('GET', `/api/projects/${projectId}/pipeline/status`),
    skip: (projectId, step, reason) =>
      request('POST', `/api/projects/${projectId}/pipeline/skip`,
              { step, reason }, { timeout: 30000 }),
    unskip: (projectId, step) =>
      request('POST', `/api/projects/${projectId}/pipeline/skip`,
              { step, undo: true }, { timeout: 30000 }),
    setBgm: (projectId, file) => uploadFile(`/api/projects/${projectId}/pipeline/bgm`, file),
    finalize: (projectId) =>
      request('POST', `/api/projects/${projectId}/pipeline/finalize`, {}, { timeout: 120000 }),
    reset: (projectId) =>
      request('POST', `/api/projects/${projectId}/pipeline/reset`, {}, { timeout: 30000 }),
  },

  // 🎬 一键成片（不依赖付费视频模型 Key 的主路径）
  compose: {
    start: (projectId, data) =>
      request('POST', `/api/projects/${projectId}/compose`, data, { timeout: 60000 }),
    status: (projectId) =>
      request('GET', `/api/projects/${projectId}/compose/status`),
    cancel: (projectId) =>
      request('POST', `/api/projects/${projectId}/compose/cancel`, {}, { timeout: 20000 }),
    finalInfo: (projectId) =>
      request('GET', `/api/projects/${projectId}/final/info`),
    reveal: (projectId) =>
      request('POST', `/api/projects/${projectId}/reveal-output`, {}, { timeout: 40000 }),
    /** 成片下载直链（带 attachment，避免 WebView 只播放不下载） */
    finalDownloadUrl: (projectId) => `/api/projects/${projectId}/final?download=1`,
    /** 成片播放/下载直链 */
    finalUrl: (projectId) => `/api/projects/${projectId}/final`,
  },

  // 资产库（跨剧本复用）
  library: {
    characters: (excludeProject) =>
      request('GET', `/api/library/characters${excludeProject ? '?exclude_project=' + encodeURIComponent(excludeProject) : ''}`),
    scenes: (excludeProject) =>
      request('GET', `/api/library/scenes${excludeProject ? '?exclude_project=' + encodeURIComponent(excludeProject) : ''}`),
  },

  // 道具
  props: {
    list: (projectId) => request('GET', `/api/projects/${projectId}/props`),
    create: (data) => request('POST', '/api/props', data),
    update: (id, data) => request('PATCH', `/api/props/${id}`, data),
    delete: (id) => request('DELETE', `/api/props/${id}`),
  },

  // 分镜
  shots: {
    list: (projectId) => request('GET', `/api/projects/${projectId}/shots`),
    create: (data) => request('POST', '/api/shots', data),
    get: (id) => request('GET', `/api/shots/${id}`),
    update: (id, data) => request('PATCH', `/api/shots/${id}`, data),
    delete: (id) => request('DELETE', `/api/shots/${id}`),
    reorder: (projectId, orderedIds) =>
      request('POST', `/api/projects/${projectId}/shots/reorder`, orderedIds),
    generate: (id, data) => request('POST', `/api/shots/${id}/generate`, data, { timeout: 60000 }),
    /** 本地出片（不需要付费视频模型 Key） */
    render: (id, data) => request('POST', `/api/shots/${id}/render`, data, { timeout: 60000 }),
    renderStatus: (id) => request('GET', `/api/shots/${id}/render/status`),
    /**
     * 拍摄计划：**花钱之前**先算清楚会怎么拍。
     *
     * 为什么要这个接口：用户的核心抱怨是"分层定 10s 但最终只能生成 5s，
     * 画面还跟描述不相干"。根因是分镜写的时长和模型真实能力对不上，
     * 而这个对不上的过程以前是**黑盒** —— 用户只看到结果不对，不知道为什么。
     * 这个接口把"你写了 12 秒 / 台词真实只要 6.1 秒 / 所以按 10 秒一次拍完"
     * 直接摊开，并且 measure=1 会真的量一遍配音，保证和正式生成完全一致。
     * 超时给足：第一次要合成台词量时长（之后走缓存）。
     */
    plan: (projectId, id, opts = {}) => {
      const q = new URLSearchParams();
      if (opts.resolution) q.set('resolution', opts.resolution);
      if (opts.duration) q.set('duration', String(opts.duration));
      q.set('measure', opts.measure === false ? '0' : '1');
      return request('GET', `/api/projects/${projectId}/shots/${id}/plan?${q}`,
                     null, { timeout: 180000 });
    },
    /**
     * 提示词体检：**花钱之前**把这条视频提示词按"视频提示词契约"过一遍机器审查。
     *
     * 为什么要这个接口：用户的抱怨是"有的视频太扯了"。过去提示词里同时写了两个
     * 镜头运动、要求的动作根本塞不进这一镜的时长、只有抽象形容没有主体、
     * 对白被写成画面可见的文字……这些在**提交给付费模型之前**就能查出来，
     * 但没有任何地方告诉用户。这个接口把 errors / warnings / 分数摊开，
     * 每条都带"改哪一句"的说明。
     *
     * 注意：`/render` 在**要花钱**的模式下会自己拦一次（有 error 直接 400），
     * 想强行生成得显式传 force。这个接口是给用户**提前看**用的。
     */
    promptAudit: async (id) => {
      const d = await request('GET', `/api/shots/${id}/prompt-audit`, null, { timeout: 60000 });
      // 后端把体检结果包在 `audit` 里，顺手摊平并**带上原始提示词** ——
      // 调用方要知道"哪里不合格"，也常常要顺手把提示词显示/复制出来。
      const au = (d && d.audit) || {};
      return Object.assign({}, au, {
        prompt: (d && d.prompt) || '',
        negative_prompt: (d && d.negative_prompt) || '',
        camera_moves_seen: d && d.camera_moves_seen,
        camera_conflict: d && d.camera_conflict,
        has_action: d && d.has_action,
      });
    },
    renderAll: (projectId, data) =>
      request('POST', `/api/projects/${projectId}/render-all`, data, { timeout: 60000 }),
    /** 给已有分镜补角色/场景关联（不重建分镜、不丢已生成的视频） */
    relink: (projectId) =>
      request('POST', `/api/projects/${projectId}/shots/relink`, {}, { timeout: 120000 }),
    /** 分镜关联体检（悬空角色 ID / 文字提到但未关联 → 模型会自己编一个人） */
    refCheck: (projectId) =>
      request('GET', `/api/projects/${projectId}/shots/ref-check`),
    renderAllStatus: (projectId) =>
      request('GET', `/api/projects/${projectId}/render-all/status`),
    /**
     * 联络表：把该项目**已选中的候选片段**逐镜抽帧拼成一张图。
     *
     * 为什么要有它：有些问题**自动判据抓不住**。实测（2026-09-13）有一个镜头
     * 末尾约 1.7 秒整体倒过来、还有两个项目的"5 个镜头"其实是同一张图
     * （只是换了压在上面的文字）。试过三种自动判据想认倒置，全都不合格
     * （真正倒置的漏报、正常镜头反而误报），所以产品给的是
     * **"一眼看全"的入口**，而不是一个不可靠的红灯。
     */
    /**
     * 一键配音：整片选角（按角色卡打分）+ 逐句韵律 + 把音轨挂进每个镜头。
     * 不重新生成画面 → 对已有素材也有效，零视频成本。
     */
    dubAll: (projectId, opts = {}) =>
      request('POST', `/api/projects/${projectId}/voice/dub`,
              { select: opts.select !== false }, { timeout: 1800000 }),
    framesheet: (projectId, opts = {}) => {
      const q = new URLSearchParams();
      if (opts.per) q.set('per', String(opts.per));
      if (opts.cols) q.set('cols', String(opts.cols));
      const qs = q.toString();
      return request('GET',
                     `/api/projects/${projectId}/framesheet${qs ? '?' + qs : ''}`,
                     null, { timeout: 300000 });
    },
    selectCandidate: (id, candidateId) =>
      request('POST', `/api/shots/${id}/select-candidate?candidate_id=${encodeURIComponent(candidateId)}`, {},
              { timeout: 20000 }),
    /** 本地媒体（视频/音频/图片）统一走 HTTP，WebView 里 file:// 会被拦 */
    mediaUrl: (p) => (p ? `/api/media?path=${encodeURIComponent(p)}` : ''),
    /** 下载直链：带 download=1 让后端返回 attachment，否则 WebView2 只会播放不落地 */
    mediaDownloadUrl: (p) => (p ? `/api/media?download=1&path=${encodeURIComponent(p)}` : ''),
  },

  // 剧本导入（文档 / 粘贴）
  scriptImport: {
    file: (projectId, file, { structurize = true, title = '' } = {}) => {
      const fd = new FormData();
      fd.append('file', file);
      fd.append('structurize', String(structurize));
      fd.append('title', title);
      return fetch(`${API_BASE}/api/projects/${projectId}/script/import`, {
        method: 'POST', body: fd,
      }).then(async (r) => {
        const j = await r.json();
        if (!r.ok || (j && j.code && j.code !== '000000')) {
          throw new Error((j && (j.message || j.detail)) || `HTTP ${r.status}`);
        }
        return j.data;
      });
    },
    paste: (projectId, text, structurize = true, title = '') =>
      request('POST', `/api/projects/${projectId}/script/paste`,
              { text, structurize, title }, { timeout: 600000 }),
  },

  // 视频模型连通性 / 默认模型
  videoModels: {
    test: (data) => request('POST', '/api/video-models/test', data || {}, { timeout: 180000 }),
    configured: () => request('GET', '/api/video-models/configured'),
    /** 模型能力目录（清晰度/时长/适用场景说明；kind=video|llm） */
    catalog: (kind, provider) =>
      request('GET', `/api/model-catalog?kind=${kind || 'video'}${provider ? '&provider=' + encodeURIComponent(provider) : ''}`,
              null, { timeout: 60000 }),
    /** 给定想要的清晰度/时长，取一组一定合法的参数 */
    resolveParams: (provider, model, resolution, duration) =>
      request('GET', '/api/model-catalog/resolve'
        + `?provider=${encodeURIComponent(provider)}&model=${encodeURIComponent(model)}`
        + (resolution ? `&resolution=${encodeURIComponent(resolution)}` : '')
        + (duration ? `&duration=${duration}` : ''), null, { timeout: 30000 }),
    /** 各厂商已知模型版本（换模型弹窗用） */
    known: (provider) =>
      request('GET', `/api/video-models/known${provider ? '?provider=' + encodeURIComponent(provider) : ''}`,
              null, { timeout: 60000 }),
    /** 拉取厂商账号下真实可用的模型清单（解决"开通了却用不了"） */
    discover: (provider) =>
      request('GET', `/api/video-models/discover?provider=${encodeURIComponent(provider || 'seedance')}`,
              null, { timeout: 90000 }),
  },

  // 输出目录 / 系统对话框
  system: {    setOutputDir: (path) => request('POST', '/api/settings/output-dir', { path }, { timeout: 30000 }),
    pickFolder: (initial) => request('POST', '/api/dialog/pick-folder', { initial }, { timeout: 200000 }),
  },

  // 任务
  tasks: {
    get: (id) => request('GET', `/api/tasks/${id}`),
    list: (status = null) => {
      const q = status ? `?status=${status}` : '';
      return request('GET', `/api/tasks${q}`);
    },
  },

  // 版权抽象化
  abstract: (projectId, sourceDescription) =>
    request('POST', `/api/projects/${projectId}/abstract`, { source_description: sourceDescription }),

  // 设置
  settings: {
    get: () => request('GET', '/api/settings'),
    update: (data) => request('PUT', '/api/settings', data),
  },

  // 后期
  postprocess: {
    stitch: (projectId, shotIds, outputFilename, addSubtitles) =>
      request('POST', `/api/projects/${projectId}/stitch?output_filename=${outputFilename}&add_subtitles=${addSubtitles}`, shotIds),
    /** 产出文件列表（成片 / 分镜片段 / 导出件） */
    outputs: (projectId) => request('GET', `/api/projects/${projectId}/outputs`),
    exportAspectOf: (projectId, sourcePath, targetAspect) =>
      request('POST', `/api/projects/${projectId}/outputs/export-aspect`,
              { source_path: sourcePath, target_aspect: targetAspect }, { timeout: 600000 }),
    deleteOutput: (projectId, sourcePath) =>
      request('POST', `/api/projects/${projectId}/outputs/delete`, { source_path: sourcePath }),
    /** BGM 混流（支持淡入淡出） */
    mixBgmFile: (projectId, sourcePath, file, volume, fadeIn, fadeOut) => {
      const fd = new FormData();
      fd.append('source_path', sourcePath);
      fd.append('bgm_volume', String(volume));
      fd.append('fade_in', String(fadeIn));
      fd.append('fade_out', String(fadeOut));
      fd.append('file', file);
      return fetch(`${API_BASE}/api/projects/${projectId}/outputs/mix-bgm`, {
        method: 'POST', body: fd,
      }).then(async (r) => {
        const j = await r.json();
        if (!r.ok || (j && j.code && j.code !== '000000')) {
          throw new Error((j && (j.message || j.detail)) || `HTTP ${r.status}`);
        }
        return j.data;
      });
    },
    exportAspect: (projectId, sourceFilename, targetAspect) =>
      request('POST', `/api/projects/${projectId}/export-aspect?source_filename=${sourceFilename}&target_aspect=${targetAspect}`),
    addBgm: (projectId, sourceFilename, bgmPath, bgmVolume) => {
      const fd = new FormData();
      fd.append('source_filename', sourceFilename);
      fd.append('bgm_path', bgmPath);
      fd.append('bgm_volume', String(bgmVolume));
      return fetch(`${API_BASE}/api/projects/${projectId}/add-bgm`, {
        method: 'POST',
        body: fd,
      }).then(r => r.json());
    },
    generateSubtitles: (projectId, outputFilename) =>
      request('POST', `/api/projects/${projectId}/generate-subtitles?output_filename=${outputFilename}`),

    // 配音（借鉴 MPT voice.py dispatcher）
    addVoice: (projectId, sourceFilename, voiceId, text, voiceRate = 1.0) => {
      const fd = new FormData();
      fd.append('source_filename', sourceFilename);
      fd.append('voice_id', voiceId);
      fd.append('text', text || '');
      fd.append('voice_rate', String(voiceRate));
      return fetch(`${API_BASE}/api/projects/${projectId}/add-voice`, {
        method: 'POST',
        body: fd,
      }).then(r => r.json());
    },
  },

  // TTS（独立路由，试听 / 单独合成）
  voice: {
    providers: () => request('GET', '/api/voice/providers'),
    voices: () => request('GET', '/api/voice/voices'),
    /**
     * 配音**模型**清单（各引擎自己声明的真实型号）。
     * 用户要求"真正的去选择配音模型" —— 型号由后端声明，前端只渲染，不抄一份。
     */
    ttsModels: () => request('GET', '/api/tts/models'),
    synthesize: (payload) => request('POST', '/api/voice/synthesize', payload, { timeout: 120000 }),
    test: (provider, voiceId, text) =>
      request('POST', '/api/voice/test', { provider, voice_id: voiceId, text },
              { timeout: 120000 }),
    /** 试听用的直链（给 <audio src> 用，避免把二进制转成 blob URL 的额外步骤） */
    previewUrl: (voiceId, text) =>
      `${API_BASE}/api/voice/preview?voice_id=${encodeURIComponent(voiceId)}` +
      `&text=${encodeURIComponent(text || '你好，这是音色试听。')}`,
    // 试听返回的是音频二进制，不是 JSON
    preview: (voiceId, text) =>      fetch(`${API_BASE}/api/voice/preview`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ voice_id: voiceId, text }),
      }).then(r => {
        if (!r.ok) return r.json().then(err => { throw new Error(err.message || err.detail || '试听失败'); });
        return r.blob();
      }),
    /** 给已有成片重新配音（替换原音轨）→ 产出 *_voice.mp4 */
    redoVoice: (projectId, sourcePath, voiceId, text, rate) =>
      request('POST', `/api/projects/${projectId}/outputs/add-voice`,
              { source_path: sourcePath, voice_id: voiceId, text, rate }, { timeout: 300000 }),
  },

  // 文件
  fileUrl: (projectId, relativePath) => {
    return `${API_BASE}/api/files/${projectId}/${relativePath}`;
  },
};


// ──── Toast 通知 ────────────

const Toast = {
  show(message, type = 'info', duration = 4000) {
    const container = document.getElementById('toastContainer');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.innerHTML = `<div class="toast-title">${message}</div>`;
    container.appendChild(toast);
    setTimeout(() => {
      toast.style.animation = 'fadeIn 200ms ease reverse';
      setTimeout(() => toast.remove(), 200);
    }, duration);
  },

  success(msg, dur) { this.show(msg, 'success', dur); },
  error(msg, dur) { this.show(msg, 'error', dur); },
  warn(msg, dur) { this.show(msg, 'warn', dur); },
  info(msg, dur) { this.show(msg, 'info', dur); },
};


// ──── 进度遮罩 ────────────

const Progress = {
  currentTask: null,

  show(title = '正在处理...') {
    document.getElementById('progressTitle').textContent = title;
    document.getElementById('progressFill').style.width = '0%';
    document.getElementById('progressMessage').textContent = '';
    document.getElementById('progressOverlay').hidden = false;
  },

  update(progress, message = '') {
    document.getElementById('progressFill').style.width = `${Math.min(100, progress * 100)}%`;
    if (message) document.getElementById('progressMessage').textContent = message;
  },

  hide() {
    document.getElementById('progressOverlay').hidden = true;
    if (this.currentTask) {
      this.currentTask.close();
      this.currentTask = null;
    }
  },

  watchTask(taskId, title = '处理中...') {
    this.show(title);
    this.currentTask = subscribeTask(taskId, (data) => {
      this.update(data.progress, data.status === 'completed' ? '完成' : data.status);
      if (data.status === 'completed' || data.status === 'failed') {
        setTimeout(() => this.hide(), 500);
      }
    });
  },
};


// 导出到全局
window.API = API;
window.APIError = APIError;
window.Toast = Toast;
window.Progress = Progress;

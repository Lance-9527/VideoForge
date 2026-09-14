/**
 * VideoForge · 对话模块
 * 与 AI Agent 流式对话
 */

const Chat = {
  messages: [],         // 当前会话消息
  conversations: [],    // 会话列表（持久化）
  currentConvId: null,
  llmProviders: [],
  currentProvider: 'deepseek',
  currentModel: 'deepseek-chat',
  currentApiKey: '',
  streaming: false,
  eventSource: null,

  async init() {
    // 加载 LLM providers
    try {
      const data = await API.chatProviders();
      this.llmProviders = data.providers || [];
    } catch (e) {
      console.error('Load LLM providers failed:', e);
    }

    // 加载设置（默认 provider/model/api key）
    try {
      const settings = await API.settings.get();
      this.currentProvider = settings.llm_provider || settings.remote_llm_provider || 'deepseek';
      this.currentModel = settings.llm_model || '';
      // 找默认 provider 的 api key
      const llmKeys = settings.llm_api_keys || {};
      this.currentApiKey = llmKeys[this.currentProvider] || '';
      this.updateLlmPill();
    } catch (e) {
      console.error('Load settings failed:', e);
    }

    // 加载会话列表
    this.loadConversations();
  },

  populateProviders() {
    // 不再需要 - 由设置面板替代
  },

  updateLlmPill() {
    // 顶部已整合为单一 ai-pill，由 app.js 的 updateModelPill() 负责渲染。
    // 这里只做安全更新（元素可能不存在），并触发 app 的全局刷新。
    try {
      if (typeof window.updateModelPill === 'function') {
        window.updateModelPill();
      }
    } catch (e) {
      // ignore
    }
  },

  loadConversations() {
    // 优先从服务端加载；失败则回退 localStorage
    API.listConversations()
      .then(d => {
        if (d && d.conversations && d.conversations.length) {
          this.conversations = d.conversations;
          this.renderConversations();
        } else {
          this.loadConversationsFromLocal();
        }
      })
      .catch(() => this.loadConversationsFromLocal());
  },

  loadConversationsFromLocal() {
    try {
      const stored = localStorage.getItem('vf_conversations');
      if (stored) {
        this.conversations = JSON.parse(stored);
        this.renderConversations();
      }
    } catch (e) {
      console.error('Load conversations failed:', e);
    }
  },

  saveConversations() {
    try {
      localStorage.setItem('vf_conversations', JSON.stringify(this.conversations));
    } catch (e) {
      console.error('Save conversations failed:', e);
    }
  },

  // 同步当前会话到服务端（后台静默，不阻塞 UI）
  syncToServer(conv) {
    if (!conv || !conv.id) return;
    API.upsertConversation({
      id: conv.id,
      title: conv.title || '新会话',
      messages: conv.messages || [],
    }).catch(e => console.warn('sync to server failed:', e));
  },

  deleteConvOnServer(cid) {
    API.deleteConversation(cid).catch(() => {});
  },

  renderConversations() {
    const list = document.getElementById('chatList');
    if (!list) return;
    if (!this.conversations.length) {
      list.innerHTML = '<div class="dim" style="padding: 8px 12px; font-size: 12px;">暂无会话</div>';
      return;
    }
    // 历史会话补标题：早期版本因为判断写错，所有会话都叫「新会话」，
    // 这里用第一条用户消息回填，让列表能区分开。
    let dirty = false;
    for (const c of this.conversations) {
      if (!c.title || c.title === '新会话') {
        const firstUser = (c.messages || []).find(m => m.role === 'user');
        const t = String((firstUser || {}).content || '').replace(/\s+/g, ' ').trim().slice(0, 18);
        if (t) { c.title = t; dirty = true; }
      }
    }
    if (dirty) {
      this.saveConversations();
      this.conversations.filter(c => c.title && c.title !== '新会话')
        .forEach(c => this.syncToServer(c));
    }
    list.innerHTML = this.conversations.slice(0, 20).map(c => `
      <div class="project-list-item ${c.id === this.currentConvId ? 'active' : ''}" data-cid="${c.id}"
           title="${escapeHtml(c.title || '新会话')}">
        <div class="conv-title">${escapeHtml(c.title || '新会话')}</div>
        <div class="project-status">${(c.messages || []).length} 条消息</div>
      </div>
    `).join('');
    list.querySelectorAll('[data-cid]').forEach(el => {
      el.addEventListener('click', () => this.loadConversation(el.dataset.cid));
    });
  },

  newConversation() {
    this.currentConvId = 'c_' + Date.now();
    this.messages = [];
    document.getElementById('chatMessages').innerHTML = '';
    document.getElementById('chatWelcome').style.display = '';
    this.conversations.unshift({
      id: this.currentConvId,
      title: '新会话',
      messages: [],
      created_at: new Date().toISOString(),
    });
    this.saveConversations();
    this.renderConversations();
  },

  loadConversation(cid) {
    // 立即可见的反馈：先切到对话页 + 高亮 + 本地缓存渲染（避免"点击没反应"）
    const applyLocal = () => {
      const conv = this.conversations.find(c => c.id === cid);
      this.currentConvId = cid;
      this.messages = (conv && conv.messages) || [];
      this.renderMessages();
      this.renderConversations();
    };

    // 1) 先切页（无论后端快慢，用户立刻看到跳转）
    if (typeof window.navigateTo === 'function') window.navigateTo('chat');
    this.currentConvId = cid;
    this.renderConversations();          // 立刻高亮选中项
    applyLocal();                        // 立刻用本地缓存渲染

    // 2) 再从服务端拉取最新（成功则覆盖，失败保留本地渲染，并提示）
    //    注意：API.request() 已解包 data，这里拿到的就是会话对象本身
    API.getConversation(cid)
      .then(d => {
        if (!d) { return; }              // 服务端无此会话：保留本地渲染
        const conv = this.conversations.find(c => c.id === cid);
        if (conv) {
          conv.messages = d.messages || conv.messages || [];
          conv.title = d.title || conv.title;
        }
        this.messages = d.messages || this.messages || [];
        this.renderMessages();
        this.renderConversations();
      })
      .catch(e => {
        // 网络/超时/业务错误：本地渲染已生效，仅提示，不阻塞界面
        console.warn('load conversation from server failed:', e);
        if (window.Toast) Toast.warn('会话已加载（本地缓存）；服务端同步失败：' + (e.message || e));
      });
  },

  renderMessages() {
    const container = document.getElementById('chatMessages');
    if (!this.messages.length) {
      container.innerHTML = '';
      document.getElementById('chatWelcome').style.display = '';
      return;
    }
    document.getElementById('chatWelcome').style.display = 'none';
    container.innerHTML = this.messages.map((m, i) => this.renderMessage(m, i)).join('');
    this.scrollToBottom();
    // 重新绑定代码块复制按钮
    container.querySelectorAll('[data-copy-idx]').forEach(btn => {
      btn.addEventListener('click', () => {
        const idx = parseInt(btn.dataset.copyIdx);
        const code = this.messages[idx].content;
        navigator.clipboard.writeText(code);
        Toast.success('已复制');
      });
    });
  },

  renderMessage(m, idx) {
    const isUser = m.role === 'user';
    const avatar = isUser ? '👤' : '🎬';
    const time = new Date(m.timestamp || Date.now()).toLocaleTimeString('zh-CN', {
      hour: '2-digit', minute: '2-digit',
    });

    // 简单 markdown：处理代码块、列表
    let content = escapeHtml(m.content);
    // 代码块 ```...```
    content = content.replace(/```(\w*)\n?([\s\S]*?)```/g, (m, lang, code) => {
      return `<div class="msg-code-wrapper"><button class="msg-code-copy" data-copy-idx="${idx}">复制</button><pre class="msg-code">${escapeHtml(code)}</pre></div>`;
    });
    // 行内代码 `...`
    content = content.replace(/`([^`\n]+)`/g, '<code class="msg-inline-code">$1</code>');
    // 粗体 **...**
    content = content.replace(/\*\*([^\*]+)\*\*/g, '<strong>$1</strong>');
    // 列表 - ...
    content = content.replace(/^- (.+)$/gm, '<li>$1</li>');
    content = content.replace(/(<li>.*<\/li>\n?)+/g, '<ul>$&</ul>');
    // 换行
    content = content.replace(/\n/g, '<br>');

    return `
      <div class="msg ${isUser ? 'msg-user' : 'msg-assistant'}">
        <div class="msg-avatar">${avatar}</div>
        <div class="msg-body">
          <div class="msg-header">
            <span class="msg-name">${isUser ? '你' : 'VideoForge AI'}</span>
            <span class="msg-model">${m.model || ''}</span>
            <span class="msg-time">${time}</span>
          </div>
          <div class="msg-content">${content}</div>
        </div>
      </div>
    `;
  },

  async sendMessage(content) {
    if (!content.trim() || this.streaming) return;

    // 首次消息时新建会话
    if (!this.currentConvId) {
      this.newConversation();
    }

    // 关闭欢迎区
    document.getElementById('chatWelcome').style.display = 'none';

    // 添加用户消息
    const userMsg = {
      role: 'user',
      content: content.trim(),
      timestamp: new Date().toISOString(),
    };
    this.messages.push(userMsg);
    this.appendMessage(userMsg);

    // 准备 AI 消息占位
    const aiMsg = {
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString(),
      streaming: true,
    };
    this.messages.push(aiMsg);
    const aiEl = this.appendMessage(aiMsg);

    // 自动生成会话标题（用第一条用户消息）。
    // ⚠ 旧判断是 `messages.length === 1`，但此处数组里已经有「用户 + AI」两条
    //   → 条件永远不成立 → 所有会话都叫「新会话」，根本分不清。
    const conv = this.conversations.find(c => c.id === this.currentConvId);
    if (conv && (!conv.title || conv.title === '新会话' || conv.title.trim() === '')) {
      const firstUser = (this.messages.find(m => m.role === 'user') || {}).content || content || '';
      const t = String(firstUser).replace(/\s+/g, ' ').trim().slice(0, 18);
      if (t) {
        conv.title = t;
        this.saveConversations();
        this.renderConversations();
      }
    }

    this.streaming = true;
    const sendBtn = document.getElementById('sendChatBtn');
    sendBtn.disabled = true;

    try {
      const messages = this.messages
        .filter(m => !m.streaming)
        .map(m => ({ role: m.role, content: m.content }));

      const body = {
        provider: this.currentProvider,
        model: this.currentModel,
        api_key: this.currentApiKey,
        messages,
        stream: true,
        temperature: 0.7,
      };

      const response = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });

      if (!response.ok) {
        const err = await response.json().catch(() => ({}));
        throw new Error(err.detail?.message || `HTTP ${response.status}`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        const lines = buffer.split('\n');
        buffer = lines.pop(); // 剩余的不完整行

        for (const line of lines) {
          if (!line.startsWith('data:')) continue;
          const payload = line.slice(5).trim();
          if (payload === '[DONE]') {
            aiMsg.streaming = false;
            this.streaming = false;
            sendBtn.disabled = false;
            this.saveCurrentConv();
            return;
          }
          try {
            const data = JSON.parse(payload);
            if (data.error) {
              throw new Error(data.error);
            }
            if (data.delta) {
              aiMsg.content += data.delta;
              this.updateAIMessage(aiEl, aiMsg.content);
              this.scrollToBottom();
            }
          } catch (e) {
            if (e.message && !e.message.includes('JSON')) throw e;
          }
        }
      }
    } catch (e) {
      aiMsg.content += `\n\n[错误] ${e.message}`;
      aiMsg.streaming = false;
      aiMsg.error = true;
      this.updateAIMessage(aiEl, aiMsg.content);
      Toast.error(e.message);
    } finally {
      this.streaming = false;
      sendBtn.disabled = false;
      this.saveCurrentConv();
    }
  },

  appendMessage(m) {
    const container = document.getElementById('chatMessages');
    const div = document.createElement('div');
    div.innerHTML = this.renderMessage(m, this.messages.indexOf(m));
    container.appendChild(div.firstElementChild);
    this.scrollToBottom();
    return container.lastElementChild;
  },

  updateAIMessage(el, content) {
    if (!el) return;
    const contentEl = el.querySelector('.msg-content');
    // 重新解析 markdown
    let html = escapeHtml(content);
    html = html.replace(/```(\w*)\n?([\s\S]*?)```/g, (m, lang, code) => {
      return `<div class="msg-code-wrapper"><button class="msg-code-copy">复制</button><pre class="msg-code">${escapeHtml(code)}</pre></div>`;
    });
    html = html.replace(/`([^`\n]+)`/g, '<code class="msg-inline-code">$1</code>');
    html = html.replace(/\*\*([^\*]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/- (.+)/g, '<li>$1</li>');
    html = html.replace(/(<li>.*<\/li>\n?)+/g, '<ul>$&</ul>');
    html = html.replace(/\n/g, '<br>');
    contentEl.innerHTML = html;

    // 重新绑定复制按钮
    contentEl.querySelectorAll('.msg-code-copy').forEach(btn => {
      btn.addEventListener('click', () => {
        navigator.clipboard.writeText(content);
        Toast.success('已复制');
      });
    });
  },

  scrollToBottom() {
    // 取消内层滚动后：整页滚动到底部（输入框 sticky 吸底）
    const area = document.querySelector('.content') || document.scrollingElement;
    try {
      window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
      if (area && area.scrollTo) area.scrollTo({ top: area.scrollHeight, behavior: 'smooth' });
    } catch (e) {
      window.scrollTo(0, document.body.scrollHeight);
    }
  },

  saveCurrentConv() {
    const conv = this.conversations.find(c => c.id === this.currentConvId);
    if (conv) {
      conv.messages = this.messages.filter(m => !m.streaming);
      conv.updated_at = new Date().toISOString();
    }
    this.saveConversations();
    this.syncToServer(conv);   // 同步到服务端 SQLite，跨会话/卸载持久
    this.renderConversations();
  },
};

// 全局函数：发送
window.sendChat = function() {
  const input = document.getElementById('chatInput');
  if (!input) return;
  const content = input.value;
  if (!content.trim()) {
    if (window.Toast) Toast.info('请输入内容后再发送');
    return;
  }
  if (Chat.streaming) {
    if (window.Toast) Toast.warn('上一条还在生成中，请稍候…');
    return;
  }
  Chat.sendMessage(content);
  input.value = '';
  input.style.height = 'auto';
  input.focus();
};

// 绑定回车发送（在 app.js 启动后调用）
window.bindChatInputKeys = function() {
  const input = document.getElementById('chatInput');
  if (!input || input.__keyBound) return;
  input.__keyBound = true;

  input.addEventListener('keydown', function(e) {
    // 注意：原生 DOM 事件没有 nativeEvent（那是 React 的属性），
    // 之前写成 e.nativeEvent.isComposing 会在每次按键时抛 TypeError，导致 Enter 完全失效。
    const composing = e.isComposing === true
      || e.keyCode === 229                       // 部分输入法在组合输入时置 229
      || (e.nativeEvent && e.nativeEvent.isComposing === true);

    if (e.key === 'Enter' && !e.shiftKey && !composing) {
      e.preventDefault();
      e.stopPropagation();
      try {
        window.sendChat();
      } catch (err) {
        console.error('[VideoForge] 发送失败:', err);
        if (window.Toast) Toast.error('发送失败：' + (err.message || err));
      }
    }
  });

  // 自动增高
  input.addEventListener('input', function() {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 200) + 'px';
  });
};

// 全局工具
function escapeHtml(s) {
  if (!s) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
window.escapeHtml = escapeHtml;

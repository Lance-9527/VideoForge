```javascript
/**
 * VideoForge Chat UI - 改造版
 * 支持新事件类型：text / progress / action_required / image / video / done
 * 向后兼容原有聊天功能
 */

class VideoChatUI {
    constructor(containerId = 'chat-container') {
        this.container = document.getElementById(containerId);
        this.messagesContainer = null;
        this.inputField = null;
        this.sendButton = null;
        this.sessionId = this.generateSessionId();
        this.projectId = null;
        this.currentMessageElement = null;
        this.activeEventSource = null;
        
        this.init();
    }

    generateSessionId() {
        return `session_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
    }

    init() {
        // 构建 UI 结构
        this.container.innerHTML = `
            <div class="video-chat-wrapper">
                <div class="chat-messages" id="chat-messages"></div>
                <div class="chat-input-area">
                    <textarea 
                        id="chat-input" 
                        placeholder="Describe your video idea..."
                        rows="2"
                    ></textarea>
                    <button id="chat-send" class="send-btn">
                        <svg width="20" height="20" viewBox="0 0 20 20" fill="currentColor">
                            <path d="M2 10l16-8-4 18-4-8-8-2z"/>
                        </svg>
                    </button>
                </div>
            </div>
        `;

        this.messagesContainer = document.getElementById('chat-messages');
        this.inputField = document.getElementById('chat-input');
        this.sendButton = document.getElementById('chat-send');

        // 事件监听
        this.sendButton.addEventListener('click', () => this.sendMessage());
        this.inputField.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                this.sendMessage();
            }
        });

        // 从 URL 获取 project_id（如果存在）
        const urlParams = new URLSearchParams(window.location.search);
        this.projectId = urlParams.get('project_id');

        this.addSystemMessage('👋 Hello! Describe your video idea and I\'ll help you create it.');
    }

    async sendMessage() {
        const message = this.inputField.value.trim();
        if (!message) return;

        // 显示用户消息
        this.addUserMessage(message);
        this.inputField.value = '';
        this.inputField.style.height = 'auto';

        // 创建 AI 消息容器
        this.currentMessageElement = this.createMessageElement('assistant');
        this.messagesContainer.appendChild(this.currentMessageElement);
        this.scrollToBottom();

        try {
            await this.streamChat(message);
        } catch (error) {
            this.appendToCurrentMessage(`\n❌ Error: ${error.message}`, 'error');
        }
    }

    async streamChat(message) {
        const payload = {
            message: message,
            session_id: this.sessionId,
            project_id: this.projectId
        };

        // 关闭之前的连接
        if (this.activeEventSource) {
            this.activeEventSource.close();
        }

        const response = await fetch('/api/chat/stream', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${this.getAuthToken()}`
            },
            body: JSON.stringify(payload)
        });

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';

            for (const line of lines) {
                if (line.startsWith('data: ')) {
                    try {
                        const eventData = JSON.parse(line.slice(6));
                        await this.handleEvent(eventData);
                    } catch (e) {
                        console.warn('Failed to parse SSE event:', e);
                    }
                } else if (line.startsWith('event: ')) {
                    // SSE event type (可选处理)
                }
            }
        }
    }

    async handleEvent(eventData) {
        const { type } = eventData;

        switch (type) {
            case 'text':
                this.handleTextEvent(eventData);
                break;
            case 'progress':
                this.handleProgressEvent(eventData);
                break;
            case 'action_required':
                this.handleActionRequiredEvent(eventData);
                break;
            case 'image':
                this.handleImageEvent(eventData);
                break;
            case 'video':
                this.handleVideoEvent(eventData);
                break;
            case 'done':
                this.handleDoneEvent();
                break;
            default:
                console.warn('Unknown event type:', type);
        }

        this.scrollToBottom();
    }

    handleTextEvent(data) {
        this.appendToCurrentMessage(data.content);
    }

    handleProgressEvent(data) {
        const { message, percent } = data;
        
        // 查找或创建进度条
        let progressBar = this.currentMessageElement.querySelector('.progress-bar');
        
        if (!progressBar) {
            const progressContainer = document.createElement('div');
            progressContainer.className = 'progress-container';
            progressContainer.innerHTML = `
                <div class="progress-label">${message}</div>
                <div class="progress-bar-wrapper">
                    <div class="progress-bar" style="width: ${percent}%"></div>
                </div>
                <div class="progress-percent">${percent}%</div>
            `;
            this.currentMessageElement.appendChild(progressContainer);
            progressBar = progressContainer.querySelector('.progress-bar');
        } else {
            // 更新现有进度条
            const container = progressBar.closest('.progress-container');
            container.querySelector('.progress-label').textContent = message;
            progressBar.style.width = `${percent}%`;
            container.querySelector('.progress-percent').textContent = `${percent}%`;
        }

        // 100% 时淡出
        if (percent >= 100) {
            setTimeout(() => {
                const container = progressBar.closest('.progress-container');
                if (container) {
                    container.style.opacity = '0';
                    setTimeout(() => container.remove(), 300);
                }
            }, 1000);
        }
    }

    handleActionRequiredEvent(data) {
        const { action_id, prompt, choices } = data;
        
        const actionContainer = document.createElement('div');
        actionContainer.className = 'action-required';
        actionContainer.innerHTML = `
            <div class="action-prompt">${prompt}</div>
            <div class="action-choices" data-action-id="${action_id}">
                ${choices.map(choice => `
                    <button class="action-choice-btn" data-choice-id="${choice.id}">
                        ${choice.label}
                    </button>
                `).join('')}
            </div>
        `;

        this.currentMessageElement.appendChild(actionContainer);

        // 绑定按钮事件
        actionContainer.querySelectorAll('.action-choice-btn').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                const choiceId = e.target.dataset.choiceId;
                await this.handleActionChoice(action_id, choiceId, actionContainer);
            });
        });
    }

    async handleActionChoice(actionId, choiceId, containerElement) {
        // 禁用所有按钮
        containerElement.querySelectorAll('.action-choice-btn').forEach(btn => {
            btn.disabled = true;
            if (btn.dataset.choiceId !== choiceId) {
                btn.style.opacity = '0.3';
            }
        });

        try {
            const response = await fetch('/api/chat/action', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Authorization': `Bearer ${this.getAuthToken()}`
                },
                body: JSON.stringify({
                    action_id: actionId,
                    choice: choiceId
                })
            });

            if (!response.ok) {
                throw new Error(`Failed to submit action: ${response.statusText}`);
            }

            // 显示确认
            const selectedBtn = containerElement.querySelector(`[data-choice-id="${choiceId}"]`);
            selectedBtn.innerHTML += ' ✓';
            selectedBtn.style.backgroundColor = 'var(--accent-color, #38BDF8)';

        } catch (error) {
            this.appendToCurrentMessage(`\n❌ Action failed: ${error.message}`, 'error');
        }
    }

    handleImageEvent(data) {
        const { url, caption } = data;
        
        const imageContainer = document.createElement('div');
        imageContainer.className = 'media-container image-container';
        imageContainer.innerHTML = `
            <img src="${url}" alt="${caption || 'Generated image'}" class="chat-image" loading="lazy">
            ${caption ? `<div class="media-caption">${caption}</div>` : ''}
        `;

        // 点击放大
        const img = imageContainer.querySelector('img');
        img.addEventListener('click', () => this.openImageModal(url, caption));

        this.currentMessageElement.appendChild(imageContainer);
    }

    handleVideoEvent(data) {
        const { url, thumbnail, duration } = data;
        
        const videoContainer = document.createElement('div');
        videoContainer.className = 'media-container video-container';
        videoContainer.innerHTML = `
            <video 
                src="${url}" 
                ${thumbnail ? `poster="${thumbnail}"` : ''}
                controls 
                preload="metadata"
                class="chat-video"
            >
                Your browser does not support video playback.
            </video>
            ${duration ? `<div class="media-caption">Duration: ${duration.toFixed(1)}s</div>` : ''}
        `;

        this.currentMessageElement.appendChild(videoContainer);
    }

    handleDoneEvent() {
        // 标记消息完成（可选添加时间戳等）
        if (this.currentMessageElement) {
            this.currentMessageElement.classList.add('message-done');
        }
    }

    // ============ UI 辅助方法 ============

    createMessageElement(role) {
        const messageDiv = document.createElement('div');
        messageDiv.className = `chat-message ${role}-message`;
        return messageDiv;
    }

    addUserMessage(text) {
        const messageElement = this.createMessageElement('user');
        messageElement.textContent = text;
        this.messagesContainer.appendChild(messageElement);
        this.scrollToBottom();
    }

    addSystemMessage(text) {
        const messageElement = this.createMessageElement('system');
        messageElement.textContent = text;
        this.messagesContainer.appendChild(messageElement);
        this.scrollToBottom();
    }

    appendToCurrentMessage(text, className = '') {
        if (!this.currentMessageElement) return;
        
        let textNode = this

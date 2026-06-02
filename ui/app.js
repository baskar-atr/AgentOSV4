/**
 * AgentOS UI — loaded via /ui/app.js (must run behind uvicorn, not file://)
 */
(function () {
    'use strict';

    const el = (id) => document.getElementById(id);
    const API_BASE = window.location.origin;

    let currentConversationId = null;
    let ws = null;
    let wsSessionComplete = false;
    let runTimeoutId = null;
    let pollIntervalId = null;
    let activeSessionId = null;
    let streamingEl = null;
    let isRunning = false;
    let promptSlots = [];
    let studioConfig = null;
    let obsData = null;
    let obsSelectedConversation = null;
    let obsLastThreadId = null;
    let obsSelectedNodeId = null;
    const obsExpanded = new Set();
    let obsWs = null;
    let obsLive = false;
    let obsRefreshTimer = null;
    let sessionUsageCache = {};
    let phaseStatusState = {};
    let statusDisplay = null;
    let phaseHoldTimerId = null;
    const PHASE_ORDER = ['planning', 'scheduling', 'execution', 'finalization', 'session'];

    const promptEdit = { id: null, isNew: false, isBuiltin: false };
    const skillEdit = { id: null, isNew: false, isBuiltin: false };
    const llmEdit = { id: null, isNew: false, isBuiltin: false };
    const mcpEdit = { id: null, isNew: false, isBuiltin: false };
    const toolEdit = { id: null, isNew: false, isBuiltin: false };

    let llmProviders = [];
    let mcpTransports = [];
    let toolMeta = { handler_types: [], builtin_handlers: [] };

    function formatApiError(payload, fallback) {
        if (!payload) return fallback;
        const d = payload.detail;
        if (typeof d === 'string') return d;
        if (Array.isArray(d)) return d.map((x) => x.msg || `${(x.loc || []).join('.')}: ${x.type}`).join('; ');
        return fallback;
    }

    async function api(path, opts = {}) {
        const method = opts.method || 'GET';
        const headers = { ...(opts.headers || {}) };
        if (opts.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
        const url = path.startsWith('http') ? path : API_BASE + path;
        let res;
        try {
            res = await fetch(url, { method, headers, body: opts.body, cache: 'no-store' });
        } catch {
            throw new Error(
                'Cannot reach API at ' + API_BASE +
                '. Run ./start.sh from the AgentOSV4 folder.'
            );
        }
        if (!res.ok) {
            const e = await res.json().catch(() => ({}));
            let msg = formatApiError(e, res.statusText || `HTTP ${res.status}`);
            if (res.status === 404 && path.includes('/api/builders/')) {
                msg +=
                    ' — stale backend on port 8000. Stop all uvicorn processes and run ./start.sh';
            }
            throw new Error(msg);
        }
        if (res.status === 204) return null;
        const text = await res.text();
        return text ? JSON.parse(text) : null;
    }

    let toastTimer;
    function showToast(msg, type = 'ok') {
        const node = el('toast');
        if (!node) return;
        node.textContent = msg;
        node.className = 'toast ' + type;
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => node.classList.add('hide'), 4000);
        console.log('[AgentOS]', type, msg);
    }

    function escapeHtml(s) {
        if (s == null) return '';
        const d = document.createElement('div');
        d.textContent = String(s);
        return d.innerHTML;
    }

    function splitCsv(value) {
        return value.split(',').map((x) => x.trim()).filter(Boolean);
    }

    function parseEnvCsv(value) {
        const env = {};
        splitCsv(value).forEach((pair) => {
            const i = pair.indexOf('=');
            if (i > 0) env[pair.slice(0, i).trim()] = pair.slice(i + 1).trim();
        });
        return env;
    }

    function fillSelectOptions(select, items, valueKey, labelKey, selected) {
        if (!select) return;
        select.innerHTML = (items || [])
            .map(
                (item) =>
                    `<option value="${escapeHtml(item[valueKey])}"${
                        item[valueKey] === selected ? ' selected' : ''
                    }>${escapeHtml(item[labelKey])}</option>`
            )
            .join('');
    }

    // --- Navigation ---
    document.querySelectorAll('.nav-item').forEach((btn) => {
        btn.addEventListener('click', () => switchView(btn.dataset.view));
    });

    function switchView(name) {
        document.querySelectorAll('.nav-item').forEach((n) =>
            n.classList.toggle('active', n.dataset.view === name)
        );
        document.querySelectorAll('.view').forEach((v) =>
            v.classList.toggle('active', v.id === 'view-' + name)
        );
        const sidebar = el('chat-sidebar');
        if (sidebar) sidebar.classList.toggle('hidden', name !== 'chat');
        if (name === 'prompts') loadPromptBuilder();
        if (name === 'skills') loadSkillBuilder();
        if (name === 'llms') loadLlmBuilder();
        if (name === 'mcp') loadMcpBuilder();
        if (name === 'tools') loadToolBuilder();
        if (name === 'agents') loadAgentStudio();
        if (name === 'observability') loadObservability();
    }

    // --- Chat ---
    function updateComposerUI() {
        const userInput = el('user-input');
        const sendBtn = el('send-btn');
        const form = el('composer-form');
        if (!userInput || !sendBtn) return;
        userInput.disabled = false;
        userInput.readOnly = false;
        userInput.removeAttribute('aria-disabled');
        const hasText = !!userInput.value.trim();
        sendBtn.disabled = isRunning || !hasText;
        sendBtn.classList.toggle('ready', hasText && !isRunning);
        form?.classList.toggle('running', isRunning);
    }

    function _phaseSortValue(phase) {
        const idx = PHASE_ORDER.indexOf(phase);
        return idx >= 0 ? idx : 999;
    }

    function _formatPhaseText(phase) {
        if (!phase) return 'Working';
        return phase.charAt(0).toUpperCase() + phase.slice(1);
    }

    function _pickCurrentRunningPhase() {
        const entries = Object.entries(phaseStatusState)
            .filter(([, v]) => v && v.status === 'running')
            .sort((a, b) => {
                const phaseCmp = _phaseSortValue(a[0]) - _phaseSortValue(b[0]);
                if (phaseCmp !== 0) return phaseCmp;
                return (a[1].timestamp || 0) - (b[1].timestamp || 0);
            });
        if (!entries.length) return null;
        const [phase, state] = entries[entries.length - 1];
        return { phase, ...state };
    }

    function _setStatusDisplay(phase, state) {
        statusDisplay = {
            phase,
            status: state?.status || 'running',
            message: state?.message || '',
            timestamp: state?.timestamp || Date.now(),
        };
        renderRunStatus();
    }

    function renderRunStatus() {
        const node = el('run-status');
        if (!node) return;
        if (!statusDisplay) {
            node.textContent = '';
            return;
        }
        const label = _formatPhaseText(statusDisplay.phase);
        const verb =
            statusDisplay.status === 'completed'
                ? 'completed'
                : statusDisplay.status === 'failed'
                  ? 'failed'
                  : 'in progress';
        const msg = statusDisplay.message || `${label} ${verb}`;
        node.innerHTML = `<span class="phase-pill ${escapeHtml(statusDisplay.status)}">${escapeHtml(label)} ${escapeHtml(verb)}</span><span>${escapeHtml(msg)}</span>`;
    }

    function setPhaseStatus(phase, status, timestamp, message) {
        if (!phase) return;
        phaseStatusState[phase] = {
            status: status || 'running',
            timestamp: timestamp || Date.now(),
            message: message || '',
        };
        if (status === 'completed' || status === 'failed') {
            _setStatusDisplay(phase, phaseStatusState[phase]);
            clearTimeout(phaseHoldTimerId);
            phaseHoldTimerId = setTimeout(() => {
                if (status === 'failed') return;
                const running = _pickCurrentRunningPhase();
                if (running) _setStatusDisplay(running.phase, running);
                else if (!isRunning) {
                    statusDisplay = null;
                    renderRunStatus();
                }
            }, 2200);
            return;
        }
        if (phaseHoldTimerId) return;
        _setStatusDisplay(phase, phaseStatusState[phase]);
    }

    function clearPhaseStatus() {
        clearTimeout(phaseHoldTimerId);
        phaseHoldTimerId = null;
        phaseStatusState = {};
        statusDisplay = null;
        renderRunStatus();
    }

    function completeOpenPhases(timestamp) {
        const ts = timestamp || Date.now();
        for (const phase of Object.keys(phaseStatusState)) {
            if (phaseStatusState[phase]?.status === 'running') {
                phaseStatusState[phase] = {
                    ...phaseStatusState[phase],
                    status: 'completed',
                    timestamp: ts,
                };
            }
        }
    }

    async function getSessionUsage(sessionId) {
        if (!sessionId) return null;
        if (sessionUsageCache[sessionId]) return sessionUsageCache[sessionId];
        try {
            const usage = await api('/api/sessions/' + encodeURIComponent(sessionId) + '/usage');
            sessionUsageCache[sessionId] = usage;
            return usage;
        } catch {
            return null;
        }
    }

    function applyUsageToLastAssistant(usage) {
        if (!usage) return;
        const rows = document.querySelectorAll('.msg-row');
        for (let i = rows.length - 1; i >= 0; i -= 1) {
            const row = rows[i];
            if (!row.querySelector('.msg-avatar.assistant')) continue;
            let usageNode = row.querySelector('.msg-usage');
            if (!usageNode) {
                usageNode = document.createElement('div');
                usageNode.className = 'msg-usage';
                row.querySelector('.msg-body')?.parentElement?.appendChild(usageNode);
            }
            usageNode.innerHTML = renderUsageFooter({ ...usage }, true);
            break;
        }
    }

    function finishRun(responseText) {
        if (wsSessionComplete) return;
        const completedSessionId = activeSessionId;
        wsSessionComplete = true;
        clearTimeout(runTimeoutId);
        clearInterval(pollIntervalId);
        runTimeoutId = null;
        pollIntervalId = null;
        activeSessionId = null;
        isRunning = false;
        const streamTarget = streamingEl;
        streamingEl = null;
        const userInput = el('user-input');
        if (responseText) {
            if (streamTarget) {
                streamTarget.textContent = responseText;
            } else {
                const rows = document.querySelectorAll('.msg-row .msg-body');
                const last = rows[rows.length - 1];
                const lastRow = last?.closest('.msg-row');
                const lastIsEmptyAssistant =
                    last &&
                    lastRow?.querySelector('.msg-avatar.assistant') &&
                    !last.textContent.trim();
                if (lastIsEmptyAssistant) last.textContent = responseText;
                else appendMessage('assistant', responseText);
            }
        }
        updateComposerUI();
        syncConversationFromServer();
        getSessionUsage(completedSessionId).then((usage) => {
            if (usage) applyUsageToLastAssistant(usage);
        });
        completeOpenPhases(Date.now());
        if ((phaseStatusState.session || {}).status !== 'failed') {
            setPhaseStatus('session', 'completed');
        }
        disconnectWs();
        userInput?.focus();
    }

    function completeRun() {
        finishRun('');
    }

    function disconnectWs() {
        if (ws) {
            ws.onclose = null;
            ws.close();
            ws = null;
        }
        const dot = el('ws-dot');
        const label = el('ws-label');
        if (dot) dot.classList.remove('on');
        if (label) label.textContent = 'Ready';
    }

    function resetChatComposer() {
        wsSessionComplete = false;
        isRunning = false;
        streamingEl = null;
        clearTimeout(runTimeoutId);
        clearInterval(pollIntervalId);
        activeSessionId = null;
        clearPhaseStatus();
        disconnectWs();
        updateComposerUI();
    }

    function startSessionPoll(sessionId) {
        activeSessionId = sessionId;
        clearInterval(pollIntervalId);
        pollIntervalId = setInterval(async () => {
            if (!isRunning || wsSessionComplete || activeSessionId !== sessionId) {
                clearInterval(pollIntervalId);
                return;
            }
            try {
                const s = await api('/api/sessions/' + encodeURIComponent(sessionId));
                if (s.is_completed) {
                    finishRun(s.final_response || '');
                }
            } catch {
                /* session may not exist yet */
            }
        }, 1200);
    }

    async function syncConversationFromServer() {
        if (!currentConversationId) return;
        try {
            const conv = await api('/api/conversations/' + encodeURIComponent(currentConversationId));
            const title = el('chat-title');
            if (title) title.textContent = conv.title;
            const domRows = document.querySelectorAll('.msg-row').length;
            if (conv.messages.length !== domRows) {
                document.querySelectorAll('.msg-row').forEach((r) => r.remove());
                const welcome = el('welcome');
                if (welcome) welcome.style.display = conv.messages.length ? 'none' : 'block';
                await renderConversationMessages(conv.messages);
            }
            loadConversations();
        } catch {
            /* keep local view if sync fails */
        }
    }

    async function renderConversationMessages(messages) {
        for (const m of messages) {
            let usage = null;
            if (m.role === 'assistant' && m.session_id) {
                usage = await getSessionUsage(m.session_id);
            }
            appendMessage(m.role, m.content, { usage });
        }
    }

    function armRunTimeout() {
        clearTimeout(runTimeoutId);
        runTimeoutId = setTimeout(() => {
            if (!isRunning || wsSessionComplete) return;
            finishRun('');
            showToast('Run timed out — you can continue the conversation', 'err');
        }, 120000);
    }

    function initChat() {
        const userInput = el('user-input');
        const sendBtn = el('send-btn');
        if (!userInput || !sendBtn) return;

        userInput.addEventListener('input', () => {
            userInput.style.height = 'auto';
            userInput.style.height = Math.min(userInput.scrollHeight, 160) + 'px';
            updateComposerUI();
        });

        userInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                el('composer-form')?.requestSubmit();
            }
        });

        document.querySelectorAll('.chip').forEach((c) =>
            c.addEventListener('click', () => {
                userInput.value = c.dataset.q || '';
                userInput.dispatchEvent(new Event('input'));
                userInput.focus();
            })
        );

        const form = el('composer-form');
        if (form) {
            form.addEventListener('submit', async (e) => {
                e.preventDefault();
                const text = userInput.value.trim();
                if (!text || isRunning) return;
                if (!currentConversationId) {
                    const conv = await api('/api/conversations', {
                        method: 'POST',
                        body: JSON.stringify({ agent_id: el('agent-select')?.value }),
                    });
                    currentConversationId = conv.id;
                }
                appendMessage('user', text);
                userInput.value = '';
                userInput.style.height = 'auto';
                isRunning = true;
                wsSessionComplete = false;
                streamingEl = null;
                clearPhaseStatus();
                setPhaseStatus('session', 'running');
                setPhaseStatus('planning', 'running');
                updateComposerUI();
                try {
                    const res = await api('/api/sessions', {
                        method: 'POST',
                        body: JSON.stringify({
                            query: text,
                            conversation_id: currentConversationId,
                            agent_id: el('agent-select')?.value,
                        }),
                    });
                    if (res.conversation_id) currentConversationId = res.conversation_id;
                    connectWs(res.session_id);
                    startSessionPoll(res.session_id);
                    armRunTimeout();
                    onSessionStarted(res.session_id, currentConversationId);
                    loadConversations();
                } catch (err) {
                    appendMessage('assistant', 'Error: ' + err.message);
                    resetChatComposer();
                }
            });
        }

        const newChat = el('new-chat-btn');
        if (newChat) {
            newChat.addEventListener('click', async () => {
                try {
                    const conv = await api('/api/conversations', {
                        method: 'POST',
                        body: JSON.stringify({ agent_id: el('agent-select')?.value }),
                    });
                    currentConversationId = conv.id;
                    document.querySelectorAll('.msg-row').forEach((r) => r.remove());
                    const welcome = el('welcome');
                    if (welcome) welcome.style.display = 'block';
                    const title = el('chat-title');
                    if (title) title.textContent = 'New chat';
                    resetChatComposer();
                    switchView('chat');
                    userInput.focus();
                    userInput.dispatchEvent(new Event('input'));
                    loadConversations();
                } catch (err) {
                    showToast(err.message, 'err');
                }
            });
        }

        const agentSel = el('agent-select');
        if (agentSel) {
            agentSel.addEventListener('change', async (e) => {
                await api('/api/agents/activate', {
                    method: 'POST',
                    body: JSON.stringify({ agent_id: e.target.value }),
                });
            });
        }
    }

    async function loadAgentsDropdown() {
        const agents = await api('/api/agents');
        ['agent-select', 'studio-agent-select'].forEach((id) => {
            const sel = el(id);
            if (!sel) return;
            sel.innerHTML = agents
                .map(
                    (a) =>
                        `<option value="${escapeHtml(a.id)}"${a.is_active ? ' selected' : ''}>${escapeHtml(a.name)}</option>`
                )
                .join('');
        });
    }

    async function loadConversations() {
        const list = el('conv-list');
        if (!list) return;
        const convs = await api('/api/conversations');
        list.innerHTML = convs.length
            ? convs
                  .map(
                      (c) => `
        <div class="conv-item ${c.id === currentConversationId ? 'active' : ''}" data-id="${escapeHtml(c.id)}">
            <span>${escapeHtml(c.title)}</span>
            <button type="button" data-del="${escapeHtml(c.id)}" style="background:none;border:none;color:var(--text-muted);cursor:pointer">×</button>
        </div>`
                  )
                  .join('')
            : '<div class="empty-hint">No chats</div>';

        list.querySelectorAll('.conv-item').forEach((item) => {
            item.addEventListener('click', (e) => {
                if (e.target.closest('[data-del]')) return;
                selectConversation(item.getAttribute('data-id'));
            });
        });
        list.querySelectorAll('[data-del]').forEach((b) => {
            b.addEventListener('click', async (e) => {
                e.stopPropagation();
                const id = b.getAttribute('data-del');
                await api('/api/conversations/' + encodeURIComponent(id), { method: 'DELETE' });
                if (currentConversationId === id) {
                    currentConversationId = null;
                    const welcome = el('welcome');
                    if (welcome) welcome.style.display = 'block';
                }
                loadConversations();
            });
        });
    }

    function renderUsageFooter(usage, innerOnly = false) {
        const tools = usage?.tools || [];
        const mcp = usage?.mcp_calls || [];
        const llm = usage?.llm_calls || [];
        const chunks = [];
        if (tools.length) {
            chunks.push(
                `<span class="tag">Tools: ${escapeHtml(tools.map((t) => t.name).join(', '))}</span>`
            );
        }
        if (mcp.length) {
            chunks.push(
                `<span class="tag">MCP: ${escapeHtml(mcp.map((m) => `${m.mcp_server_id}/${m.mcp_tool_name}`).join(', '))}</span>`
            );
        }
        if (llm.length) {
            chunks.push(
                `<span class="tag">LLM: ${escapeHtml(llm.map((l) => l.model_name).join(', '))}</span>`
            );
        }
        if (!chunks.length) return '';
        return innerOnly ? chunks.join(' ') : `<div class="msg-usage">${chunks.join(' ')}</div>`;
    }

    function appendMessage(role, content, meta = null) {
        const welcome = el('welcome');
        if (welcome) welcome.style.display = 'none';
        const messages = el('messages');
        if (!messages) return null;
        const row = document.createElement('div');
        row.className = 'msg-row';
        row.innerHTML = `<div class="msg-inner">
        <div class="msg-avatar ${role}">${role === 'user' ? 'You' : 'AI'}</div>
        <div>
            <div class="msg-body">${escapeHtml(content)}</div>
            ${role === 'assistant' ? renderUsageFooter(meta?.usage) : ''}
        </div>
    </div>`;
        messages.appendChild(row);
        messages.scrollTop = messages.scrollHeight;
        return row.querySelector('.msg-body');
    }

    function scrollMessagesToBottom(force = false) {
        const messages = el('messages');
        if (!messages) return;
        const distanceFromBottom =
            messages.scrollHeight - (messages.scrollTop + messages.clientHeight);
        // Auto-follow while streaming if user is already near bottom.
        if (force || distanceFromBottom < 120) {
            messages.scrollTop = messages.scrollHeight;
        }
    }

    async function selectConversation(id) {
        currentConversationId = id;
        resetChatComposer();
        const conv = await api('/api/conversations/' + encodeURIComponent(id));
        const title = el('chat-title');
        if (title) title.textContent = conv.title;
        document.querySelectorAll('.msg-row').forEach((r) => r.remove());
        const welcome = el('welcome');
        if (welcome) welcome.style.display = 'none';
        await renderConversationMessages(conv.messages);
        updateComposerUI();
        loadConversations();
    }

    function connectWs(sessionId) {
        disconnectWs();
        wsSessionComplete = false;
        ws = new WebSocket(
            `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/${sessionId}`
        );
        ws.onopen = () => {
            const dot = el('ws-dot');
            const label = el('ws-label');
            if (dot) dot.classList.add('on');
            if (label) label.textContent = 'Live';
        };
        ws.onclose = () => {
            const dot = el('ws-dot');
            const label = el('ws-label');
            if (dot) dot.classList.remove('on');
            if (label) label.textContent = wsSessionComplete ? 'Ready' : 'Offline';
            ws = null;
            if (isRunning && !wsSessionComplete) {
                /* poll fallback will detect is_completed */
            }
        };
        ws.onmessage = (e) => {
            let msg;
            try {
                msg = JSON.parse(e.data);
            } catch {
                return;
            }
            if (msg.event_type === 'STATE_SYNC') return;
            if (msg.event_type === 'TOKEN_STREAM') {
                if (!streamingEl) streamingEl = appendMessage('assistant', '');
                if (streamingEl) streamingEl.textContent += msg.payload?.chunk || '';
                scrollMessagesToBottom(true);
            }
            if (msg.event_type === 'OBSERVABILITY' && msg.payload?.kind === 'phase_status') {
                setPhaseStatus(
                    msg.payload.phase,
                    msg.payload.status || 'running',
                    msg.timestamp,
                    msg.payload.message || ''
                );
            }
            if (msg.event_type === 'FINAL_RESPONSE') {
                finishRun(msg.payload?.response_text || '');
            }
            onObservabilityEvent(msg);
            if (msg.event_type === 'ERROR') {
                setPhaseStatus('session', 'failed', msg.timestamp);
                appendMessage('assistant', 'Error: ' + (msg.payload?.error_message || 'unknown'));
                finishRun('');
            }
        };
    }

    // --- Prompt Builder (static form) ---
    function fillPromptSlotsSelect() {
        const sel = el('pf-slot');
        if (!sel) return;
        sel.innerHTML = promptSlots
            .map((s) => `<option value="${escapeHtml(s.slot)}">${escapeHtml(s.slot)}</option>`)
            .join('');
    }

    function showPromptForm(show) {
        const form = el('prompt-form');
        const empty = el('prompt-empty');
        if (form) form.style.display = show ? 'block' : 'none';
        if (empty) empty.style.display = show ? 'none' : 'block';
    }

    async function refreshPromptList() {
        const list = el('prompt-list');
        if (!list) return [];
        const prompts = await api('/api/builders/prompts');
        const selected = promptEdit.isNew ? 'new' : promptEdit.id;
        list.innerHTML = prompts
            .map(
                (p) => `
        <div class="list-card ${p.id === selected ? 'selected' : ''}" data-prompt-id="${escapeHtml(p.id)}">
            <h4>${escapeHtml(p.name)}</h4>
            <p><span class="tag">${escapeHtml(p.slot)}</span>${p.is_builtin ? '<span class="tag">builtin</span>' : ''}</p>
        </div>`
            )
            .join('') || '<p class="empty-hint">No prompts yet</p>';
        return prompts;
    }

    async function openPromptEditor(id) {
        promptEdit.isNew = id === 'new';
        promptEdit.id = promptEdit.isNew ? null : id;

        if (promptEdit.isNew) {
            promptEdit.isBuiltin = false;
            showPromptForm(true);
            el('pf-title').textContent = 'New prompt';
            el('pf-name').value = '';
            el('pf-desc').value = '';
            el('pf-content').value = '';
            el('pf-tags').value = '';
            if (promptSlots.length) el('pf-slot').value = promptSlots[0].slot;
            el('pf-delete').style.display = 'none';
            await refreshPromptList();
            return;
        }

        const p = await api('/api/builders/prompts/' + encodeURIComponent(id));
        promptEdit.isBuiltin = !!p.is_builtin;
        showPromptForm(true);
        el('pf-title').textContent = 'Edit: ' + p.name;
        el('pf-name').value = p.name || '';
        el('pf-slot').value = p.slot || 'planner_system_prompt';
        el('pf-desc').value = p.description || '';
        el('pf-content').value = p.content || '';
        el('pf-tags').value = (p.tags || []).join(', ');
        el('pf-delete').style.display = p.is_builtin ? 'none' : 'inline-block';
        await refreshPromptList();
    }

    async function loadPromptBuilder() {
        try {
            promptSlots = await api('/api/builders/prompt-slots');
            fillPromptSlotsSelect();
            const prompts = await refreshPromptList();
            if (promptEdit.isNew) {
                await openPromptEditor('new');
            } else if (promptEdit.id) {
                await openPromptEditor(promptEdit.id);
            } else if (prompts.length) {
                await openPromptEditor(prompts[0].id);
            } else {
                showPromptForm(false);
            }
        } catch (err) {
            showToast(err.message, 'err');
        }
    }

    function initPromptBuilder() {
        const list = el('prompt-list');
        if (list) {
            list.addEventListener('click', (e) => {
                const card = e.target.closest('[data-prompt-id]');
                if (!card) return;
                openPromptEditor(card.getAttribute('data-prompt-id')).catch((err) =>
                    showToast(err.message, 'err')
                );
            });
        }

        const newBtn = el('prompt-new-btn');
        if (newBtn) {
            newBtn.addEventListener('click', () => {
                openPromptEditor('new').catch((err) => showToast(err.message, 'err'));
            });
        }

        const form = el('prompt-form');
        if (form) {
            form.addEventListener('submit', async (e) => {
                e.preventDefault();
                const body = {
                    name: el('pf-name').value.trim(),
                    slot: el('pf-slot').value,
                    description: el('pf-desc').value.trim(),
                    content: el('pf-content').value,
                    tags: splitCsv(el('pf-tags').value),
                };
                if (!body.name || !body.content) {
                    showToast('Name and content are required', 'err');
                    return;
                }
                const status = el('pf-status');
                if (status) status.textContent = 'Saving…';
                try {
                    let saved;
                    if (promptEdit.isNew) {
                        saved = await api('/api/builders/prompts', {
                            method: 'POST',
                            body: JSON.stringify(body),
                        });
                        promptEdit.id = saved.id;
                        promptEdit.isNew = false;
                    } else {
                        saved = await api(
                            '/api/builders/prompts/' + encodeURIComponent(promptEdit.id),
                            { method: 'PUT', body: JSON.stringify(body) }
                        );
                    }
                    promptEdit.isBuiltin = !!saved.is_builtin;
                    await openPromptEditor(saved.id);
                    showToast('Prompt saved successfully');
                } catch (err) {
                    showToast(err.message, 'err');
                } finally {
                    if (status) status.textContent = '';
                }
            });
        }

        const delBtn = el('pf-delete');
        if (delBtn) {
            delBtn.addEventListener('click', async () => {
                if (promptEdit.isNew || !promptEdit.id) return;
                if (!confirm('Delete this prompt template?')) return;
                try {
                    await api('/api/builders/prompts/' + encodeURIComponent(promptEdit.id), {
                        method: 'DELETE',
                    });
                    promptEdit.id = null;
                    showPromptForm(false);
                    await refreshPromptList();
                    showToast('Prompt deleted');
                } catch (err) {
                    showToast(err.message, 'err');
                }
            });
        }
    }

    // --- Skill Builder (static form) ---
    function showSkillForm(show) {
        const form = el('skill-form');
        const empty = el('skill-empty');
        if (form) form.style.display = show ? 'block' : 'none';
        if (empty) empty.style.display = show ? 'none' : 'block';
    }

    async function refreshSkillList() {
        const list = el('skill-list');
        if (!list) return [];
        const skills = await api('/api/builders/skills');
        const selected = skillEdit.isNew ? 'new' : skillEdit.id;
        list.innerHTML = skills
            .map(
                (s) => `
        <div class="list-card ${s.id === selected ? 'selected' : ''}" data-skill-id="${escapeHtml(s.id)}">
            <h4>${escapeHtml(s.name)}</h4>
            <p>${escapeHtml((s.description || '').slice(0, 80))}</p>
        </div>`
            )
            .join('') || '<p class="empty-hint">No skills yet</p>';
        return skills;
    }

    async function openSkillEditor(id) {
        skillEdit.isNew = id === 'new';
        skillEdit.id = skillEdit.isNew ? null : id;

        if (skillEdit.isNew) {
            skillEdit.isBuiltin = false;
            showSkillForm(true);
            el('sf-title').textContent = 'New skill';
            el('sf-name').value = '';
            el('sf-desc').value = '';
            el('sf-triggers').value = '';
            el('sf-tools').value = '';
            el('sf-hints').value = '';
            el('sf-deps').value = '';
            el('sf-parallel').checked = true;
            el('sf-delete').style.display = 'none';
            await refreshSkillList();
            return;
        }

        const s = await api('/api/builders/skills/' + encodeURIComponent(id));
        skillEdit.isBuiltin = !!s.is_builtin;
        showSkillForm(true);
        el('sf-title').textContent = 'Edit: ' + s.name;
        el('sf-name').value = s.name || '';
        el('sf-desc').value = s.description || '';
        el('sf-triggers').value = (s.trigger_conditions || []).join(', ');
        el('sf-tools').value = (s.tools || []).join(', ');
        el('sf-hints').value = s.planner_hints || '';
        el('sf-deps').value = (s.dependencies || []).join(', ');
        el('sf-parallel').checked = s.parallelizable !== false;
        el('sf-delete').style.display = s.is_builtin ? 'none' : 'inline-block';
        await refreshSkillList();
    }

    async function loadSkillBuilder() {
        try {
            const skills = await refreshSkillList();
            if (skillEdit.isNew) {
                await openSkillEditor('new');
            } else if (skillEdit.id) {
                await openSkillEditor(skillEdit.id);
            } else if (skills.length) {
                await openSkillEditor(skills[0].id);
            } else {
                showSkillForm(false);
            }
        } catch (err) {
            showToast(err.message, 'err');
        }
    }

    function initSkillBuilder() {
        const list = el('skill-list');
        if (list) {
            list.addEventListener('click', (e) => {
                const card = e.target.closest('[data-skill-id]');
                if (!card) return;
                openSkillEditor(card.getAttribute('data-skill-id')).catch((err) =>
                    showToast(err.message, 'err')
                );
            });
        }

        const newBtn = el('skill-new-btn');
        if (newBtn) {
            newBtn.addEventListener('click', () => {
                openSkillEditor('new').catch((err) => showToast(err.message, 'err'));
            });
        }

        const form = el('skill-form');
        if (form) {
            form.addEventListener('submit', async (e) => {
                e.preventDefault();
                const body = {
                    name: el('sf-name').value.trim(),
                    description: el('sf-desc').value.trim(),
                    trigger_conditions: splitCsv(el('sf-triggers').value),
                    tools: splitCsv(el('sf-tools').value),
                    planner_hints: el('sf-hints').value.trim(),
                    dependencies: splitCsv(el('sf-deps').value),
                    parallelizable: el('sf-parallel').checked,
                };
                if (!body.name) {
                    showToast('Skill name is required', 'err');
                    return;
                }
                const status = el('sf-status');
                if (status) status.textContent = 'Saving…';
                try {
                    let saved;
                    if (skillEdit.isNew) {
                        saved = await api('/api/builders/skills', {
                            method: 'POST',
                            body: JSON.stringify(body),
                        });
                        skillEdit.id = saved.id;
                        skillEdit.isNew = false;
                    } else {
                        saved = await api(
                            '/api/builders/skills/' + encodeURIComponent(skillEdit.id),
                            { method: 'PUT', body: JSON.stringify(body) }
                        );
                    }
                    await openSkillEditor(saved.id);
                    showToast('Skill saved successfully');
                } catch (err) {
                    showToast(err.message, 'err');
                } finally {
                    if (status) status.textContent = '';
                }
            });
        }

        const delBtn = el('sf-delete');
        if (delBtn) {
            delBtn.addEventListener('click', async () => {
                if (skillEdit.isNew || !skillEdit.id) return;
                if (!confirm('Delete this skill?')) return;
                try {
                    await api('/api/builders/skills/' + encodeURIComponent(skillEdit.id), {
                        method: 'DELETE',
                    });
                    skillEdit.id = null;
                    showSkillForm(false);
                    await refreshSkillList();
                    showToast('Skill deleted');
                } catch (err) {
                    showToast(err.message, 'err');
                }
            });
        }
    }

    // --- LLM Builder ---
    function showLlmForm(show) {
        const form = el('llm-form');
        const empty = el('llm-empty');
        if (form) form.style.display = show ? 'block' : 'none';
        if (empty) empty.style.display = show ? 'none' : 'block';
    }

    async function ensureLlmMeta() {
        if (llmProviders.length) return;
        const meta = await api('/api/builders/llms/meta');
        llmProviders = meta.providers || [];
        fillSelectOptions(
            el('lf-provider'),
            llmProviders.map((p) => ({ id: p.id, label: p.label })),
            'id',
            'label'
        );
    }

    async function refreshLlmList() {
        const list = el('llm-list');
        if (!list) return [];
        const models = await api('/api/builders/llms');
        const selected = llmEdit.isNew ? 'new' : llmEdit.id;
        list.innerHTML =
            models
                .map(
                    (m) => `
        <div class="list-card ${m.id === selected ? 'selected' : ''}" data-llm-id="${escapeHtml(m.id)}">
            <h4>${escapeHtml(m.name)}</h4>
            <p>${escapeHtml(m.provider)} · ${escapeHtml(m.model_name)}</p>
        </div>`
                )
                .join('') || '<p class="empty-hint">No models yet</p>';
        return models;
    }

    async function openLlmEditor(id) {
        await ensureLlmMeta();
        llmEdit.isNew = id === 'new';
        llmEdit.id = llmEdit.isNew ? null : id;
        if (llmEdit.isNew) {
            llmEdit.isBuiltin = false;
            showLlmForm(true);
            el('lf-title').textContent = 'New model';
            el('lf-name').value = '';
            el('lf-provider').value = llmProviders[0]?.id || 'simulated';
            el('lf-model').value = '';
            el('lf-desc').value = '';
            el('lf-api-env').value = '';
            el('lf-base').value = '';
            el('lf-temp').value = '0';
            el('lf-tokens').value = '2048';
            el('lf-tags').value = '';
            el('lf-delete').style.display = 'none';
            await refreshLlmList();
            return;
        }
        const m = await api('/api/builders/llms/' + encodeURIComponent(id));
        llmEdit.isBuiltin = !!m.is_builtin;
        showLlmForm(true);
        el('lf-title').textContent = 'Edit: ' + m.name;
        el('lf-name').value = m.name || '';
        el('lf-provider').value = m.provider || 'simulated';
        el('lf-model').value = m.model_name || '';
        el('lf-desc').value = m.description || '';
        el('lf-api-env').value = m.api_key_env || '';
        el('lf-base').value = m.base_url || '';
        el('lf-temp').value = String(m.temperature ?? 0);
        el('lf-tokens').value = String(m.max_tokens ?? 2048);
        el('lf-tags').value = (m.tags || []).join(', ');
        el('lf-delete').style.display = m.is_builtin ? 'none' : 'inline-block';
        await refreshLlmList();
    }

    async function loadLlmBuilder() {
        try {
            const models = await refreshLlmList();
            if (llmEdit.isNew) await openLlmEditor('new');
            else if (llmEdit.id) await openLlmEditor(llmEdit.id);
            else if (models.length) await openLlmEditor(models[0].id);
            else showLlmForm(false);
        } catch (err) {
            showToast(err.message, 'err');
        }
    }

    function initLlmBuilder() {
        const list = el('llm-list');
        if (list) {
            list.addEventListener('click', (e) => {
                const card = e.target.closest('[data-llm-id]');
                if (!card) return;
                openLlmEditor(card.getAttribute('data-llm-id')).catch((err) =>
                    showToast(err.message, 'err')
                );
            });
        }
        el('llm-new-btn')?.addEventListener('click', () =>
            openLlmEditor('new').catch((err) => showToast(err.message, 'err'))
        );
        el('llm-form')?.addEventListener('submit', async (e) => {
            e.preventDefault();
            const body = {
                name: el('lf-name').value.trim(),
                provider: el('lf-provider').value,
                model_name: el('lf-model').value.trim(),
                description: el('lf-desc').value.trim(),
                api_key_env: el('lf-api-env').value.trim(),
                base_url: el('lf-base').value.trim(),
                temperature: parseFloat(el('lf-temp').value) || 0,
                max_tokens: parseInt(el('lf-tokens').value, 10) || 2048,
                tags: splitCsv(el('lf-tags').value),
            };
            const status = el('lf-status');
            if (status) status.textContent = 'Saving…';
            try {
                let saved;
                if (llmEdit.isNew) {
                    saved = await api('/api/builders/llms', {
                        method: 'POST',
                        body: JSON.stringify(body),
                    });
                    llmEdit.id = saved.id;
                    llmEdit.isNew = false;
                } else {
                    saved = await api('/api/builders/llms/' + encodeURIComponent(llmEdit.id), {
                        method: 'PUT',
                        body: JSON.stringify(body),
                    });
                }
                await openLlmEditor(saved.id);
                showToast('LLM model saved');
            } catch (err) {
                showToast(err.message, 'err');
            } finally {
                if (status) status.textContent = '';
            }
        });
        el('lf-delete')?.addEventListener('click', async () => {
            if (llmEdit.isNew || !llmEdit.id) return;
            if (!confirm('Delete this LLM model?')) return;
            try {
                await api('/api/builders/llms/' + encodeURIComponent(llmEdit.id), {
                    method: 'DELETE',
                });
                llmEdit.id = null;
                showLlmForm(false);
                await refreshLlmList();
                showToast('Model deleted');
            } catch (err) {
                showToast(err.message, 'err');
            }
        });
    }

    // --- MCP Builder ---
    function showMcpForm(show) {
        const form = el('mcp-form');
        const empty = el('mcp-empty');
        if (form) form.style.display = show ? 'block' : 'none';
        if (empty) empty.style.display = show ? 'none' : 'block';
    }

    async function ensureMcpMeta() {
        if (mcpTransports.length) return;
        const meta = await api('/api/builders/mcp/meta');
        mcpTransports = meta.transports || [];
        fillSelectOptions(
            el('mf-transport'),
            mcpTransports.map((t) => ({ id: t.id, label: t.label })),
            'id',
            'label'
        );
    }

    async function refreshMcpList() {
        const list = el('mcp-list');
        if (!list) return [];
        const servers = await api('/api/builders/mcp');
        const selected = mcpEdit.isNew ? 'new' : mcpEdit.id;
        list.innerHTML =
            servers
                .map(
                    (s) => `
        <div class="list-card ${s.id === selected ? 'selected' : ''}" data-mcp-id="${escapeHtml(s.id)}">
            <h4>${escapeHtml(s.name)}</h4>
            <p>${escapeHtml(s.transport)}${s.enabled ? '' : ' · disabled'}</p>
        </div>`
                )
                .join('') || '<p class="empty-hint">No MCP servers yet</p>';
        return servers;
    }

    async function openMcpEditor(id) {
        await ensureMcpMeta();
        mcpEdit.isNew = id === 'new';
        mcpEdit.id = mcpEdit.isNew ? null : id;
        if (mcpEdit.isNew) {
            mcpEdit.isBuiltin = false;
            showMcpForm(true);
            el('mf-title').textContent = 'New MCP server';
            el('mf-name').value = '';
            el('mf-transport').value = mcpTransports[0]?.id || 'stdio';
            el('mf-desc').value = '';
            el('mf-cmd').value = '';
            el('mf-args').value = '';
            el('mf-url').value = '';
            el('mf-env').value = '';
            el('mf-enabled').checked = true;
            el('mf-tags').value = '';
            el('mf-delete').style.display = 'none';
            await refreshMcpList();
            return;
        }
        const s = await api('/api/builders/mcp/' + encodeURIComponent(id));
        mcpEdit.isBuiltin = !!s.is_builtin;
        showMcpForm(true);
        el('mf-title').textContent = 'Edit: ' + s.name;
        el('mf-name').value = s.name || '';
        el('mf-transport').value = s.transport || 'stdio';
        el('mf-desc').value = s.description || '';
        el('mf-cmd').value = s.command || '';
        el('mf-args').value = (s.args || []).join(', ');
        el('mf-url').value = s.url || '';
        el('mf-env').value = Object.entries(s.env || {})
            .map(([k, v]) => `${k}=${v}`)
            .join(', ');
        el('mf-enabled').checked = s.enabled !== false;
        el('mf-tags').value = (s.tags || []).join(', ');
        el('mf-delete').style.display = s.is_builtin ? 'none' : 'inline-block';
        await refreshMcpList();
    }

    async function loadMcpBuilder() {
        try {
            const servers = await refreshMcpList();
            if (mcpEdit.isNew) await openMcpEditor('new');
            else if (mcpEdit.id) await openMcpEditor(mcpEdit.id);
            else if (servers.length) await openMcpEditor(servers[0].id);
            else showMcpForm(false);
        } catch (err) {
            showToast(err.message, 'err');
        }
    }

    function initMcpBuilder() {
        el('mcp-list')?.addEventListener('click', (e) => {
            const card = e.target.closest('[data-mcp-id]');
            if (!card) return;
            openMcpEditor(card.getAttribute('data-mcp-id')).catch((err) =>
                showToast(err.message, 'err')
            );
        });
        el('mcp-new-btn')?.addEventListener('click', () =>
            openMcpEditor('new').catch((err) => showToast(err.message, 'err'))
        );
        el('mcp-form')?.addEventListener('submit', async (e) => {
            e.preventDefault();
            const body = {
                name: el('mf-name').value.trim(),
                transport: el('mf-transport').value,
                description: el('mf-desc').value.trim(),
                command: el('mf-cmd').value.trim(),
                args: splitCsv(el('mf-args').value),
                url: el('mf-url').value.trim(),
                env: parseEnvCsv(el('mf-env').value),
                enabled: el('mf-enabled').checked,
                tags: splitCsv(el('mf-tags').value),
            };
            const status = el('mf-status');
            if (status) status.textContent = 'Saving…';
            try {
                let saved;
                if (mcpEdit.isNew) {
                    saved = await api('/api/builders/mcp', {
                        method: 'POST',
                        body: JSON.stringify(body),
                    });
                    mcpEdit.id = saved.id;
                    mcpEdit.isNew = false;
                } else {
                    saved = await api('/api/builders/mcp/' + encodeURIComponent(mcpEdit.id), {
                        method: 'PUT',
                        body: JSON.stringify(body),
                    });
                }
                await openMcpEditor(saved.id);
                showToast('MCP server saved');
            } catch (err) {
                showToast(err.message, 'err');
            } finally {
                if (status) status.textContent = '';
            }
        });
        el('mf-delete')?.addEventListener('click', async () => {
            if (mcpEdit.isNew || !mcpEdit.id) return;
            if (!confirm('Delete this MCP server?')) return;
            try {
                await api('/api/builders/mcp/' + encodeURIComponent(mcpEdit.id), {
                    method: 'DELETE',
                });
                mcpEdit.id = null;
                showMcpForm(false);
                await refreshMcpList();
                showToast('MCP server deleted');
            } catch (err) {
                showToast(err.message, 'err');
            }
        });
    }

    // --- Tools Config Builder ---
    function showToolForm(show) {
        const form = el('tool-form');
        const empty = el('tool-empty');
        if (form) form.style.display = show ? 'block' : 'none';
        if (empty) empty.style.display = show ? 'none' : 'block';
    }

    async function ensureToolMeta() {
        if (toolMeta.handler_types.length) return;
        toolMeta = await api('/api/builders/tools/meta');
        fillSelectOptions(
            el('tf-handler'),
            (toolMeta.handler_types || []).map((h) => ({ id: h.id, label: h.label })),
            'id',
            'label'
        );
        fillSelectOptions(
            el('tf-builtin'),
            [{ id: '', label: '— select —' }].concat(
                (toolMeta.builtin_handlers || []).map((h) => ({ id: h.id, label: h.label }))
            ),
            'id',
            'label'
        );
    }

    async function refreshToolList() {
        const list = el('tool-list');
        if (!list) return [];
        const tools = await api('/api/builders/tools');
        const selected = toolEdit.isNew ? 'new' : toolEdit.id;
        list.innerHTML =
            tools
                .map(
                    (t) => `
        <div class="list-card ${t.id === selected ? 'selected' : ''}" data-tool-id="${escapeHtml(t.id)}">
            <h4>${escapeHtml(t.name)}</h4>
            <p>${escapeHtml(t.handler_type)} · ${escapeHtml((t.description || '').slice(0, 60))}</p>
        </div>`
                )
                .join('') || '<p class="empty-hint">No tools yet</p>';
        return tools;
    }

    async function openToolEditor(id) {
        await ensureToolMeta();
        toolEdit.isNew = id === 'new';
        toolEdit.id = toolEdit.isNew ? null : id;
        if (toolEdit.isNew) {
            toolEdit.isBuiltin = false;
            showToolForm(true);
            el('tf-title').textContent = 'New tool';
            el('tf-name').value = '';
            el('tf-handler').value = 'simulated';
            el('tf-builtin').value = '';
            el('tf-desc').value = '';
            el('tf-mcp-server').value = '';
            el('tf-mcp-tool').value = '';
            el('tf-tags').value = '';
            el('tf-delete').style.display = 'none';
            await refreshToolList();
            return;
        }
        const t = await api('/api/builders/tools/' + encodeURIComponent(id));
        toolEdit.isBuiltin = !!t.is_builtin;
        showToolForm(true);
        el('tf-title').textContent = 'Edit: ' + t.name;
        el('tf-name').value = t.name || '';
        el('tf-handler').value = t.handler_type || 'simulated';
        el('tf-builtin').value = t.builtin_handler || '';
        el('tf-desc').value = t.description || '';
        el('tf-mcp-server').value = t.mcp_server_id || '';
        el('tf-mcp-tool').value = t.mcp_tool_name || '';
        el('tf-tags').value = (t.tags || []).join(', ');
        el('tf-delete').style.display = t.is_builtin ? 'none' : 'inline-block';
        await refreshToolList();
    }

    async function loadToolBuilder() {
        try {
            const tools = await refreshToolList();
            if (toolEdit.isNew) await openToolEditor('new');
            else if (toolEdit.id) await openToolEditor(toolEdit.id);
            else if (tools.length) await openToolEditor(tools[0].id);
            else showToolForm(false);
        } catch (err) {
            showToast(err.message, 'err');
        }
    }

    function initToolBuilder() {
        el('tool-list')?.addEventListener('click', (e) => {
            const card = e.target.closest('[data-tool-id]');
            if (!card) return;
            openToolEditor(card.getAttribute('data-tool-id')).catch((err) =>
                showToast(err.message, 'err')
            );
        });
        el('tool-new-btn')?.addEventListener('click', () =>
            openToolEditor('new').catch((err) => showToast(err.message, 'err'))
        );
        el('tool-form')?.addEventListener('submit', async (e) => {
            e.preventDefault();
            const body = {
                name: el('tf-name').value.trim(),
                description: el('tf-desc').value.trim(),
                handler_type: el('tf-handler').value,
                builtin_handler: el('tf-builtin').value,
                mcp_server_id: el('tf-mcp-server').value.trim(),
                mcp_tool_name: el('tf-mcp-tool').value.trim(),
                tags: splitCsv(el('tf-tags').value),
            };
            const status = el('tf-status');
            if (status) status.textContent = 'Saving…';
            try {
                let saved;
                if (toolEdit.isNew) {
                    saved = await api('/api/builders/tools', {
                        method: 'POST',
                        body: JSON.stringify(body),
                    });
                    toolEdit.id = saved.id;
                    toolEdit.isNew = false;
                } else {
                    saved = await api('/api/builders/tools/' + encodeURIComponent(toolEdit.id), {
                        method: 'PUT',
                        body: JSON.stringify(body),
                    });
                }
                await openToolEditor(saved.id);
                showToast('Tool saved');
            } catch (err) {
                showToast(err.message, 'err');
            } finally {
                if (status) status.textContent = '';
            }
        });
        el('tf-delete')?.addEventListener('click', async () => {
            if (toolEdit.isNew || !toolEdit.id) return;
            if (!confirm('Delete this tool definition?')) return;
            try {
                await api('/api/builders/tools/' + encodeURIComponent(toolEdit.id), {
                    method: 'DELETE',
                });
                toolEdit.id = null;
                showToolForm(false);
                await refreshToolList();
                showToast('Tool deleted');
            } catch (err) {
                showToast(err.message, 'err');
            }
        });
    }

    // --- Agent Studio ---
    async function loadAgentStudio() {
        await loadAgentsDropdown();
        const agentId = el('studio-agent-select')?.value;
        if (!agentId) return;
        studioConfig = await api('/api/agents/' + encodeURIComponent(agentId) + '/configuration');
        renderStudioBindings();
        renderStudioSkills();
        renderStudioLlms();
        renderStudioMcp();
        renderStudioTools();
        updateStudioPreview();
    }

    function renderStudioBindings() {
        const container = el('studio-prompt-bindings');
        if (!container || !studioConfig) return;
        const { prompt_slots, prompt_bindings, prompt_library } = studioConfig;
        container.innerHTML = (prompt_slots || [])
            .map((slot) => {
                const slotKey = slot.slot;
                const desc = slot.description;
                const opts =
                    '<option value="">— none —</option>' +
                    (prompt_library || [])
                        .map(
                            (p) =>
                                `<option value="${escapeHtml(p.id)}"${
                                    prompt_bindings[slotKey] === p.id ? ' selected' : ''
                                }>${escapeHtml(p.name)} (${escapeHtml(p.slot)})</option>`
                        )
                        .join('');
                return `<div class="slot-row">
            <div><strong style="font-size:13px">${escapeHtml(slotKey)}</strong><br>
            <span style="font-size:11px;color:var(--text-muted)">${escapeHtml(desc)}</span></div>
            <select class="inp studio-prompt-select" data-slot="${escapeHtml(slotKey)}">${opts}</select>
        </div>`;
            })
            .join('');
    }

    function renderStudioSkills() {
        const container = el('studio-skills');
        if (!container || !studioConfig) return;
        const { skill_library, enabled_skills } = studioConfig;
        container.innerHTML = (skill_library || [])
            .map(
                (s) => `
        <label class="skill-assign">
            <input type="checkbox" class="studio-skill-cb" value="${escapeHtml(s.name)}"${
                    (enabled_skills || []).includes(s.name) ? ' checked' : ''
                }>
            <div>
                <strong style="font-size:13px">${escapeHtml(s.name)}</strong>
                <p style="font-size:12px;color:var(--text-muted)">${escapeHtml(s.description)}</p>
            </div>
        </label>`
            )
            .join('');
    }

    function renderStudioLlms() {
        if (!studioConfig) return;
        const { llm_library, primary_llm_id, secondary_llm_id } = studioConfig;
        const opts =
            '<option value="">— inline default —</option>' +
            (llm_library || [])
                .map(
                    (m) =>
                        `<option value="${escapeHtml(m.id)}">${escapeHtml(m.name)} (${escapeHtml(m.model_name)})</option>`
                )
                .join('');
        const primary = el('studio-primary-llm');
        const secondary = el('studio-secondary-llm');
        if (primary) {
            primary.innerHTML = opts;
            primary.value = primary_llm_id || '';
        }
        if (secondary) {
            secondary.innerHTML = opts;
            secondary.value = secondary_llm_id || '';
        }
    }

    function renderStudioMcp() {
        const container = el('studio-mcp');
        if (!container || !studioConfig) return;
        const { mcp_library, enabled_mcp_servers } = studioConfig;
        container.innerHTML = (mcp_library || [])
            .map(
                (s) => `
        <label class="skill-assign">
            <input type="checkbox" class="studio-mcp-cb" value="${escapeHtml(s.id)}"${
                    (enabled_mcp_servers || []).includes(s.id) ? ' checked' : ''
                }${s.enabled === false ? ' disabled' : ''}>
            <div>
                <strong style="font-size:13px">${escapeHtml(s.name)}</strong>
                <p style="font-size:12px;color:var(--text-muted)">${escapeHtml(s.transport)} · ${escapeHtml(s.description || '')}</p>
            </div>
        </label>`
            )
            .join('') || '<p class="empty-hint">No MCP servers — add them in MCP Servers builder.</p>';
    }

    function renderStudioTools() {
        const container = el('studio-tools');
        if (!container || !studioConfig) return;
        const { tool_library, enabled_tools } = studioConfig;
        container.innerHTML = (tool_library || [])
            .map(
                (t) => `
        <label class="skill-assign">
            <input type="checkbox" class="studio-tool-cb" value="${escapeHtml(t.name)}"${
                    (enabled_tools || []).includes(t.name) ? ' checked' : ''
                }>
            <div>
                <strong style="font-size:13px">${escapeHtml(t.name)}</strong>
                <p style="font-size:12px;color:var(--text-muted)">${escapeHtml(t.handler_type)} · ${escapeHtml(t.description || '')}</p>
            </div>
        </label>`
            )
            .join('');
    }

    function updateStudioPreview() {
        const prev = el('studio-preview');
        if (!prev || !studioConfig) return;
        const resolved = studioConfig.resolved_prompts || {};
        const lines = Object.entries(resolved).map(
            ([k, v]) => `## ${k}\n${String(v).slice(0, 300)}`
        );
        const p = studioConfig.resolved_primary_llm;
        const s = studioConfig.resolved_secondary_llm;
        if (p) lines.push(`## primary_llm\n${p.provider} / ${p.model_name}`);
        if (s) lines.push(`## secondary_llm\n${s.provider} / ${s.model_name}`);
        prev.style.display = lines.length ? 'block' : 'none';
        prev.textContent = lines.join('\n\n') || 'No configuration resolved yet.';
    }

    function initAgentStudio() {
        const sel = el('studio-agent-select');
        if (sel) sel.addEventListener('change', () => loadAgentStudio().catch((e) => showToast(e.message, 'err')));

        const saveBtn = el('studio-save-btn');
        if (saveBtn) {
            saveBtn.addEventListener('click', async () => {
                const agentId = el('studio-agent-select')?.value;
                const prompt_bindings = {};
                document.querySelectorAll('.studio-prompt-select').forEach((s) => {
                    if (s.value) prompt_bindings[s.getAttribute('data-slot')] = s.value;
                });
                const enabled_skills = [...document.querySelectorAll('.studio-skill-cb:checked')].map(
                    (cb) => cb.value
                );
                const enabled_tools = [...document.querySelectorAll('.studio-tool-cb:checked')].map(
                    (cb) => cb.value
                );
                const enabled_mcp_servers = [...document.querySelectorAll('.studio-mcp-cb:checked')].map(
                    (cb) => cb.value
                );
                const primary_llm_id = el('studio-primary-llm')?.value || null;
                const secondary_llm_id = el('studio-secondary-llm')?.value || null;
                try {
                    await api('/api/agents/' + encodeURIComponent(agentId) + '/configuration', {
                        method: 'PUT',
                        body: JSON.stringify({
                            prompt_bindings,
                            enabled_skills,
                            enabled_tools,
                            enabled_mcp_servers,
                            primary_llm_id: primary_llm_id || null,
                            secondary_llm_id: secondary_llm_id || null,
                        }),
                    });
                    await loadAgentStudio();
                    showToast('Agent configuration saved');
                } catch (err) {
                    showToast(err.message, 'err');
                }
            });
        }

        const newAgent = el('studio-new-agent-btn');
        if (newAgent) {
            newAgent.addEventListener('click', async () => {
                const name = prompt('Agent name:');
                if (!name?.trim()) return;
                try {
                    const agent = await api('/api/agents', {
                        method: 'POST',
                        body: JSON.stringify({ name: name.trim() }),
                    });
                    await api('/api/agents/activate', {
                        method: 'POST',
                        body: JSON.stringify({ agent_id: agent.id }),
                    });
                    await loadAgentsDropdown();
                    el('studio-agent-select').value = agent.id;
                    await loadAgentStudio();
                    showToast('Created agent: ' + agent.name);
                } catch (err) {
                    showToast(err.message, 'err');
                }
            });
        }
    }

    // --- Observability ---
    function onSessionStarted(sessionId, conversationId) {
        obsLastThreadId = sessionId;
        if (conversationId) obsSelectedConversation = conversationId;
        if (document.getElementById('view-observability')?.classList.contains('active')) {
            if (obsSelectedConversation) {
                loadObservabilityConversation(obsSelectedConversation, true).catch(() => {});
            } else {
                loadObservabilityHierarchy(true).catch(() => {});
            }
        }
        loadObservabilitySessions().catch(() => {});
    }

    function onObservabilityEvent(msg) {
        if (!obsLive) return;
        if (msg.session_id) obsLastThreadId = msg.session_id;
        clearTimeout(obsRefreshTimer);
        obsRefreshTimer = setTimeout(() => {
            if (obsSelectedConversation) {
                loadObservabilityConversation(obsSelectedConversation, true).catch(() => {});
            } else {
                loadObservabilityHierarchy(true).catch(() => {});
            }
        }, 400);
    }

    function expandObsDefaults(node, depth) {
        if (!node) return;
        obsExpanded.add(node.id);
        if (depth < 4) {
            (node.children || []).forEach((c) => expandObsDefaults(c, depth + 1));
        }
    }

    function formatTs(ts) {
        if (!ts) return '—';
        return new Date(ts * 1000).toLocaleTimeString();
    }

    function formatDuration(ms) {
        if (ms == null) return '';
        if (ms < 1000) return ms + 'ms';
        return (ms / 1000).toFixed(2) + 's';
    }

    async function loadObservabilitySessions() {
        const list = el('obs-session-list');
        if (!list) return;
        let convs;
        try {
            convs = await api('/api/observability/conversations');
        } catch (err) {
            list.innerHTML = '<p class="empty-hint">' + escapeHtml(err.message) + '</p>';
            return;
        }
        const allBtn = `<div class="list-card ${!obsSelectedConversation ? 'selected' : ''}" data-obs-all="1">
            <h4>All sessions</h4>
            <p>Platform-wide hierarchy</p>
        </div>`;
        if (!convs.length) {
            list.innerHTML =
                allBtn +
                '<p class="empty-hint">No chat sessions yet. Send a message in Chat first.</p>';
            return;
        }
        list.innerHTML =
            allBtn +
            convs
                .map((c) => {
                    const sel = c.conversation_id === obsSelectedConversation;
                    return `<div class="list-card ${sel ? 'selected' : ''}" data-obs-conv="${escapeHtml(c.conversation_id)}">
            <h4>${escapeHtml(c.title)}</h4>
            <p><span class="tag">${c.thread_count} threads</span> ${c.message_count} messages</p>
        </div>`;
                })
                .join('');
    }

    async function loadObservabilityHierarchy(quiet) {
        obsSelectedConversation = null;
        if (!quiet) await loadObservabilitySessions();
        try {
            obsData = await api('/api/observability/hierarchy');
        } catch (err) {
            if (!quiet) showToast(err.message, 'err');
            return;
        }
        obsExpanded.clear();
        expandObsDefaults(obsData.tree, 0);
        renderObsSummary();
        renderObsTree();
    }

    async function loadObservabilityConversation(convId, quiet) {
        obsSelectedConversation = convId;
        if (!quiet) await loadObservabilitySessions();
        try {
            obsData = await api(
                '/api/observability/conversations/' + encodeURIComponent(convId)
            );
        } catch (err) {
            if (!quiet) showToast(err.message, 'err');
            return;
        }
        obsExpanded.clear();
        expandObsDefaults(obsData.tree, 0);
        renderObsSummary();
        renderObsTree();
        if (obsLive && obsLastThreadId) connectObsWs(obsLastThreadId);
    }

    function renderObsSummary() {
        const box = el('obs-summary');
        if (!box || !obsData) return;
        box.style.display = 'flex';
        const st = obsData.stats || {};
        const title = obsData.title || obsData.conversation_id || 'All sessions';
        box.innerHTML = `
            <span><strong>View</strong> ${escapeHtml(title)}</span>
            <span><strong>Status</strong> ${escapeHtml(obsData.status || '—')}</span>
            <span><strong>Duration</strong> ${formatDuration(obsData.duration_ms)}</span>
            <span><strong>Threads</strong> ${obsData.thread_count ?? '—'}</span>
            <span><strong>Sessions</strong> ${obsData.session_count ?? '—'}</span>
            <span><strong>Events</strong> ${st.total_events ?? '—'}</span>
            <span><strong>Errors</strong> ${st.errors ?? 0}</span>`;
    }

    function renderObsTreeNode(node, depth) {
        const hasChildren = node.children && node.children.length > 0;
        const expanded = obsExpanded.has(node.id);
        const selected = obsSelectedNodeId === node.id;
        const status = node.status || 'info';
        const dur = node.duration_ms != null ? formatDuration(node.duration_ms) : '';
        const spanType = node.attributes?.span_type;
        const meta = [node.kind, spanType, dur].filter(Boolean).join(' · ');
        let html = `<li>
            <div class="obs-node ${selected ? 'selected' : ''}" data-obs-node="${escapeHtml(node.id)}" style="padding-left:${depth * 4}px">
                <span class="toggle ${hasChildren ? '' : 'empty'}" data-obs-toggle="${escapeHtml(node.id)}">${hasChildren ? (expanded ? '▼' : '▶') : ''}</span>
                <span class="obs-badge ${escapeHtml(status)}">${escapeHtml(status)}</span>
                <div style="min-width:0;flex:1">
                    <div style="font-size:13px">${escapeHtml(node.name)}</div>
                    <div class="obs-node-meta">${escapeHtml(meta)}</div>
                </div>
            </div>`;
        if (hasChildren && expanded) {
            html += '<ul>' + node.children.map((c) => renderObsTreeNode(c, depth + 1)).join('') + '</ul>';
        }
        html += '</li>';
        return html;
    }

    function renderObsTree() {
        const treeEl = el('obs-tree');
        const empty = el('obs-tree-empty');
        if (!treeEl || !obsData?.tree) return;
        empty.style.display = 'none';
        treeEl.style.display = 'block';
        treeEl.innerHTML = renderObsTreeNode(obsData.tree, 0);
        treeEl.querySelectorAll('[data-obs-toggle]').forEach((btn) => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const id = btn.getAttribute('data-obs-toggle');
                if (obsExpanded.has(id)) obsExpanded.delete(id);
                else obsExpanded.add(id);
                renderObsTree();
            });
        });
        treeEl.querySelectorAll('[data-obs-node]').forEach((row) => {
            row.addEventListener('click', () => selectObsNode(row.getAttribute('data-obs-node')));
        });
    }

    function findObsNode(node, id) {
        if (!node) return null;
        if (node.id === id) return node;
        for (const c of node.children || []) {
            const found = findObsNode(c, id);
            if (found) return found;
        }
        return null;
    }

    function expandObsDetailPane() {
        const pane = el('obs-pane-detail');
        const toggle = document.querySelector('[data-pane="obs-pane-detail"]');
        if (!pane || !pane.classList.contains('collapsed')) return;
        pane.classList.remove('collapsed');
        if (toggle) toggle.textContent = '▶';
    }

    function selectObsNode(nodeId) {
        obsSelectedNodeId = nodeId;
        renderObsTree();
        const node = findObsNode(obsData?.tree, nodeId);
        const detail = el('obs-detail');
        const empty = el('obs-detail-empty');
        if (!node || !detail) return;
        expandObsDetailPane();
        empty.style.display = 'none';
        detail.style.display = 'block';
        const attrs = node.attributes || {};
        detail.innerHTML = `
            <div class="obs-kind">${escapeHtml(node.kind)}</div>
            <h3 style="font-size:14px;margin:8px 0">${escapeHtml(node.name)}</h3>
            <p><strong>Status</strong> ${escapeHtml(node.status)}</p>
            <p><strong>Start</strong> ${formatTs(node.start_time)}</p>
            <p><strong>End</strong> ${formatTs(node.end_time)}</p>
            <p><strong>Duration</strong> ${formatDuration(node.duration_ms) || '—'}</p>
            ${node.event_type ? '<p><strong>Event</strong> ' + escapeHtml(node.event_type) + '</p>' : ''}
            <pre>${escapeHtml(JSON.stringify(attrs, null, 2))}</pre>`;
    }

    function connectObsWs(sessionId) {
        if (obsWs) obsWs.close();
        if (!sessionId || !obsLive) {
            const dot = el('obs-ws-dot');
            if (dot) dot.classList.remove('on');
            return;
        }
        obsWs = new WebSocket(
            `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/${sessionId}`
        );
        obsWs.onopen = () => el('obs-ws-dot')?.classList.add('on');
        obsWs.onclose = () => el('obs-ws-dot')?.classList.remove('on');
        obsWs.onmessage = (e) => {
            try {
                onObservabilityEvent(JSON.parse(e.data));
            } catch {
                /* ignore */
            }
        };
    }

    async function loadObservability() {
        await loadObservabilitySessions();
        if (obsSelectedConversation) {
            await loadObservabilityConversation(obsSelectedConversation, true);
        } else {
            await loadObservabilityHierarchy(true);
        }
    }

    function initObservability() {
        el('obs-session-list')?.addEventListener('click', (e) => {
            const all = e.target.closest('[data-obs-all]');
            const card = e.target.closest('[data-obs-conv]');
            obsExpanded.clear();
            obsSelectedNodeId = null;
            if (el('obs-detail')) el('obs-detail').style.display = 'none';
            if (el('obs-detail-empty')) el('obs-detail-empty').style.display = 'block';
            if (all) {
                loadObservabilityHierarchy().catch((err) => showToast(err.message, 'err'));
                return;
            }
            if (!card) return;
            const convId = card.getAttribute('data-obs-conv');
            loadObservabilityConversation(convId).catch((err) => showToast(err.message, 'err'));
        });
        el('obs-refresh-btn')?.addEventListener('click', () => {
            loadObservability().catch((err) => showToast(err.message, 'err'));
        });
        el('obs-live-cb')?.addEventListener('change', (e) => {
            obsLive = e.target.checked;
            connectObsWs(obsLastThreadId);
        });
    }

    function initCollapsiblePanes() {
        document.querySelectorAll('.pane-toggle').forEach((btn) => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const pane = el(btn.dataset.pane);
                if (!pane) return;
                pane.classList.toggle('collapsed');
                const id = btn.dataset.pane;
                if (id === 'obs-pane-summary') {
                    btn.textContent = pane.classList.contains('collapsed') ? '▼' : '▲';
                } else if (id === 'obs-pane-detail') {
                    btn.textContent = pane.classList.contains('collapsed') ? '◀' : '▶';
                } else {
                    btn.textContent = pane.classList.contains('collapsed') ? '▶' : '◀';
                }
            });
        });
        el('sidebar-collapse-btn')?.addEventListener('click', () => {
            el('app-sidebar')?.classList.add('collapsed');
        });
        el('sidebar-expand-btn')?.addEventListener('click', () => {
            el('app-sidebar')?.classList.remove('collapsed');
        });
    }

    // --- Boot ---
    function init() {
        initChat();
        initPromptBuilder();
        initSkillBuilder();
        initLlmBuilder();
        initMcpBuilder();
        initToolBuilder();
        initAgentStudio();
        initObservability();
        initCollapsiblePanes();
        updateComposerUI();
        el('user-input')?.focus();

        api('/api/health')
            .then(() => showToast('Connected to AgentOS API', 'ok'))
            .catch((err) => showToast(err.message, 'err'));

        loadAgentsDropdown().catch(() => {});
        loadConversations().catch(() => {});
        api('/api/builders/prompt-slots')
            .then((slots) => {
                promptSlots = slots;
                fillPromptSlotsSelect();
            })
            .catch(() => {});
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();

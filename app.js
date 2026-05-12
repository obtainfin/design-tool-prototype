// ─── Constants ────────────────────────────────────────────────────────────────

const DEFAULT_SYSTEM_PROMPT = `You assist a primary school tutor (students aged 5–11). You help plan lessons, create materials, and solve teaching problems — you don't interact with students directly.

What you help with:
- Lesson plans with clear objectives, timings, and activities
- Worksheets, quizzes, flashcards, and practice problems (with answer keys)
- Explanations of tricky concepts in kid-friendly language the tutor can adapt
- Differentiation ideas for struggling or advanced learners
- Engaging hooks, games, and analogies to make lessons stick
- Quick assessments and progress-tracking ideas
- Parent communication drafts (progress notes, feedback)

Subjects: Maths, English (reading, writing, spelling, grammar), science, HASS. Default to the Australian curriculum unless told otherwise.

If the tutor hasn't told you, ask about: year level, lesson length, topic, group size (1:1 or small group), and any learning needs.

Style:
- Practical and ready-to-use — minimal fluff.
- Age-appropriate language in any student-facing material.
- Include answer keys, time estimates, and materials needed.
- Flag common misconceptions and how to address them.
- Keep it simple. Don't overwhelm with text.

Format: Lead with the deliverable. Use clear headings or numbered steps for lesson plans and worksheets. Keep planning chat conversational and short.`;

const MODELS = [
  { id: 'claude-opus-4-7',          label: 'Claude Opus 4.7 (Most capable)' },
  { id: 'claude-sonnet-4-6',        label: 'Claude Sonnet 4.6 (Balanced)' },
  { id: 'claude-haiku-4-5-20251001',label: 'Claude Haiku 4.5 (Fastest)' },
];

const QUICK_PROMPTS = [
  { icon: '📋', label: 'Lesson plan',        text: 'Create a lesson plan for me. Ask me about year level, topic, lesson length, and group size first.' },
  { icon: '✏️', label: 'Spelling worksheet', text: 'Make a spelling worksheet with activities and an answer key. Ask me the year level and word list (or theme) first.' },
  { icon: '✉️', label: 'Parent note',        text: 'Help me write a parent progress note. Ask me about the student\'s strengths, areas to work on, and tone (formal or friendly).' },
  { icon: '🎮', label: 'Learning game',      text: 'Suggest a fun learning game or activity. Ask me the subject, topic, and year level first.' },
  { icon: '📊', label: 'Quick assessment',   text: 'Create a short formative assessment. Ask me the subject, topic, and year level first.' },
  { icon: '💡', label: 'Explain a concept',  text: 'Help me explain a tricky concept in simple terms. Ask me which concept and the year level first.' },
];

// ─── State ────────────────────────────────────────────────────────────────────

let state = {
  apiKey: '',
  model: MODELS[0].id,
  systemPrompt: DEFAULT_SYSTEM_PROMPT,
  conversations: [],   // [{ id, title, messages: [{role, content}], createdAt }]
  activeId: null,
  streaming: false,
};

function persist() {
  localStorage.setItem('tutor_state', JSON.stringify({
    apiKey: state.apiKey,
    model: state.model,
    systemPrompt: state.systemPrompt,
    conversations: state.conversations,
    activeId: state.activeId,
  }));
}

function load() {
  try {
    const raw = localStorage.getItem('tutor_state');
    if (!raw) return;
    const saved = JSON.parse(raw);
    Object.assign(state, saved);
  } catch { /* ignore */ }
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function uid() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
}

function activeConv() {
  return state.conversations.find(c => c.id === state.activeId) || null;
}

function renderMarkdown(text) {
  const raw = marked.parse(text, { breaks: true });
  return DOMPurify.sanitize(raw);
}

function escapeHtml(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ─── Sidebar ──────────────────────────────────────────────────────────────────

function renderSidebar() {
  const list = document.getElementById('conv-list');
  list.innerHTML = '';
  const sorted = [...state.conversations].sort((a, b) => b.createdAt - a.createdAt);
  sorted.forEach(conv => {
    const li = document.createElement('li');
    li.dataset.id = conv.id;
    li.className = `conv-item group flex items-center gap-2 px-3 py-2 rounded-lg cursor-pointer text-sm transition-colors
      ${conv.id === state.activeId ? 'bg-terracotta/10 text-terracotta font-medium' : 'hover:bg-paper-dark text-ink/70 hover:text-ink'}`;

    const title = document.createElement('span');
    title.className = 'flex-1 truncate';
    title.textContent = conv.title || 'New chat';

    const actions = document.createElement('span');
    actions.className = 'hidden group-hover:flex gap-1 shrink-0';

    const renameBtn = document.createElement('button');
    renameBtn.innerHTML = '✏️';
    renameBtn.title = 'Rename';
    renameBtn.className = 'text-xs p-0.5 rounded hover:bg-paper-dark';
    renameBtn.addEventListener('click', e => { e.stopPropagation(); startRename(conv.id); });

    const deleteBtn = document.createElement('button');
    deleteBtn.innerHTML = '🗑';
    deleteBtn.title = 'Delete';
    deleteBtn.className = 'text-xs p-0.5 rounded hover:bg-red-100';
    deleteBtn.addEventListener('click', e => { e.stopPropagation(); deleteConv(conv.id); });

    actions.append(renameBtn, deleteBtn);
    li.append(title, actions);
    li.addEventListener('click', () => switchConv(conv.id));
    list.appendChild(li);
  });
}

function startRename(id) {
  const conv = state.conversations.find(c => c.id === id);
  if (!conv) return;
  const newTitle = prompt('Rename conversation:', conv.title || 'New chat');
  if (newTitle !== null) {
    conv.title = newTitle.trim() || 'New chat';
    persist();
    renderSidebar();
  }
}

function deleteConv(id) {
  if (!confirm('Delete this conversation?')) return;
  state.conversations = state.conversations.filter(c => c.id !== id);
  if (state.activeId === id) {
    state.activeId = state.conversations[0]?.id || null;
  }
  persist();
  renderSidebar();
  renderChat();
}

function switchConv(id) {
  state.activeId = id;
  persist();
  renderSidebar();
  renderChat();
  // Close sidebar on mobile
  document.getElementById('sidebar').classList.add('-translate-x-full');
  document.getElementById('sidebar-overlay').classList.add('hidden');
}

function newChat() {
  const conv = { id: uid(), title: 'New chat', messages: [], createdAt: Date.now() };
  state.conversations.unshift(conv);
  state.activeId = conv.id;
  persist();
  renderSidebar();
  renderChat();
}

// ─── Chat rendering ───────────────────────────────────────────────────────────

function renderChat() {
  const conv = activeConv();
  const messages = document.getElementById('messages');
  const empty = document.getElementById('empty-state');
  const inputArea = document.getElementById('input-area');

  if (!conv || conv.messages.length === 0) {
    messages.innerHTML = '';
    empty.classList.remove('hidden');
    inputArea.classList.remove('hidden');
    return;
  }

  empty.classList.add('hidden');
  messages.innerHTML = '';

  conv.messages.forEach(msg => {
    messages.appendChild(buildMessageEl(msg.role, msg.content));
  });

  messages.scrollTop = messages.scrollHeight;
}

function buildMessageEl(role, content, streaming = false) {
  const wrap = document.createElement('div');
  wrap.className = `flex gap-3 ${role === 'user' ? 'justify-end' : 'justify-start'} message-row`;

  if (role === 'assistant') {
    const avatar = document.createElement('div');
    avatar.className = 'shrink-0 w-8 h-8 rounded-full bg-terracotta/15 flex items-center justify-center text-sm';
    avatar.textContent = '🎓';
    wrap.appendChild(avatar);
  }

  const bubble = document.createElement('div');
  bubble.className = role === 'user'
    ? 'max-w-[80%] bg-terracotta text-white px-4 py-3 rounded-2xl rounded-tr-sm text-sm leading-relaxed'
    : 'max-w-[85%] bg-white border border-stone-200 px-4 py-3 rounded-2xl rounded-tl-sm text-sm leading-relaxed shadow-sm';

  const contentDiv = document.createElement('div');
  contentDiv.className = role === 'assistant' ? 'prose prose-sm max-w-none prose-headings:font-fraunces prose-headings:text-ink' : '';
  if (role === 'assistant') {
    contentDiv.innerHTML = renderMarkdown(content);
  } else {
    contentDiv.textContent = content;
  }
  bubble.appendChild(contentDiv);

  if (role === 'assistant' && !streaming) {
    const actions = document.createElement('div');
    actions.className = 'mt-2 flex justify-end';
    const copyBtn = document.createElement('button');
    copyBtn.className = 'text-xs text-ink/40 hover:text-terracotta transition-colors flex items-center gap-1';
    copyBtn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>Copy';
    copyBtn.addEventListener('click', () => {
      navigator.clipboard.writeText(content).then(() => {
        copyBtn.textContent = 'Copied!';
        setTimeout(() => {
          copyBtn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>Copy';
        }, 2000);
      });
    });
    actions.appendChild(copyBtn);
    bubble.appendChild(actions);
  }

  wrap.appendChild(bubble);

  if (role === 'user') {
    const avatar = document.createElement('div');
    avatar.className = 'shrink-0 w-8 h-8 rounded-full bg-stone-200 flex items-center justify-center text-sm';
    avatar.textContent = '👩‍🏫';
    wrap.appendChild(avatar);
  }

  return wrap;
}

// ─── Streaming API call ───────────────────────────────────────────────────────

async function sendMessage(userText) {
  if (!state.apiKey) { openSettings(); return; }
  if (state.streaming) return;

  let conv = activeConv();
  if (!conv) {
    newChat();
    conv = activeConv();
  }

  conv.messages.push({ role: 'user', content: userText });

  // Auto-title from first message
  if (conv.messages.length === 1) {
    conv.title = userText.slice(0, 50) + (userText.length > 50 ? '…' : '');
    renderSidebar();
  }

  persist();
  renderChat();

  // Append streaming bubble
  const messages = document.getElementById('messages');
  const empty = document.getElementById('empty-state');
  empty.classList.add('hidden');

  const streamEl = buildMessageEl('assistant', '', true);
  const contentDiv = streamEl.querySelector('div > div');
  messages.appendChild(streamEl);
  messages.scrollTop = messages.scrollHeight;

  state.streaming = true;
  updateSendButton();

  let accumulated = '';

  try {
    const response = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: {
        'x-api-key': state.apiKey,
        'anthropic-version': '2023-06-01',
        'anthropic-dangerous-direct-browser-access': 'true',
        'content-type': 'application/json',
      },
      body: JSON.stringify({
        model: state.model,
        max_tokens: 4096,
        system: state.systemPrompt,
        messages: conv.messages,
        stream: true,
      }),
    });

    if (!response.ok) {
      const errBody = await response.json().catch(() => ({}));
      let msg = errBody?.error?.message || `HTTP ${response.status}`;
      if (response.status === 401) msg = 'Invalid API key. Check your key in Settings.';
      else if (response.status === 429) msg = 'Rate limit hit. Wait a moment and try again.';
      throw new Error(msg);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop(); // keep incomplete line

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const data = line.slice(6).trim();
        if (data === '[DONE]') continue;
        try {
          const parsed = JSON.parse(data);
          if (parsed.type === 'content_block_delta' && parsed.delta?.type === 'text_delta') {
            accumulated += parsed.delta.text;
            contentDiv.innerHTML = renderMarkdown(accumulated);
            messages.scrollTop = messages.scrollHeight;
          }
        } catch { /* skip malformed */ }
      }
    }

    // Replace streaming bubble with final rendered message
    const finalEl = buildMessageEl('assistant', accumulated, false);
    messages.replaceChild(finalEl, streamEl);

    conv.messages.push({ role: 'assistant', content: accumulated });
    persist();
    messages.scrollTop = messages.scrollHeight;

  } catch (err) {
    const errEl = document.createElement('div');
    errEl.className = 'flex justify-center';
    errEl.innerHTML = `<div class="bg-red-50 border border-red-200 text-red-700 text-sm px-4 py-2 rounded-lg">${escapeHtml(err.message)}</div>`;
    messages.replaceChild(errEl, streamEl);
    // Remove the optimistically added user message on error
    conv.messages.pop();
    persist();
  } finally {
    state.streaming = false;
    updateSendButton();
  }
}

function updateSendButton() {
  const btn = document.getElementById('send-btn');
  if (state.streaming) {
    btn.disabled = true;
    btn.innerHTML = `<svg class="animate-spin w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>`;
  } else {
    btn.disabled = false;
    btn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>`;
  }
}

// ─── Settings modal ───────────────────────────────────────────────────────────

function openSettings() {
  const modal = document.getElementById('settings-modal');
  document.getElementById('settings-api-key').value = state.apiKey;
  document.getElementById('settings-model').value = state.model;
  document.getElementById('settings-system-prompt').value = state.systemPrompt;
  modal.classList.remove('hidden');
  modal.classList.add('flex');
}

function closeSettings() {
  const modal = document.getElementById('settings-modal');
  modal.classList.add('hidden');
  modal.classList.remove('flex');
}

function saveSettings() {
  state.apiKey = document.getElementById('settings-api-key').value.trim();
  state.model = document.getElementById('settings-model').value;
  state.systemPrompt = document.getElementById('settings-system-prompt').value.trim() || DEFAULT_SYSTEM_PROMPT;
  persist();
  closeSettings();
}

// ─── Welcome modal ────────────────────────────────────────────────────────────

function showWelcome() {
  document.getElementById('welcome-modal').classList.remove('hidden');
  document.getElementById('welcome-modal').classList.add('flex');
}

function closeWelcome() {
  document.getElementById('welcome-modal').classList.add('hidden');
  document.getElementById('welcome-modal').classList.remove('flex');
}

// ─── Quick prompts ────────────────────────────────────────────────────────────

function renderQuickPrompts() {
  const container = document.getElementById('quick-prompts');
  container.innerHTML = '';
  QUICK_PROMPTS.forEach(qp => {
    const btn = document.createElement('button');
    btn.className = 'flex items-center gap-2 px-4 py-2.5 bg-white border border-stone-200 rounded-xl text-sm text-ink/70 hover:border-terracotta hover:text-terracotta transition-colors shadow-sm text-left';
    btn.innerHTML = `<span>${qp.icon}</span><span>${escapeHtml(qp.label)}</span>`;
    btn.addEventListener('click', () => {
      document.getElementById('user-input').value = qp.text;
      submitMessage();
    });
    container.appendChild(btn);
  });
}

// ─── Input handling ───────────────────────────────────────────────────────────

function submitMessage() {
  const input = document.getElementById('user-input');
  const text = input.value.trim();
  if (!text || state.streaming) return;
  input.value = '';
  autoResizeTextarea(input);
  sendMessage(text);
}

function autoResizeTextarea(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 200) + 'px';
}

// ─── Bootstrap ────────────────────────────────────────────────────────────────

function init() {
  load();

  // Sidebar new-chat button
  document.getElementById('new-chat-btn').addEventListener('click', newChat);

  // Mobile sidebar toggle
  document.getElementById('sidebar-toggle').addEventListener('click', () => {
    document.getElementById('sidebar').classList.toggle('-translate-x-full');
    document.getElementById('sidebar-overlay').classList.toggle('hidden');
  });
  document.getElementById('sidebar-overlay').addEventListener('click', () => {
    document.getElementById('sidebar').classList.add('-translate-x-full');
    document.getElementById('sidebar-overlay').classList.add('hidden');
  });

  // Settings
  document.getElementById('settings-btn').addEventListener('click', openSettings);
  document.getElementById('settings-close').addEventListener('click', closeSettings);
  document.getElementById('settings-save').addEventListener('click', saveSettings);
  document.getElementById('settings-reset-prompt').addEventListener('click', () => {
    document.getElementById('settings-system-prompt').value = DEFAULT_SYSTEM_PROMPT;
  });
  document.getElementById('settings-clear-data').addEventListener('click', () => {
    if (!confirm('Delete all conversations and settings? This cannot be undone.')) return;
    localStorage.removeItem('tutor_state');
    location.reload();
  });
  document.getElementById('settings-modal').addEventListener('click', e => {
    if (e.target === e.currentTarget) closeSettings();
  });

  // Model picker population
  const modelSelect = document.getElementById('settings-model');
  MODELS.forEach(m => {
    const opt = document.createElement('option');
    opt.value = m.id;
    opt.textContent = m.label;
    modelSelect.appendChild(opt);
  });

  // Welcome
  document.getElementById('welcome-close').addEventListener('click', () => {
    closeWelcome();
    openSettings();
  });
  document.getElementById('welcome-modal').addEventListener('click', e => {
    if (e.target === e.currentTarget) closeWelcome();
  });

  // Input
  const input = document.getElementById('user-input');
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submitMessage();
    }
  });
  input.addEventListener('input', () => autoResizeTextarea(input));
  document.getElementById('send-btn').addEventListener('click', submitMessage);

  // Render quick prompts
  renderQuickPrompts();

  // Initial render
  if (state.conversations.length === 0 || !state.activeId) {
    if (state.conversations.length > 0) {
      state.activeId = state.conversations[0].id;
    }
  }

  renderSidebar();
  renderChat();

  // Show welcome if no API key
  if (!state.apiKey) {
    showWelcome();
  }
}

document.addEventListener('DOMContentLoaded', init);

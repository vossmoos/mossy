"""Web UI channel for Mossy.

Registers GET /ui — a self-contained HTML chat page that:
  1. Asks the user for their Mossy API key.
  2. On success, opens a GPT/Claude-style chat that streams from AG-UI.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import HTMLResponse

if TYPE_CHECKING:
    pass  # no runtime import needed


_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Chat</title>
<style>
  :root {
    --bg:        #faf8f3;
    --surface:   #fffef9;
    --border:    #b8d4bc;
    --accent:    #3d7a52;
    --accent-hi: #4d9464;
    --text:      #1a1a1a;
    --muted:     #5c6b5e;
    --user-bg:   #fffef9;
    --bot-bg:    #fffef9;
    --input-bg:  #fffef9;
    --radius:    12px;
    --font:      -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; background: var(--bg); color: var(--text); font-family: var(--font); }

  /* ── Auth screen ── */
  #auth-screen {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    height: 100%;
    gap: 24px;
    padding: 24px;
  }
  #auth-box {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 32px;
    width: 100%;
    max-width: 380px;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  #auth-box label { font-size: 0.85rem; color: var(--muted); }
  #key-input {
    width: 100%;
    background: var(--input-bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    font-size: 1rem;
    padding: 10px 14px;
    outline: none;
    transition: border-color 0.15s;
  }
  #key-input:focus { border-color: var(--accent); }
  #auth-btn {
    background: var(--accent);
    color: #fff;
    border: none;
    border-radius: 8px;
    padding: 11px;
    font-size: 0.95rem;
    font-weight: 600;
    cursor: pointer;
    transition: background 0.15s;
  }
  #auth-btn:hover { background: var(--accent-hi); }
  #auth-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  #auth-error { font-size: 0.82rem; color: #c0392b; min-height: 1.2em; }

  /* ── Chat layout ── */
  #chat-screen {
    display: none;
    flex-direction: column;
    height: 100%;
  }

  /* header */
  #chat-header {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 14px 20px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
    flex-shrink: 0;
  }
  #chat-header .name { font-weight: 700; font-size: 1rem; color: var(--accent); }
  #chat-header .thread-label {
    font-size: 0.73rem;
    color: var(--muted);
    margin-left: auto;
    font-family: monospace;
  }
  #new-chat-btn,
  #files-btn {
    background: none;
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--muted);
    font-size: 0.78rem;
    padding: 4px 10px;
    cursor: pointer;
    transition: border-color 0.15s, color 0.15s;
    white-space: nowrap;
  }
  #new-chat-btn:hover,
  #files-btn:hover { border-color: var(--accent); color: var(--accent); }

  /* messages */
  #messages {
    flex: 1;
    overflow-y: auto;
    padding: 24px 0;
    scroll-behavior: smooth;
  }
  .msg-row {
    display: flex;
    padding: 6px 24px;
    gap: 12px;
    max-width: 820px;
    margin: 0 auto;
    width: 100%;
  }
  .avatar {
    width: 30px;
    height: 30px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.75rem;
    font-weight: 700;
    flex-shrink: 0;
    margin-top: 2px;
  }
  .avatar.user  { background: var(--surface); color: var(--muted); border: 1px solid var(--border); }
  .avatar.bot   { background: var(--accent); color: #fff; border: 1px solid var(--accent); }
  .bubble {
    font-size: 0.92rem;
    line-height: 1.65;
    color: var(--text);
    word-break: break-word;
    background: var(--bot-bg);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 10px 14px;
  }
  .bubble.user-msg {
    white-space: pre-wrap;
    background: var(--user-bg);
  }
  .bubble.thinking { color: var(--muted); font-style: italic; background: transparent; border-color: transparent; padding: 0; }
  .bubble.html p { margin: 0 0 0.55em; }
  .bubble.html p:last-child { margin-bottom: 0; }
  .bubble.html ul, .bubble.html ol { margin: 0.45em 0; padding-left: 1.35em; }
  .bubble.html li { margin: 0.2em 0; }
  .bubble.html strong { font-weight: 600; }
  .bubble.html a { color: var(--accent); }
  .msg-content { flex: 1; display: flex; flex-direction: column; gap: 4px; }
  .msg-meta { font-size: 0.72rem; color: var(--muted); }

  /* input bar */
  #input-bar {
    border-top: 1px solid var(--border);
    background: var(--surface);
    padding: 16px 24px;
    flex-shrink: 0;
  }
  #input-wrap {
    display: flex;
    align-items: flex-end;
    gap: 10px;
    max-width: 820px;
    margin: 0 auto;
    background: var(--input-bg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 10px 14px;
    transition: border-color 0.15s;
  }
  #input-wrap:focus-within { border-color: var(--accent); }
  #msg-input {
    flex: 1;
    background: none;
    border: none;
    outline: none;
    color: var(--text);
    font-size: 0.94rem;
    font-family: var(--font);
    resize: none;
    max-height: 180px;
    overflow-y: auto;
    line-height: 1.5;
  }
  #send-btn {
    background: var(--accent);
    border: none;
    border-radius: 7px;
    width: 34px;
    height: 34px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    transition: background 0.15s, opacity 0.15s;
  }
  #send-btn:hover { background: var(--accent-hi); }
  #send-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  #send-btn svg { width: 16px; height: 16px; fill: #fff; }
</style>
</head>
<body>

<!-- ── Auth screen ── -->
<div id="auth-screen">
  <div id="auth-box">
    <label for="key-input">API Key</label>
    <input id="key-input" type="password" placeholder="Enter API key" autocomplete="off" />
    <button id="auth-btn">Connect</button>
    <div id="auth-error"></div>
  </div>
</div>

<!-- ── Chat screen ── -->
<div id="chat-screen">
  <div id="chat-header">
    <span class="name">Chat</span>
    <span class="thread-label" id="thread-label"></span>
    <button id="files-btn">Files</button>
    <button id="new-chat-btn">+ New chat</button>
  </div>
  <div id="messages"></div>
  <div id="input-bar">
    <div id="input-wrap">
      <textarea id="msg-input" rows="1" placeholder="Message…"></textarea>
      <button id="send-btn" title="Send">
        <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
          <path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/>
        </svg>
      </button>
    </div>
  </div>
</div>

<script>
(function () {
  const $ = id => document.getElementById(id);
  const authScreen  = $('auth-screen');
  const chatScreen  = $('chat-screen');
  const keyInput    = $('key-input');
  const authBtn     = $('auth-btn');
  const authError   = $('auth-error');
  const messagesEl  = $('messages');
  const msgInput    = $('msg-input');
  const sendBtn     = $('send-btn');
  const threadLabel = $('thread-label');
  const newChatBtn  = $('new-chat-btn');
  const filesBtn    = $('files-btn');

  // Derive base URL from current page so this works on any deployment
  const BASE = window.location.origin;
  const AGUI_PATH = __AGUI_PATH__;

  let apiKey   = sessionStorage.getItem('mossy_key') || '';
  let threadId = null;
  let aguiMessages = [];
  let messageSeq = 0;
  let busy     = false;

  // ── Restore session ──────────────────────────────────────────────
  if (apiKey) showChat();

  // ── Auth ─────────────────────────────────────────────────────────
  authBtn.addEventListener('click', doAuth);
  keyInput.addEventListener('keydown', e => { if (e.key === 'Enter') doAuth(); });

  async function doAuth() {
    const k = keyInput.value.trim();
    if (!k) return;
    authBtn.disabled = true;
    authError.textContent = '';
    try {
      // Probe a protected endpoint without spending a model turn.
      const res = await fetch(`${BASE}/queue`, {
        headers: { 'Authorization': `Bearer ${k}` },
      });
      if (res.status === 401) throw new Error('Invalid API key.');
      if (!res.ok) throw new Error(`Server error (${res.status}).`);
      apiKey = k;
      sessionStorage.setItem('mossy_key', k);
      showChat();
    } catch (err) {
      authError.textContent = err.message || 'Connection failed.';
    } finally {
      authBtn.disabled = false;
    }
  }

  function showChat() {
    authScreen.style.display  = 'none';
    chatScreen.style.display  = 'flex';
    msgInput.focus();
  }

  // ── New chat ──────────────────────────────────────────────────────
  newChatBtn.addEventListener('click', () => {
    threadId = null;
    aguiMessages = [];
    messageSeq = 0;
    threadLabel.textContent = '';
    messagesEl.innerHTML = '';
    msgInput.focus();
  });
  filesBtn.addEventListener('click', showFiles);
  messagesEl.addEventListener('click', e => {
    const link = e.target.closest('[data-download-path]');
    if (!link) return;
    e.preventDefault();
    downloadArchiveFile(link.dataset.downloadPath);
  });

  // ── Textarea auto-grow ────────────────────────────────────────────
  msgInput.addEventListener('input', () => {
    msgInput.style.height = 'auto';
    msgInput.style.height = msgInput.scrollHeight + 'px';
  });

  // ── Send on Enter (Shift+Enter = newline) ─────────────────────────
  msgInput.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });
  sendBtn.addEventListener('click', send);

  // ── Send message ─────────────────────────────────────────────────
  async function send() {
    const text = msgInput.value.trim();
    if (!text || busy) return;

    busy = true;
    sendBtn.disabled = true;
    msgInput.value = '';
    msgInput.style.height = 'auto';

    appendMsg('user', text);
    const thinkEl = appendMsg('bot', 'Thinking.', true);
    const stopThinking = startThinkingDots(thinkEl);
    scrollBottom();

    const t0 = Date.now();
    const currentThreadId = threadId || newId('thread');
    const userMessage = {
      id: newId('msg'),
      role: 'user',
      content: text,
    };
    const requestMessages = [...aguiMessages, userMessage];
    try {
      const res = await fetch(`${BASE}${AGUI_PATH}`, {
        method: 'POST',
        headers: {
          'Accept': 'text/event-stream',
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${apiKey}`,
        },
        body: JSON.stringify({
          threadId: currentThreadId,
          runId: newId('run'),
          messages: requestMessages,
          state: {},
          context: [],
          tools: [],
          forwardedProps: {},
        }),
      });

      if (res.status === 401) {
        sessionStorage.removeItem('mossy_key');
        apiKey = '';
        chatScreen.style.display = 'none';
        authScreen.style.display = 'flex';
        authError.textContent = 'Session expired — please reconnect.';
        stopThinking();
        thinkEl.remove();
        return;
      }

      if (!res.ok) {
        const detail = await res.text().catch(() => res.status);
        stopThinking();
        thinkEl.classList.remove('thinking');
        thinkEl.textContent = `Error: ${detail}`;
        return;
      }

      stopThinking();
      thinkEl.classList.remove('thinking');
      thinkEl.textContent = '';
      let reply = '';
      await readAguiStream(res, chunk => {
        if (!chunk) return;
        if (!reply) thinkEl.classList.remove('thinking');
        reply += chunk;
        thinkEl.textContent = reply;
        scrollBottom();
      }, status => {
        if (reply) return;  // answer is already streaming — don't overwrite it
        thinkEl.classList.add('thinking');
        thinkEl.textContent = status;
        scrollBottom();
      });
      if (!threadId) {
        threadId = currentThreadId;
        threadLabel.textContent = `thread: ${threadId.slice(0, 8)}…`;
      }
      aguiMessages = [
        ...requestMessages,
        { id: newId('msg'), role: 'assistant', content: reply },
      ];
      const secs = Math.round((Date.now() - t0) / 1000);
      setBubbleHtml(thinkEl, formatBotText(reply || '(no reply)'));
      appendMeta(thinkEl.closest('.msg-row'), `${secs}s`);
    } catch (err) {
      stopThinking();
      thinkEl.classList.remove('thinking');
      thinkEl.textContent = `Stream error: ${err.message}`;
    } finally {
      stopThinking();
      busy = false;
      sendBtn.disabled = false;
      scrollBottom();
      msgInput.focus();
    }
  }

  async function showFiles() {
    if (!apiKey) return;
    const bubble = appendMsg('bot', 'Loading files.', true);
    try {
      const res = await fetch(`${BASE}/files`, {
        headers: { 'Authorization': `Bearer ${apiKey}` },
      });
      if (res.status === 401) {
        sessionStorage.removeItem('mossy_key');
        apiKey = '';
        chatScreen.style.display = 'none';
        authScreen.style.display = 'flex';
        authError.textContent = 'Session expired — please reconnect.';
        bubble.remove();
        return;
      }
      if (!res.ok) {
        bubble.classList.remove('thinking');
        bubble.textContent = `Could not load files (${res.status}).`;
        return;
      }
      const data = await res.json();
      bubble.classList.remove('thinking');
      setBubbleHtml(bubble, renderSharedFiles(data));
    } catch (err) {
      bubble.classList.remove('thinking');
      bubble.textContent = `Could not load files: ${err.message}`;
    } finally {
      scrollBottom();
    }
  }

  function renderSharedFiles(data) {
    const entries = data.entries || [];
    if (!entries.length) return '<p>No shared files yet.</p>';
    const items = entries.map(entry => {
      const label = `${entry.is_dir ? 'folder' : 'file'} ${escapeHtml(entry.path)}`;
      if (entry.is_dir) return `<li>${label}</li>`;
      const size = typeof entry.bytes === 'number' ? ` (${entry.bytes} bytes)` : '';
      return `<li><a href="#" data-download-path="${escapeHtml(entry.path)}">${label}${size}</a></li>`;
    }).join('');
    const more = data.truncated ? '<p>List truncated. Ask Mossy for a narrower folder.</p>' : '';
    return `<p>Shared files:</p><ul>${items}</ul>${more}`;
  }

  async function downloadArchiveFile(path) {
    try {
      const res = await fetch(`${BASE}/files/${encodeURIComponent(path).replaceAll('%2F', '/')}`, {
        headers: { 'Authorization': `Bearer ${apiKey}` },
      });
      if (res.status === 401) {
        sessionStorage.removeItem('mossy_key');
        apiKey = '';
        chatScreen.style.display = 'none';
        authScreen.style.display = 'flex';
        authError.textContent = 'Session expired — please reconnect.';
        return;
      }
      if (!res.ok) throw new Error(`download failed (${res.status})`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = path.split('/').pop() || 'download';
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      appendMsg('bot', `Could not download ${escapeHtml(path)}: ${escapeHtml(err.message)}`);
      scrollBottom();
    }
  }

  async function readAguiStream(res, onText, onStatus) {
    if (!res.body) throw new Error('Streaming is not supported by this browser.');

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    const ctx = { toolArgs: new Map() };  // toolCallId -> {name, args}

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replaceAll('\\r\\n', '\\n');

      let boundary = buffer.indexOf('\\n\\n');
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        handleSseFrame(frame, onText, onStatus, ctx);
        boundary = buffer.indexOf('\\n\\n');
      }
    }

    buffer += decoder.decode().replaceAll('\\r\\n', '\\n');
    if (buffer.trim()) handleSseFrame(buffer, onText, onStatus, ctx);
  }

  function handleSseFrame(frame, onText, onStatus, ctx) {
    const data = frame
      .split('\\n')
      .filter(line => line.startsWith('data:'))
      .map(line => line.slice(5).trimStart())
      .join('\\n');
    if (!data || data === '[DONE]') return;

    let event;
    try {
      event = JSON.parse(data);
    } catch (err) {
      throw new Error(`Invalid AG-UI event: ${err.message}`);
    }
    if (event.type === 'RUN_ERROR' || event.type === 'ERROR') {
      throw new Error(event.message || event.error || 'AG-UI stream error');
    }

    switch (event.type) {
      case 'TEXT_MESSAGE_CONTENT': {
        const chunk = event.delta ?? event.content ?? event.text ?? '';
        if (chunk) onText(chunk);
        break;
      }
      case 'TOOL_CALL_START': {
        const rec = { name: event.toolCallName || 'tool', args: '' };
        if (ctx) ctx.toolArgs.set(event.toolCallId, rec);
        if (onStatus) onStatus(statusLabel(rec.name));
        break;
      }
      case 'TOOL_CALL_ARGS': {
        const rec = ctx && ctx.toolArgs.get(event.toolCallId);
        if (rec && onStatus) {
          rec.args += event.delta ?? '';
          // Surface the concrete script for the generic skill-runner tool.
          const m = rec.args.match(/"script_name"\\s*:\\s*"([^"]+)"/);
          if (m) onStatus(statusLabel(rec.name, m[1]));
        }
        break;
      }
      default:
        break;
    }
  }

  function statusLabel(tool, script) {
    if (script) return `Running ${script}…`;
    const map = {
      run_skill_script: 'Running a skill script…',
      load_skill: 'Loading skill instructions…',
      list_skills: 'Looking up skills…',
      read_skill_resource: 'Reading a reference…',
      write_file: 'Writing a file…',
      append_file: 'Writing a section…',
      read_file: 'Reading a file…',
      list_dir: 'Listing files…',
      delete_file: 'Deleting a file…',
      zip_files: 'Building the archive…',
      unzip_file: 'Extracting an archive…',
      list_zip: 'Inspecting an archive…',
      share_file: 'Preparing the download…',
      list_shared_files: 'Listing shared files…',
      get_download_info: 'Preparing the download…',
      unshare_file: 'Removing a shared file…',
    };
    return map[tool] || `Working… (${tool})`;
  }

  // ── Helpers ───────────────────────────────────────────────────────
  function newId(prefix) {
    const random = window.crypto?.randomUUID?.() || `${Date.now()}-${++messageSeq}`;
    return `${prefix}-${random}`;
  }

  function formatBotText(value) {
    let html = escapeHtml(value).replaceAll('\\n', '<br>');
    // Turn protected /files/<path> references into authenticated download links.
    // The /files endpoint needs the bearer key, so a plain href would 401 — route
    // through data-download-path, which the click handler fetches with the key.
    html = html.replace(/`?(\/files\/[A-Za-z0-9._~%\/-]+)`?/g, (_m, url) => {
      const rel = url.replace(/^\/files\//, '');
      const name = rel.split('/').pop() || rel;
      return `<a href="#" data-download-path="${rel}">⬇ ${name}</a>`;
    });
    return html;
  }

  function appendMeta(row, timeStr) {
    const content = row.querySelector('.msg-content');
    const meta = document.createElement('div');
    meta.className = 'msg-meta';
    meta.textContent = `spent: ${timeStr}`;
    content.appendChild(meta);
  }

  function setBubbleHtml(bubble, html) {
    bubble.classList.add('html');
    bubble.innerHTML = html;
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#39;');
  }

  function startThinkingDots(el) {
    let dots = 1;
    let stopped = false;
    const render = () => {
      el.textContent = `Thinking${'.'.repeat(dots)}`;
      dots = dots === 3 ? 1 : dots + 1;
    };
    render();
    const intervalId = window.setInterval(render, 450);
    return () => {
      if (stopped) return;
      stopped = true;
      window.clearInterval(intervalId);
    };
  }

  function appendMsg(role, text, isThinking = false) {
    const row = document.createElement('div');
    row.className = 'msg-row';

    const av = document.createElement('div');
    av.className = `avatar ${role === 'user' ? 'user' : 'bot'}`;
    av.textContent = role === 'user' ? 'You' : 'M';

    const content = document.createElement('div');
    content.className = 'msg-content';

    const bubble = document.createElement('div');
    bubble.className = 'bubble' + (isThinking ? ' thinking' : '');
    if (role === 'user') {
      bubble.classList.add('user-msg');
      bubble.textContent = text;
    } else if (isThinking) {
      bubble.textContent = text;
    } else {
      setBubbleHtml(bubble, text);
    }

    content.appendChild(bubble);
    row.appendChild(av);
    row.appendChild(content);
    messagesEl.appendChild(row);
    return bubble;
  }

  function scrollBottom() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }
})();
</script>
</body>
</html>
"""


def register_web_routes(app: FastAPI, *, agui_path: str = "/agui") -> None:
    """Mount the browser chat UI at GET /ui."""
    html = _HTML.replace("__AGUI_PATH__", json.dumps(agui_path))

    @app.get("/ui", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/ui/", response_class=HTMLResponse, include_in_schema=False)
    async def web_ui(request: Request) -> HTMLResponse:  # noqa: ARG001
        return HTMLResponse(html)

"""Adaptive UI channel for Mossy.

Registers:
  GET  /aui       — chat page with an adaptive side panel of widget boxes.
  POST /aui/run   — AG-UI endpoint whose agent has the ui-panel toolset attached.

The panel is skill-agnostic: it renders a fixed widget vocabulary (card_grid,
table, markdown, progress, log, document) from AG-UI STATE_SNAPSHOT/STATE_DELTA
events emitted by the panel_* tools. What to show is decided by skills — see
README.md in this folder for the skill-authoring contract.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from mossy.capabilities.ui_panel import ui_panel_capability
from mossy.channels.agui.app import AguiChannel

if TYPE_CHECKING:
    from mossy.runtime import Runtime


_PANEL_INSTRUCTIONS = """\
This channel renders an adaptive panel of widget boxes next to the chat (ui-panel tools).
Use it whenever a skill or task produces structured, browsable, or long-running output:
entity teasers as card_grid (with detail popups and action buttons), tabular data as table,
long-running work as progress or log boxes, and shared files as document boxes (share the
file first, then reference its shared path).

Rules:
- The chat reply must stand alone: a user on a plain chat channel must get the same substance.
- Re-use a box_id to update that box in place (progress updates, refreshed teaser lists).
- User messages starting with '[panel action]' are widget button clicks; resolve them
  according to the skill that created the button, using the ids and payload in the message.
- Do not narrate panel mechanics to the user; just use the tools."""


def _aui_run_path() -> str:
    path = os.getenv("AUI_RUN_PATH", "/aui/run").strip() or "/aui/run"
    if not path.startswith("/"):
        path = f"/{path}"
    return path.rstrip("/") or "/aui/run"


_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Chat + Panel</title>
<style>
  :root {
    --bg:        #faf8f3;
    --surface:   #fffef9;
    --border:    #b8d4bc;
    --accent:    #3d7a52;
    --accent-hi: #4d9464;
    --danger:    #c0392b;
    --text:      #1a1a1a;
    --muted:     #5c6b5e;
    --user-bg:   #fffef9;
    --bot-bg:    #fffef9;
    --input-bg:  #fffef9;
    --radius:    12px;
    --font:      -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    --mono:      ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; background: var(--bg); color: var(--text); font-family: var(--font); }

  /* ── Auth screen ── */
  #auth-screen {
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    height: 100%; gap: 24px; padding: 24px;
  }
  #auth-box {
    background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 32px; width: 100%; max-width: 380px; display: flex; flex-direction: column; gap: 16px;
  }
  #auth-box label { font-size: 0.85rem; color: var(--muted); }
  #key-input {
    width: 100%; background: var(--input-bg); border: 1px solid var(--border); border-radius: 8px;
    color: var(--text); font-size: 1rem; padding: 10px 14px; outline: none; transition: border-color 0.15s;
  }
  #key-input:focus { border-color: var(--accent); }
  #auth-btn {
    background: var(--accent); color: #fff; border: none; border-radius: 8px; padding: 11px;
    font-size: 0.95rem; font-weight: 600; cursor: pointer; transition: background 0.15s;
  }
  #auth-btn:hover { background: var(--accent-hi); }
  #auth-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  #auth-error { font-size: 0.82rem; color: var(--danger); min-height: 1.2em; }

  /* ── Chat layout ── */
  #chat-screen { display: none; flex-direction: column; height: 100%; }

  #chat-header {
    display: flex; align-items: center; gap: 10px; padding: 14px 20px;
    border-bottom: 1px solid var(--border); background: var(--surface); flex-shrink: 0;
  }
  #chat-header .name { font-weight: 700; font-size: 1rem; color: var(--accent); }
  #chat-header .thread-label { font-size: 0.73rem; color: var(--muted); margin-left: auto; font-family: var(--mono); }
  #new-chat-btn, #files-btn {
    background: none; border: 1px solid var(--border); border-radius: 6px; color: var(--muted);
    font-size: 0.78rem; padding: 4px 10px; cursor: pointer;
    transition: border-color 0.15s, color 0.15s; white-space: nowrap;
  }
  #new-chat-btn:hover, #files-btn:hover { border-color: var(--accent); color: var(--accent); }

  #main { display: flex; flex: 1; min-height: 0; }
  #chat-col { flex: 1; display: flex; flex-direction: column; min-width: 0; }

  /* messages */
  #messages { flex: 1; overflow-y: auto; padding: 24px 0; scroll-behavior: smooth; }
  .msg-row { display: flex; padding: 6px 24px; gap: 12px; max-width: 820px; margin: 0 auto; width: 100%; }
  .avatar {
    width: 30px; height: 30px; border-radius: 50%; display: flex; align-items: center;
    justify-content: center; font-size: 0.75rem; font-weight: 700; flex-shrink: 0; margin-top: 2px;
  }
  .avatar.user { background: var(--surface); color: var(--muted); border: 1px solid var(--border); }
  .avatar.bot  { background: var(--accent); color: #fff; border: 1px solid var(--accent); }
  .bubble {
    font-size: 0.92rem; line-height: 1.65; color: var(--text); word-break: break-word;
    background: var(--bot-bg); border: 1px solid var(--border); border-radius: 10px; padding: 10px 14px;
  }
  .bubble.user-msg { white-space: pre-wrap; background: var(--user-bg); }
  .bubble.thinking { color: var(--muted); font-style: italic; background: transparent; border-color: transparent; padding: 0; }
  .bubble.html p { margin: 0 0 0.55em; }
  .bubble.html p:last-child { margin-bottom: 0; }
  .bubble.html ul, .bubble.html ol { margin: 0.45em 0; padding-left: 1.35em; }
  .bubble.html li { margin: 0.2em 0; }
  .bubble.html strong { font-weight: 600; }
  .bubble.html a { color: var(--accent); }
  .bubble.html code { font-family: var(--mono); font-size: 0.85em; background: var(--bg); padding: 1px 4px; border-radius: 4px; }
  .msg-content { flex: 1; display: flex; flex-direction: column; gap: 4px; min-width: 0; }
  .msg-meta { font-size: 0.72rem; color: var(--muted); }

  /* input bar */
  #input-bar { border-top: 1px solid var(--border); background: var(--surface); padding: 16px 24px; flex-shrink: 0; }
  #input-wrap {
    display: flex; align-items: flex-end; gap: 10px; max-width: 820px; margin: 0 auto;
    background: var(--input-bg); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 10px 14px; transition: border-color 0.15s;
  }
  #input-wrap:focus-within { border-color: var(--accent); }
  #msg-input {
    flex: 1; background: none; border: none; outline: none; color: var(--text); font-size: 0.94rem;
    font-family: var(--font); resize: none; max-height: 180px; overflow-y: auto; line-height: 1.5;
  }
  #send-btn {
    background: var(--accent); border: none; border-radius: 7px; width: 34px; height: 34px;
    cursor: pointer; display: flex; align-items: center; justify-content: center; flex-shrink: 0;
    transition: background 0.15s, opacity 0.15s;
  }
  #send-btn:hover { background: var(--accent-hi); }
  #send-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  #send-btn svg { width: 16px; height: 16px; fill: #fff; }

  /* ── Adaptive panel ── */
  #panel {
    width: 400px; max-width: 45%; border-left: 1px solid var(--border); background: var(--bg);
    overflow-y: auto; padding: 16px; display: none; flex-direction: column; gap: 14px; flex-shrink: 0;
  }
  .box { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }
  .box-header {
    display: flex; align-items: center; gap: 8px; padding: 10px 14px;
    border-bottom: 1px solid var(--border); font-weight: 600; font-size: 0.88rem; color: var(--accent);
  }
  .box-header .box-actions { margin-left: auto; display: flex; gap: 6px; }
  .box-body { padding: 12px 14px; font-size: 0.88rem; }

  .pbtn {
    background: none; border: 1px solid var(--border); border-radius: 6px; color: var(--muted);
    font-size: 0.76rem; padding: 3px 9px; cursor: pointer; white-space: nowrap;
    transition: border-color 0.15s, color 0.15s, background 0.15s;
  }
  .pbtn:hover { border-color: var(--accent); color: var(--accent); }
  .pbtn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  .pbtn.primary:hover { background: var(--accent-hi); }
  .pbtn.danger { border-color: var(--danger); color: var(--danger); }
  .pbtn:disabled { opacity: 0.5; cursor: not-allowed; }

  /* card grid */
  .cards { display: flex; flex-direction: column; gap: 10px; }
  .card {
    border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; background: var(--surface);
    transition: border-color 0.15s, box-shadow 0.15s;
  }
  .card.clickable { cursor: pointer; }
  .card.clickable:hover { border-color: var(--accent); box-shadow: 0 1px 4px rgba(61,122,82,0.15); }
  .card-top { display: flex; align-items: baseline; gap: 8px; }
  .card-title { font-weight: 600; font-size: 0.88rem; }
  .card-badge {
    margin-left: auto; font-size: 0.68rem; font-weight: 600; color: var(--accent);
    border: 1px solid var(--border); border-radius: 10px; padding: 1px 8px; white-space: nowrap;
  }
  .card-subtitle { font-size: 0.78rem; color: var(--muted); margin-top: 2px; }
  .card-fields { margin-top: 6px; display: grid; grid-template-columns: auto 1fr; gap: 2px 10px; font-size: 0.78rem; }
  .card-fields .k { color: var(--muted); }
  .card-actions { margin-top: 8px; display: flex; gap: 6px; flex-wrap: wrap; }

  /* table */
  .ptable { border-collapse: collapse; width: 100%; font-size: 0.8rem; }
  .ptable th, .ptable td { border: 1px solid var(--border); padding: 5px 8px; text-align: left; vertical-align: top; }
  .ptable th { color: var(--accent); background: var(--bg); font-weight: 600; }

  /* progress */
  .progress-status { font-size: 0.84rem; margin-bottom: 8px; }
  .progress-track { height: 8px; border-radius: 4px; background: var(--bg); border: 1px solid var(--border); overflow: hidden; }
  .progress-fill { height: 100%; background: var(--accent); transition: width 0.3s; }
  .progress-fill.indeterminate { width: 30%; animation: slide 1.2s linear infinite; }
  @keyframes slide { from { margin-left: -30%; } to { margin-left: 100%; } }

  /* log */
  .logbox {
    font-family: var(--mono); font-size: 0.74rem; line-height: 1.5; background: #1e2620; color: #cde3d2;
    border-radius: 8px; padding: 10px 12px; max-height: 240px; overflow-y: auto; white-space: pre-wrap; word-break: break-word;
  }

  /* document */
  .doc { display: flex; align-items: center; gap: 10px; }
  .doc-icon { font-size: 1.4rem; }
  .doc-info { min-width: 0; flex: 1; }
  .doc-name { font-weight: 600; font-size: 0.84rem; word-break: break-all; }
  .doc-desc { font-size: 0.76rem; color: var(--muted); }
  .doc-actions { display: flex; gap: 6px; flex-wrap: wrap; }

  /* modal */
  #modal {
    display: none; position: fixed; inset: 0; background: rgba(26,26,26,0.45);
    align-items: center; justify-content: center; z-index: 50; padding: 24px;
  }
  #modal-card {
    background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
    width: 100%; max-width: 560px; max-height: 85vh; display: flex; flex-direction: column; overflow: hidden;
  }
  #modal-header {
    display: flex; align-items: center; gap: 10px; padding: 14px 18px; border-bottom: 1px solid var(--border);
  }
  #modal-title { font-weight: 700; font-size: 0.98rem; color: var(--accent); }
  #modal-close {
    margin-left: auto; background: none; border: none; color: var(--muted); font-size: 1.2rem;
    cursor: pointer; line-height: 1;
  }
  #modal-close:hover { color: var(--text); }
  #modal-body { padding: 16px 18px; overflow-y: auto; font-size: 0.9rem; line-height: 1.6; }
  #modal-body .card-fields { margin: 0 0 10px; }
  #modal-footer { padding: 12px 18px; border-top: 1px solid var(--border); display: flex; gap: 8px; flex-wrap: wrap; }
  #modal-footer:empty { display: none; }

  @media (max-width: 760px) {
    #main { flex-direction: column; }
    #panel { width: 100%; max-width: none; border-left: none; border-top: 1px solid var(--border); max-height: 45%; }
  }
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
  <div id="main">
    <div id="chat-col">
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
    <aside id="panel"></aside>
  </div>
</div>

<!-- ── Detail modal ── -->
<div id="modal">
  <div id="modal-card">
    <div id="modal-header">
      <span id="modal-title"></span>
      <button id="modal-close" title="Close">&times;</button>
    </div>
    <div id="modal-body"></div>
    <div id="modal-footer"></div>
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
  const panelEl     = $('panel');
  const modalEl     = $('modal');
  const modalTitle  = $('modal-title');
  const modalBody   = $('modal-body');
  const modalFooter = $('modal-footer');

  const BASE = window.location.origin;
  const AUI_RUN_PATH = __AUI_RUN_PATH__;

  let apiKey     = sessionStorage.getItem('mossy_key') || '';
  let threadId   = null;
  let aguiMessages = [];
  let messageSeq = 0;
  let busy       = false;
  let panelState = { boxes: {} };

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
      const res = await fetch(`${BASE}/queue`, { headers: { 'Authorization': `Bearer ${k}` } });
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
    authScreen.style.display = 'none';
    chatScreen.style.display = 'flex';
    msgInput.focus();
  }

  function expireSession(message) {
    sessionStorage.removeItem('mossy_key');
    apiKey = '';
    chatScreen.style.display = 'none';
    authScreen.style.display = 'flex';
    authError.textContent = message || 'Session expired — please reconnect.';
  }

  // ── New chat ──────────────────────────────────────────────────────
  newChatBtn.addEventListener('click', () => {
    threadId = null;
    aguiMessages = [];
    messageSeq = 0;
    threadLabel.textContent = '';
    messagesEl.innerHTML = '';
    panelState = { boxes: {} };
    renderPanel();
    closeModal();
    msgInput.focus();
  });
  filesBtn.addEventListener('click', showFiles);
  messagesEl.addEventListener('click', e => {
    const link = e.target.closest('[data-download-path]');
    if (!link) return;
    e.preventDefault();
    downloadArchiveFile(link.dataset.downloadPath);
  });

  // ── Modal ─────────────────────────────────────────────────────────
  $('modal-close').addEventListener('click', closeModal);
  modalEl.addEventListener('click', e => { if (e.target === modalEl) closeModal(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

  function closeModal() { modalEl.style.display = 'none'; }

  function openItemModal(box, item) {
    modalTitle.textContent = item.title || item.id;
    modalBody.innerHTML = '';
    if (item.subtitle) {
      const sub = el('div', 'card-subtitle');
      sub.textContent = item.subtitle;
      sub.style.marginBottom = '10px';
      modalBody.appendChild(sub);
    }
    if (item.fields) modalBody.appendChild(renderFields(item.fields));
    if (item.detail) {
      const detail = el('div', 'bubble html');
      detail.style.border = 'none';
      detail.style.padding = '0';
      detail.innerHTML = formatRichText(item.detail);
      modalBody.appendChild(detail);
    }
    modalFooter.innerHTML = '';
    (item.actions || []).forEach(action => {
      modalFooter.appendChild(actionButton(action, { boxId: box.id, itemId: item.id }));
    });
    modalEl.style.display = 'flex';
  }

  // ── Textarea + send ───────────────────────────────────────────────
  msgInput.addEventListener('input', () => {
    msgInput.style.height = 'auto';
    msgInput.style.height = msgInput.scrollHeight + 'px';
  });
  msgInput.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });
  sendBtn.addEventListener('click', () => send());

  async function send(overrideText) {
    const fromInput = typeof overrideText !== 'string';
    const text = (fromInput ? msgInput.value : overrideText).trim();
    if (!text || busy) return;

    busy = true;
    sendBtn.disabled = true;
    if (fromInput) {
      msgInput.value = '';
      msgInput.style.height = 'auto';
    }

    appendMsg('user', text);
    const thinkEl = appendMsg('bot', 'Thinking.', true);
    const stopThinking = startThinkingDots(thinkEl);
    scrollBottom();

    const t0 = Date.now();
    const currentThreadId = threadId || newId('thread');
    const userMessage = { id: newId('msg'), role: 'user', content: text };
    const requestMessages = [...aguiMessages, userMessage];
    try {
      const res = await fetch(`${BASE}${AUI_RUN_PATH}`, {
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
          state: panelState,
          context: [],
          tools: [],
          forwardedProps: {},
        }),
      });

      if (res.status === 401) {
        expireSession();
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
      setBubbleHtml(thinkEl, formatRichText(reply || '(no reply)'));
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

  // ── Panel actions ─────────────────────────────────────────────────
  function runAction(action, ctx) {
    if (busy) return;
    let text = action.prompt;
    if (!text) {
      const ref = { box: ctx.boxId };
      if (ctx.itemId) ref.item = ctx.itemId;
      if (action.payload) ref.payload = action.payload;
      text = `[panel action] id=${action.id} ${JSON.stringify(ref)}`;
    }
    closeModal();
    send(text);
  }

  function actionButton(action, ctx) {
    const btn = el('button', 'pbtn' + (action.style && action.style !== 'default' ? ` ${action.style}` : ''));
    btn.textContent = action.label || action.id;
    btn.addEventListener('click', e => {
      e.stopPropagation();
      runAction(action, ctx);
    });
    return btn;
  }

  // ── Panel state + rendering ───────────────────────────────────────
  function applyJsonPatch(state, ops) {
    for (const op of ops || []) {
      const segs = String(op.path || '')
        .split('/')
        .slice(1)
        .map(s => s.replaceAll('~1', '/').replaceAll('~0', '~'));
      if (!segs.length) {
        if ((op.op === 'replace' || op.op === 'add') && op.value && typeof op.value === 'object') state = op.value;
        continue;
      }
      let parent = state;
      for (let i = 0; i < segs.length - 1; i++) {
        const k = segs[i];
        if (parent[k] === undefined || parent[k] === null) {
          parent[k] = (segs[i + 1] === '-' || /^\d+$/.test(segs[i + 1])) ? [] : {};
        }
        parent = parent[k];
      }
      const last = segs[segs.length - 1];
      if (op.op === 'remove') {
        if (Array.isArray(parent)) parent.splice(Number(last), 1);
        else delete parent[last];
      } else if (op.op === 'add' || op.op === 'replace') {
        if (Array.isArray(parent)) {
          if (last === '-') parent.push(op.value);
          else parent.splice(Number(last), op.op === 'replace' ? 1 : 0, op.value);
        } else {
          parent[last] = op.value;
        }
      }
    }
    return state;
  }

  function renderPanel() {
    if (!panelState || typeof panelState !== 'object') panelState = { boxes: {} };
    if (!panelState.boxes || typeof panelState.boxes !== 'object') panelState.boxes = {};
    const boxes = Object.values(panelState.boxes).filter(b => b && typeof b === 'object');
    panelEl.innerHTML = '';
    panelEl.style.display = boxes.length ? 'flex' : 'none';
    for (const box of boxes) panelEl.appendChild(renderBox(box));
    panelEl.querySelectorAll('.logbox').forEach(log => { log.scrollTop = log.scrollHeight; });
  }

  function renderBox(box) {
    const boxEl = el('div', 'box');
    const header = el('div', 'box-header');
    const title = el('span');
    title.textContent = box.title || box.id;
    header.appendChild(title);
    if (Array.isArray(box.actions) && box.actions.length) {
      const wrap = el('div', 'box-actions');
      box.actions.forEach(action => wrap.appendChild(actionButton(action, { boxId: box.id })));
      header.appendChild(wrap);
    }
    boxEl.appendChild(header);

    const body = el('div', 'box-body');
    const w = box.widget || {};
    switch (w.type) {
      case 'card_grid': body.appendChild(renderCardGrid(box, w)); break;
      case 'table':     body.appendChild(renderTable(w)); break;
      case 'markdown':  body.appendChild(renderMarkdown(w)); break;
      case 'progress':  body.appendChild(renderProgress(w)); break;
      case 'log':       body.appendChild(renderLog(w)); break;
      case 'document':  body.appendChild(renderDocument(box, w)); break;
      default: {
        const unknown = el('div', 'card-subtitle');
        unknown.textContent = `Unsupported widget type: ${w.type || '(none)'}`;
        body.appendChild(unknown);
      }
    }
    boxEl.appendChild(body);
    return boxEl;
  }

  function renderCardGrid(box, w) {
    const wrap = el('div', 'cards');
    for (const item of w.items || []) {
      const card = el('div', 'card');
      const top = el('div', 'card-top');
      const title = el('span', 'card-title');
      title.textContent = item.title || item.id;
      top.appendChild(title);
      if (item.badge) {
        const badge = el('span', 'card-badge');
        badge.textContent = item.badge;
        top.appendChild(badge);
      }
      card.appendChild(top);
      if (item.subtitle) {
        const sub = el('div', 'card-subtitle');
        sub.textContent = item.subtitle;
        card.appendChild(sub);
      }
      if (item.fields) card.appendChild(renderFields(item.fields, 3));
      if (Array.isArray(item.actions) && item.actions.length) {
        const actions = el('div', 'card-actions');
        item.actions.forEach(action => actions.appendChild(actionButton(action, { boxId: box.id, itemId: item.id })));
        card.appendChild(actions);
      }
      if (item.detail || item.fields) {
        card.classList.add('clickable');
        card.addEventListener('click', () => openItemModal(box, item));
      }
      wrap.appendChild(card);
    }
    return wrap;
  }

  function renderFields(fields, limit) {
    const wrap = el('div', 'card-fields');
    let count = 0;
    for (const [k, v] of Object.entries(fields || {})) {
      if (limit && count >= limit) break;
      const kEl = el('span', 'k');
      kEl.textContent = k;
      const vEl = el('span');
      vEl.textContent = String(v);
      wrap.appendChild(kEl);
      wrap.appendChild(vEl);
      count++;
    }
    return wrap;
  }

  function renderTable(w) {
    const table = el('table', 'ptable');
    const thead = document.createElement('thead');
    const headRow = document.createElement('tr');
    for (const col of w.columns || []) {
      const th = document.createElement('th');
      th.textContent = String(col);
      headRow.appendChild(th);
    }
    thead.appendChild(headRow);
    table.appendChild(thead);
    const tbody = document.createElement('tbody');
    for (const row of w.rows || []) {
      const tr = document.createElement('tr');
      for (const cell of row || []) {
        const td = document.createElement('td');
        td.textContent = String(cell);
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    return table;
  }

  function renderMarkdown(w) {
    const div = el('div', 'bubble html');
    div.style.border = 'none';
    div.style.padding = '0';
    div.innerHTML = formatRichText(w.text || '');
    return div;
  }

  function renderProgress(w) {
    const wrap = el('div');
    const status = el('div', 'progress-status');
    status.textContent = (w.done ? '✓ ' : '') + (w.status || '');
    wrap.appendChild(status);
    const track = el('div', 'progress-track');
    const fill = el('div', 'progress-fill');
    if (w.done) {
      fill.style.width = '100%';
    } else if (typeof w.percent === 'number') {
      fill.style.width = `${Math.max(0, Math.min(100, w.percent))}%`;
    } else {
      fill.classList.add('indeterminate');
    }
    track.appendChild(fill);
    wrap.appendChild(track);
    return wrap;
  }

  function renderLog(w) {
    const pre = el('pre', 'logbox');
    pre.textContent = (w.lines || []).join('\n');
    return pre;
  }

  function renderDocument(box, w) {
    const wrap = el('div', 'doc');
    const icon = el('div', 'doc-icon');
    const path = String(w.path || '').replace(/^\/?files\//, '');
    icon.textContent = /\.pdf$/i.test(path) ? '📄' : '📎';
    wrap.appendChild(icon);
    const info = el('div', 'doc-info');
    const name = el('div', 'doc-name');
    name.textContent = path.split('/').pop() || path;
    info.appendChild(name);
    if (w.description) {
      const desc = el('div', 'doc-desc');
      desc.textContent = w.description;
      info.appendChild(desc);
    }
    const actions = el('div', 'doc-actions');
    actions.style.marginTop = '6px';
    const openBtn = el('button', 'pbtn primary');
    openBtn.textContent = 'Open';
    openBtn.addEventListener('click', () => openSharedFile(path));
    actions.appendChild(openBtn);
    const dlBtn = el('button', 'pbtn');
    dlBtn.textContent = 'Download';
    dlBtn.addEventListener('click', () => downloadArchiveFile(path));
    actions.appendChild(dlBtn);
    (w.actions || []).forEach(action => actions.appendChild(actionButton(action, { boxId: box.id })));
    info.appendChild(actions);
    wrap.appendChild(info);
    return wrap;
  }

  // ── Shared files (authenticated /files endpoints) ─────────────────
  async function fetchSharedBlob(path) {
    const res = await fetch(`${BASE}/files/${encodeURIComponent(path).replaceAll('%2F', '/')}`, {
      headers: { 'Authorization': `Bearer ${apiKey}` },
    });
    if (res.status === 401) {
      expireSession();
      throw new Error('unauthorized');
    }
    if (!res.ok) throw new Error(`fetch failed (${res.status})`);
    return res.blob();
  }

  async function openSharedFile(path) {
    try {
      const blob = await fetchSharedBlob(path);
      const url = URL.createObjectURL(blob);
      window.open(url, '_blank');
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    } catch (err) {
      if (err.message !== 'unauthorized') {
        appendMsg('bot', `Could not open ${path}: ${err.message}`);
        scrollBottom();
      }
    }
  }

  async function downloadArchiveFile(path) {
    try {
      const blob = await fetchSharedBlob(path);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = path.split('/').pop() || 'download';
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      if (err.message !== 'unauthorized') {
        appendMsg('bot', `Could not download ${path}: ${err.message}`);
        scrollBottom();
      }
    }
  }

  async function showFiles() {
    if (!apiKey) return;
    const bubble = appendMsg('bot', 'Loading files.', true);
    try {
      const res = await fetch(`${BASE}/files`, { headers: { 'Authorization': `Bearer ${apiKey}` } });
      if (res.status === 401) {
        expireSession();
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
    const more = data.truncated ? '<p>List truncated. Ask for a narrower folder.</p>' : '';
    return `<p>Shared files:</p><ul>${items}</ul>${more}`;
  }

  // ── AG-UI stream ──────────────────────────────────────────────────
  async function readAguiStream(res, onText, onStatus) {
    if (!res.body) throw new Error('Streaming is not supported by this browser.');

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    const ctx = { toolArgs: new Map() };  // toolCallId -> {name, args}

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replaceAll('\r\n', '\n');

      let boundary = buffer.indexOf('\n\n');
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        handleSseFrame(frame, onText, onStatus, ctx);
        boundary = buffer.indexOf('\n\n');
      }
    }

    buffer += decoder.decode().replaceAll('\r\n', '\n');
    if (buffer.trim()) handleSseFrame(buffer, onText, onStatus, ctx);
  }

  function handleSseFrame(frame, onText, onStatus, ctx) {
    const data = frame
      .split('\n')
      .filter(line => line.startsWith('data:'))
      .map(line => line.slice(5).trimStart())
      .join('\n');
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
      case 'STATE_SNAPSHOT': {
        panelState = (event.snapshot && typeof event.snapshot === 'object') ? event.snapshot : { boxes: {} };
        renderPanel();
        break;
      }
      case 'STATE_DELTA': {
        panelState = applyJsonPatch(panelState, event.delta || []) || panelState;
        renderPanel();
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
          const m = rec.args.match(/"script_name"\s*:\s*"([^"]+)"/);
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
      panel_upsert_box: 'Updating the panel…',
      panel_append_log: 'Streaming to the panel…',
      panel_remove_box: 'Updating the panel…',
      panel_clear: 'Clearing the panel…',
    };
    return map[tool] || `Working… (${tool})`;
  }

  // ── Helpers ───────────────────────────────────────────────────────
  function el(tag, cls) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    return node;
  }

  function newId(prefix) {
    const random = window.crypto?.randomUUID?.() || `${Date.now()}-${++messageSeq}`;
    return `${prefix}-${random}`;
  }

  function formatRichText(value) {
    let html = escapeHtml(value).replaceAll('\n', '<br>');
    // Turn protected /files/<path> references into authenticated download links.
    html = html.replace(/`?(\/files\/[A-Za-z0-9._~%\/-]+)`?/g, (_m, url) => {
      const rel = url.replace(/^\/files\//, '');
      const name = rel.split('/').pop() || rel;
      return `<a href="#" data-download-path="${rel}">⬇ ${name}</a>`;
    });
    html = html
      .replace(/\*\*([^*<][^*]*?)\*\*/g, '<strong>$1</strong>')
      .replace(/`([^`<]+)`/g, '<code>$1</code>');
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

  function startThinkingDots(elm) {
    let dots = 1;
    let stopped = false;
    const render = () => {
      elm.textContent = `Thinking${'.'.repeat(dots)}`;
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


def register_aui_routes(app: FastAPI, runtime: "Runtime") -> AguiChannel:
    """Mount GET /aui (page) and POST /aui/run (panel-enabled AG-UI endpoint)."""
    channel = AguiChannel(
        runtime,
        path=_aui_run_path(),
        extra_capabilities=[ui_panel_capability()],
        extra_instructions=_PANEL_INSTRUCTIONS,
    )

    @app.post(channel.path)
    @app.post(f"{channel.path}/")
    async def aui_run(request: Request) -> Response:
        return await channel.handle_request(request)

    html = _HTML.replace("__AUI_RUN_PATH__", json.dumps(channel.path))

    @app.get("/aui", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/aui/", response_class=HTMLResponse, include_in_schema=False)
    async def aui_page(request: Request) -> HTMLResponse:  # noqa: ARG001
        return HTMLResponse(html)

    return channel

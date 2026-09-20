'use strict';
const scroller = document.getElementById('scroller');
const log = document.getElementById('log');
const empty = document.getElementById('empty');
const working = document.getElementById('working');
const input = document.getElementById('input');
const send = document.getElementById('send');
const status = document.getElementById('status');
const chatList = document.getElementById('chats');
const newChat = document.getElementById('new-chat');
const token = document.querySelector('meta[name="forge-token"]').content;

// Chats live in this page only; the first one is whatever the server rendered.
const chats = [{ title: 'Chat 1', messages: readLog() }];
let current = chats[0];

function readLog() {
  return [...log.children].map((li) => ({
    role: [...li.classList].find((c) => c !== 'msg'),
    text: li.querySelector('pre').textContent,
  }));
}

function scrollDown() { scroller.scrollTop = scroller.scrollHeight; }

// Pull <artifact ...>...</artifact> blocks out of a reply. A block the model
// forgot to close runs to the end of the reply.
function parseArtifacts(text) {
  const found = [];
  const prose = text.replace(/<artifact\s+([^>]*)>([\s\S]*?)(?:<\/artifact>|$)/g, (_, attrs, body) => {
    const a = { id: '', type: 'code', title: 'Artifact', language: '', content: body.replace(/^\n/, '').replace(/\n$/, '') };
    for (const m of attrs.matchAll(/(\w+)="([^"]*)"/g)) {
      if (m[1] in a && m[1] !== 'content') a[m[1]] = m[2];
    }
    found.push(a);
    return '';
  });
  return { prose: prose.trim(), found };
}

// Returns the artifacts found in the message so the caller can open one.
function draw(role, text) {
  const li = document.createElement('li');
  li.className = 'msg ' + role;
  const who = document.createElement('span');
  who.className = 'who';
  who.textContent = role === 'user' ? 'You' : role === 'error' ? 'Error' : 'forge';
  li.append(who);
  let found = [];
  let prose = text;
  if (role === 'assistant') ({ prose, found } = parseArtifacts(text));
  if (prose || !found.length) {
    const pre = document.createElement('pre');
    pre.textContent = prose;
    li.append(pre);
  }
  found.forEach((a) => {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'artifact-card';
    card.textContent = a.title + ' (' + a.type + ')';
    card.addEventListener('click', () => openArtifact(a));
    li.append(card);
  });
  log.append(li);
  empty.hidden = true;
  scrollDown();
  return found;
}

function add(role, text) {
  current.messages.push({ role, text });
  if (role === 'user' && current.title.startsWith('Chat ') && current.messages.length === 1) {
    current.title = text.length > 28 ? text.slice(0, 28) + '...' : text;
    renderList();
  }
  const found = draw(role, text);
  if (found.length) openArtifact(found[found.length - 1]);
}

const pane = document.getElementById('artifact-pane');
const paneTitle = document.getElementById('artifact-title');
const frame = document.getElementById('artifact-frame');
const codeView = document.getElementById('artifact-code');
const tabPreview = document.getElementById('tab-preview');
const tabCode = document.getElementById('tab-code');
let shown = null;

function showTab(name) {
  const preview = name === 'preview';
  frame.hidden = !preview;
  codeView.hidden = preview;
  tabPreview.classList.toggle('active', preview);
  tabCode.classList.toggle('active', !preview);
}

// html and svg run in a sandboxed iframe served by the server with its own
// CSP; code and markdown are only ever shown as text.
async function openArtifact(a) {
  shown = a;
  paneTitle.textContent = a.title;
  codeView.textContent = a.content;
  pane.hidden = false;
  const runnable = a.type === 'html' || a.type === 'svg';
  tabPreview.hidden = !runnable;
  showTab('code');
  if (!runnable) return;
  const page = a.type === 'svg'
    ? '<!doctype html><html><body style="margin:0">' + a.content + '</body></html>'
    : a.content;
  try {
    const response = await fetch('/api/artifact', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Forge-Token': token },
      body: JSON.stringify({ content: page }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'HTTP ' + response.status);
    if (shown !== a) return;
    frame.src = data.url;
    showTab('preview');
  } catch (error) {
    if (shown === a) codeView.textContent = '(preview failed: ' + error.message + ')\n\n' + a.content;
  }
}

tabPreview.addEventListener('click', () => showTab('preview'));
tabCode.addEventListener('click', () => showTab('code'));
document.getElementById('artifact-close').addEventListener('click', () => {
  pane.hidden = true;
  shown = null;
  frame.removeAttribute('src');
});

function renderChat() {
  log.replaceChildren();
  current.messages.forEach((m) => draw(m.role, m.text));
  empty.hidden = current.messages.length > 0;
  renderList();
  scrollDown();
  input.focus();
}

function renderList() {
  chatList.replaceChildren();
  chats.forEach((chat) => {
    const li = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'chat-item' + (chat === current ? ' active' : '');
    button.textContent = chat.title;
    button.disabled = send.disabled;
    button.addEventListener('click', () => { current = chat; renderChat(); });
    li.append(button);
    chatList.append(li);
  });
}

function setBusy(busy) {
  send.disabled = busy;
  newChat.disabled = busy;
  chatList.querySelectorAll('button').forEach((b) => { b.disabled = busy; });
  working.hidden = !busy;
  status.className = busy ? 'pill busy' : 'pill';
  status.textContent = busy ? 'working...' : 'ready';
  if (busy) scrollDown();
}

function autosize() {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 192) + 'px';
}

async function submit() {
  const message = input.value.trim();
  if (!message || send.disabled) return;
  input.value = '';
  autosize();
  add('user', message);
  setBusy(true);
  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Forge-Token': token },
      body: JSON.stringify({ message }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'HTTP ' + response.status);
    add('assistant', data.reply || '(no reply)');
    setBusy(false);
  } catch (error) {
    add('error', error.message);
    setBusy(false);
    status.className = 'pill error';
    status.textContent = 'failed';
  } finally {
    input.focus();
  }
}

newChat.addEventListener('click', () => {
  current = { title: 'Chat ' + (chats.length + 1), messages: [] };
  chats.push(current);
  pane.hidden = true;
  renderChat();
});
document.getElementById('form').addEventListener('submit', (e) => { e.preventDefault(); submit(); });
input.addEventListener('input', autosize);
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); }
});
renderChat();

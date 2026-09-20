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

function draw(role, text) {
  const li = document.createElement('li');
  li.className = 'msg ' + role;
  const who = document.createElement('span');
  who.className = 'who';
  who.textContent = role === 'user' ? 'You' : role === 'error' ? 'Error' : 'forge';
  const pre = document.createElement('pre');
  pre.textContent = text;
  li.append(who, pre);
  log.append(li);
  empty.hidden = true;
  scrollDown();
}

function add(role, text) {
  current.messages.push({ role, text });
  if (role === 'user' && current.title.startsWith('Chat ') && current.messages.length === 1) {
    current.title = text.length > 28 ? text.slice(0, 28) + '...' : text;
    renderList();
  }
  draw(role, text);
}

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
  renderChat();
});
document.getElementById('form').addEventListener('submit', (e) => { e.preventDefault(); submit(); });
input.addEventListener('input', autosize);
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); }
});
renderList();
scrollDown();

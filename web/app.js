let csrf = '';
let selected = null;
let busy = false;
const list = document.querySelector('#conversation-list');
const messages = document.querySelector('#messages');
const form = document.querySelector('#composer');
const input = document.querySelector('#message-input');
const notice = document.querySelector('#notice');
const send = document.querySelector('#send');
const newChat = document.querySelector('#new-chat');
const labels = {
  MODEL_NOT_CONFIGURED: '模型接口尚未配置。消息已保存，请配置服务端环境变量后主动发送新消息。',
  MODEL_REQUEST_FAILED: '模型接口拒绝请求。消息已保存，没有自动重试。',
  MODEL_OUTCOME_UNKNOWN: '模型响应未确认。请核对服务端记录，没有自动重试。',
  MODEL_RESPONSE_INVALID: '模型返回格式无效。消息已保存，没有自动重试。',
  CONTEXT_LIMIT: '对话上下文过长，请新建对话。',
};

function showNotice(text) {
  notice.textContent = text;
  notice.hidden = !text;
}

async function api(path, body) {
  const response = await fetch('/api' + path, {
    method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.message || '服务请求失败。');
  return data;
}

function render(detail) {
  messages.replaceChildren();
  for (const message of detail.messages) {
    const article = document.createElement('article');
    article.className = message.role === 'user' ? 'user-message' : 'assistant-message';
    if (message.role === 'assistant') {
      const label = document.createElement('span');
      label.className = 'draft-label';
      label.textContent = '辅助分析草稿';
      article.append(label);
    }
    const content = document.createElement('p');
    content.textContent = message.state === 'ERROR'
      ? labels[message.error_code] || '当前回复未完成，消息已保存。'
      : message.state === 'PENDING' ? '处理状态尚未确认。刷新不会重新调用模型。'
      : message.content;
    article.append(content);
    if (message.role === 'assistant' && message.state === 'READY') {
      const button = document.createElement('button');
      button.className = 'copy';
      button.textContent = '复制草稿';
      button.addEventListener('click', async () => {
        try { await navigator.clipboard.writeText(message.content); button.textContent = '已复制'; }
        catch { showNotice('复制失败，请直接选择文字复制。'); }
      });
      article.append(button);
    }
    messages.append(article);
  }
  document.querySelector('.intro').classList.toggle('compact', detail.messages.length > 0);
}

async function refreshList() {
  const data = await api('/conversations');
  list.replaceChildren();
  for (const item of data.items) {
    const button = document.createElement('button');
    button.textContent = item.title;
    button.title = item.title;
    button.setAttribute('aria-current', selected === item.id ? 'page' : 'false');
    button.disabled = busy;
    button.addEventListener('click', async () => {
      try {
        selected = item.id;
        render(await api('/conversations/' + encodeURIComponent(item.id)));
        history.replaceState(null, '', '?conversation=' + encodeURIComponent(selected));
        showNotice('');
        await refreshList();
      } catch (error) { showNotice(error.message); }
    });
    list.append(button);
  }
}

function setBusy(value) {
  busy = value;
  send.disabled = value;
  newChat.disabled = value;
  send.textContent = value ? '正在处理…' : '发送 ↑';
  for (const button of list.querySelectorAll('button')) button.disabled = value;
}

newChat.addEventListener('click', () => {
  selected = null;
  render({ messages: [] });
  input.value = '';
  showNotice('');
  history.replaceState(null, '', '/');
  void refreshList().catch(error => showNotice(error.message));
  input.focus();
});

form.addEventListener('submit', async event => {
  event.preventDefault();
  if (busy || !input.value.trim()) return;
  const content = input.value.trim();
  setBusy(true);
  showNotice('');
  try {
    if (!selected) selected = (await api('/conversations', {})).id;
    history.replaceState(null, '', '?conversation=' + encodeURIComponent(selected));
    render(await api('/conversations/' + encodeURIComponent(selected)));
    const pending = document.createElement('article');
    pending.className = 'user-message';
    pending.textContent = content;
    messages.append(pending);
    const waiting = document.createElement('p');
    waiting.className = 'waiting';
    waiting.textContent = '正在整理你的描述，请等待当前请求完成…';
    messages.append(waiting);
    const detail = await api('/conversations/' + encodeURIComponent(selected) + '/messages', { content });
    render(detail);
    // Preserve anything typed while the request was pending.
    if (input.value.trim() === content) input.value = '';
  } catch (error) {
    showNotice(error.message);
    if (selected) {
      try { render(await api('/conversations/' + encodeURIComponent(selected))); }
      catch { /* Preserve the visible failure and unsent input. */ }
    }
  } finally {
    setBusy(false);
    try { await refreshList(); } catch (error) { showNotice(error.message); }
  }
});

(async () => {
  try {
    csrf = (await api('/session')).csrf_token;
    selected = new URLSearchParams(location.search).get('conversation');
    if (selected) render(await api('/conversations/' + encodeURIComponent(selected)));
    await refreshList();
  } catch (error) { showNotice(error.message); }
})();

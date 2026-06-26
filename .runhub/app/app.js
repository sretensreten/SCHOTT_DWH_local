const state = {
  project: {},
  categories: [],
  actions: [],
  activeCategory: 'overview',
  search: ''
};

const $ = id => document.getElementById(id);

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || 'Request failed');
  return data;
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function renderTemplate(value, inputs = {}) {
  return String(value ?? '').replace(/\{\{\s*([a-zA-Z0-9_\-]+)\s*\}\}/g, (_, key) => inputs[key] ?? '');
}

function commandPreview(action) {
  return [renderTemplate(action.command), ...(action.args || []).map(renderTemplate)].join(' ');
}

function renderCategories() {
  const counts = state.actions.reduce((acc, a) => {
    acc[a.category] = (acc[a.category] || 0) + 1;
    return acc;
  }, {});
  counts.overview = state.actions.length;

  $('categories').innerHTML = state.categories.map(c => `
    <button class="category-button ${state.activeCategory === c.id ? 'active' : ''}" data-category="${escapeHtml(c.id)}">
      <span>${escapeHtml(c.icon || '')}</span>
      <span class="category-title">${escapeHtml(c.title)}</span>
      <span class="category-count">${counts[c.id] || 0}</span>
    </button>
  `).join('');

  document.querySelectorAll('[data-category]').forEach(btn => {
    btn.addEventListener('click', () => {
      state.activeCategory = btn.dataset.category;
      render();
    });
  });
}

function visibleActions() {
  return state.actions.filter(a => {
    const inCategory = state.activeCategory === 'overview' || a.category === state.activeCategory;
    const q = state.search.trim().toLowerCase();
    const inSearch = !q || `${a.title} ${a.description} ${a.category}`.toLowerCase().includes(q);
    return inCategory && inSearch;
  });
}

function renderActions() {
  const list = visibleActions();
  $('actionCount').textContent = `${list.length} shown`;
  $('actions').innerHTML = list.map(action => `
    <article class="card" data-action-card="${escapeHtml(action.id)}">
      <div>
        <h3>${escapeHtml(action.title)}</h3>
        <p>${escapeHtml(action.description || '')}</p>
      </div>
      <div class="badges">
        <span class="badge">${escapeHtml(action.category)}</span>
        <span class="badge ${escapeHtml(action.risk || 'low')}">risk: ${escapeHtml(action.risk || 'low')}</span>
        ${action.requiresConfirmation ? '<span class="badge high">confirmation</span>' : ''}
      </div>
      <details class="command-preview">
        <summary>Preview command</summary>
        <code>${escapeHtml(commandPreview(action))}</code>
      </details>
      <div class="card-spacer"></div>
      <div class="button-row">
        <button class="primary-button" data-run-action="${escapeHtml(action.id)}">Run</button>
      </div>
    </article>
  `).join('') || `<div class="card"><p>No actions found.</p></div>`;

  document.querySelectorAll('[data-run-action]').forEach(btn => btn.addEventListener('click', () => runAction(btn.dataset.runAction)));
}

function render() {
  renderCategories();
  renderActions();
}

function appendLog(text) {
  const terminal = $('terminal');
  if (terminal.textContent === 'Run a DWH action to see logs here...') terminal.textContent = '';
  terminal.textContent += text;
  terminal.scrollTop = terminal.scrollHeight;
}

function streamRun(runId) {
  $('runStatus').textContent = `Running ${runId}`;
  const events = new EventSource(`/api/runs/${runId}/events`);
  events.onmessage = event => {
    const item = JSON.parse(event.data);
    if (item.type === 'start') appendLog(`\n▶ ${item.message}\n${item.commandPreview || ''}\n\n`);
    if (item.type === 'stdout') appendLog(item.message);
    if (item.type === 'stderr') appendLog(item.message);
    if (item.type === 'error') appendLog(`\nERROR: ${item.message}\n`);
    if (item.type === 'finish') {
      appendLog(`\n${item.message}\n`);
      $('runStatus').textContent = item.status || 'Finished';
      events.close();
    }
  };
  events.onerror = () => {
    appendLog('\nConnection to run log closed.\n');
    events.close();
  };
}

async function confirmIfNeeded(action) {
  if (!action.requiresConfirmation) return { ok: true, confirmPhrase: '' };
  $('confirmText').textContent = `This action is marked as ${action.risk || 'high'} risk: ${action.title}`;
  $('confirmCommand').textContent = commandPreview(action);
  $('confirmPhraseInput').value = '';
  const phraseLabel = $('phraseLabel');
  if (action.confirmPhrase) {
    phraseLabel.classList.remove('hidden');
    phraseLabel.querySelector('input').placeholder = action.confirmPhrase;
  } else {
    phraseLabel.classList.add('hidden');
  }
  const dialog = $('confirmDialog');
  dialog.showModal();
  const result = await new Promise(resolve => dialog.addEventListener('close', () => resolve(dialog.returnValue), { once: true }));
  if (result === 'cancel') return { ok: false };
  return { ok: true, confirmPhrase: $('confirmPhraseInput').value };
}

async function runAction(actionId) {
  try {
    const action = state.actions.find(a => a.id === actionId);
    const confirmation = await confirmIfNeeded(action);
    if (!confirmation.ok) return;
    const result = await api('/api/run', {
      method: 'POST',
      body: JSON.stringify({ actionId, inputs: {}, confirmationAccepted: action.requiresConfirmation, confirmPhrase: confirmation.confirmPhrase })
    });
    streamRun(result.runId);
  } catch (err) {
    appendLog(`\nERROR: ${err.message}\n`);
    $('runStatus').textContent = 'Error';
  }
}

async function init() {
  state.project = await api('/api/project');
  state.categories = await api('/api/categories');
  state.actions = await api('/api/actions');
  $('projectTitle').textContent = state.project.title || 'SCHOTT DWH Local Control Center';
  $('projectDescription').textContent = state.project.description || '';
  $('search').addEventListener('input', e => { state.search = e.target.value; renderActions(); });
  $('clearLog').addEventListener('click', () => { $('terminal').textContent = ''; $('runStatus').textContent = 'Waiting'; });
  render();
}

init().catch(err => appendLog(`ERROR: ${err.message}\n`));

const http = require('http');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const ROOT = process.cwd();
const APP_DIR = path.join(ROOT, '.runhub', 'app');
const PROJECT_DIR = path.join(ROOT, '.runhub.project');
const RUNS_DIR = path.join(PROJECT_DIR, 'runs');
const PORT = Number(process.env.RUNHUB_PORT || 3002);

fs.mkdirSync(RUNS_DIR, { recursive: true });
const clients = new Map();
const runs = new Map();

function readJson(relativePath, fallback) {
  const fullPath = path.join(ROOT, relativePath);
  if (!fs.existsSync(fullPath)) return fallback;
  return JSON.parse(fs.readFileSync(fullPath, 'utf8'));
}

function writeJson(res, status, payload) {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(payload, null, 2));
}

function sendText(res, status, body, contentType = 'text/plain; charset=utf-8') {
  res.writeHead(status, { 'Content-Type': contentType });
  res.end(body);
}

function getBody(req) {
  return new Promise((resolve, reject) => {
    let data = '';
    req.on('data', chunk => {
      data += chunk;
      if (data.length > 1024 * 1024) {
        reject(new Error('Request body too large'));
        req.destroy();
      }
    });
    req.on('end', () => {
      try { resolve(data ? JSON.parse(data) : {}); }
      catch (err) { reject(err); }
    });
  });
}

function actions() { return readJson('.runhub.project/actions.json', []); }
function categories() { return readJson('.runhub.project/categories.json', []); }

function safeInputValue(value) {
  if (typeof value === 'boolean') return value;
  const s = String(value ?? '');
  if (s.length > 300) throw new Error('Input value too long.');
  if (/[;&|`$<>]/.test(s)) throw new Error(`Unsafe input value: ${s}`);
  return s;
}

function validateInputs(action, provided) {
  const out = {};
  for (const [key, value] of Object.entries(provided || {})) out[key] = safeInputValue(value);
  return out;
}

function renderTemplate(value, inputs) {
  return String(value ?? '').replace(/\{\{\s*([a-zA-Z0-9_\-]+)\s*\}\}/g, (_, key) => {
    if (!Object.prototype.hasOwnProperty.call(inputs, key)) throw new Error(`Missing template input: ${key}`);
    return String(inputs[key]);
  });
}

function buildCommand(action, providedInputs) {
  const inputValues = validateInputs(action, providedInputs || {});
  const command = renderTemplate(action.command, inputValues);
  const args = (action.args || []).map(arg => renderTemplate(arg, inputValues));
  return { command, args, inputValues };
}

function emit(runId, type, payload) {
  const line = { type, ts: new Date().toISOString(), ...payload };
  const run = runs.get(runId);
  if (run) run.events.push(line);
  const res = clients.get(runId);
  if (res) res.write(`data: ${JSON.stringify(line)}\n\n`);
}

function persistRun(runId) {
  const run = runs.get(runId);
  if (!run) return;
  fs.writeFileSync(path.join(RUNS_DIR, `${runId}.json`), JSON.stringify(run, null, 2), 'utf8');
}

function startAction(action, inputs) {
  if (!action) throw new Error('Action not found.');
  const { command, args, inputValues } = buildCommand(action, inputs);
  const runId = `run_${Date.now()}_${Math.random().toString(16).slice(2)}`;
  const run = { runId, actionId: action.id, title: action.title, command, args, inputValues, status: 'running', events: [] };
  runs.set(runId, run);

  setTimeout(() => {
    emit(runId, 'start', { message: `Starting: ${action.title}`, commandPreview: [command, ...args].join(' ') });
    const child = spawn(command, args, { cwd: ROOT, shell: false, windowsHide: false, env: process.env });
    run.pid = child.pid;
    child.stdout.on('data', chunk => emit(runId, 'stdout', { message: chunk.toString() }));
    child.stderr.on('data', chunk => emit(runId, 'stderr', { message: chunk.toString() }));
    child.on('error', err => {
      run.status = 'failed';
      emit(runId, 'error', { message: err.message });
      persistRun(runId);
    });
    child.on('close', code => {
      run.status = code === 0 ? 'success' : 'failed';
      run.exitCode = code;
      emit(runId, 'finish', { message: `Finished with exit code ${code}`, exitCode: code, status: run.status });
      persistRun(runId);
    });
  }, 10);

  return run;
}

function serveStatic(req, res) {
  const url = new URL(req.url, `http://${req.headers.host}`);
  let filePath = url.pathname === '/' ? '/index.html' : url.pathname;
  filePath = path.normalize(filePath).replace(/^([.][.][\/\\])+/, '');
  const fullPath = path.join(APP_DIR, filePath);
  if (!fullPath.startsWith(APP_DIR)) return sendText(res, 403, 'Forbidden');
  if (!fs.existsSync(fullPath) || fs.statSync(fullPath).isDirectory()) return sendText(res, 404, 'Not found');
  const ext = path.extname(fullPath).toLowerCase();
  const contentTypes = { '.html': 'text/html; charset=utf-8', '.js': 'application/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8' };
  res.writeHead(200, { 'Content-Type': contentTypes[ext] || 'application/octet-stream' });
  fs.createReadStream(fullPath).pipe(res);
}

async function handleApi(req, res) {
  const url = new URL(req.url, `http://${req.headers.host}`);
  if (req.method === 'GET' && url.pathname === '/api/project') return writeJson(res, 200, readJson('.runhub.project/project.json', {}));
  if (req.method === 'GET' && url.pathname === '/api/categories') return writeJson(res, 200, categories());
  if (req.method === 'GET' && url.pathname === '/api/actions') return writeJson(res, 200, actions());

  if (req.method === 'POST' && url.pathname === '/api/run') {
    const body = await getBody(req);
    const action = actions().find(a => a.id === body.actionId);
    if (!action) return writeJson(res, 404, { error: 'Action not found.' });
    if (action.requiresConfirmation) {
      if (!body.confirmationAccepted) return writeJson(res, 400, { error: 'Confirmation required.' });
      if (action.confirmPhrase && body.confirmPhrase !== action.confirmPhrase) return writeJson(res, 400, { error: `Type confirmation phrase: ${action.confirmPhrase}` });
    }
    const run = startAction(action, body.inputs || {});
    return writeJson(res, 200, { runId: run.runId, status: run.status });
  }

  const eventMatch = url.pathname.match(/^\/api\/runs\/([^/]+)\/events$/);
  if (req.method === 'GET' && eventMatch) {
    const runId = eventMatch[1];
    res.writeHead(200, { 'Content-Type': 'text/event-stream; charset=utf-8', 'Cache-Control': 'no-cache, no-transform', 'Connection': 'keep-alive' });
    clients.set(runId, res);
    const run = runs.get(runId);
    if (run) for (const event of run.events) res.write(`data: ${JSON.stringify(event)}\n\n`);
    req.on('close', () => clients.delete(runId));
    return;
  }

  return writeJson(res, 404, { error: 'API endpoint not found.' });
}

const server = http.createServer(async (req, res) => {
  try {
    if (req.url.startsWith('/api/')) return await handleApi(req, res);
    return serveStatic(req, res);
  } catch (err) {
    return writeJson(res, 500, { error: err.message });
  }
});

server.listen(PORT, () => {
  console.log(`DWH RunHub running at http://localhost:${PORT}`);
  console.log('Standard engine: .runhub/');
  console.log('Project config:  .runhub.project/');
});

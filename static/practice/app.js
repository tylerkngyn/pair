'use strict';
const $ = id => document.getElementById(id);
const views = ['account', 'setup', 'workspace', 'summary', 'lobby-view', 'queue-view', 'loading-view', 'match-intro'];
let identity = null, catalog = [], session = null, problems = {}, active = null;
let registering = false, selected = new Set(), draftTimer = null, saveQueue = Promise.resolve();
let clock = null, clockOffset = 0, busy = false, runner = null, harness = null;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function notice(message = '') { $('notice').textContent = message; $('notice').hidden = !message; }
function show(view) {
  if (view !== 'queue-view') stopSearchClock();
  if (!['loading-view', 'match-intro', 'workspace'].includes(view)) cancelMatchIntro();
  views.forEach(id => $(id).hidden = id !== view); document.body.dataset.view = view; notice();
}
function guard(action) { return async event => { try { await action(event); } catch (error) { notice(error.message || String(error)); } }; }
async function api(path, options = {}) {
  const headers = { ...(options.body ? {'Content-Type': 'application/json'} : {}), ...(identity ? {'X-CSRF-Token': identity.csrf} : {}), ...options.headers };
  const response = await fetch(`/api${path}`, {...options, signal:options.signal || AbortSignal.timeout(20000), headers, credentials: 'same-origin'});
  const body = await response.json();
  if (!response.ok) {
    if (response.status === 401 && !path.startsWith('/auth/')) { runner?.stop(); identity = null; updateAccount(); show('account'); }
    const error = new Error(typeof body.detail === 'string' ? body.detail : body.detail?.[0]?.msg || 'Request failed');
    error.status = response.status; throw error;
  }
  return body;
}
function updateAccount() { $('username').textContent = identity?.username || ''; $('signout').hidden = !identity; $('online-users').hidden = !identity; }
function theme(value) {
  document.documentElement.dataset.theme = value;
  $('theme').textContent = value === 'light' ? 'Dark' : 'Light';
  $('theme').setAttribute('aria-label', `Switch to ${value === 'light' ? 'dark' : 'light'} mode`);
  try { localStorage.setItem('pair-theme', value); } catch {}
}
try { theme(localStorage.getItem('pair-theme') || 'dark'); } catch { theme('dark'); }
$('theme').onclick = () => theme(document.documentElement.dataset.theme === 'light' ? 'dark' : 'light');

function accountMode(value) {
  registering = value;
  $('register-tab').classList.toggle('selected', value);
  $('login-tab').classList.toggle('selected', !value);
  $('account-submit').textContent = value ? 'Create account' : 'Sign in';
  $('password').autocomplete = value ? 'new-password' : 'current-password';
  notice();
}
$('register-tab').onclick = () => accountMode(true);
$('login-tab').onclick = () => accountMode(false);
$('account-form').onsubmit = guard(async event => {
  event.preventDefault(); $('account-submit').disabled = true; notice();
  try {
    identity = await api(`/auth/${registering ? 'register' : 'login'}`, {method:'POST', body:JSON.stringify({username:$('account-name').value, password:$('password').value})});
    $('password').value = ''; updateAccount(); await setup();
  } finally { $('account-submit').disabled = false; }
});
$('signout').onclick = guard(async () => {
  await stopMultiplayer();
  runner?.stop(); clearInterval(clock); clearTimeout(draftTimer);
  if (session && !$('workspace').hidden && !['completed', 'expired'].includes(session.status)) await flushDraft();
  await api('/auth/logout', {method:'POST'});
  identity = null; session = null; active = null; updateAccount(); show('account');
});

function filtered() {
  return catalog.filter(p => (!$('category').value || p.category === $('category').value) && ($('difficulty').value === 'mixed' || p.difficulty === $('difficulty').value));
}
function renderPicker() {
  renderProblemGraph(filtered(), $('search').value.trim().toLowerCase());
  updateMatching();
}
function updateMatching() {
  $('matching').textContent = selected.size ? `${selected.size} selected` : 'Random selection';
  const basket = $('selected-problems'); basket.replaceChildren(); basket.hidden = !selected.size;
  for (const pid of selected) {
    const problem = catalog.find(p => p.id === pid);
    const remove = element('button', `${problem.title} ×`, 'selected-problem'); remove.type = 'button';
    remove.setAttribute('aria-label', `Remove ${problem.title}`);
    remove.onclick = () => { selected.delete(pid); if (selected.size) $('problem-count').value = selected.size; renderPicker(); };
    basket.append(remove);
  }
}
for (const id of ['category', 'difficulty']) $(id).onchange = () => { $('difficulty').dataset.level = $('difficulty').value; renderPicker(); };
$('search').oninput = renderPicker;
$('randomize').onclick = () => { selected.clear(); renderPicker(); notice(); };
$('graph-back').onclick = () => {
  $('category').value = ''; $('search').value = ''; renderPicker();
  $('problem-list').querySelector('button')?.focus({preventScroll:true});
};
async function setup() {
  runner?.stop(); clearInterval(clock); clearTimeout(draftTimer); session = null; active = null;
  if (!catalog.length) {
    catalog = (await api('/catalog')).problems;
    for (const category of new Set(catalog.map(p => p.category))) { const option = element('option', category); option.value = category; $('category').append(option); }
  }
  show('setup'); renderPicker(); await Promise.all([renderHistory(), multiplayerHome()]);
}
async function renderHistory() {
  const history = (await api('/practice')).sessions, list = $('history'); list.replaceChildren();
  for (const s of history) {
    const row = element('button', undefined, 'history-row'); row.type = 'button';
    const title = element('span', s.problems.map(p => p.title).join(' / '));
    title.append(element('small', new Date(s.started_at * 1000).toLocaleString()));
    row.append(title, element('span', `${s.correctness}% · ${s.status}`));
    row.onclick = guard(() => openSession(s.id)); list.append(row);
  }
  await renderMatchHistory(list);
  if (!list.children.length) list.append(element('p', 'No sessions', 'hint'));
}
$('setup-form').onsubmit = guard(async event => {
  event.preventDefault(); notice();
  if (playTogether) { if ($('join-code').value.trim()) await joinLobby(); else await createLobby(); return; }
  const count = Number($('problem-count').value);
  if (selected.size && selected.size !== count) throw new Error(`Select exactly ${count} problems, or choose Random.`);
  const chosen = catalog.filter(p => selected.has(p.id));
  const categories = chosen.length ? [...new Set(chosen.map(p => p.category))] : ($('category').value ? [$('category').value] : []);
  const difficulty = chosen.length ? (chosen.every(p => p.difficulty === chosen[0].difficulty) ? chosen[0].difficulty : 'mixed') : $('difficulty').value;
  $('start').disabled = true;
  try {
    const result = await api('/practice', {method:'POST', body:JSON.stringify({categories, difficulty, duration_minutes:Number($('duration').value), problem_count:count, allow_overtime:$('overtime').checked, problem_ids:[...selected]})});
    await openSession(result.id);
  } finally { $('start').disabled = false; }
});

function draftKey(pid) { return `pair-draft:${identity.username}:${session.id}:${pid}`; }
function localDraft(pid, code) { try { localStorage.setItem(draftKey(pid), JSON.stringify({code, revision:session.work[pid].revision})); } catch {} }
function clearLocal(pid) { try { localStorage.removeItem(draftKey(pid)); } catch {} }
async function openSession(id) {
  $('again').textContent = 'New session';
  showLoading('Loading workspace…');
  try {
  session = await api(`/practice/${id}`);
  session.kind = 'solo';
  await mountWorkspace();
  } catch (error) { show('setup'); throw error; }
}
async function mountWorkspace(preloaded = null) {
  clockOffset = session.clock_offset ?? session.server_now * 1000 - Date.now();
  if (!Number.isFinite(clockOffset)) clockOffset = 0;
  const data = preloaded || await Promise.all(session.problems.map(p => api(`/problems/${p.id}`)));
  problems = Object.fromEntries(data.map(p => [p.id, p]));
  for (const p of data) {
    try {
      const local = JSON.parse(localStorage.getItem(draftKey(p.id)));
      if (local && local.revision >= session.work[p.id].revision && !['completed', 'expired'].includes(session.status)) session.work[p.id].localCode = local.code;
    } catch {}
  }
  if (['completed', 'expired'].includes(session.status)) { showSummary(); return; }
  show('workspace'); $('problem-tabs').replaceChildren();
  for (const [i, p] of data.entries()) {
    const tab = element('button', `${i + 1}. ${p.title}`); tab.type = 'button'; tab.dataset.problem = p.id;
    tab.onclick = guard(async () => { if (active === p.id || busy) return; await flushDraft(); renderProblem(p.id); }); $('problem-tabs').append(tab);
  }
  renderProblem(data[0].id); renderOpponent(); clearInterval(clock); clock = setInterval(tick, 500); tick();
}
function renderProblem(pid) {
  active = pid; const p = problems[pid], work = session.work[pid];
  for (const tab of $('problem-tabs').children) tab.classList.toggle('selected', tab.dataset.problem === pid);
  $('problem-meta').replaceChildren(element('span', p.category), element('span', p.difficulty, `badge difficulty-${p.difficulty}`)); $('problem-title').textContent = p.title; $('prompt').textContent = p.prompt; $('problem-link').href = p.url;
  $('examples').replaceChildren();
  for (const c of p.cases.slice(0, 2)) $('examples').append(element('pre', `Input: ${JSON.stringify(c.args)}\nOutput: ${JSON.stringify(c.expected)}`, 'example'));
  $('editor').value = work.localCode ?? work.code; lines(); $('save-state').textContent = work.localCode !== undefined ? 'Unsaved draft restored' : 'Saved';
  $('results').replaceChildren();
  if (work.result) renderResults(work.result, true);
  lockControls();
}
function lines() { $('line-numbers').textContent = Array.from({length:$('editor').value.split('\n').length}, (_, i) => i + 1).join('\n'); }
$('editor').onscroll = () => { $('line-numbers').scrollTop = $('editor').scrollTop; };
$('editor').oninput = () => {
  lines(); if (!session || !active) return;
  session.work[active].localCode = $('editor').value; localDraft(active, $('editor').value);
  $('save-state').textContent = 'Unsaved'; clearTimeout(draftTimer);
  draftTimer = setTimeout(() => { flushDraft().catch(error => notice(error.message)); }, 700);
};
$('editor').onkeydown = event => {
  if (event.key === 'Tab') { event.preventDefault(); $('editor').setRangeText('    ', $('editor').selectionStart, $('editor').selectionEnd, 'end'); $('editor').oninput(); }
  if (event.key === 'Enter' && !event.ctrlKey && !event.metaKey) {
    event.preventDefault(); const before = $('editor').value.slice(0, $('editor').selectionStart).split('\n').pop();
    const indent = before.match(/^\s*/)[0] + (before.trimEnd().endsWith(':') ? '    ' : '');
    $('editor').setRangeText('\n' + indent, $('editor').selectionStart, $('editor').selectionEnd, 'end'); $('editor').oninput();
  }
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); if (!busy) execute(false).catch(error => notice(error.message)); }
};
function flushDraft(report = null, code = $('editor').value, pid = active) {
  clearTimeout(draftTimer);
  if (!session || !pid || session.my_finished || ['completed', 'expired'].includes(session.status)) return Promise.resolve();
  const current = session, owner = identity?.username;
  const task = saveQueue.catch(() => {}).then(async () => {
    if (current !== session || owner !== identity?.username) return;
    const work = current.work[pid];
    if (!report && work.code === code) return;
    $('save-state').textContent = 'Saving…';
    try {
      const prefix = current.kind === 'match' ? '/ranked/matches' : '/practice';
      const saved = await api(`${prefix}/${current.id}/work/${pid}`, {method:'PUT', body:JSON.stringify({code, revision:work.revision, ...(report ? {report} : {})})});
      work.code = code; work.revision = saved.revision; work.result = saved.result;
      if (work.localCode === code) { delete work.localCode; clearLocal(pid); }
      else if (work.localCode !== undefined) localDraft(pid, work.localCode);
      $('save-state').textContent = work.localCode === undefined ? 'Saved' : 'Unsaved';
    } catch (error) { $('save-state').textContent = 'Not saved'; throw error; }
  });
  saveQueue = task; return task;
}
$('reset').onclick = () => { if (busy || !$('editor').value || !confirm('Replace this draft with the starter code?')) return; $('editor').value = problems[active].starter; $('editor').oninput(); };
function lockControls() {
  const ended = !session || session.my_finished || ['completed', 'expired'].includes(session.status);
  for (const id of ['run', 'submit', 'reset']) $(id).disabled = busy || ended;
  $('editor').readOnly = busy || ended;
  $('finish').disabled = busy || Boolean(session?.my_finished);
  $('finish').textContent = session?.kind === 'match' ? (session.my_finished ? 'Waiting for opponent' : 'Finish match') : 'Finish session';
  $('stop').hidden = !busy;
  for (const tab of $('problem-tabs').children) tab.disabled = busy;
}
function tick() {
  if (!session) return;
  const seconds = Math.ceil(session.deadline - (Date.now() + clockOffset) / 1000), overtime = seconds < 0;
  const absolute = Math.abs(seconds); $('timer').textContent = `${overtime ? '+' : ''}${Math.floor(absolute / 60).toString().padStart(2, '0')}:${(absolute % 60).toString().padStart(2, '0')}`;
  $('timer').title = overtime ? 'Overtime' : 'Time remaining';
  $('timer').classList.toggle('timer-warning', seconds <= 300 && seconds > 60);
  $('timer').classList.toggle('timer-danger', seconds <= 60);
  if (overtime && !session.settings.allow_overtime && session.status !== 'expired') {
    session.status = 'expired'; runner?.stop(); clearInterval(clock); lockControls();
    notice(session.kind === 'match' ? 'Time is up. Calculating the match result…' : 'Time is up. Editing has stopped; your local draft is retained. Finish the session to view results.');
  }
}

class PythonRunner {
  constructor() { this.cancel = null; }
  stop() { this.cancel?.(new Error('Execution stopped.')); }
  run(problem, code, full) {
    return new Promise((resolve, reject) => {
      const old = $('runner-frame'), frame = old.cloneNode(false); old.replaceWith(frame);
      let port, timer, finished = false, assets;
      const download = new AbortController();
      const cleanup = () => { clearTimeout(timer); download.abort(); window.removeEventListener('message', ready); port?.postMessage({type:'stop'}); port?.close(); frame.removeAttribute('src'); this.cancel = null; };
      const end = (error, result) => { if (finished) return; finished = true; cleanup(); error ? reject(error) : resolve(result); };
      this.cancel = error => end(error);
      const timeout = (ms, message) => { clearTimeout(timer); timer = setTimeout(() => end(new Error(message)), ms); };
      const ready = event => {
        if (event.source !== frame.contentWindow || event.origin !== 'null') return;
        if (event.data === 'pair-runner-ready') { end(new Error('Chrome opened an outdated Python runner. Refresh this page to load the current version.')); return; }
        if (event.data !== 'pair-runner-ready:bundled-v2') return;
        const channel = new MessageChannel(); port = channel.port1;
        port.onmessage = ({data}) => {
          if (data.type === 'ready') {
            if (data.version !== 'bundled-v2') { end(new Error('Python runner version mismatch. Refresh this page.')); return; }
            port.postMessage({type:'run', harness, problem, code, full, assets});
          }
          if (data.type === 'loading') { $('execution-state').textContent = 'Starting Python…'; timeout(30000, 'Python downloaded but could not initialize in this browser. Refresh the page and try Run again.'); }
          if (data.type === 'running') { $('execution-state').textContent = 'Running tests…'; timeout(10000, 'Execution exceeded 10 seconds. Check for an infinite loop or a slow approach.'); }
          if (data.type === 'result') end(null, data.result);
          if (data.type === 'error') end(new Error(data.message));
        };
        port.start(); frame.contentWindow.postMessage('pair-connect', '*', [channel.port2]);
      };
      timeout(90000, 'Python loading timed out. Check your connection to this app and try Run again.');
      $('execution-state').textContent = 'Loading Python…';
      loadPythonAssets(download.signal, (loaded, total) => {
        if (!finished) $('execution-state').textContent = `Loading Python · ${(loaded / 1048576).toFixed(1)} / ${(total / 1048576).toFixed(1)} MB`;
      }).then(bundle => {
        if (finished) return;
        assets = bundle; window.addEventListener('message', ready); frame.src = '/assets/runner.html?rev=bundled-v2';
        timeout(10000, 'The Python runner did not open. Refresh the page to load its latest version.');
      }).catch(error => end(error));
    });
  }
}
async function execute(full) {
  if (busy || !session || session.my_finished || ['completed', 'expired'].includes(session.status)) return;
  busy = true; lockControls(); notice();
  const pid = active, code = $('editor').value;
  try {
    $('execution-state').textContent = 'Saving draft…';
    await flushDraft();
    if (!harness) { const response = await fetch('/assets/harness.py', {signal:AbortSignal.timeout(20000)}); if (!response.ok) throw new Error('Could not load the Python test harness.'); harness = await response.text(); }
    runner = new PythonRunner(); const result = await runner.run(problems[pid], code, full);
    renderResults(result, full);
    if (full) { await flushDraft(result, code, pid); $('execution-state').textContent = 'Submission saved.'; }
    else $('execution-state').textContent = 'Examples complete.';
  } catch (error) { $('execution-state').textContent = error.message; throw error; }
  finally { busy = false; runner = null; lockControls(); }
}
$('run').onclick = guard(() => execute(false)); $('submit').onclick = guard(() => execute(true)); $('stop').onclick = () => runner?.stop();
function timing(result) {
  if (result.runtime_ms == null) return 'Efficiency measured after all tests pass.';
  const reference = result.reference_ms;
  const relative = reference >= .05 ? ` · ${(result.runtime_ms / reference).toFixed(2)}× reference time` : ' · suite too small for a stable efficiency ratio';
  return `Suite ${result.runtime_ms.toFixed(3)} ms · reference ${reference.toFixed(3)} ms${relative}`;
}
function renderResults(result, full) {
  const root = $('results'); root.replaceChildren();
  const metrics = element('div', undefined, 'result-metrics'), correct = element('div', full ? 'Practice correctness' : 'Example results');
  correct.prepend(element('strong', `${result.passed} / ${result.total} passed`)); metrics.append(correct); root.append(metrics, element('p', timing(result), 'hint'));
  if (result.error) root.append(element('pre', result.error, 'output'));
  for (const [i, c] of result.cases.entries()) {
    const details = element('details', undefined, 'case'); details.open = !c.passed; details.dataset.passed = c.passed;
    details.append(element('summary', `${c.passed ? 'Passed' : 'Failed'} · case ${i + 1}`), element('pre', c.error || `Expected: ${c.expected}\nReceived: ${c.actual}`)); root.append(details);
  }
  if (result.stdout) root.append(element('h2', 'Output'), element('pre', result.stdout, 'output'));
}
$('finish').onclick = guard(async () => {
  if (session?.kind === 'match') { await finishCurrentMatch(false); return; }
  if (!session || busy) return; await flushDraft(); clearInterval(clock);
  session = await api(`/practice/${session.id}/finish`, {method:'POST'}); showSummary();
});
function showSummary() {
  clearInterval(clock); show('summary');
  $('summary-outcome').hidden = true;
  $('summary-score').textContent = `${session.correctness}% correctness`;
  $('summary-time').textContent = `${session.submitted} / ${session.problems.length} problems submitted · ${Math.max(0, Math.round(((session.finished_at || Math.min(Date.now() / 1000, session.deadline)) - session.started_at) / 60))} minutes`;
  $('summary-problems').replaceChildren();
  for (const p of session.problems) {
    const work = session.work[p.id], item = element('div', undefined, 'summary-item');
    item.append(element('strong', p.title));
    item.append(element('p', work.result ? `${work.result.passed}/${work.result.total} tests passed` : 'Not submitted'), element('p', work.result ? timing(work.result) : '', 'hint'));
    let reviewCode = work.code, localReview = false;
    try {
      const local = JSON.parse(localStorage.getItem(draftKey(p.id)));
      if (local && local.revision >= work.revision && local.code !== work.code) { reviewCode = local.code; localReview = true; }
    } catch {}
    const details = element('details'); details.append(element('summary', localReview ? 'Your code · unsaved local draft' : 'Your code'), element('pre', reviewCode)); item.append(details); $('summary-problems').append(item);
  }
  if (session.kind === 'match') renderMatchSummary();
}
$('again').onclick = guard(setup);
window.addEventListener('beforeunload', event => {
  if (active && session?.work[active]?.localCode !== undefined && !['completed', 'expired'].includes(session.status)) { event.preventDefault(); event.returnValue = ''; }
});
(async () => {
  try { identity = await api('/auth/me'); updateAccount(); await setup(); }
  catch (error) { show('account'); if (error.message !== 'Sign in to continue') notice(error.message); }
})();

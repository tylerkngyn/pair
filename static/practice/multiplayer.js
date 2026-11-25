'use strict';
let playTogether = false, currentLobby = null, searching = false, searchStarted = 0;
let multiplayerSocket = null, socketHeartbeat = null, socketRetry = null, multiplayerPoll = null;
let pollBusy = false, openingMatch = null, allowSocket = false, nextLobbyRefresh = 0;
let lobbyAction = false;
let nextPresenceRefresh = 0;
let searchClock = null, searchVersion = 0, matchIntroController = null;

function stopSearchClock() { clearTimeout(searchClock); searchClock = null; }
function startSearchClock() {
  stopSearchClock();
  const tickSearch = () => {
    if (!searching || $('queue-view').hidden) return;
    const elapsedMs = Math.max(0, Date.now() - searchStarted), seconds = Math.floor(elapsedMs / 1000);
    const label = `${String(Math.floor(seconds / 60)).padStart(2,'0')}:${String(seconds % 60).padStart(2,'0')}`;
    if ($('queue-elapsed').textContent !== label) $('queue-elapsed').textContent = label;
    searchClock = setTimeout(tickSearch, 1000 - elapsedMs % 1000);
  };
  tickSearch();
}
function cancelMatchIntro() {
  matchIntroController?.abort(); openingMatch = null;
}
function introPause(ms, signal) {
  signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    const abort = () => { clearTimeout(timer); reject(signal.reason); };
    const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve(); }, ms);
    signal.addEventListener('abort', abort, {once:true});
  });
}

function showLoading(title) {
  $('loading-title').textContent = title; show('loading-view');
}
async function refreshPresence() {
  try {
    const result = await api('/presence');
    $('online-count').textContent = result.online;
    $('online-users').dataset.unavailable = 'false';
    $('online-users').setAttribute('aria-label', `${result.online} users online`);
  } catch {
    $('online-count').textContent = '—'; $('online-users').dataset.unavailable = 'true';
    $('online-users').setAttribute('aria-label', 'Online count unavailable');
  }
}
function multiplayerFilters() {
  return {categories:$('category').value ? [$('category').value] : [], difficulty:$('difficulty').value,
    duration_minutes:Number($('duration').value), problem_count:Number($('problem-count').value)};
}
function settingsChips(root, settings) {
  root.replaceChildren(element('span', settings.categories.length ? settings.categories.join(', ') : 'All topics'),
    element('span', settings.difficulty, `difficulty-${settings.difficulty}`),
    element('span', `${settings.problem_count} problem${settings.problem_count === 1 ? '' : 's'}`), element('span', `${settings.duration_minutes} min`));
}
function setPlayMode(together) {
  playTogether = together;
  $('solo-mode').classList.toggle('selected', !together); $('multiplayer-mode').classList.toggle('selected', together);
  $('solo-mode').setAttribute('aria-pressed', String(!together)); $('multiplayer-mode').setAttribute('aria-pressed', String(together));
  $('solo-picker').hidden = together; $('lobby-browser').hidden = !together;
  $('multiplayer-actions').hidden = !together; $('start').hidden = together;
  $('overtime').parentElement.hidden = together; $('matching').hidden = together;
  $('lobby-privacy').hidden = !together;
  if (together && identity) refreshLobbies().catch(error => notice(error.message));
}
function connectMultiplayer() {
  if (!allowSocket || !identity || multiplayerSocket?.readyState < WebSocket.CLOSING) return;
  const socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws`);
  multiplayerSocket = socket;
  socket.onopen = () => {
    if (multiplayerSocket !== socket) return;
    $('queue-connection').textContent = 'Connected'; refreshPresence();
    clearInterval(socketHeartbeat);
    socketHeartbeat = setInterval(() => { if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({type:'ping', payload:{}})); }, 20000);
  };
  socket.onmessage = event => {
    if (multiplayerSocket !== socket) return;
    let data; try { data = JSON.parse(event.data); } catch { return; }
    if (data.type === 'match_found' && searching) openMatch(data.payload.match_id).catch(error => notice(error.message));
    if (data.type === 'opponent_progress' && session?.kind === 'match') refreshCurrentMatch().catch(() => {});
    if (data.type === 'match_result' && session?.kind === 'match') refreshCurrentMatch().catch(() => {});
  };
  socket.onclose = () => {
    if (multiplayerSocket !== socket) return;
    multiplayerSocket = null; clearInterval(socketHeartbeat);
    $('queue-connection').textContent = 'Reconnecting…';
    if (allowSocket && identity) socketRetry = setTimeout(connectMultiplayer, 2000);
  };
  socket.onerror = () => {}; // Polling remains available while WS reconnects.
}
async function multiplayerHome() {
  allowSocket = true; connectMultiplayer();
  if (!multiplayerPoll) multiplayerPoll = setInterval(() => pollMultiplayer().catch(error => {
    if (searching) $('queue-connection').textContent = 'Reconnecting…';
    else if (currentLobby) $('lobby-status').textContent = 'Reconnecting…';
  }), 1800);
  setPlayMode(playTogether);
  const rating = await api('/ranked/rating');
  $('rating-chip').textContent = `${rating.tier} · ${rating.rating} Elo`; $('rating-chip').hidden = false;
  const existing = await api('/ranked/active');
  if (existing.match?.status === 'active') { await openMatch(existing.match.id); return; }
  const home = await api('/lobbies/current');
  if (home.lobby) { renderLobby(home.lobby); return; }
  const queue = await api('/ranked/queue');
  if (queue.status === 'matched') { await openMatch(queue.match_id); return; }
  if (queue.status === 'queued') { showSearch(queue.filters, queue.queued_at); return; }
  const invite = new URLSearchParams(location.search).get('lobby');
  if (invite) { setPlayMode(true); $('join-code').value = invite.toUpperCase(); }
}
async function refreshLobbies() {
  const result = await api('/lobbies'), root = $('lobby-list'); root.replaceChildren();
  for (const lobby of result.lobbies) {
    const row = element('div', undefined, 'lobby-card'), title = element('div');
    title.append(element('strong', `${lobby.host}'s lobby`), element('p', `${lobby.settings.categories[0] || 'All topics'} · ${lobby.settings.difficulty} · ${lobby.settings.duration_minutes} min`));
    const button = element('button', lobby.is_host ? 'Your lobby' : 'Join'); button.type = 'button';
    button.onclick = guard(async () => { button.disabled = true; try { await joinLobby(lobby.code); } finally { button.disabled = false; } });
    row.append(title, button); root.append(row);
  }
  if (!root.children.length) {
    const empty = element('div', undefined, 'empty-state');
    empty.append(element('strong', 'No open lobbies')); root.append(empty);
  }
}
async function createLobby() {
  if (lobbyAction) return; lobbyAction = true; $('create-lobby').disabled = true;
  showLoading('Creating lobby…');
  try { renderLobby(await api('/lobbies', {method:'POST', body:JSON.stringify({...multiplayerFilters(), is_private:$('private-lobby').checked})})); }
  catch (error) { show('setup'); throw error; }
  finally { lobbyAction = false; $('create-lobby').disabled = false; }
}
async function joinLobby(code = $('join-code').value) {
  code = code.trim().toUpperCase();
  if (!/^[A-Z0-9]{6}$/.test(code)) throw new Error('Enter a six-character room code.');
  if (lobbyAction) return; lobbyAction = true;
  showLoading('Joining lobby…');
  try { renderLobby(await api(`/lobbies/${code}/join`, {method:'POST'})); }
  catch (error) { show('setup'); throw error; }
  finally { lobbyAction = false; }
}
function renderLobby(lobby) {
  // A response sent before Start must not replace an opening/running match.
  if (lobby.status === 'waiting' && (openingMatch || (session?.kind === 'match' && session.status === 'active'))) return;
  playTogether = true;
  currentLobby = lobby; searching = false;
  if (lobby.status === 'started') { openMatch(lobby.match_id).catch(error => notice(error.message)); return; }
  if (lobby.status === 'closed') { currentLobby = null; setPlayMode(true); show('setup'); notice('Lobby closed.'); return; }
  if ($('lobby-view').hidden) show('lobby-view');
  $('room-code-display').textContent = lobby.code;
  $('lobby-visibility').textContent = `${lobby.is_private ? 'Private' : 'Public'} · Unranked`;
  settingsChips($('lobby-settings'), lobby.settings);
  const slots = $('lobby-players'); slots.replaceChildren();
  for (const [name, ready, label] of [[lobby.host, lobby.host_ready, 'Host'], [lobby.guest, lobby.guest_ready, 'Partner']]) {
    const slot = element('div', undefined, `player-slot${name ? '' : ' empty'}`);
    slot.append(element('div', name ? name[0].toUpperCase() : '…', 'avatar'), element('strong', name ? `${name}${name === identity.username ? ' (you)' : ''}` : 'Open seat'), element('p', name ? `${label} · ${ready ? 'Ready' : 'Not ready'}` : 'Waiting to join', ready ? 'ready' : ''));
    slots.append(slot);
  }
  const ownReady = lobby.is_host ? lobby.host_ready : lobby.guest_ready;
  $('lobby-ready').textContent = ownReady ? 'Not ready' : 'Ready';
  $('lobby-start').hidden = !lobby.is_host; $('lobby-start').disabled = !lobby.guest || !lobby.host_ready || !lobby.guest_ready;
  $('lobby-status').textContent = !lobby.guest ? '1 / 2 players' : (lobby.host_ready && lobby.guest_ready ? 'Ready' : '2 / 2 players');
}
function showSearch(settings, queuedAt) {
  searchVersion++;
  playTogether = true; searching = true; searchStarted = queuedAt ? queuedAt * 1000 : Date.now(); currentLobby = null;
  $('category').value = settings.categories[0] || ''; $('difficulty').value = settings.difficulty;
  $('difficulty').dataset.level = settings.difficulty;
  $('problem-count').value = settings.problem_count; $('duration').value = settings.duration_minutes;
  settingsChips($('queue-settings'), settings); show('queue-view'); startSearchClock();
}
async function findRanked() {
  if (searching) return;
  const filters = multiplayerFilters(); showSearch(filters);
  try {
    const result = await api('/ranked/queue', {method:'POST', body:JSON.stringify(filters)});
    if (result.status === 'matched') await openMatch(result.match_id);
    else if (result.queued_at && searching) { searchStarted = result.queued_at * 1000; startSearchClock(); }
  }
  catch (error) {
    if (!searching && (openingMatch || session?.kind === 'match')) return;
    searching = false; show('setup'); throw error;
  }
}
async function pollMultiplayer() {
  if (!identity || pollBusy || openingMatch) return; pollBusy = true;
  try {
    if (Date.now() > nextPresenceRefresh) { nextPresenceRefresh = Date.now() + 5000; await refreshPresence(); }
    if (searching) {
      const version = searchVersion;
      const result = await api('/ranked/queue');
      if (!searching || version !== searchVersion) return;
      if (result.status === 'matched') await openMatch(result.match_id);
      else if (result.status === 'idle') {
        const existing = await api('/ranked/active');
        if (existing.match?.status === 'active') await openMatch(existing.match.id);
        else { searching = false; await setup(); notice('Your search ended. Start a new search to try again.'); }
      }
    } else if (currentLobby && !$('lobby-view').hidden) {
      const code = currentLobby.code;
      try {
        const lobby = await api(`/lobbies/${code}`);
        if (openingMatch || currentLobby?.code !== code || $('lobby-view').hidden) return;
        renderLobby(lobby);
      }
      catch (error) {
        if (error.status !== 404) throw error;
        currentLobby = null; await setup(); notice('Your lobby seat expired. Join again or create a lobby.');
      }
    } else if (session?.kind === 'match' && !$('workspace').hidden) {
      await refreshCurrentMatch();
    } else if (playTogether && !$('setup').hidden && Date.now() > nextLobbyRefresh) {
      nextLobbyRefresh = Date.now() + 5000; await refreshLobbies();
    }
  } finally { pollBusy = false; }
}
function matchSession(data) {
  return {...data, kind:'match', clock_offset:data.server_now * 1000 - Date.now(), settings:{allow_overtime:false}, result_source:'browser_practice'};
}
async function showMatchIntro(current, signal) {
  $('intro-mode').textContent = current.is_ranked ? 'Ranked · 1v1' : 'Unranked · 1v1';
  for (const [side, player] of Object.entries(current.players)) {
    $(`intro-${side}-avatar`).textContent = player.username[0].toUpperCase();
    $(`intro-${side}-name`).textContent = player.username;
    $(`intro-${side}-rating`).textContent = `${player.rating} Elo · ${player.tier}`;
    $(`intro-${side}-games`).textContent = `${player.matches_played} ranked matches`;
    $(`intro-${side}-rating`).hidden = !current.is_ranked;
    $(`intro-${side}-games`).hidden = !current.is_ranked;
  }
  $('intro-countdown').textContent = ''; $('intro-heading').textContent = 'Match found';
  show('match-intro');
  const requestOptions = () => ({signal:AbortSignal.any([signal, AbortSignal.timeout(20000)])});
  const receive = data => {
    if (signal.aborted || session !== current || $('match-intro').hidden) return;
    Object.assign(current, matchSession(data));
  };
  if (current.intro && !current.intro.my_ready) {
    // Acknowledge only after this browser loaded the problems AND displayed
    // the versus screen, so a fast peer cannot start the slow peer's clock.
    await introPause(1200, signal);
    receive(await api(`/ranked/matches/${current.id}/ready`, {...requestOptions(), method:'POST'}));
  }
  let refreshing = false, nextRefresh = 0;
  while (true) {
    signal.throwIfAborted();
    if (current.status === 'completed') { showSummary(); return; }
    const startsAt = current.intro ? current.intro.starts_at : current.started_at;
    const remaining = startsAt === null ? null : startsAt * 1000 - (Date.now() + current.clock_offset);
    if (remaining !== null && remaining <= 0) break;
    const value = remaining !== null && remaining <= 3000 ? String(Math.ceil(remaining / 1000)) : '';
    const heading = value ? 'Starting in' : 'Match found';
    if ($('intro-heading').textContent !== heading) $('intro-heading').textContent = heading;
    if ($('intro-countdown').textContent !== value) {
      $('intro-countdown').textContent = value;
      if (!matchMedia('(prefers-reduced-motion: reduce)').matches) $('intro-countdown').animate([{opacity:.25, transform:'scale(.9)'},{opacity:1, transform:'scale(1)'}], {duration:200});
    }
    if (current.intro && !refreshing && Date.now() >= nextRefresh) {
      refreshing = true; nextRefresh = Date.now() + 800;
      api(`/ranked/matches/${current.id}`, requestOptions()).then(receive).catch(() => {
        if (!signal.aborted) $('intro-heading').textContent = 'Reconnecting…';
      }).finally(() => { refreshing = false; });
    }
    await introPause(remaining === null ? 100 : Math.min(100, remaining), signal);
  }
}
async function openMatch(id) {
  if (openingMatch || (session?.id === id && session.kind === 'match' && !$('workspace').hidden)) return;
  openingMatch = id; searching = false; currentLobby = null; playTogether = true;
  const controller = new AbortController(), owner = identity?.username, connectedAt = performance.now();
  matchIntroController = controller; clearInterval(clock);
  const options = () => ({signal:AbortSignal.any([controller.signal, AbortSignal.timeout(20000)])});
  showLoading('Connecting…');
  try {
    const data = await api(`/ranked/matches/${id}`, options());
    controller.signal.throwIfAborted();
    session = matchSession(data);
    const current = session;
    const preloaded = await Promise.all(current.problems.map(p => api(`/problems/${p.id}`, options())));
    controller.signal.throwIfAborted();
    const pendingIntro = current.intro ? current.intro.starts_at === null || current.server_now < current.intro.starts_at : current.server_now < current.started_at;
    if (current.status === 'active' && pendingIntro) {
      await introPause(Math.max(0, 700 - (performance.now() - connectedAt)), controller.signal);
      await showMatchIntro(current, controller.signal);
    }
    controller.signal.throwIfAborted();
    if (identity?.username !== owner || session !== current) return;
    await mountWorkspace(preloaded);
    try { localStorage.setItem(`pair-active-match:${identity.username}`, id); } catch {}
  } catch (error) {
    if (!controller.signal.aborted && identity?.username === owner) { show('setup'); throw error; }
  }
  finally {
    controller.abort(); // Stop any intro poll still in flight after mounting.
    if (matchIntroController === controller) { matchIntroController = null; openingMatch = null; }
  }
}
function renderOpponent() {
  const isMatch = session?.kind === 'match'; $('versus-bar').hidden = !isMatch;
  $('match-waiting').hidden = !isMatch || !session.my_finished || session.status === 'completed';
  if (!isMatch) return;
  $('match-mode').textContent = session.is_ranked ? 'Ranked · 1v1' : 'Unranked · 1v1';
  $('opponent-name').textContent = `${identity.username} vs ${session.opponent}`;
  const passed = Object.values(session.opponent_statuses || {}).filter(s => s === 'passed').length;
  $('opponent-progress').textContent = session.opponent_finished ? `${session.opponent} has finished` : `${session.opponent}: ${passed}/${session.problems.length} solved · ${session.opponent_correctness}% tests passed`;
}
async function refreshCurrentMatch() {
  if (!session || session.kind !== 'match') return;
  const current = session, data = await api(`/ranked/matches/${current.id}`);
  if (session !== current) return;
  // Keep locally edited code/revisions while polling opponent state.
  const {work, ...meta} = data; Object.assign(current, meta);
  renderOpponent(); lockControls();
  if (data.status === 'completed') {
    runner?.stop(); clearTimeout(draftTimer); showSummary();
  }
}
async function finishCurrentMatch(forfeit) {
  if (!session || busy || session.kind !== 'match') return;
  if (forfeit && !confirm(session.is_ranked ? 'Leave this match? This counts as a ranked loss.' : 'Leave this match? Your opponent will win this unranked match.')) return;
  if (!forfeit && !confirm('Finish this match? Your answers will be locked while your opponent finishes.')) return;
  await flushDraft(); $('finish').disabled = true;
  try {
    const data = await api(`/ranked/matches/${session.id}/finish`, {method:'POST', body:JSON.stringify({forfeit})});
    Object.assign(session, data); renderOpponent(); lockControls();
    if (session.status === 'completed') showSummary();
  } catch (error) { lockControls(); throw error; }
}
function renderMatchSummary() {
  if (session.intro?.cancelled) {
    $('summary-outcome').hidden = false; $('summary-outcome').className = 'result-outcome badge';
    $('summary-outcome').textContent = 'Match cancelled'; $('summary-score').textContent = 'Opponent unavailable';
    $('summary-time').textContent = 'Rating unchanged'; $('summary-problems').replaceChildren();
    $('again').textContent = 'Back to practice'; return;
  }
  const outcome = session.my_outcome || 'draw';
  $('summary-outcome').hidden = false; $('summary-outcome').className = `result-outcome badge ${outcome}`;
  $('summary-outcome').textContent = {win:'You won', loss:'Good match', draw:'A draw', abandoned:'Match left'}[outcome] || 'Match complete';
  const delta = (session.my_rating_after ?? session.my_rating_before) - session.my_rating_before;
  $('summary-time').textContent = `${session.opponent}: ${session.opponent_correctness}% correctness · ${session.is_ranked ? `${session.my_rating_after} Elo (${delta >= 0 ? '+' : ''}${delta})` : 'Unranked · no rating change'}`;
  $('again').textContent = 'Back to practice';
  try { localStorage.removeItem(`pair-active-match:${identity.username}`); } catch {}
}
async function renderMatchHistory(root) {
  const matches = (await api('/ranked/history')).matches;
  for (const match of matches) {
    const button = element('button', undefined, 'history-row'); button.type = 'button';
    const title = element('span', `vs ${match.opponent || 'opponent'}`); title.prepend(element('span', match.is_ranked === false ? 'LOBBY' : '1V1', 'history-label'));
    title.append(element('small', new Date(match.started_at * 1000).toLocaleString()));
    button.append(title, element('span', match.cancelled ? 'Cancelled' : match.outcome || 'In progress')); button.onclick = guard(() => openMatch(match.id)); root.append(button);
  }
}
async function stopMultiplayer() {
  stopSearchClock(); cancelMatchIntro();
  if (searching) {
    try { await api('/ranked/queue', {method:'DELETE'}); } catch (error) { /* A simultaneous match remains resumable on sign-in. */ }
  }
  if (currentLobby?.status === 'waiting') await api(`/lobbies/${currentLobby.code}`, {method:'DELETE'});
  allowSocket = false; searching = false; currentLobby = null;
  clearTimeout(socketRetry); clearInterval(socketHeartbeat); clearInterval(multiplayerPoll); multiplayerPoll = null;
  multiplayerSocket?.close(); multiplayerSocket = null; $('rating-chip').hidden = true;
}

document.addEventListener('DOMContentLoaded', () => {
  $('solo-mode').onclick = () => setPlayMode(false); $('multiplayer-mode').onclick = () => setPlayMode(true);
  $('create-lobby').onclick = guard(createLobby); $('find-ranked').onclick = guard(findRanked);
  $('join-lobby').onclick = guard(() => joinLobby()); $('refresh-lobbies').onclick = guard(refreshLobbies);
  $('copy-code').onclick = guard(async () => {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(currentLobby.code);
    else { notice(`Room code: ${currentLobby.code}`); return; }
    $('copy-code').textContent = 'Copied'; setTimeout(() => $('copy-code').textContent = 'Copy code', 1600);
  });
  $('lobby-ready').onclick = guard(async () => { const own = currentLobby.is_host ? currentLobby.host_ready : currentLobby.guest_ready; renderLobby(await api(`/lobbies/${currentLobby.code}/ready`, {method:'PUT', body:JSON.stringify({ready:!own})})); });
  $('lobby-start').onclick = guard(async () => { $('lobby-start').disabled = true; try { renderLobby(await api(`/lobbies/${currentLobby.code}/start`, {method:'POST'})); } catch (error) { $('lobby-start').disabled = false; throw error; } });
  $('leave-lobby').onclick = guard(async () => { await api(`/lobbies/${currentLobby.code}`, {method:'DELETE'}); currentLobby = null; await setup(); setPlayMode(true); });
  $('cancel-queue').onclick = guard(async () => {
    $('cancel-queue').disabled = true;
    try { await api('/ranked/queue', {method:'DELETE'}); searching = false; await setup(); }
    catch (error) { const result = await api('/ranked/active'); if (result.match?.status === 'active') await openMatch(result.match.id); else throw error; }
    finally { $('cancel-queue').disabled = false; }
  });
  $('leave-match').onclick = guard(() => finishCurrentMatch(true));
});

const $ = id => document.getElementById(id);
let selected = null;
let refreshing = false;
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const chip = s => `<span class="chip">${esc(s)}</span>`;
let sessionPromise = fetch('/api/session', {credentials:'same-origin'}).then(async response => {
  if (response.status === 404) return null; // Accepted loopback mode.
  if (!response.ok) throw Error('Session unavailable');
  $('logout').hidden = false;
  return response.json();
});
async function showView(view) {
  $('workspace-view').hidden = view === 'brain' || view === 'connections';
  $('brain-view').hidden = view !== 'brain';
  $('connections-view').hidden = view !== 'connections';
  document.querySelectorAll('.main-nav button').forEach(button => button.setAttribute('aria-current', button.dataset.view === view ? 'page' : 'false'));
  if (view === 'connections') {
    try {
      const data = await api('/api/connections');
      $('connections-list').innerHTML = data.connections.map(c => `<article class="panel connection-card"><h2>${esc(c.tool)}</h2><p>${chip(c.status)}</p><dl><dt>Auth status</dt><dd>${esc(c.auth_status)}</dd><dt>Health</dt><dd>${esc(c.health)}</dd><dt>Capabilities</dt><dd>${(c.capabilities || []).map(esc).join(', ')}</dd></dl></article>`).join('');
    } catch (error) { $('connections-list').textContent = 'Connection registry unavailable: ' + error.message; }
  }
  if (!['brain', 'connections'].includes(view)) {
    const target = {chat: 'chat', work: 'work', systems: 'systems', approvals: 'approvals-activity'}[view];
    $(target)?.scrollIntoView({behavior: 'smooth'});
  }
}
document.querySelectorAll('.main-nav button').forEach(button => button.addEventListener('click', () => { location.hash = button.dataset.view; showView(button.dataset.view); }));
const initialView = location.hash.slice(1);
showView(['chat', 'work', 'brain', 'connections', 'systems', 'approvals'].includes(initialView) ? initialView : 'chat');
async function api(path, options) {
  if (options?.method === 'POST') {
    const session = await sessionPromise;
    if (session) options = {...options, headers:{...options.headers, 'X-CSRF-Token':session.csrf}};
  }
  const response = await fetch(path, {...options, credentials:'same-origin'});
  const value = await response.json();
  if (!response.ok) throw Error(value.error || response.status);
  return value;
}
$('logout').addEventListener('click', async () => {
  try { await api('/api/logout', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'}); }
  finally { location.assign('/signed-out'); }
});
function renderGoal(g) {
  $('goal-status').textContent = g.status.replaceAll('_', ' ');
  $('goal-status').className = 'pill ' + g.status;
  $('goal-text').textContent = g.instruction;
  $('goal-meta').innerHTML = `<div class="meta">${chip('Route: ' + (g.route || 'pending'))}${chip('Created: ' + g.created_at)}</div><small>Goal ${esc(g.id)} · Big Dizzi orchestrator</small><p><a href="/api/goals/${esc(g.id)}/export" download="${esc(g.id)}.json">Export portable result</a>${['running','awaiting_input','awaiting_approval'].includes(g.status) ? ` <button id="cancel-goal" type="button">Cancel running turn</button>` : ''}</p>`;
  $('goal-result').innerHTML = g.result ? `<h3>Result</h3><div class="result">${esc(g.result)}</div>` : '';
  $('goal-error').innerHTML = g.error ? `<p class="error">${esc(g.error)}</p>` : '';
  const artifactKey = g.artifact ? g.id + ':' + g.artifact.verification.browser_verified : '';
  if ($('artifact').dataset.goal !== artifactKey) {
    $('artifact').dataset.goal = artifactKey;
    $('artifact').innerHTML = g.artifact ? `<h3>${esc(g.artifact.title)}</h3><p class="hint">${g.artifact.verification.browser_verified ? 'Browser interactions verified' : 'Browser acceptance pending'} · ${esc(g.artifact.verification.limitation || '')}</p><p><a href="${esc(g.artifact.url)}" download>Download artifact</a></p>${g.artifact.previewable === false ? `<small>${(g.artifact.files || []).map(f => esc(f.name)).join(', ')}</small>` : `<iframe sandbox="allow-scripts" title="Application preview" src="${esc(g.artifact.preview_url || g.artifact.url)}"></iframe>`}` : '';
  }
  $('tasks').innerHTML = g.tasks.length ? g.tasks.map(t => {
    const r = t.record, u = r.usage || {}, actual = r.actual;
    const parent = t.parent_id === g.id ? 'Big Dizzi' : g.tasks.find(p => p.record.task_id === t.parent_id)?.capability || t.parent_id;
    const actualModel = actual?.model?.id || 'unreported';
    return `<div class="task"><small>↳ ${esc(parent)}</small><strong>${esc(t.capability)}</strong> <span class="pill ${esc(r.status)}">${esc(r.status)}</span><small>Requested: ${esc(r.request.model.provider)} / ${esc(r.request.model.id)}</small>${r.runtime_metadata?.auth_mode ? `<small>Codex — ChatGPT plan · Auth: ${esc(r.runtime_metadata.auth_mode)} · Plan: ${esc(r.runtime_metadata.plan || 'unreported')}</small><small>Plan usage: ${r.runtime_metadata.rate_limits?.primary?.usedPercent ?? 'unreported'}% in primary window · Reset: ${r.runtime_metadata.rate_limits?.primary?.resetsAt ? esc(new Date(r.runtime_metadata.rate_limits.primary.resetsAt * 1000).toLocaleString()) : 'unreported'}</small>` : ''}<small>Observed worker: ${esc(actual?.worker_id || 'unreported')} · Model: ${esc(actualModel)} · Runtime: ${esc(actual?.runtime || 'unreported')}</small><small>Task ${esc(r.task_id)} · Attempt ${t.attempt ?? 'unknown'} · Adapter retries ${t.adapter_retries ?? 'unreported'}</small><small>Started ${esc(r.timestamps.started_at || '—')} · Ended ${esc(r.timestamps.ended_at || '—')}</small><small>Input ${u.input_tokens ?? 'unknown'} · Output ${u.output_tokens ?? 'unknown'} · Reasoning ${u.reasoning_tokens ?? 'unknown'} · Cache read ${u.cache_read_tokens ?? 'unknown'} · Cost unknown</small>${r.status === 'running' && r.result ? `<div class="result">${esc(r.result.slice(-500))}</div>` : ''}${r.status !== 'running' && r.result ? `<details><summary>Task result</summary><div class="result">${esc(r.result)}</div></details>` : ''}${r.error ? `<p class="error">${esc(r.error.message)}</p>` : ''}</div>`;
  }).join('') : '<div class="empty">No worker tasks for this goal yet</div>';
  $('approvals').innerHTML = g.approvals.length ? g.approvals.map(a => `<div class="task"><strong>${esc(a.action)}</strong><p>${esc(a.decision)} · ${esc(a.at)}</p>${a.decision === 'requested' ? `<p class="hint">A reviewed package and supported executor are required before this action can run. Approval here records intent only.</p><div class="approval-actions"><button data-approval="${esc(a.id)}" data-decision="approved">Record approval</button><button data-approval="${esc(a.id)}" data-decision="denied">Deny</button></div>` : ''}</div>`).join('') : '<p class="empty">No approval requested for this goal.</p>';
  const requests = g.runtime_requests || [];
  const requestKey = JSON.stringify(requests.map(r => [r.id, r.status]));
  if ($('runtime-requests').dataset.key !== requestKey) {
    $('runtime-requests').dataset.key = requestKey;
    $('runtime-requests').innerHTML = requests.map(r => {
      const p = r.payload;
      if (p.questions) return `<form data-input-request="${esc(r.id)}" class="task"><strong>Runtime input · ${esc(r.status)}</strong>${p.questions.map(q => `<label>${esc(q.question)}${(q.options || []).length ? `<small>Options: ${q.options.map(o => esc(o.label)).join(' / ')}</small>` : ''}<textarea name="${esc(q.id)}" required maxlength="2000" ${r.status !== 'pending' ? 'disabled' : ''}></textarea></label>`).join('')}${r.status === 'pending' ? '<button type="submit">Send input</button>' : ''}</form>`;
      return `<div class="task"><strong>Runtime approval · ${esc(r.status)}</strong><p>${esc(p.action)}</p><pre>${esc(p.command || (p.paths || []).join('\n'))}</pre><p>${esc(p.reason)}</p><p class="hint">${esc(p.boundary)}</p>${r.status === 'pending' ? `<button data-runtime-request="${esc(r.id)}" data-decision="approved" ${p.can_approve ? '' : 'disabled'}>Approve</button> <button data-runtime-request="${esc(r.id)}" data-decision="denied">Deny</button>` : ''}</div>`;
    }).join('');
  }
  const continuationKey = g.id + ':' + g.status;
  if ($('continuation').dataset.key !== continuationKey) {
    $('continuation').dataset.key = continuationKey;
    $('continuation').innerHTML = g.status === 'interrupted' ? '<form id="continue-form"><p>Interrupted work is not replayed. Continue in read-only mode to inspect evidence and finish a report. Further file changes require a new scoped goal.</p><label>What should Codex inspect or explain?<textarea name="instruction" required maxlength="2000"></textarea></label><button type="submit">Continue with inspection only</button></form>' : '';
  }
  $('events').innerHTML = g.events.length ? g.events.slice().reverse().map(e => `<div class="event">${esc(e.event)}${e.detail ? ' · ' + esc(e.detail) : ''}<small>${esc(e.at)}</small></div>`).join('') : '<div class="empty">No activity yet</div>';
}
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const goals = await api('/api/goals');
    $('goals').innerHTML = goals.map(g => `<button class="goal-row" data-id="${esc(g.id)}"><span>${esc(g.instruction)}</span><span class="pill ${esc(g.status)}">${esc(g.status.replaceAll('_', ' '))}</span></button>`).join('') || '<div class="empty">No goals yet</div>';
    if (!selected && goals.length) selected = goals[0].id;
    if (selected) { const id = selected; const goal = await api('/api/goals/' + encodeURIComponent(id)); if (selected === id) renderGoal(goal); }
    const system = await api('/api/system');
    $('system').innerHTML = `<div class="meta">${chip(system.provider === 'codex' ? 'Codex — ChatGPT plan · checked at task start' : 'API key: ' + (system.cloud_configured ? 'present · access unverified' : 'missing'))}${chip('Core audit: ' + system.core_audit.length + ' findings')}${chip('Cost: unknown')}</div><p class="muted">${esc(system.local_status)}</p>${system.core_audit.slice(0,8).map(i => `<small>${esc(i.kind)} · ${esc(i.path)}</small><br>`).join('')}`;
    $('connection').textContent = 'Local UI · connected';
  } catch (e) { $('connection').textContent = 'Connection error'; $('goal-error').textContent = e.message; }
  finally { refreshing = false; }
}
$('goal-form').addEventListener('submit', async e => {
  e.preventDefault();
  const instruction = $('instruction').value.trim();
  if (!instruction) return;
  const button = e.target.querySelector('button'); button.disabled = true;
  try {
    const g = await api('/api/goals', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({instruction})});
    selected = g.id; $('instruction').value = ''; await refresh();
  } catch (error) { $('goal-error').textContent = error.message; }
  finally { button.disabled = false; }
});
$('goals').addEventListener('click', e => { const row = e.target.closest('[data-id]'); if (row) { selected = row.dataset.id; refresh(); } });
$('approvals').addEventListener('click', async e => {
  const button = e.target.closest('[data-approval]'); if (!button || !selected) return;
  button.disabled = true;
  try { await api(`/api/goals/${selected}/approvals/${button.dataset.approval}`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({decision:button.dataset.decision})}); await refresh(); }
  catch (error) { $('goal-error').textContent = error.message; }
  finally { button.disabled = false; }
});
refresh(); setInterval(refresh, 2500);

$('goal-meta').addEventListener('click', async e => { if (e.target.id !== 'cancel-goal' || !selected) return; e.target.disabled = true; try { await api(`/api/goals/${selected}/cancel`, {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'}); await refresh(); } catch (error) { $('goal-error').textContent = error.message; } });

$('runtime-requests').addEventListener('click', async e => {
  const b=e.target.closest('[data-runtime-request]'); if(!b || !selected) return;
  b.disabled=true;
  try { await api(`/api/goals/${selected}/requests/${b.dataset.runtimeRequest}`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({decision:b.dataset.decision})}); await refresh(); }
  catch(error) { $('goal-error').textContent=error.message; b.disabled=false; }
});
$('runtime-requests').addEventListener('submit', async e => {
  const f=e.target.closest('[data-input-request]'); if(!f || !selected) return; e.preventDefault();
  const b=f.querySelector('button'); b.disabled=true;
  try { await api(`/api/goals/${selected}/requests/${f.dataset.inputRequest}`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({answers:Object.fromEntries(new FormData(f))})}); await refresh(); }
  catch(error) { $('goal-error').textContent=error.message; b.disabled=false; }
});
$('continuation').addEventListener('submit', async e => {
  e.preventDefault(); const f=e.target, b=f.querySelector('button'); b.disabled=true;
  try { await api(`/api/goals/${selected}/continue`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({instruction:new FormData(f).get('instruction')})}); await refresh(); }
  catch(error) { $('goal-error').textContent=error.message; b.disabled=false; }
});

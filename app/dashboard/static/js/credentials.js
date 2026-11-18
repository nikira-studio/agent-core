// A date input yields YYYY-MM-DD with no timezone. The operator means "the end
// of that day where they are", so the instant is built in the browser's own
// timezone and converted to UTC — not stamped 23:59:59Z, which lands on the
// wrong calendar day for anyone east of London.
function endOfDayUtc(dateValue) {
  if (!dateValue) return null;
  const [y, m, d] = dateValue.split('-').map(Number);
  if (!y || !m || !d) return null;
  return new Date(y, m - 1, d, 23, 59, 59, 999).toISOString();
}

async function createCredential(e) {
  e.preventDefault();
  const body = {
    name: document.getElementById('credential-name').value,
    label: document.getElementById('credential-label').value || null,
    scope: document.getElementById('credential-scope').value,
    value: document.getElementById('credential-value').value,
  };
  const expires = endOfDayUtc(document.getElementById('credential-expires').value);
  if (expires) body.expires_at = expires;
  const j = await apiFetch('/api/credentials/entries', { method: 'POST', body: JSON.stringify(body) });
  if (j.ok) {
    showToast('Credential created', 'success');
    closeModal('create-credential-modal');
    document.getElementById('create-credential-form').reset();
    location.reload();
  } else {
    showToast(j.error?.message || 'Failed to create credential', 'danger');
  }
}

async function deleteCredential(id) {
  // Say which connections and agents use it before it goes (the list is admin-only; without it, a general warning).
  let used = '';
  const access = await apiFetch('/api/credential-consumers/entries/' + id + '/access');
  if (access.ok && access.data) {
    const names = (access.data.services || []).map(function(s) { return s.name; });
    const agents = (access.data.grants || []).map(function(g) { return g.display_name || g.agent_id; });
    if (names.length) used += '\n\nThese connections use it and will stop working until you attach another credential: ' + names.join(', ') + '.';
    if (agents.length) used += '\n\nThese agents are granted it and will lose it: ' + agents.join(', ') + '.';
  } else {
    used = '\n\nConnections that use it will stop working until you attach another credential.';
  }
  if (!await confirmDialog('Delete this credential?' + used)) return;
  const j = await apiFetch('/api/credentials/entries/' + id, { method: 'DELETE' });
  if (j.ok) { showToast('Credential deleted', 'success'); location.reload(); }
  else { showToast(j.error?.message || 'Failed to delete credential', 'danger'); }
}

async function editCredential(id) {
  const j = await apiFetch('/api/credentials/entries/' + id);
  if (!j.ok) { showToast(j.error?.message || 'Error', 'danger'); return; }
  const c = j.data.entry;
  document.getElementById('edit-credential-id').value = id;
  document.getElementById('edit-credential-name').value = c.name || '';
  document.getElementById('edit-credential-label').value = c.label || '';
  document.getElementById('edit-credential-scope').value = c.scope || '';
  document.getElementById('edit-credential-scope').dataset.originalScope = c.scope || '';
  document.getElementById('edit-credential-value').value = '';
  // Every credential has a value; say so, with when the entry last changed, and never show it.
  setSecretState('edit-credential-value', true, 'value', '', c.updated_at || c.created_at);
  // Back to a date in the viewer's timezone, so what they saved is what they see.
  document.getElementById('edit-credential-expires').value = c.expires_at
    ? new Date(c.expires_at).toLocaleDateString('en-CA')
    : '';
  openModal('edit-credential-modal');
}

async function submitEditCredential(e) {
  e.preventDefault();
  const id = document.getElementById('edit-credential-id').value;
  const replacementValue = document.getElementById('edit-credential-value').value;
  const body = {
    scope: document.getElementById('edit-credential-scope').value,
    name: document.getElementById('edit-credential-name').value,
    label: document.getElementById('edit-credential-label').value || null,
  };
  if (replacementValue) body.value = replacementValue;
  // Sent even when empty: clearing the field is how an expiry is removed.
  body.expires_at = endOfDayUtc(document.getElementById('edit-credential-expires').value);
  const originalScope = document.getElementById('edit-credential-scope').dataset.originalScope;
  if (body.scope !== originalScope && !await confirmDialog(
    'Move this credential from ' + originalScope + ' to ' + body.scope + '? Connections that use it keep their current workspace.'
  )) return;
  const j = await apiFetch('/api/credentials/entries/' + id, { method: 'PUT', body: JSON.stringify(body) });
  if (j.ok) {
    showToast('Credential updated', 'success');
    closeModal('edit-credential-modal');
    document.getElementById('edit-credential-form').reset();
    location.reload();
  } else {
    showToast(j.error?.message || 'Failed to update credential', 'danger');
  }
}


// Row actions carry their id as data. Interpolating it into an inline handler
// puts a value into JavaScript source, where HTML escaping does not protect it:
// entities are decoded before the handler is parsed.
document.addEventListener('click', function(ev) {
  const edit = ev.target.closest('[data-credential-edit]');
  if (edit) { ev.preventDefault(); editCredential(edit.dataset.credentialEdit); return; }
  const del = ev.target.closest('[data-credential-delete]');
  if (del) { ev.preventDefault(); deleteCredential(del.dataset.credentialDelete); }
});


async function fillCredentialRequest(e) {
  e.preventDefault();
  const id = document.getElementById('fill-request-id').value;
  const body = { value: document.getElementById('fill-request-value').value };
  const expires = endOfDayUtc(document.getElementById('fill-request-expires').value);
  if (expires) body.expires_at = expires;
  const j = await apiFetch('/api/credentials/requests/' + encodeURIComponent(id) + '/fulfill', {
    method: 'POST', body: JSON.stringify(body),
  });
  if (j.ok) { showToast('Saved. The agent receives the reference on its next request.', 'success'); location.reload(); }
  else { showToast(j.error?.message || 'Failed to save', 'danger'); }
}

document.addEventListener('click', async (event) => {
  const fill = event.target.closest('[data-request-fill]');
  if (fill) {
    document.getElementById('fill-request-form').reset();
    document.getElementById('fill-request-id').value = fill.dataset.requestFill;
    document.getElementById('fill-request-name').textContent = fill.dataset.requestName;
    openModal('fill-request-modal');
    return;
  }
  const cancel = event.target.closest('[data-request-cancel]');
  if (cancel) {
    if (!await confirmDialog('Decline this request? The agent will see it was not filled.')) return;
    const j = await apiFetch('/api/credentials/requests/' + encodeURIComponent(cancel.dataset.requestCancel) + '/cancel', { method: 'POST' });
    if (j.ok) location.reload(); else showToast(j.error?.message || 'Failed', 'danger');
  }
});


// ── Who can use a credential ────────────────────────────────────────────
// Everything from the server goes in through textContent, never innerHTML.
let accessEntryId = null;

function accessEl(tag, text, cls) {
  const el = document.createElement(tag);
  if (text != null) el.textContent = text;
  if (cls) el.className = cls;
  return el;
}

async function openAccess(entryId) {
  accessEntryId = entryId;
  document.getElementById('access-expires').value = '';
  document.getElementById('access-write').checked = false;
  await renderAccess();
  openModal('access-modal');
}

function closeAccess() {
  closeModal('access-modal');
}

async function renderAccess() {
  const j = await apiFetch('/api/credential-consumers/entries/' + encodeURIComponent(accessEntryId) + '/access');
  if (!j.ok) { showToast(j.error?.message || 'Could not load access', 'danger'); return; }
  const d = j.data;
  document.getElementById('access-name').textContent = d.entry.name;

  const grants = document.getElementById('access-grants');
  grants.replaceChildren();
  if (!d.grants.length) grants.append(accessEl('p', 'No one. Give it to an agent below so it can fetch the secret at startup.', 'text-muted'));
  d.grants.forEach(g => {
    const row = accessEl('div', null, 'access-row');
    row.append(accessEl('strong', g.display_name));
    if (g.expires_at) row.append(accessEl('span', ' until ' + new Date(g.expires_at).toLocaleDateString(), 'text-muted'));
    if (g.allow_write) row.append(accessEl('span', ' can write', 'badge badge-warning'));
    const remove = accessEl('button', 'Remove', 'btn btn-sm btn-secondary');
    remove.type = 'button';
    remove.addEventListener('click', async () => {
      const r = await apiFetch('/api/credential-consumers/grants/' + encodeURIComponent(g.id), { method: 'DELETE' });
      if (r.ok) { showToast('Access removed', 'success'); renderAccess(); }
      else showToast(r.error?.message || 'Could not remove access', 'danger');
    });
    row.append(remove);
    grants.append(row);
  });

  const pick = document.getElementById('access-agent');
  pick.replaceChildren();
  d.candidates.forEach(a => { const o = accessEl('option', a.display_name); o.value = a.id; pick.append(o); });
  document.getElementById('access-add').disabled = !d.candidates.length;
  if (!d.candidates.length) { const o = accessEl('option', 'No agent has the Service role'); o.value = ''; pick.append(o); }

  const cannot = document.getElementById('access-cannot');
  const list = document.getElementById('access-cannot-list');
  list.replaceChildren();
  cannot.style.display = d.cannot_receive.length ? '' : 'none';
  document.getElementById('access-cannot-count').textContent = d.cannot_receive.length;
  d.cannot_receive.forEach(a => {
    const row = accessEl('div', null, 'access-row');
    row.append(accessEl('span', a.display_name + ' needs the Service role or the Fetch secrets permission. '));
    const link = accessEl('a', 'Change its role');
    link.href = '/agents?edit=' + encodeURIComponent(a.id);
    row.append(link);
    list.append(row);
  });

  const services = document.getElementById('access-services');
  services.replaceChildren();
  if (!d.services.length) services.append(accessEl('p', 'None. No connection uses this credential yet.', 'text-muted'));
  d.services.forEach(sv => {
    const row = accessEl('div', null, 'access-row');
    const link = accessEl('a', sv.name + (sv.service ? ' (' + sv.service + ')' : ''));
    link.href = '/connectors#bindings';
    row.append(link);
    if (!sv.enabled) row.append(accessEl('span', ' off', 'text-muted'));
    services.append(row);
  });

  const readers = document.getElementById('access-scope-readers');
  readers.replaceChildren();
  if (d.scope_readers.length) {
    readers.append(accessEl('p', d.scope_readers.join(', '), 'used-by-others'));
    readers.append(accessEl('p', REFERENCE_NOTE + ' They can see it because they can read ' + accessScopeName(d.entry) + '.', 'form-hint'));
  } else {
    readers.append(accessEl('p', 'No one else. Only the agents above can use it.', 'text-muted'));
  }
  renderUsedBy(d);
}

// The scope as the list shows it in plain words (a workspace's name, "Just me"), never the raw scope string.
function accessScopeName(entry) {
  const tr = document.querySelector('tr[data-credential-id="' + CSS.escape(entry.id) + '"]');
  const cell = tr && tr.querySelector("td[data-label='Scope']");
  return (cell && cell.textContent.trim()) || 'its scope';
}

const REFERENCE_NOTE = 'They can see its name (its reference) and use it wherever Agent Core makes a connection with it. Agent Core does not show them the secret itself.';

// The Used by cell in the list shows the same two groups under the same labels as this dialog, so it is rebuilt
// from the dialog's data whenever access changes (the page is not reloaded). Mirrors used_by_cell in credentials_page.py.
function renderUsedBy(d) {
  const tr = document.querySelector('tr[data-credential-id="' + CSS.escape(d.entry.id) + '"]');
  const cell = tr && tr.querySelector("td[data-label='Used by']");
  if (!cell) return;
  cell.replaceChildren();
  const given = accessEl('div', null, 'used-by-given');
  given.append(accessEl('span', 'Given the secret:', 'used-by-label'), document.createTextNode(' '));
  if (!d.grants.length) given.append(accessEl('span', 'no one', 'text-muted'));
  d.grants.slice(0, 4).forEach(g => {
    const chip = accessEl('span', g.display_name, 'badge badge-info chip-trunc');
    chip.title = g.display_name;
    given.append(chip, document.createTextNode(' '));
  });
  if (d.grants.length > 4) given.append(accessEl('span', '+' + (d.grants.length - 4) + ' more', 'text-muted'));
  cell.append(given);
  if (d.scope_readers.length) {
    const seen = accessEl('div', d.scope_readers.length + ' other' + (d.scope_readers.length === 1 ? '' : 's') + ' can see its name', 'text-muted used-by-seen');
    seen.style.fontSize = '0.8rem';
    seen.title = d.scope_readers.join(', ') + '. ' + REFERENCE_NOTE;
    cell.append(seen);
  }
  if (d.services.length) {
    const sv = accessEl('div', d.services.length + ' connection' + (d.services.length === 1 ? '' : 's'), 'text-muted');
    sv.style.fontSize = '0.8rem';
    cell.append(sv);
  }
}

document.addEventListener('click', async (ev) => {
  const open = ev.target.closest('[data-credential-access]');
  if (open) { ev.preventDefault(); openAccess(open.dataset.credentialAccess); return; }
  if (ev.target.id === 'access-add') {
    const agent = document.getElementById('access-agent').value;
    if (!agent) return;
    const body = { consumer_agent_id: agent, entry_id: accessEntryId, allow_write: document.getElementById('access-write').checked };
    const expires = endOfDayUtc(document.getElementById('access-expires').value);
    if (expires) body.expires_at = expires;
    const r = await apiFetch('/api/credential-consumers/grants', { method: 'POST', body: JSON.stringify(body) });
    if (r.ok) { showToast('Access given', 'success'); renderAccess(); }
    else showToast(r.error?.message || 'Could not give access', 'danger');
  }
});

// ── One agent's credentials (opened from the Agents page) ─────────────────
let agentAccessId = null;

function credentialNames() {
  const names = {};
  document.querySelectorAll('tr[data-credential-id]').forEach(tr => {
    names[tr.dataset.credentialId] = tr.querySelector('td').childNodes[0].textContent.trim();
  });
  return names;
}

async function openAgentAccess(agentId) {
  agentAccessId = agentId;
  document.getElementById('agent-access-name').textContent = agentId;
  await renderAgentAccess();
  openModal('agent-access-modal');
}

function closeAgentAccess() { closeModal('agent-access-modal'); }

async function renderAgentAccess() {
  const j = await apiFetch('/api/credential-consumers/grants?consumer_agent_id=' + encodeURIComponent(agentAccessId));
  if (!j.ok) { showToast(j.error?.message || 'Could not load access', 'danger'); return; }
  const names = credentialNames();
  const active = j.data.grants.filter(g => !g.revoked_at && (!g.expires_at || new Date(g.expires_at) > new Date()));
  const list = document.getElementById('agent-access-list');
  list.replaceChildren();
  if (!active.length) list.append(accessEl('p', 'Nothing yet. Add a credential below.', 'text-muted'));
  active.forEach(g => {
    const row = accessEl('div', null, 'access-row');
    row.append(accessEl('strong', names[g.entry_id] || g.entry_id));
    const remove = accessEl('button', 'Remove', 'btn btn-sm btn-secondary');
    remove.type = 'button';
    remove.addEventListener('click', async () => {
      const r = await apiFetch('/api/credential-consumers/grants/' + encodeURIComponent(g.id), { method: 'DELETE' });
      if (r.ok) { showToast('Access removed', 'success'); renderAgentAccess(); }
      else showToast(r.error?.message || 'Could not remove access', 'danger');
    });
    row.append(remove);
    list.append(row);
  });
  const held = new Set(active.map(g => g.entry_id));
  const pick = document.getElementById('agent-access-pick');
  pick.replaceChildren();
  Object.entries(names).filter(([id]) => !held.has(id)).forEach(([id, name]) => {
    const o = accessEl('option', name); o.value = id; pick.append(o);
  });
  document.getElementById('agent-access-add').disabled = !pick.options.length;
}

document.addEventListener('click', async (ev) => {
  if (ev.target.id !== 'agent-access-add') return;
  const entry = document.getElementById('agent-access-pick').value;
  if (!entry) return;
  const r = await apiFetch('/api/credential-consumers/grants', {
    method: 'POST', body: JSON.stringify({ consumer_agent_id: agentAccessId, entry_id: entry, allow_write: false }),
  });
  if (r.ok) { showToast('Access given', 'success'); renderAgentAccess(); }
  else showToast(r.error?.message || 'Could not give access', 'danger');
});

document.addEventListener('DOMContentLoaded', () => {
  const agent = new URLSearchParams(location.search).get('agent');
  if (agent) openAgentAccess(agent);
});

// The reference (AC_SECRET_...) is a name, not the secret; copying it is how it is used.
document.addEventListener('click', async (ev) => {
  const btn = ev.target.closest('[data-copy-reference]');
  if (!btn) return;
  ev.preventDefault();
  const text = btn.dataset.copyReference;
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    const area = document.createElement('textarea');
    area.value = text; area.style.position = 'fixed'; area.style.opacity = '0';
    document.body.appendChild(area); area.select();
    try { document.execCommand('copy'); } finally { area.remove(); }
  }
  showToast('Reference copied', 'success');
});

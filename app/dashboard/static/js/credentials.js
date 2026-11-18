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
    if (agents.length) used += '\n\nThese services receive it and will lose it: ' + agents.join(', ') + '.';
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
// One list, in three groups: the capabilities that sign in with this credential, the agents that can reach it,
// and the services that receive its value. Everything from the server goes in through textContent, never innerHTML.
let accessEntryId = null;
let accessData = null;
let accessOwnerExtra = null;  // the owner of this credential, when the page's own list does not have it

function accessEl(tag, text, cls) {
  const el = document.createElement(tag);
  if (text != null) el.textContent = text;
  if (cls) el.className = cls;
  return el;
}

// A date as a person reads it (30 Nov 2026). The stored instant is the end of that day, so it is read in UTC.
function accessDate(value) {
  const when = new Date(value);
  if (isNaN(when.getTime())) return '';
  return when.toLocaleDateString('en-GB', { timeZone: 'UTC', day: 'numeric', month: 'short', year: 'numeric' });
}

// A connection is named by the capability it belongs to and by its own name: GitHub, account A.
function connectionLabel(connection) {
  return connection.capability ? connection.capability + ', ' + connection.name : connection.name;
}

// Turning a permission on is one recorded action by a person. Nothing here ever turns one off.
function turnOnButton(user) {
  const button = accessEl('button', 'Turn on', 'btn btn-sm btn-secondary');
  button.type = 'button';
  button.addEventListener('click', async () => {
    button.disabled = true;
    const r = await apiFetch(
      '/api/agents/' + encodeURIComponent(user.agent_id) + '/permissions/' + encodeURIComponent(user.needs) + '/turn-on',
      { method: 'POST' }
    );
    if (r.ok) {
      showToast(user.needs_label + ' turned on for ' + user.display_name, 'success');
      await renderAccess();
    } else {
      showToast(r.error?.message || 'Could not turn it on', 'danger');
      button.disabled = false;
    }
  });
  return button;
}

async function openAccess(entryId) {
  accessEntryId = entryId;
  document.getElementById('access-expires').value = '';
  document.getElementById('access-write').checked = false;
  closeOwnerPanel();
  await renderAccess();
  openModal('access-modal');
}

function closeAccess() {
  closeModal('access-modal');
}

async function renderAccess() {
  const j = await apiFetch('/api/access/credentials/' + encodeURIComponent(accessEntryId));
  if (!j.ok) { showToast(j.error?.message || 'Could not load access', 'danger'); return; }
  const d = j.data;
  accessData = d;
  document.getElementById('access-name').textContent = d.credential.name;
  renderAccessConnections(d);
  renderAccessAgents(d);
  renderAccessServices(d);
}

// ── the capabilities that sign in with it ─────────────────────────────────

function renderAccessConnections(d) {
  const list = document.getElementById('access-connections');
  list.replaceChildren();
  if (!d.connections.length) {
    list.append(accessEl('p', 'No capability signs in with this yet.', 'text-muted'));
  }
  d.connections.forEach(connection => {
    const box = accessEl('div', null, 'access-connection');
    const row = accessEl('div', null, 'access-row');
    row.append(accessEl('strong', connectionLabel(connection)));
    if (!connection.enabled) row.append(accessEl('span', 'off', 'badge badge-warning'));
    const remove = accessEl('button', 'Remove', 'btn btn-sm btn-secondary');
    remove.type = 'button';
    remove.addEventListener('click', () => removeAccessConnection(connection));
    row.append(remove);
    box.append(row);
    box.append(connectionUsers(connection));
    list.append(box);
  });

  const pick = document.getElementById('access-connection-pick');
  pick.replaceChildren();
  d.connections_to_add.forEach(connection => {
    const option = accessEl('option', connectionLabel(connection));
    option.value = connection.id;
    pick.append(option);
  });
  document.getElementById('access-connection-empty').style.display = d.connections_to_add.length ? 'none' : '';
  document.getElementById('access-connection-add').disabled = !d.connections_to_add.length;
}

// Who can run a connection: the count opens the list, and each agent says why it can (or cannot yet).
function connectionUsers(connection) {
  const details = document.createElement('details');
  details.append(accessEl('summary', connection.users.length === 1
    ? '1 agent can use it'
    : connection.users.length + ' agents can use it'));
  if (!connection.users.length) {
    details.append(accessEl('p', 'No agent can use it.', 'text-muted'));
    return details;
  }
  connection.users.forEach(user => {
    const row = accessEl('div', null, 'access-row');
    row.append(accessEl('span', user.display_name));
    row.append(accessEl('span', user.reason, 'text-muted'));
    if (user.status === 'needs_permission') {
      row.append(accessEl('span', 'needs permission: ' + user.needs_label, 'badge badge-warning'));
      row.append(turnOnButton(user));
    } else {
      row.append(accessEl('span', 'can use it', 'badge badge-success'));
    }
    details.append(row);
  });
  return details;
}

async function addAccessConnection() {
  const binding = document.getElementById('access-connection-pick').value;
  if (!binding) return;
  const r = await apiFetch('/api/connector-bindings/' + encodeURIComponent(binding), {
    method: 'PUT', body: JSON.stringify({ credential_id: accessEntryId }),
  });
  if (r.ok) {
    showToast('Connection added', 'success');
    await renderAccess();
    await renderUsedBy();
  } else {
    showToast(r.error?.message || 'Could not add the connection', 'danger');
  }
}

async function removeAccessConnection(connection) {
  const ok = await confirmDialog('Remove ' + connectionLabel(connection)
    + '? The connection will stop working until it has another credential.');
  if (!ok) return;
  const r = await apiFetch('/api/connector-bindings/' + encodeURIComponent(connection.id), {
    method: 'PUT', body: JSON.stringify({ reset_credential: true }),
  });
  if (r.ok) {
    showToast('Connection removed', 'success');
    await renderAccess();
    await renderUsedBy();
  } else {
    showToast(r.error?.message || 'Could not remove the connection', 'danger');
  }
}

// ── the agents that can reach it ──────────────────────────────────────────

function renderAccessAgents(d) {
  document.getElementById('access-owner').textContent = d.credential.owner;
  const list = document.getElementById('access-agents');
  list.replaceChildren();
  if (!d.agents.length) {
    list.append(accessEl('p', 'No agent can reach this.', 'text-muted'));
  }
  d.agents.forEach(agent => {
    const row = accessEl('div', null, 'access-row');
    row.append(accessEl('span', agent.display_name));
    row.append(accessEl('span', agent.reason, 'text-muted'));
    if (agent.status === 'needs_permission') {
      row.append(accessEl('span', 'needs permission: ' + agent.needs_label, 'badge badge-warning'));
      row.append(turnOnButton(agent));
    } else {
      row.append(accessEl('span', 'can use its reference', 'text-muted'));
    }
    list.append(row);
  });
}

// Changing the owner is previewed before it is saved: who gains it, who loses it, and who keeps it.
async function openOwnerPanel() {
  if (!accessData) return;
  const pick = document.getElementById('access-owner-pick');
  // The list is the one the page's own form uses: the owners a credential can live under.
  if (accessOwnerExtra) { accessOwnerExtra.remove(); accessOwnerExtra = null; }
  if (!Array.from(pick.options).some(o => o.value === accessData.credential.scope)) {
    const option = accessEl('option', accessData.credential.owner);
    option.value = accessData.credential.scope;
    pick.append(option);
    accessOwnerExtra = option;
  }
  pick.value = accessData.credential.scope;
  document.getElementById('access-owner-panel').style.display = '';
  await previewOwner();
}

async function previewOwner() {
  const scope = document.getElementById('access-owner-pick').value;
  const box = document.getElementById('access-owner-preview');
  box.replaceChildren();
  if (!scope) return;
  const j = await apiFetch('/api/access/preview/credential-owner', {
    method: 'POST', body: JSON.stringify({ entry_id: accessEntryId, scope }),
  });
  if (!j.ok) { showToast(j.error?.message || 'Could not preview the change', 'danger'); return; }
  const p = j.data;
  box.append(accessEl('p', 'Owner: ' + p.from + ' → ' + p.to, 'text-muted'));
  box.append(accessEl('p', 'Gains access: ' + ownerNames(p.gain)));
  box.append(accessEl('p', 'Loses access: ' + ownerNames(p.lose)));
  box.append(accessEl('p', p.unchanged + (p.unchanged === 1 ? ' agent keeps access' : ' agents keep access'), 'text-muted'));
}

function ownerNames(agents) {
  return agents.length ? agents.map(a => a.display_name).join(', ') : 'nobody';
}

async function saveOwner() {
  const scope = document.getElementById('access-owner-pick').value;
  if (!scope) return;
  const r = await apiFetch('/api/credentials/entries/' + encodeURIComponent(accessEntryId), {
    method: 'PUT', body: JSON.stringify({ scope }),
  });
  if (r.ok) {
    showToast('Owner changed', 'success');
    closeOwnerPanel();
    location.reload();  // the row's owner and the Used by cell both change with it
  } else {
    showToast(r.error?.message || 'Could not change the owner', 'danger');
  }
}

function closeOwnerPanel() {
  const panel = document.getElementById('access-owner-panel');
  if (panel) panel.style.display = 'none';
  const preview = document.getElementById('access-owner-preview');
  if (preview) preview.replaceChildren();
}

// ── the services that receive its value ───────────────────────────────────

function renderAccessServices(d) {
  const list = document.getElementById('access-services');
  list.replaceChildren();
  if (!d.services.length) {
    list.append(accessEl('p', 'No service receives this yet.', 'text-muted'));
  }
  d.services.forEach(service => {
    const row = accessEl('div', null, 'access-row');
    row.append(accessEl('strong', service.display_name));
    row.append(accessEl('span', service.allow_write ? 'read and write' : 'read only', 'text-muted'));
    row.append(accessEl('span', service.expires_at ? 'until ' + accessDate(service.expires_at) : 'no end date', 'text-muted'));
    const remove = accessEl('button', 'Remove', 'btn btn-sm btn-secondary');
    remove.type = 'button';
    remove.addEventListener('click', () => removeAccessService(service));
    row.append(remove);
    list.append(row);
  });
  if (d.held_by_agents.length) {
    list.append(accessEl('p', 'Held from before by entries that are not services: '
      + d.held_by_agents.join(', ') + '. These keep working. New access goes to a service.', 'text-muted'));
  }

  const pick = document.getElementById('access-service-pick');
  pick.replaceChildren();
  d.services_to_add.forEach(service => {
    const option = accessEl('option', service.display_name);
    option.value = service.id;
    pick.append(option);
  });
  document.getElementById('access-service-empty').style.display = d.services_to_add.length ? 'none' : '';
  document.getElementById('access-service-add').disabled = !d.services_to_add.length;
}

async function addAccessService() {
  const service = document.getElementById('access-service-pick').value;
  if (!service) return;
  const body = {
    consumer_agent_id: service,
    entry_id: accessEntryId,
    allow_write: document.getElementById('access-write').checked,
  };
  const expires = endOfDayUtc(document.getElementById('access-expires').value);
  if (expires) body.expires_at = expires;
  const r = await apiFetch('/api/credential-consumers/grants', { method: 'POST', body: JSON.stringify(body) });
  if (r.ok) {
    showToast('Service added', 'success');
    document.getElementById('access-expires').value = '';
    document.getElementById('access-write').checked = false;
    await renderAccess();
    await renderUsedBy();
  } else {
    showToast(r.error?.message || 'Could not give the service this secret', 'danger');
  }
}

async function removeAccessService(service) {
  const r = await apiFetch('/api/credential-consumers/grants/' + encodeURIComponent(service.grant_id), {
    method: 'DELETE',
  });
  if (r.ok) {
    showToast('Service removed', 'success');
    await renderAccess();
    await renderUsedBy();
  } else {
    showToast(r.error?.message || 'Could not remove the service', 'danger');
  }
}

document.addEventListener('click', async (ev) => {
  const open = ev.target.closest('[data-credential-access]');
  if (open) { ev.preventDefault(); openAccess(open.dataset.credentialAccess); return; }
  if (ev.target.closest('#access-connection-add')) { await addAccessConnection(); return; }
  if (ev.target.closest('#access-service-add')) { await addAccessService(); return; }
  if (ev.target.closest('#access-owner-change')) { await openOwnerPanel(); return; }
  if (ev.target.closest('#access-owner-save')) { await saveOwner(); return; }
  if (ev.target.closest('#access-owner-cancel')) { closeOwnerPanel(); }
});

document.addEventListener('change', async (ev) => {
  if (ev.target.id === 'access-owner-pick') await previewOwner();
});

const REFERENCE_NOTE = 'They can see its name (its reference) and use it wherever Agent Core makes a connection with it. Agent Core does not show them the secret itself.';

// The Used by cell in the list says who was given the secret and who else can see its name. It is rebuilt
// whenever the dialog changes something, so the row follows without a reload.
// Mirrors used_by_cell in credentials_page.py.
async function renderUsedBy() {
  const j = await apiFetch('/api/credential-consumers/entries/' + encodeURIComponent(accessEntryId) + '/access');
  if (!j.ok) return;
  const d = j.data;
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

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

// ── showing a stored value ─────────────────────────────────────────────────
// The value is asked for only when a person presses Show value, put in the page with textContent, and taken out
// again when they press Hide, after 30 seconds, when the tab is hidden or when the dialog closes. It is never kept
// anywhere else: not in the table, not in a variable, not in an attribute and not in the address.
const REVEAL_SECONDS = 30;
let revealTimer = null;

function hideRevealedValue() {
  if (revealTimer) { clearTimeout(revealTimer); revealTimer = null; }
  const box = document.getElementById('edit-credential-revealed');
  const value = document.getElementById('edit-credential-revealed-value');
  if (value) value.textContent = '';
  if (box) box.hidden = true;
  const show = document.getElementById('edit-credential-reveal');
  if (show) show.style.display = '';
}

async function showCredentialValue() {
  const id = document.getElementById('edit-credential-id').value;
  if (!id) return;
  const button = document.getElementById('edit-credential-reveal');
  button.disabled = true;
  try {
    const j = await apiFetch('/api/credentials/entries/' + encodeURIComponent(id) + '/reveal', { method: 'POST' });
    if (!j.ok) { showToast((j.error && j.error.message) || 'Could not show the value', 'danger'); return; }
    document.getElementById('edit-credential-revealed-value').textContent = String(j.data.value);
    document.getElementById('edit-credential-revealed').hidden = false;
    button.style.display = 'none';
    if (revealTimer) clearTimeout(revealTimer);
    revealTimer = setTimeout(hideRevealedValue, REVEAL_SECONDS * 1000);
  } finally {
    button.disabled = false;
  }
}

async function copyRevealedValue() {
  const text = document.getElementById('edit-credential-revealed-value').textContent;
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    showToast('Copied', 'success');
  } catch (err) {
    showToast('Could not copy: select the value and copy it by hand', 'danger');
  }
}

(function wireRevealedValue() {
  const modal = document.getElementById('edit-credential-modal');
  document.getElementById('edit-credential-reveal').addEventListener('click', showCredentialValue);
  document.getElementById('edit-credential-reveal-hide').addEventListener('click', hideRevealedValue);
  document.getElementById('edit-credential-reveal-copy').addEventListener('click', copyRevealedValue);
  // However the dialog is closed (Cancel, the X, Escape, a click outside, Save), the value goes with it.
  new MutationObserver(function () {
    if (modal.style.display === 'none' || !modal.classList.contains('open')) hideRevealedValue();
  }).observe(modal, { attributes: true, attributeFilter: ['style', 'class', 'aria-hidden'] });
  document.addEventListener('visibilitychange', function () { if (document.hidden) hideRevealedValue(); });
})();

async function editCredential(id) {
  hideRevealedValue();
  const j = await apiFetch('/api/credentials/entries/' + id);
  if (!j.ok) { showToast(j.error?.message || 'Error', 'danger'); return; }
  const c = j.data.entry;
  document.getElementById('edit-credential-id').value = id;
  document.getElementById('edit-credential-name').value = c.name || '';
  document.getElementById('edit-credential-label').value = c.label || '';
  const scopeSelect = document.getElementById('edit-credential-scope');
  scopeSelect.value = c.scope || '';
  scopeSelect.dataset.originalScope = c.scope || '';
  // A credential that belongs to a connection has an owner the form does not list. Keep it, so that saving does
  // not move the credential out of its connection by accident.
  if (c.scope && !Array.from(scopeSelect.options).some(function(o) { return o.value === c.scope; })) {
    const option = accessEl('option', accessScopeName(c.scope));
    option.value = c.scope;
    scopeSelect.append(option);
    scopeSelect.value = c.scope;
  }
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
    'Move this credential from ' + accessScopeWords(originalScope) + ' to ' + accessScopeWords(body.scope) + '? Connections that use it keep their current workspace.'
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

// Every "Add" picker starts on "Choose...", so nothing is added by accident: the Add button stays
// disabled until a real option is picked.
function accessChooseOption(text) {
  const option = accessEl('option', text || 'Choose...');
  option.value = '';
  return option;
}

// A date as a person reads it (30 Nov 2026). The stored instant is the end of that day, so it is read in UTC.
function accessDate(value) {
  const when = new Date(value);
  if (isNaN(when.getTime())) return '';
  return when.toLocaleDateString('en-GB', { timeZone: 'UTC', day: 'numeric', month: 'short', year: 'numeric' });
}

// A connection is shown by its own name; the capability is added only when the name does not already contain it or
// its first word: "Context7" and "UniFi read-only" stay as they are, "Work account" becomes "Work account (GitHub)".
// A name with brackets of its own gets the capability after a dot, so two brackets never follow each other.
function connectionLabel(connection) {
  var name = connection.name || '';
  var capability = (connection.capability || '').trim();
  if (!capability) return name;
  var lowered = name.toLowerCase();
  var wanted = capability.toLowerCase();
  if (lowered.indexOf(wanted) >= 0 || lowered.indexOf(wanted.split(/\s+/)[0]) >= 0) return name;
  return name.indexOf('(') >= 0 ? name + ' \u00b7 ' + capability : name + ' (' + capability + ')';
}

// The connections this page knows about, by id. A credential that belongs to a connection is shown with its name.
function accessConnectionNames() {
  const names = window.AC_CREDENTIAL_CONNECTIONS;
  return names && typeof names === 'object' ? names : null;
}

// The owners the page's own form offers, by their stored form: a workspace, a person or an agent.
function accessScopeNames() {
  const names = {};
  const select = document.getElementById('edit-credential-scope') || document.getElementById('credential-scope');
  if (!select) return names;
  Array.from(select.options || []).forEach(function(option) {
    if (option.value) names[option.value] = (option.textContent || '').trim();
  });
  return names;
}

// The owner of a credential in plain words. A credential that belongs to a connection says which one, by name;
// the raw scope is never shown.
function accessScopeName(scope) {
  const raw = String(scope || '');
  const kind = raw.split(':')[0];
  if (kind === 'capability') {
    const names = accessConnectionNames();
    if (!names) return 'A connection';
    const name = names[raw.slice('capability:'.length)];
    return name ? 'Connection ' + name : 'A deleted connection';
  }
  const known = accessScopeNames()[raw];
  if (known) return known;
  if (kind === 'user') return 'Just me (personal)';
  if (kind === 'workspace') return 'A workspace';
  if (kind === 'agent') return 'An agent';
  return 'Nowhere';
}

// A credential that belongs to a connection: only Agent Core uses it, and no agent can reach it.
function accessBelongsToConnection(credential) {
  return String((credential && credential.scope) || '').startsWith('capability:');
}

// The scope in words, when it is a connection: never the raw scope, never the word "capability".
function accessScopeWords(scope) {
  const raw = String(scope || '');
  return accessBelongsToConnection({ scope: raw }) ? accessScopeName(raw) : raw;
}

// Turning a permission on is one recorded action by a person. Nothing here ever turns one off.
async function turnOnPermission(agentId, permission) {
  const r = await apiFetch(
    '/api/agents/' + encodeURIComponent(agentId) + '/permissions/' + encodeURIComponent(permission) + '/turn-on',
    { method: 'POST' }
  );
  if (!r.ok) {
    showToast((r.error && r.error.message) || 'Could not turn it on', 'danger');
    return false;
  }
  return true;
}

// The rows of a list that are missing a permission, one group per permission.
function accessMissingPermissions(items, fallbackPermission, fallbackLabel) {
  const groups = [];
  (items || []).forEach(function (item) {
    if (!item || item.status !== 'needs_permission') return;
    const permission = item.needs || fallbackPermission;
    const label = item.needs_label || fallbackLabel;
    let group = groups.find(function (g) { return g.permission === permission; });
    if (!group) {
      group = { permission: permission, label: label, rows: [] };
      groups.push(group);
    }
    group.rows.push(item);
  });
  return groups;
}

// The notice above a list of other agents: they are the ones who cannot use it yet, and the sentence names them.
// One agent, several of them, or all of them: the sentence says which.
function accessAgentsNoticeText(label, needed, total, names) {
  const who = names || [];
  if (!who.length) return 'These agents need the ' + label + ' permission before they can use it.';
  if (who.length === 1) return who[0] + ' needs the ' + label + ' permission before it can use it.';
  if (needed === total) return 'All of these agents need the ' + label + ' permission before they can use it.';
  return accessNameList(who) + ' need the ' + label + ' permission before they can use it.';
}

// The display names of the agents that need the permission, each once, in the order the list shows them.
function accessNamesOf(rows) {
  const names = [];
  (rows || []).forEach(function (row) {
    const name = row && row.display_name ? String(row.display_name) : '';
    if (name && !names.includes(name)) names.push(name);
  });
  return names;
}

// The names of several agents, said the way a person says them: two, three, or the first three and how many more.
function accessNameList(names) {
  if (names.length === 2) return names[0] + ' and ' + names[1];
  if (names.length === 3) return names[0] + ', ' + names[1] + ' and ' + names[2];
  return names[0] + ', ' + names[1] + ', ' + names[2] + ' and ' + (names.length - 3) + ' more';
}

// One notice above a list, and one button: the list says once what its rows are missing, and the button turns
// that permission on for every row that needs it. The rows keep a quiet "needs permission" of their own.
// The sentence is told how many rows need it, how many rows the list has, and which agents need it, so it can
// say what is true and name who.
function accessPermissionNotice(groups, turnOn, total) {
  if (!groups.length) return null;
  const box = accessEl('div', null, 'access-permission-notice');
  groups.forEach((group) => {
    const line = accessEl('div', null, 'access-permission-line');
    line.append(accessEl('span', accessAgentsNoticeText(group.label, group.rows.length, total, accessNamesOf(group.rows))));
    const button = accessEl('button', 'Turn on ' + group.label, 'btn btn-sm btn-secondary');
    button.type = 'button';
    button.addEventListener('click', () => turnOn(group));
    line.append(button);
    box.append(line);
  });
  return box;
}

// Turn one permission on for every row of a group that needs it, then show the list again.
async function turnOnForRows(group, reload) {
  // The agents this button reaches, each once.
  const people = [];
  for (const row of group.rows) {
    if (!people.some(function (p) { return p.agent_id === row.agent_id; })) people.push(row);
  }
  // One button can reach several agents: say which, and ask first. A permission is only ever turned on by a person who saw who gets it.
  if (people.length > 1) {
    const who = people.map(function (row) { return row.display_name; }).join(', ');
    if (!await confirmDialog('Turn on ' + group.label + ' for ' + who + '?')) return;
  }
  for (const row of people) {
    if (!await turnOnPermission(row.agent_id, group.permission)) return;
  }
  showToast(group.label + ' turned on for ' + people.map(function (row) { return row.display_name; }).join(', '), 'success');
  await reload();
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
    list.append(accessEl('p', 'No connection signs in with this yet.', 'text-muted'));
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
  pick.append(accessChooseOption());
  d.connections_to_add.forEach(connection => {
    const option = accessEl('option', connectionLabel(connection));
    option.value = connection.id;
    pick.append(option);
  });
  document.getElementById('access-connection-empty').style.display = d.connections_to_add.length ? 'none' : '';
  // Nothing is chosen yet, so nothing can be added yet.
  document.getElementById('access-connection-add').disabled = true;
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
  // The agents that may not run it yet say so once, above the list, with one button.
  const notice = accessPermissionNotice(
    accessMissingPermissions(connection.users, 'connectors_execute', 'Run capabilities'),
    (group) => turnOnForRows(group, renderAccess),
    connection.users.length
  );
  if (notice) details.append(notice);
  connection.users.forEach(user => {
    const row = accessEl('div', null, 'access-row');
    row.append(accessEl('span', user.display_name));
    row.append(accessEl('span', user.reason, 'text-muted'));
    if (user.status === 'needs_permission') {
      row.append(accessEl('span', 'needs permission', 'text-muted access-needs'));
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
  // A connection signs in with one credential: adding this one replaces the one it has now, so say so first.
  const chosen = ((accessData && accessData.connections_to_add) || []).find(c => c.id === binding);
  if (chosen && chosen.signs_in_with) {
    const ok = await confirmDialog(
      connectionLabel(chosen) + ' signs in with ' + chosen.signs_in_with.name + ' now. Adding this credential '
      + 'replaces it. ' + chosen.signs_in_with.name + ' stays stored and can be added back.',
      { title: 'Replace the credential ' + connectionLabel(chosen) + ' signs in with?', confirmLabel: 'Replace credential', danger: false }
    );
    if (!ok) return;
  }
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
  const ownedByConnection = accessBelongsToConnection(d.credential);
  const owner = ownedByConnection ? accessScopeName(d.credential.scope) : d.credential.owner;
  document.getElementById('access-owner').textContent = owner;
  const list = document.getElementById('access-agents');
  list.replaceChildren();
  // The line about agents getting a reference does not hold for a credential no agent can reach.
  const hint = document.getElementById('access-agents-hint');
  if (hint) hint.style.display = ownedByConnection ? 'none' : '';
  if (ownedByConnection) {
    list.append(accessEl('p', 'No agent can reach this. It belongs to ' + owner + ', so only Agent Core uses it.', 'text-muted'));
    return;
  }
  if (!d.agents.length) {
    list.append(accessEl('p', 'No agent can reach this.', 'text-muted'));
  }
  // The agents that may not use it yet say so once, above the list, with one button.
  const notice = accessPermissionNotice(
    accessMissingPermissions(d.agents, 'credentials', 'Reference credentials'),
    (group) => turnOnForRows(group, renderAccess),
    d.agents.length
  );
  if (notice) list.append(notice);
  d.agents.forEach(agent => {
    const row = accessEl('div', null, 'access-row');
    row.append(accessEl('span', agent.display_name));
    row.append(accessEl('span', agent.reason, 'text-muted'));
    if (agent.status === 'needs_permission') {
      row.append(accessEl('span', 'needs permission', 'text-muted access-needs'));
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
  const ownedByConnection = accessBelongsToConnection(accessData.credential);
  // A connection's own owner is never offered: it cannot be given to an agent, and the credential only leaves it
  // when a person moves it to a workspace or a person.
  if (!ownedByConnection && !Array.from(pick.options).some(o => o.value === accessData.credential.scope)) {
    const option = accessEl('option', accessData.credential.owner);
    option.value = accessData.credential.scope;
    pick.append(option);
    accessOwnerExtra = option;
  }
  pick.value = ownedByConnection ? '' : accessData.credential.scope;
  const panel = document.getElementById('access-owner-panel');
  panel.style.display = '';
  await previewOwner();
  showOwnerPanel(panel);
}

// The panel is inside a dialog whose Close bar sticks to the bottom of the sheet. Scroll the dialog just far enough
// that the whole panel, Save and Cancel included, is above that bar. Nothing is added to the body: padding there
// would lift the bar off the bottom of the sheet.
function showOwnerPanel(panel) {
  const scroller = panel.closest('.modal-scroll');
  const footer = scroller ? scroller.querySelector('.modal-footer') : null;
  if (!scroller) { panel.scrollIntoView({ block: 'nearest' }); return; }
  const barTop = footer ? footer.getBoundingClientRect().top : scroller.getBoundingClientRect().bottom;
  const over = panel.getBoundingClientRect().bottom - (barTop - 12);
  if (over > 0) scroller.scrollTop += over;
  const under = scroller.getBoundingClientRect().top - panel.getBoundingClientRect().top;
  if (under > 0) scroller.scrollTop -= under;  // never so far that the panel's top is lost
}

// The owner it has now: the scope the credential lives under.
function currentOwnerScope() {
  return accessData && accessData.credential ? (accessData.credential.scope || '') : '';
}

async function previewOwner() {
  const scope = document.getElementById('access-owner-pick').value;
  const box = document.getElementById('access-owner-preview');
  const save = document.getElementById('access-owner-save');
  box.replaceChildren();
  const current = currentOwnerScope();
  const changed = !!scope && scope !== current;
  // Nothing is saved until the chosen owner is not the one it has now.
  if (save) save.disabled = !changed;
  if (!scope) return;
  if (!changed) {
    box.append(accessEl('p', 'Nothing changes.', 'text-muted'));
    return;
  }
  const j = await apiFetch('/api/access/preview/credential-owner', {
    method: 'POST', body: JSON.stringify({ entry_id: accessEntryId, scope }),
  });
  if (!j.ok) { showToast(j.error?.message || 'Could not preview the change', 'danger'); return; }
  const p = j.data;
  box.append(accessEl('p', 'Owner: ' + p.from + ' → ' + p.to, 'text-muted'));
  box.append(accessEl('p', 'Gains access: ' + ownerNames(p.gain)));
  box.append(accessEl('p', 'Loses access: ' + ownerNames(p.lose)));
  box.append(accessEl('p', p.unchanged + (p.unchanged === 1 ? ' agent keeps access' : ' agents keep access'), 'text-muted'));
  // The preview made the panel taller: bring Save and Cancel back into view, clear of the Close bar.
  const panel = document.getElementById('access-owner-panel');
  if (panel) showOwnerPanel(panel);
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
  const save = document.getElementById('access-owner-save');
  if (save) save.disabled = true;
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
  pick.append(accessChooseOption());
  d.services_to_add.forEach(service => {
    const option = accessEl('option', service.display_name);
    option.value = service.id;
    pick.append(option);
  });
  document.getElementById('access-service-empty').style.display = d.services_to_add.length ? 'none' : '';
  // Nothing is chosen yet, so nothing can be given yet.
  document.getElementById('access-service-add').disabled = true;
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
  // An "Add" button opens only once a real option is chosen in the picker above it.
  const addFor = {
    'access-connection-pick': 'access-connection-add',
    'access-service-pick': 'access-service-add',
    'agent-access-pick': 'agent-access-add',
  };
  const addId = addFor[ev.target.id];
  if (!addId) return;
  const add = document.getElementById(addId);
  if (add) add.disabled = !ev.target.value;
});

const REFERENCE_NOTE = 'They can use it wherever Agent Core makes a connection with it. Agent Core does not show them the secret itself.';

// The Used by cell in the list says who was given the secret and who else can reach it. It is rebuilt
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
  // The same agents the Access dialog lists: the ones that reach this credential's owner scope.
  const reaching = await accessAgentsReaching();
  if (reaching.length) {
    const names = reaching.map(function (a) { return a.display_name; });
    const seen = accessEl('div', reaching.length + ' agent' + (reaching.length === 1 ? '' : 's') + ' can reach it', 'text-muted used-by-seen');
    seen.style.fontSize = '0.8rem';
    seen.title = names.join(', ') + '. ' + REFERENCE_NOTE;
    cell.append(seen);
  }
  if (d.services.length) {
    const sv = accessEl('div', d.services.length + ' connection' + (d.services.length === 1 ? '' : 's'), 'text-muted');
    sv.style.fontSize = '0.8rem';
    cell.append(sv);
  }
}

// The agents that reach the open credential: the list the Access dialog already loaded. If it is not
// there (the dialog was never opened), it is fetched once, so both places say the same number.
async function accessAgentsReaching() {
  if (accessData && accessData.credential && accessData.credential.id === accessEntryId) {
    return accessData.agents || [];
  }
  const j = await apiFetch('/api/access/credentials/' + encodeURIComponent(accessEntryId));
  if (!j.ok) return [];
  accessData = j.data;
  return accessData.agents || [];
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
  pick.append(accessChooseOption());
  Object.entries(names).filter(([id]) => !held.has(id)).forEach(([id, name]) => {
    const o = accessEl('option', name); o.value = id; pick.append(o);
  });
  // Nothing is chosen yet, so nothing can be given yet.
  document.getElementById('agent-access-add').disabled = true;
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

// What an agent, a service and a connection can reach: the Access dialogs on the Agents, Services and
// Capabilities pages.
//
// Everything the server sends goes in through textContent, never by splicing a name into markup: an agent's
// display name, a workspace name and a credential name are typed by people, and they end up on an admin's screen.
//
// The answers come from app/services/reach_service.py, the same place the request path asks. Nothing here decides
// who may reach what; it only shows the answer and asks a person to confirm it.

if (typeof window.accessEl === 'undefined') {
  // The same helper the Credentials page uses (credentials.js). This page does not load that file.
  window.accessEl = function (tag, text, cls) {
    var el = document.createElement(tag);
    if (text != null) el.textContent = text;
    if (cls) el.className = cls;
    return el;
  };
}

// ── small helpers ───────────────────────────────────────────────────────────

// A date input yields YYYY-MM-DD with no timezone. The person means the end of that day where they are, so the
// instant is built in their own timezone and converted to UTC. The same rule the Credentials page uses.
function accessEndOfDayUtc(dateValue) {
  if (!dateValue) return null;
  var parts = dateValue.split('-').map(Number);
  if (!parts[0] || !parts[1] || !parts[2]) return null;
  return new Date(parts[0], parts[1] - 1, parts[2], 23, 59, 59, 999).toISOString();
}

function accessDate(value) {
  if (!value) return '';
  var d = new Date(value);
  if (isNaN(d.getTime())) return String(value);
  return d.toLocaleDateString();
}

function accessDateTime(value) {
  if (!value) return '';
  var d = new Date(value);
  if (isNaN(d.getTime())) return String(value);
  return d.toLocaleString();
}

// The chip an entry that is saved but not usable gets: it is there, it is not switched on for anybody.
function accessNeedsChip(item, fallbackLabel) {
  if (!item || item.status !== 'needs_permission') return null;
  return accessEl('span', 'needs permission: ' + (item.needs_label || fallbackLabel), 'badge badge-warning');
}

// What the entry can do right now, in plain words.
function accessStatusText(item) {
  if (!item) return '';
  if (item.status === 'can_reference') return 'can use its reference';
  if (item.status === 'can_use') return 'can use';
  if (item.status === 'no_longer_reachable') return 'no longer reachable';
  if (item.status === 'no_longer_usable') return 'no longer usable';
  return '';
}

// Switching a permission on is one recorded action, done by a person, one permission at a time. Nothing here ever
// switches one off.
async function accessTurnOn(agentId, permission, label, name, reload) {
  var r = await apiFetch(
    '/api/agents/' + encodeURIComponent(agentId) + '/permissions/' + encodeURIComponent(permission) + '/turn-on',
    { method: 'POST' }
  );
  if (!r.ok) {
    showToast((r.error && r.error.message) || 'Could not turn that permission on', 'danger');
    return;
  }
  showToast((label || 'That permission') + ' turned on for ' + name);
  await reload();
}

// ── one agent: what it can do and reach ─────────────────────────────────────

var accessAgentId = null;
var accessAgentData = null;
var accessAgentScopes = null;
var accessAgentChanges = [];

async function openAgentAccess(id) {
  accessAgentId = id;
  accessAgentChanges = [];
  accessAgentScopes = null;
  var j = await apiFetch('/api/access/agents/' + encodeURIComponent(id));
  if (!j.ok) {
    showToast((j.error && j.error.message) || 'Could not load what this agent can reach', 'danger');
    return;
  }
  accessAgentData = j.data;
  var a = await apiFetch('/api/agents/' + encodeURIComponent(id));
  if (!a.ok) {
    showToast((a.error && a.error.message) || 'Could not load this agent', 'danger');
    return;
  }
  var agent = a.data.agent;
  // The stored lists. Everything else the agent has (its own private space, its person's) stays as it is.
  accessAgentScopes = {
    read: JSON.parse(agent.read_scopes_json || '[]'),
    write: JSON.parse(agent.write_scopes_json || '[]'),
  };
  renderAgentAccess();
  openModal('access-agent-modal');
}

async function reloadAgentAccess() {
  var j = await apiFetch('/api/access/agents/' + encodeURIComponent(accessAgentId));
  if (!j.ok) {
    showToast((j.error && j.error.message) || 'Could not load what this agent can reach', 'danger');
    return;
  }
  accessAgentData = j.data;
  renderAgentAccess();
}

function renderAgentAccess() {
  document.getElementById('access-agent-name').textContent =
    accessAgentData.agent.display_name || accessAgentData.agent.id;
  renderAgentWorkspaces();
  renderAgentCapabilities();
  renderAgentCredentials();
  renderAccessKeys(accessAgentData.keys || [], 'access-agent-keys');
  renderAgentPermissions();
}

// The workspaces this page already knows, for the picker.
function accessWorkspaceOptions() {
  if (typeof ACCESS_WORKSPACES === 'undefined' || !ACCESS_WORKSPACES) return [];
  return ACCESS_WORKSPACES;
}

function accessAgentWorkspaceName(id) {
  var list = accessWorkspaceOptions();
  for (var i = 0; i < list.length; i++) {
    if (list[i].id === id) return list[i].name;
  }
  return id;
}

// The change staged for one workspace, if there is one.
function accessAgentChange(workspaceId) {
  for (var i = accessAgentChanges.length - 1; i >= 0; i--) {
    if (accessAgentChanges[i].workspaceId === workspaceId) return accessAgentChanges[i];
  }
  return null;
}

// What the workspace gives the agent: the grant in plain words. A workspace the agent's person cannot reach is
// marked "no effect yet" next to it, so the grant and the effect are never confused.
function accessWorkspaceEffect(workspace) {
  var read = workspace.reads !== undefined ? workspace.reads : workspace.effective_read;
  var write = workspace.writes !== undefined ? workspace.writes : workspace.effective_write;
  if (read && write) return 'reads and writes';
  if (read) return 'reads';
  if (write) return 'writes';
  return 'nothing yet';
}

function renderAgentWorkspaces() {
  var box = document.getElementById('access-agent-workspaces');
  box.replaceChildren();
  var data = accessAgentData;
  var noEffect = {};
  (data.no_effect || []).forEach(function (n) { noEffect[n.workspace] = n; });

  // What the list shows: the stored workspaces, minus the ones a staged change removes, plus the ones it adds.
  var shown = [];
  (data.workspaces || []).forEach(function (w) {
    var change = accessAgentChange(w.id);
    if (change && change.action === 'remove') return;
    shown.push({
      id: w.id,
      name: w.name,
      reads: w.reads,
      writes: w.writes,
      effective_read: w.effective_read,
      effective_write: w.effective_write,
      added: !!(change && change.action === 'add'),
    });
  });
  accessAgentChanges.forEach(function (change) {
    if (change.action !== 'add') return;
    var known = shown.some(function (w) { return w.id === change.workspaceId; });
    if (known) return;
    shown.push({
      id: change.workspaceId,
      name: accessAgentWorkspaceName(change.workspaceId),
      reads: change.read,
      writes: change.write,
      effective_read: change.read,
      effective_write: change.write,
      added: true,
    });
  });

  if (!shown.length) {
    box.append(accessEl(
      'p',
      'No workspaces yet. This agent keeps only its own private memory. Add one below to share memory with other agents and people.',
      'text-muted'
    ));
  }
  shown.forEach(function (w) {
    var row = accessEl('div', null, 'access-row');
    row.append(accessEl('strong', w.name));
    row.append(accessEl('span', accessWorkspaceEffect(w), 'text-muted'));
    if (w.added) row.append(accessEl('span', 'not saved yet', 'badge badge-info'));
    var note = noEffect[w.id];
    if (note) {
      // Saved, but doing nothing yet: say so next to the workspace it applies to.
      row.append(accessEl('span', 'no effect yet', 'text-muted'));
      row.append(accessEl('span', note.message, 'text-muted'));
    }
    var remove = accessEl('button', 'Remove', 'btn btn-sm btn-secondary');
    remove.type = 'button';
    remove.addEventListener('click', function () {
      accessPreviewWorkspace(w.id, 'remove', false, false);
    });
    row.append(remove);
    box.append(row);
  });

  // The picker: the workspaces that are not listed yet.
  var pick = document.getElementById('access-agent-workspace-pick');
  pick.replaceChildren();
  var listed = {};
  shown.forEach(function (w) { listed[w.id] = true; });
  var offered = 0;
  accessWorkspaceOptions().forEach(function (p) {
    if (listed[p.id]) return;
    var option = accessEl('option', p.name);
    option.value = p.id;
    pick.append(option);
    offered += 1;
  });
  document.getElementById('access-agent-workspace-add').disabled = !offered;
  if (!offered) {
    var none = accessEl('option', 'Every workspace is already listed');
    none.value = '';
    pick.append(none);
  }
}

// Adding or removing a workspace is previewed before it is saved: what it opens, what it closes, and what it would
// reach that it cannot reach yet.
async function accessPreviewWorkspace(workspaceId, action, read, write) {
  var r = await apiFetch('/api/access/preview/agent-workspace', {
    method: 'POST',
    body: JSON.stringify({
      agent_id: accessAgentId,
      workspace_id: workspaceId,
      action: action,
      read: read,
      write: write,
    }),
  });
  if (!r.ok) {
    showToast((r.error && r.error.message) || 'Could not work out what this would change', 'danger');
    return;
  }
  accessAgentChanges = accessAgentChanges.filter(function (c) { return c.workspaceId !== workspaceId; });
  accessAgentChanges.push({ workspaceId: workspaceId, action: action, read: read, write: write });
  renderAgentWorkspaces();
  renderAgentWorkspacePreview(r.data);
}

function renderAgentWorkspacePreview(preview) {
  var box = document.getElementById('access-agent-workspace-preview');
  box.replaceChildren();
  var name = (preview.workspace && preview.workspace.name) || accessAgentWorkspaceName(preview.workspace.id);
  var head = accessEl('div', null, 'form-hint');
  if (preview.action === 'add') {
    head.append(accessEl('strong', preview.opens_memory
      ? ('Adding ' + name + ' opens its memory.')
      : ('Adding ' + name + ' does not open its memory yet.')));
  } else {
    head.append(accessEl('strong', preview.closes_memory
      ? ('Removing ' + name + ' closes its memory.')
      : ('Removing ' + name + ' closes nothing yet.')));
  }
  box.append(head);
  if (preview.no_effect) {
    box.append(accessEl(
      'div',
      'This has no effect yet: the person this agent acts for is not a member of ' + name + '.',
      'form-hint'
    ));
  }

  box.append(accessEl('div', 'Credentials in it:', 'form-hint'));
  var credentials = preview.credentials || [];
  if (!credentials.length) box.append(accessEl('div', 'none', 'text-muted'));
  credentials.forEach(function (c) {
    var line = accessEl('div', null, 'form-hint');
    line.append(accessEl('span', c.name + ' - '));
    var chip = accessNeedsChip(c, 'Reference credentials');
    if (chip) line.append(chip);
    else line.append(accessEl('span', accessStatusText(c)));
    box.append(line);
  });

  box.append(accessEl('div', 'Connections in it:', 'form-hint'));
  var connections = preview.connections || [];
  if (!connections.length) box.append(accessEl('div', 'none', 'text-muted'));
  connections.forEach(function (c) {
    var line = accessEl('div', null, 'form-hint');
    line.append(accessEl('span', c.name + ' - '));
    var chip = accessNeedsChip(c, 'Run capabilities');
    if (chip) line.append(chip);
    else line.append(accessEl('span', accessStatusText(c)));
    box.append(line);
  });
}

function renderAgentCapabilities() {
  var box = document.getElementById('access-agent-capabilities');
  box.replaceChildren();
  var list = accessAgentData.connections || [];
  if (!list.length) {
    box.append(accessEl(
      'p',
      'No capabilities yet. A connection to a capability is what makes it reachable; add one on the Capabilities page.',
      'text-muted'
    ));
    return;
  }
  list.forEach(function (c) {
    var row = accessEl('div', null, 'access-row');
    row.append(accessEl('strong', c.name));
    if (c.capability) row.append(accessEl('span', c.capability, 'text-muted'));
    var chip = accessNeedsChip(c, 'Run capabilities');
    if (chip) {
      row.append(chip);
      var turn = accessEl('button', 'Turn on', 'btn btn-sm btn-secondary');
      turn.type = 'button';
      turn.addEventListener('click', function () {
        accessTurnOn(
          accessAgentId,
          'connectors_execute',
          c.needs_label || 'Run capabilities',
          accessAgentData.agent.display_name || accessAgentId,
          reloadAgentAccess
        );
      });
      row.append(turn);
    } else {
      row.append(accessEl('span', 'can use', 'text-muted'));
    }
    box.append(row);
  });
}

function renderAgentCredentials() {
  var box = document.getElementById('access-agent-credentials');
  box.replaceChildren();
  var list = accessAgentData.credentials || [];
  if (!list.length) {
    box.append(accessEl(
      'p',
      'No credentials yet. A credential is reachable when its owner is a place this agent can read.',
      'text-muted'
    ));
    return;
  }
  list.forEach(function (c) {
    var row = accessEl('div', null, 'access-row');
    row.append(accessEl('strong', c.name));
    row.append(accessEl('span', c.owner, 'text-muted'));
    row.append(accessEl('span', c.reason, 'text-muted'));
    var chip = accessNeedsChip(c, 'Reference credentials');
    if (chip) {
      row.append(chip);
      var turn = accessEl('button', 'Turn on', 'btn btn-sm btn-secondary');
      turn.type = 'button';
      turn.addEventListener('click', function () {
        accessTurnOn(
          accessAgentId,
          'credentials',
          c.needs_label || 'Reference credentials',
          accessAgentData.agent.display_name || accessAgentId,
          reloadAgentAccess
        );
      });
      row.append(turn);
    } else {
      row.append(accessEl('span', 'can use its reference', 'text-muted'));
    }
    box.append(row);
  });
}

function renderAccessKeys(keys, boxId) {
  var box = document.getElementById(boxId);
  box.replaceChildren();
  if (!keys.length) {
    box.append(accessEl('p', 'No keys yet. Each key belongs to one tool or machine.', 'text-muted'));
  }
  keys.forEach(function (k) {
    var row = accessEl('div', null, 'access-row');
    row.append(accessEl('strong', k.label));
    row.append(accessEl('span', k.last_used_at ? ('last used ' + accessDateTime(k.last_used_at)) : 'never used', 'text-muted'));
    box.append(row);
  });
  box.append(accessEl('div', 'Add, rename or revoke keys under More > Keys.', 'form-hint'));
}

function renderAgentPermissions() {
  var box = document.getElementById('access-agent-permissions');
  box.replaceChildren();
  var list = accessAgentData.permissions || [];
  if (!list.length) {
    box.append(accessEl('p', 'No permissions yet. An agent with no permissions can do nothing.', 'text-muted'));
    return;
  }
  var line = accessEl('div', null, 'form-hint');
  list.forEach(function (p) {
    line.append(accessEl('span', p.label, 'badge badge-secondary'));
    line.append(document.createTextNode(' '));
  });
  box.append(line);
}

// Saving sends the whole list of workspaces, because the server keeps the list, not the difference. The agent's own
// private space and every other scope it has stay exactly as they were.
async function saveAgentAccess() {
  if (!accessAgentScopes) return;
  if (!accessAgentChanges.length) {
    showToast('Nothing to save');
    return;
  }
  var read = accessAgentScopes.read.slice();
  var write = accessAgentScopes.write.slice();
  accessAgentChanges.forEach(function (change) {
    var scope = 'workspace:' + change.workspaceId;
    read = read.filter(function (s) { return s !== scope; });
    write = write.filter(function (s) { return s !== scope; });
    if (change.action === 'add') {
      if (change.read || change.write) read.push(scope);
      if (change.write) write.push(scope);
    }
  });
  var r = await apiFetch('/api/agents/' + encodeURIComponent(accessAgentId), {
    method: 'PUT',
    body: JSON.stringify({ read_scopes: read, write_scopes: write }),
  });
  if (!r.ok) {
    showToast((r.error && r.error.message) || 'Could not save the workspaces', 'danger');
    return;
  }
  showToast('Workspaces saved');
  accessAgentChanges = [];
  await openAgentAccess(accessAgentId);
}

function cancelAgentAccess() {
  var overlay = document.getElementById('access-agent-modal');
  if (overlay) overlay.dataset.dirty = '';
  accessAgentChanges = [];
  closeModal('access-agent-modal');
}

// ── one service: what it can fetch ──────────────────────────────────────────

var accessServiceId = null;
var accessServiceData = null;

async function openServiceAccess(id) {
  accessServiceId = id;
  var j = await apiFetch('/api/access/services/' + encodeURIComponent(id));
  if (!j.ok) {
    showToast((j.error && j.error.message) || 'Could not load what this service can fetch', 'danger');
    return;
  }
  accessServiceData = j.data;
  renderServiceAccess();
  openModal('access-service-modal');
}

async function reloadServiceAccess() {
  var j = await apiFetch('/api/access/services/' + encodeURIComponent(accessServiceId));
  if (!j.ok) {
    showToast((j.error && j.error.message) || 'Could not load what this service can fetch', 'danger');
    return;
  }
  accessServiceData = j.data;
  renderServiceAccess();
}

function renderServiceAccess() {
  var data = accessServiceData;
  document.getElementById('access-service-name').textContent =
    data.service.display_name || data.service.id;

  var box = document.getElementById('access-service-secrets');
  box.replaceChildren();
  var grants = data.grants || [];
  if (!grants.length) {
    box.append(accessEl(
      'p',
      'No secrets yet. Add one below so this service can fetch it when it starts.',
      'text-muted'
    ));
  }
  grants.forEach(function (g) {
    var row = accessEl('div', null, 'access-row');
    row.append(accessEl('strong', g.name));
    row.append(accessEl('span', g.owner, 'text-muted'));
    row.append(accessEl('span', g.allow_write ? 'read and write' : 'read only', 'text-muted'));
    var end = 'no end date';
    if (g.expires_at) end = (g.expired ? 'ended ' : 'ends ') + accessDate(g.expires_at);
    row.append(accessEl('span', end, 'text-muted'));
    var remove = accessEl('button', 'Remove', 'btn btn-sm btn-secondary');
    remove.type = 'button';
    remove.addEventListener('click', async function () {
      var ok = await confirmDialog('Remove ' + g.name + '? This service will not be able to fetch it any more.');
      if (!ok) return;
      var r = await apiFetch('/api/credential-consumers/grants/' + encodeURIComponent(g.grant_id), { method: 'DELETE' });
      if (!r.ok) {
        showToast((r.error && r.error.message) || 'Could not remove that secret', 'danger');
        return;
      }
      showToast('Secret removed');
      await reloadServiceAccess();
    });
    row.append(remove);
    box.append(row);
  });

  renderAccessKeys(data.keys || [], 'access-service-keys');

  // The picker: the secrets this service does not have yet.
  var pick = document.getElementById('access-service-secret-pick');
  pick.replaceChildren();
  var held = {};
  grants.forEach(function (g) { held[g.credential_id] = true; });
  var offered = 0;
  (data.all_credentials || []).forEach(function (c) {
    if (held[c.id]) return;
    var option = accessEl('option', c.name + ' (' + c.owner + ')');
    option.value = c.id;
    pick.append(option);
    offered += 1;
  });
  document.getElementById('access-service-secret-add').disabled = !offered;
  if (!offered) {
    var none = accessEl('option', 'Every secret is already listed');
    none.value = '';
    pick.append(none);
  }
}

async function addServiceSecret() {
  var entryId = document.getElementById('access-service-secret-pick').value;
  if (!entryId) return;
  var body = {
    consumer_agent_id: accessServiceId,
    entry_id: entryId,
    allow_write: document.getElementById('access-service-secret-write').checked,
  };
  var expires = accessEndOfDayUtc(document.getElementById('access-service-secret-expires').value);
  if (expires) body.expires_at = expires;
  var r = await apiFetch('/api/credential-consumers/grants', { method: 'POST', body: JSON.stringify(body) });
  if (!r.ok) {
    showToast((r.error && r.error.message) || 'Could not add that secret', 'danger');
    return;
  }
  showToast('Secret added');
  document.getElementById('access-service-secret-write').checked = false;
  document.getElementById('access-service-secret-expires').value = '';
  await reloadServiceAccess();
}

// ── one connection: who can use it ──────────────────────────────────────────

var accessConnectionId = null;
var accessConnectionData = null;

async function openConnectionUsers(id) {
  accessConnectionId = id;
  var j = await apiFetch('/api/access/connections/' + encodeURIComponent(id));
  if (!j.ok) {
    showToast((j.error && j.error.message) || 'Could not load who can use this connection', 'danger');
    return;
  }
  accessConnectionData = j.data;
  renderConnectionUsers();
  openModal('access-connection-modal');
}

async function reloadConnectionUsers() {
  var j = await apiFetch('/api/access/connections/' + encodeURIComponent(accessConnectionId));
  if (!j.ok) {
    showToast((j.error && j.error.message) || 'Could not load who can use this connection', 'danger');
    return;
  }
  accessConnectionData = j.data;
  renderConnectionUsers();
}

function renderConnectionUsers() {
  var data = accessConnectionData;
  document.getElementById('access-connection-name').textContent = data.connection.name;

  var signIn = document.getElementById('access-connection-credential');
  signIn.replaceChildren();
  if (data.credential) {
    signIn.append(accessEl('span', 'Signs in with: '));
    signIn.append(accessEl('strong', data.credential.name));
  } else {
    signIn.append(accessEl('span', 'No credential needed'));
  }

  var box = document.getElementById('access-connection-users');
  box.replaceChildren();
  var users = data.users || [];
  if (!users.length) {
    box.append(accessEl('p', 'No agent can use this connection yet.', 'text-muted'));
    box.append(accessEl(
      'p',
      'A connection is used by the agents that can read its workspace or owner.',
      'form-hint'
    ));
    return;
  }
  users.forEach(function (u) {
    var row = accessEl('div', null, 'access-row');
    row.append(accessEl('strong', u.display_name));
    row.append(accessEl('span', u.reason, 'text-muted'));
    var chip = accessNeedsChip(u, 'Run capabilities');
    if (chip) {
      row.append(chip);
      var turn = accessEl('button', 'Turn on', 'btn btn-sm btn-secondary');
      turn.type = 'button';
      turn.addEventListener('click', function () {
        accessTurnOn(
          u.agent_id,
          u.needs || 'connectors_execute',
          u.needs_label || 'Run capabilities',
          u.display_name,
          reloadConnectionUsers
        );
      });
      row.append(turn);
    } else {
      row.append(accessEl('span', 'can use', 'text-muted'));
    }
    box.append(row);
  });
}

// ── the buttons that open the dialogs ───────────────────────────────────────

document.addEventListener('click', function (ev) {
  if (!ev.target || !ev.target.closest) return;
  var agent = ev.target.closest('[data-agent-access]');
  if (agent) {
    ev.preventDefault();
    openAgentAccess(agent.dataset.agentAccess);
    return;
  }
  var service = ev.target.closest('[data-service-access]');
  if (service) {
    ev.preventDefault();
    openServiceAccess(service.dataset.serviceAccess);
    return;
  }
  var binding = ev.target.closest('[data-binding-users]');
  if (binding) {
    ev.preventDefault();
    openConnectionUsers(binding.dataset.bindingUsers);
  }
});

document.addEventListener('DOMContentLoaded', function () {
  var add = document.getElementById('access-agent-workspace-add');
  if (add) {
    add.addEventListener('click', function () {
      var workspaceId = document.getElementById('access-agent-workspace-pick').value;
      if (!workspaceId) return;
      accessPreviewWorkspace(
        workspaceId,
        'add',
        document.getElementById('access-agent-workspace-read').checked,
        document.getElementById('access-agent-workspace-write').checked
      );
    });
  }
  var save = document.getElementById('access-agent-save');
  if (save) save.addEventListener('click', saveAgentAccess);
  var cancel = document.getElementById('access-agent-cancel');
  if (cancel) cancel.addEventListener('click', cancelAgentAccess);

  var addSecret = document.getElementById('access-service-secret-add');
  if (addSecret) addSecret.addEventListener('click', addServiceSecret);
});

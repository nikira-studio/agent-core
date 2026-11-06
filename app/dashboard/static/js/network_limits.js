// Settings → Security → Network limits. Reads and saves through /api/network-limits.
(function() {
  const root = document.getElementById('network-limits');
  if (!root) return;

  function lines(id) {
    return document.getElementById(id).value.split('\n').map(function(s) { return s.trim(); }).filter(Boolean);
  }
  function note(id, text, kind) {
    const el = document.getElementById(id);
    el.className = 'alert alert-' + (kind || 'success');
    el.textContent = text;
    el.style.display = text ? '' : 'none';
  }

  function render(state) {
    const internal = state.internal, broker = state.broker;
    const mode = document.getElementById('nl-internal-mode');
    mode.value = internal.mode;
    mode.disabled = internal.mode_from_environment;
    document.getElementById('nl-internal-env').style.display = internal.mode_from_environment ? '' : 'none';
    document.getElementById('nl-allowed-internal').value = internal.allowed.join('\n');
    const seen = document.getElementById('nl-would-block');
    if (internal.would_block_hosts && internal.would_block_hosts.length) {
      seen.innerHTML = '<p class="form-hint" style="margin-bottom:4px">These private addresses were reached in the last 7 days and would be blocked. Allow the ones you expect:</p>' +
        internal.would_block_hosts.map(function(h) {
          return '<div class="nl-seen" style="display:flex;gap:8px;align-items:center;margin:2px 0"><code>' + escapeHtml(h.host) +
            '</code><span class="text-muted">' + escapeHtml(h.address || '') + '</span>' +
            '<button type="button" class="btn btn-sm btn-secondary" data-allow-host="' + escapeHtml(h.host) + '">Allow</button></div>';
        }).join('');
    } else {
      seen.innerHTML = internal.mode === 'report' ? '<p class="form-hint">Nothing has been reached at a private address that is not already allowed.</p>' : '';
    }
    const bmode = document.getElementById('nl-broker-mode');
    bmode.value = broker.mode;
    bmode.disabled = broker.mode_from_environment;
    document.getElementById('nl-broker-env').style.display = broker.mode_from_environment ? '' : 'none';
    document.getElementById('nl-broker-allowed').value = broker.allowed.join('\n');
    document.getElementById('nl-broker-status').textContent = !broker.restricted
      ? 'Any caller that holds the broker credential is accepted. Add the addresses that should call it to restrict it.'
      : (broker.mode === 'enforce'
          ? 'Only the callers listed here (and this server itself) may call it. ' + broker.refused + ' refused in the last 7 days.'
          : 'Report only: nobody is refused yet. ' + broker.would_refuse + ' callers outside the list in the last 7 days would be.');
  }

  async function load() {
    const j = await apiFetch('/api/network-limits');
    if (j.ok) render(j.data.network_limits);
  }

  async function save(changes, noteId, confirmText) {
    if (confirmText && !confirm(confirmText)) return;
    const j = await apiFetch('/api/network-limits', { method: 'PUT', body: JSON.stringify(changes) });
    if (j.ok) { render(j.data.network_limits); note(noteId, 'Saved.'); }
    else note(noteId, (j.error && j.error.message) || 'Could not save', 'danger');
  }

  document.getElementById('nl-save-internal').addEventListener('click', function() {
    const changes = { allowed_internal: lines('nl-allowed-internal') };
    const mode = document.getElementById('nl-internal-mode');
    if (!mode.disabled) changes.internal_mode = mode.value;
    save(changes, 'nl-internal-note', mode.value === 'block'
      ? 'Block private addresses? Services, webhooks and imports at addresses like 192.168.x.x stop working unless they are in the allowed list.' : null);
  });
  document.getElementById('nl-save-broker').addEventListener('click', function() {
    const changes = { broker_allowed: lines('nl-broker-allowed') };
    const mode = document.getElementById('nl-broker-mode');
    if (!mode.disabled) changes.broker_mode = mode.value;
    save(changes, 'nl-broker-note', mode.value === 'enforce' && changes.broker_allowed.length
      ? 'Refuse every caller that is not in the list? A runner on another address stops resolving credentials.' : null);
  });
  root.addEventListener('click', function(ev) {
    const allow = ev.target.closest('[data-allow-host]');
    if (!allow) return;
    const current = lines('nl-allowed-internal');
    if (current.indexOf(allow.dataset.allowHost) < 0) current.push(allow.dataset.allowHost);
    save({ allowed_internal: current }, 'nl-internal-note');
  });
  load();
})();

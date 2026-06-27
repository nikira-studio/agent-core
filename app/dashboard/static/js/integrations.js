function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, function(c) {
    return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
  });
}

function downloadGeneratedOutput(filename, content) {
  const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function getGeneratedOutputText() {
  return document.querySelector('.output-block')?.innerText || '';
}

function getSetupScrollStorageKey() {
  return (_AC_APP_SLUG + '-integrations-scroll:') + window.location.pathname;
}

function saveSetupScrollPosition() {
  try {
    sessionStorage.setItem(getSetupScrollStorageKey(), String(window.scrollY || 0));
  } catch (e) {}
}

function restoreSetupScrollPosition() {
  try {
    const raw = sessionStorage.getItem(getSetupScrollStorageKey());
    if (raw === null) return;
    const y = parseInt(raw, 10);
    if (!Number.isFinite(y)) return;
    requestAnimationFrame(() => window.scrollTo({ top: y, behavior: 'auto' }));
  } catch (e) {}
}

function getIntegrationConnectionKeyStorageKey() {
  const params = new URLSearchParams(window.location.search);
  const userId = params.get('user_id') || document.getElementById('user_id')?.value || '';
  const workspaceId = params.get('workspace_id') || document.getElementById('workspace_id')?.value || '';
  const agentId = params.get('agent_id') || document.getElementById('agent_id')?.value || '';
  const target = params.get('target') || 'generic_mcp';
  return [_AC_APP_SLUG + '-connection-key', userId, workspaceId, agentId, target].join(':');
}

function getStoredIntegrationConnectionKey() {
  try {
    return sessionStorage.getItem(getIntegrationConnectionKeyStorageKey()) || '';
  } catch (e) {
    return '';
  }
}

function setStoredIntegrationConnectionKey(key) {
  try {
    if (key) sessionStorage.setItem(getIntegrationConnectionKeyStorageKey(), key);
  } catch (e) {}
}

function applyStoredIntegrationConnectionKey() {
  const key = getStoredIntegrationConnectionKey();
  if (!key) return;
  const block = document.querySelector('.output-block');
  if (!block) return;
  const current = block.innerText || '';
  if (!current.includes(_AC_API_KEY_ANGLE) && !current.includes(_AC_API_KEY_BRACKET)) return;
  const updated = current
    .replaceAll(_AC_API_KEY_ANGLE, key)
    .replaceAll(_AC_API_KEY_BRACKET, key);
  if (updated !== current) {
    block.innerText = updated;
    const warning = document.getElementById('connection-warning');
    if (warning) {
      warning.textContent = 'This page is using the last generated one-time key for the current context.';
      warning.style.display = 'block';
    }
  }
}

function copyGeneratedOutput(btn) {
  copyToClipboard(getGeneratedOutputText(), btn);
}

function downloadCurrentOutput(filename) {
  downloadGeneratedOutput(filename, getGeneratedOutputText());
}

async function applyRecommendedAccess() {
  const btn = document.getElementById('apply-access-btn');
  const status = document.getElementById('apply-status');
  const includeUserWrite = document.getElementById('include-user-write')?.checked ? true : false;
  btn.disabled = true;
  btn.textContent = 'Applying...';
  status.textContent = '';
  status.style.color = '';

  const params = new URLSearchParams(window.location.search);
  const userId = params.get('user_id') || document.getElementById('user_id')?.value;
  const projectId = params.get('workspace_id') || document.getElementById('workspace_id')?.value;
  const agentId = params.get('agent_id') || document.getElementById('agent_id')?.value;

  if (!userId || !projectId || !agentId) {
    status.textContent = 'Select user, workspace, and agent first.';
    status.style.color = 'var(--warning)';
    btn.disabled = false;
    btn.textContent = 'Apply Recommended Access';
    return;
  }

  try {
    const r = await fetch('/api/integrations/apply-recommended-access', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: userId, workspace_id: projectId, agent_id: agentId, include_user_write: includeUserWrite })
    });
    const j = await r.json();
    if (j.ok) {
      status.textContent = 'Access updated. Reloading...';
      status.style.color = 'var(--success)';
      setTimeout(() => { window.location.href = window.location.pathname + '?user_id=' + encodeURIComponent(userId) + '&workspace_id=' + encodeURIComponent(projectId) + '&agent_id=' + encodeURIComponent(agentId) + '&output_type=' + (params.get('output_type') || 'instructions'); }, 800);
    } else {
      status.textContent = j.error?.message || 'Failed';
      status.style.color = 'var(--danger)';
      btn.disabled = false;
      btn.textContent = 'Apply Recommended Access';
    }
  } catch(e) {
    status.textContent = 'Error applying access';
    status.style.color = 'var(--danger)';
    btn.disabled = false;
    btn.textContent = 'Apply Recommended Access';
  }
}

async function generateConnectionConfig() {
  const btn = document.getElementById('generate-connection-btn');
  const warning = document.getElementById('connection-warning');
  const params = new URLSearchParams(window.location.search);
  const userId = params.get('user_id') || document.getElementById('user_id')?.value;
  const projectId = params.get('workspace_id') || document.getElementById('workspace_id')?.value || '';
  const agentId = params.get('agent_id') || document.getElementById('agent_id')?.value;
  const target = params.get('target') || 'generic_mcp';
  const outputType = params.get('output_type') || 'env';

  if (!userId || !agentId) {
    showToast('Select user and agent first', 'warning');
    return;
  }
  if (!confirm('Generate a new one-time key and config? This rotates the agent key and invalidates any previous key for this agent.')) return;

  btn.disabled = true;
  btn.textContent = 'Generating...';
  try {
    const r = await fetch('/api/integrations/generate-connection', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        user_id: userId,
        workspace_id: projectId,
        agent_id: agentId,
        target: target,
        output_type: outputType
      })
    });
    const j = await r.json();
    if (j.ok) {
      setStoredIntegrationConnectionKey(j.data.api_key || '');
      const block = document.querySelector('.output-block');
      if (block) block.innerText = j.data.output || '';
      const label = document.querySelector('.output-label');
      if (label) label.textContent = j.data.output_label || 'Connection Config';
      if (warning) {
        warning.textContent = j.data.warning || 'This key is shown once.';
        warning.style.display = 'block';
      }
      showToast('Connection config generated', 'success');
    } else {
      showToast(j.error?.message || 'Failed to generate config', 'danger');
    }
  } catch(e) {
    showToast('Failed to generate config', 'danger');
  }
  btn.disabled = false;
  btn.textContent = btn.dataset.label || 'Generate One-Time Key + Config';
}

document.addEventListener('DOMContentLoaded', applyStoredIntegrationConnectionKey);
document.addEventListener('DOMContentLoaded', restoreSetupScrollPosition);
window.addEventListener('beforeunload', saveSetupScrollPosition);


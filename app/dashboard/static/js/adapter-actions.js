function setAdapterButtonState(btn, label) {
  if (!btn) return;
  if (!btn.dataset.originalLabel) btn.dataset.originalLabel = btn.textContent || '';
  btn.disabled = label !== null;
  btn.textContent = label === null ? (btn.dataset.originalLabel || btn.textContent || '') : label;
}

async function runAdapterAction(btn, adapterId, endpoint, method, progressLabel, successLabel) {
  setAdapterButtonState(btn, progressLabel + '...');
  const j = await apiFetch('/api/connector-types/adapters/' + adapterId + '/' + endpoint, {
    method: method,
    body: method === 'DELETE' ? undefined : JSON.stringify({})
  });
  if (!j.ok) {
    setAdapterButtonState(btn, null);
    showToast(j.error?.message || 'Failed to ' + endpoint + ' adapter', 'danger');
    return;
  }
  showToast(successLabel + ' ' + (j.data.adapter?.connector_type?.display_name || adapterId), 'success');
  location.reload();
}

async function installAdapter(btn, adapterId) {
  return runAdapterAction(btn, adapterId, 'install', 'POST', 'Installing', 'Installed');
}

async function updateAdapter(btn, adapterId) {
  return runAdapterAction(btn, adapterId, 'update', 'POST', 'Updating', 'Updated');
}

async function uninstallAdapter(btn, adapterId) {
  return runAdapterAction(btn, adapterId, 'install', 'DELETE', 'Uninstalling', 'Uninstalled');
}

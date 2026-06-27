function setAdapterButtonState(btn, label) {
  if (!btn) return;
  if (!btn.dataset.originalLabel) {
    btn.dataset.originalLabel = btn.textContent || '';
  }
  btn.disabled = label !== null;
  btn.textContent = label === null ? (btn.dataset.originalLabel || btn.textContent || '') : label;
}

async function installAdapter(btn, adapterId) {
  setAdapterButtonState(btn, 'Installing...');
  const j = await apiFetch('/api/connector-types/adapters/' + adapterId + '/install', {
    method: 'POST',
    body: JSON.stringify({})
  });
  if (!j.ok) {
    setAdapterButtonState(btn, null);
    showToast(j.error?.message || 'Failed to install adapter', 'danger');
    return;
  }
  showToast('Installed ' + (j.data.adapter?.connector_type?.display_name || adapterId), 'success');
  location.reload();
}

async function updateAdapter(btn, adapterId) {
  setAdapterButtonState(btn, 'Updating...');
  const j = await apiFetch('/api/connector-types/adapters/' + adapterId + '/update', {
    method: 'POST',
    body: JSON.stringify({})
  });
  if (!j.ok) {
    setAdapterButtonState(btn, null);
    showToast(j.error?.message || 'Failed to update adapter', 'danger');
    return;
  }
  showToast('Updated ' + (j.data.adapter?.connector_type?.display_name || adapterId), 'success');
  location.reload();
}

async function uninstallAdapter(btn, adapterId) {
  setAdapterButtonState(btn, 'Uninstalling...');
  const j = await apiFetch('/api/connector-types/adapters/' + adapterId + '/install', {
    method: 'DELETE'
  });
  if (!j.ok) {
    setAdapterButtonState(btn, null);
    showToast(j.error?.message || 'Failed to uninstall adapter', 'danger');
    return;
  }
  showToast('Uninstalled ' + adapterId, 'success');
  location.reload();
}

function filterAdapters() {
  const q = (document.getElementById('adapter-search').value || '').trim().toLowerCase();
  document.querySelectorAll('[data-adapter-card]').forEach(function(card) {
    const hay = (card.dataset.searchText || '').toLowerCase();
    card.style.display = !q || hay.includes(q) ? '' : 'none';
  });
}

document.getElementById('adapter-search').addEventListener('input', filterAdapters);

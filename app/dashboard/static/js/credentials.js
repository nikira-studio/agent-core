async function createCredential(e) {
  e.preventDefault();
  const body = {
    name: document.getElementById('credential-name').value,
    label: document.getElementById('credential-label').value || null,
    scope: document.getElementById('credential-scope').value,
    value: document.getElementById('credential-value').value,
  };
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
  if (!confirm('Delete this credential? Connector bindings using it will stop working.')) return;
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
  document.getElementById('edit-credential-value').value = '';
  openModal('edit-credential-modal');
}

async function submitEditCredential(e) {
  e.preventDefault();
  const id = document.getElementById('edit-credential-id').value;
  const replacementValue = document.getElementById('edit-credential-value').value;
  const body = {
    name: document.getElementById('edit-credential-name').value,
    label: document.getElementById('edit-credential-label').value || null,
  };
  if (replacementValue) body.value = replacementValue;
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

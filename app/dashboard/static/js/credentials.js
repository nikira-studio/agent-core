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
  document.getElementById('edit-credential-scope').dataset.originalScope = c.scope || '';
  document.getElementById('edit-credential-value').value = '';
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
  if (body.scope !== originalScope && !confirm(
    'Move this credential from ' + originalScope + ' to ' + body.scope + '? Linked connector bindings will stay in their current scope.'
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

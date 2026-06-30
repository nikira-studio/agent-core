function parseBindingGuidance() {
  const select = document.getElementById('binding-connector-type');
  if (!select || select.selectedIndex < 0) return {};
  const option = select.options[select.selectedIndex];
  const raw = option?.dataset?.guidance || '{}';
  try {
    return JSON.parse(raw) || {};
  } catch (e) {
    return {};
  }
}

function bindingPlaceholderForField(field, kind) {
  const name = String(field || '').trim();
  const lower = name.toLowerCase();

  if (kind === 'config') {
    if (lower === 'base_url' || lower === 'url' || lower.endsWith('_url')) {
      return lower === 'base_url' ? 'http://HOST:PORT' : 'https://example.com';
    }
    if (lower.includes('host')) return 'HOST';
    if (lower.includes('port')) return 'PORT';
    if (lower.includes('path')) return '/path';
    return 'your-' + name.replace(/_/g, '-');
  }

  if (lower === 'username') return 'your-username';
  if (lower.includes('password')) return 'your-password';
  if (lower.includes('token') || lower.includes('secret') || lower.includes('key')) {
    return 'YOUR_' + name.toUpperCase().replace(/[^A-Z0-9]+/g, '_');
  }
  return 'your-' + name.replace(/_/g, '-');
}

function findMatchingCredentialOption(name, scope) {
  const select = document.getElementById('binding-credential');
  if (!select) return null;
  const targetName = String(name || '').trim().toLowerCase();
  const targetScope = String(scope || '').trim();
  const options = Array.from(select.options || []);
  let exact = null;
  let nameOnly = null;
  for (const option of options) {
    if (!option.value) continue;
    const optionName = String(option.dataset?.credentialName || option.textContent || '').trim().toLowerCase();
    const optionScope = String(option.dataset?.credentialScope || '').trim();
    if (!optionName || optionName !== targetName) continue;
    if (targetScope && optionScope === targetScope) return option;
    if (!nameOnly) nameOnly = option;
    if (optionScope === targetScope) exact = option;
  }
  return exact || nameOnly;
}

function buildBindingJsonTemplate(fields, kind) {
  const out = {};
  (Array.isArray(fields) ? fields : []).forEach(function(field) {
    out[field] = bindingPlaceholderForField(field, kind);
  });
  return JSON.stringify(out, null, 2);
}

function clearBindingTemplateFlag(el) {
  if (!el || !el.dataset) return;
  el.dataset.template = 'false';
}

function resetCreateBindingForm() {
  const form = document.getElementById('create-binding-form');
  if (form) form.reset();
  const configEl = document.getElementById('binding-config');
  if (configEl) {
    configEl.value = '';
    configEl.dataset.template = 'false';
  }
  const setupGroup = document.getElementById('binding-setup-group');
  if (setupGroup) setupGroup.style.display = 'none';
  const setupEl = document.getElementById('binding-setup');
  if (setupEl) setupEl.innerHTML = '';
  const recipe = document.getElementById('binding-recipe');
  if (recipe) recipe.textContent = 'Choose a connector type to see the required credential and config fields.';
  const credentialMode = document.getElementById('binding-credential-mode');
  if (credentialMode) credentialMode.value = 'none';
  toggleBindingCredentialMode(false);
}

function prefillBindingConfig(guidance) {
  const configEl = document.getElementById('binding-config');
  if (!configEl) return;
  const fields = Array.isArray(guidance?.config_fields) ? guidance.config_fields : [];
  if (!fields.length) {
    configEl.dataset.template = 'false';
    return;
  }
  if (configEl.value.trim() && configEl.dataset.template !== 'true') return;
  configEl.value = buildBindingJsonTemplate(fields, 'config');
  configEl.dataset.template = 'true';
  if (!configEl.dataset.templateListenerAttached) {
    configEl.addEventListener('input', function() {
      clearBindingTemplateFlag(configEl);
    });
    configEl.dataset.templateListenerAttached = 'true';
  }
}

function renderBindingCredentialEditor(guidance) {
  const wrap = document.getElementById('binding-new-credential-editor');
  if (!wrap) return;
  const fields = Array.isArray(guidance?.credential_fields) ? guidance.credential_fields : [];
  if (!fields.length) {
    wrap.dataset.template = 'false';
    const authType = String(guidance?.auth_type || 'none');
    if (authType && authType !== 'none') {
      wrap.innerHTML =
        '<div class="form-group">' +
          '<label>Credential Value</label>' +
          '<input type="password" id="binding-new-credential-value" autocomplete="new-password" placeholder="Optional API key or token">' +
        '</div>' +
        '<div class="form-hint">This credential is optional. Leave credential mode set to No credential for public or unauthenticated access.</div>';
      return;
    }
    wrap.innerHTML = '<div class="form-hint">This connector does not declare a credential payload.</div>';
    return;
  }

  const isMulti = fields.length > 1;
  const hint = isMulti
    ? 'Store the credential as one JSON object with these fields: ' + fields.join(', ')
    : 'Store the single secret value for ' + fields[0];

  if (isMulti) {
    const template = buildBindingJsonTemplate(fields, 'credential');
    wrap.innerHTML =
      '<div class="form-group">' +
        '<label>Credential JSON *</label>' +
        '<textarea id="binding-new-credential-value" rows="5" autocomplete="off" data-template="true" placeholder=' +
          '"Replace the placeholders before saving"' +
        '></textarea>' +
      '</div>' +
      '<div class="form-hint">' + escapeHtml(hint) + ' The template is prefilled below.</div>';
    const valueEl = document.getElementById('binding-new-credential-value');
    if (valueEl) {
      valueEl.value = template;
      valueEl.addEventListener('input', function() {
        clearBindingTemplateFlag(valueEl);
      }, { once: true });
    }
    return;
  }

  const single = fields[0];
  const looksSecret = /password|secret|token|key/i.test(single);
  wrap.innerHTML =
    '<div class="form-group">' +
      '<label>Credential Value *</label>' +
      '<input type="' + (looksSecret ? 'password' : 'text') + '" id="binding-new-credential-value" autocomplete="new-password" placeholder="e.g. ' + escapeHtml(single) + '">' +
    '</div>' +
    '<div class="form-hint">' + escapeHtml(hint) + '</div>';
}

let pendingOAuthAuthorizationUrl = null;

function updateBindingFormContext(defaultName) {
  const guidance = parseBindingGuidance();
  const recipe = document.getElementById('binding-recipe');
  const setupGroup = document.getElementById('binding-setup-group');
  const setupEl = document.getElementById('binding-setup');
  const nameEl = document.getElementById('binding-name');
  const credNameEl = document.getElementById('binding-new-credential-name');

  if (recipe) {
    const bits = [];
    if (guidance?.suggested_binding_name) {
      bits.push('Suggested binding: ' + guidance.suggested_binding_name);
    }
    if (Array.isArray(guidance?.credential_fields) && guidance.credential_fields.length) {
      bits.push('Credential JSON fields: ' + guidance.credential_fields.join(', '));
    } else if (guidance?.auth_type && guidance.auth_type !== 'none') {
      bits.push('Credential optional');
    }
    if (Array.isArray(guidance?.config_fields) && guidance.config_fields.length) {
      bits.push('Config JSON fields: ' + guidance.config_fields.join(', '));
    }
    recipe.textContent = bits.length ? bits.join(' · ') : 'No extra binding requirements declared.';
  }

  if (setupGroup && setupEl) {
    const instructions = String(guidance?.setup_instructions || '').trim();
    const documentationUrl = String(guidance?.documentation_url || '').trim();
    const hasDocumentationUrl = documentationUrl.startsWith('https://');
    setupGroup.style.display = instructions || hasDocumentationUrl ? '' : 'none';
    setupEl.innerHTML = '';
    if (instructions) {
      setupEl.appendChild(document.createTextNode(instructions));
    }
    if (hasDocumentationUrl) {
      if (instructions) setupEl.appendChild(document.createTextNode('\\n'));
      const link = document.createElement('a');
      link.href = documentationUrl;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = 'Open setup documentation';
      setupEl.appendChild(link);
    }
  }

  if (nameEl && !nameEl.value) {
    nameEl.value = defaultName || guidance?.suggested_binding_name || '';
  }
  if (credNameEl && !credNameEl.value) {
    credNameEl.value = guidance?.suggested_credential_name || '';
  }

  const credentialSelect = document.getElementById('binding-credential');
  const credentialMode = document.getElementById('binding-credential-mode');
  const bindingScope = document.getElementById('binding-scope');
  if (
    credentialSelect &&
    credentialMode &&
    credentialMode.value === 'existing' &&
    bindingScope &&
    bindingScope.value &&
    guidance?.suggested_credential_name
  ) {
    const match = findMatchingCredentialOption(
      guidance.suggested_credential_name,
      bindingScope.value
    );
    if (match) {
      credentialSelect.value = match.value;
    }
  }

  renderBindingCredentialEditor(guidance);
  prefillBindingConfig(guidance);
}

function defaultCredentialModeForGuidance(guidance) {
  if (!bindingCredentialRequired(guidance)) return 'none';
  return Array.isArray(guidance?.credential_fields) && guidance.credential_fields.length > 1 ? 'new' : 'existing';
}

function bindingConnectorTypeChanged() {
  const credentialMode = document.getElementById('binding-credential-mode');
  const guidance = parseBindingGuidance();
  if (credentialMode) credentialMode.value = defaultCredentialModeForGuidance(guidance);
  updateBindingFormContext();
  toggleBindingCredentialMode(false);
}

function bindingCredentialRequired(guidance) {
  if (guidance && guidance.credential_required === true) return true;
  return Array.isArray(guidance?.credential_fields) && guidance.credential_fields.length > 0;
}

async function createBinding(e) {
  e.preventDefault();
  let credentialId = document.getElementById('binding-credential').value || null;
  const credentialMode = document.getElementById('binding-credential-mode').value;
  const bindingScope = document.getElementById('binding-scope').value;
  const guidance = parseBindingGuidance();
  const requiredCredentialFields = Array.isArray(guidance?.credential_fields) ? guidance.credential_fields : [];
  const credentialRequired = bindingCredentialRequired(guidance);

  if (credentialMode === 'none') {
    credentialId = null;
  } else if (credentialMode === 'new') {
    const credentialName = document.getElementById('binding-new-credential-name').value;
    const credentialValueEl = document.getElementById('binding-new-credential-value');
    const credentialValue = credentialValueEl ? credentialValueEl.value : '';
    if (!bindingScope) {
      showToast('Select a scope before creating a credential', 'danger');
      return;
    }
    if (!credentialName || !credentialValue) {
      showToast(credentialRequired ? 'Credential name and secret value are required' : 'Enter a credential value or choose No credential', 'danger');
      return;
    }
    const existingCredential = findMatchingCredentialOption(credentialName, bindingScope);
    if (existingCredential) {
      credentialId = existingCredential.value;
    } else {
      if (requiredCredentialFields.length > 1) {
        let parsed = null;
        try {
          parsed = JSON.parse(credentialValue);
        } catch (err) {
          showToast('Enter valid JSON for ' + requiredCredentialFields.join(', '), 'danger');
          return;
        }
        const missing = requiredCredentialFields.filter(function(field) {
          return parsed[field] === undefined || parsed[field] === null || parsed[field] === '';
        });
        if (missing.length) {
          showToast('Credential JSON must include: ' + missing.join(', '), 'danger');
          return;
        }
        const credentialEl = document.getElementById('binding-new-credential-value');
        if (credentialEl && credentialEl.dataset.template === 'true') {
          showToast('Replace the credential template values before saving', 'danger');
          return;
        }
      }
      const credentialBody = {
        name: credentialName,
        label: credentialName,
        scope: bindingScope,
        value: credentialValue,
      };
      const credentialResult = await apiFetch('/api/credentials/entries', { method: 'POST', body: JSON.stringify(credentialBody) });
      if (!credentialResult.ok) {
        showToast(credentialResult.error?.message || 'Failed to create credential', 'danger');
        return;
      }
      credentialId = credentialResult.data.entry.id;
    }
  }

  if (credentialRequired && !credentialId) {
    showToast('This adapter needs a credential for: ' + requiredCredentialFields.join(', '), 'danger');
    return;
  }

  if ((guidance.config_fields || []).length) {
    const configEl = document.getElementById('binding-config');
    let configValue = (configEl && configEl.value ? configEl.value.trim() : '');
    if (!configValue) {
      showToast('This adapter needs binding config JSON with: ' + guidance.config_fields.join(', '), 'danger');
      return;
    }
    let parsedConfig = null;
    try {
      parsedConfig = JSON.parse(configValue);
    } catch (err) {
      showToast('Binding config must be valid JSON', 'danger');
      return;
    }
    const missingConfig = (guidance.config_fields || []).filter(function(field) {
      return parsedConfig[field] === undefined || parsedConfig[field] === null || parsedConfig[field] === '';
    });
    if (missingConfig.length) {
      showToast('Binding config must include: ' + missingConfig.join(', '), 'danger');
      return;
    }
    if (configEl && configEl.dataset.template === 'true') {
      showToast('Replace the config template values before saving', 'danger');
      return;
    }
  }

  const body = {
    connector_type_id: document.getElementById('binding-connector-type').value,
    name: document.getElementById('binding-name').value,
    scope: bindingScope,
    credential_id: credentialId,
    config_json: document.getElementById('binding-config').value || null,
    enabled: document.getElementById('binding-enabled').checked,
  };
  const j = await apiFetch('/api/connector-bindings', { method: 'POST', body: JSON.stringify(body) });
  if (j.ok) {
    showToast('Binding created', 'success');
    closeModal('create-binding-modal');
    document.getElementById('create-binding-form').reset();
    toggleBindingCredentialMode();
    location.reload();
  } else {
    showToast(j.error?.message || 'Failed to create binding', 'danger');
  }
}

function toggleBindingCredentialMode(refreshContext) {
  const mode = document.getElementById('binding-credential-mode').value;
  document.getElementById('binding-existing-credential-fields').style.display = mode === 'existing' ? '' : 'none';
  document.getElementById('binding-new-credential-fields').style.display = mode === 'new' ? '' : 'none';
  if (refreshContext !== false) updateBindingFormContext();
}

async function editBinding(id) {
  const j = await apiFetch('/api/connector-bindings/' + id);
  if (!j.ok) { showToast(j.error?.message || 'Error', 'danger'); return; }
  const b = j.data.binding;
  document.getElementById('edit-binding-id').value = id;
  document.getElementById('edit-binding-name').value = b.name || '';
  document.getElementById('edit-binding-scope').value = b.scope || '';
  document.getElementById('edit-binding-credential').value = b.credential_id || '';
  document.getElementById('edit-binding-config').value = b.config_json || '';
  document.getElementById('edit-binding-enabled').checked = !!b.enabled;
  openModal('edit-binding-modal');
}

async function submitEditBinding(e) {
  e.preventDefault();
  const id = document.getElementById('edit-binding-id').value;
  const body = {
    name: document.getElementById('edit-binding-name').value,
    scope: document.getElementById('edit-binding-scope').value,
    credential_id: document.getElementById('edit-binding-credential').value || null,
    config_json: document.getElementById('edit-binding-config').value || null,
    enabled: document.getElementById('edit-binding-enabled').checked,
  };
  const j = await apiFetch('/api/connector-bindings/' + id, { method: 'PUT', body: JSON.stringify(body) });
  if (j.ok) { showToast('Updated', 'success'); closeModal('edit-binding-modal'); location.reload(); }
  else { showToast(j.error?.message || 'Failed', 'danger'); }
}

async function deleteBinding(id) {
  if (!confirm('Delete this binding? This cannot be undone.')) return;
  const j = await apiFetch('/api/connector-bindings/' + id, { method: 'DELETE' });
  if (j.ok) { showToast('Deleted', 'success'); location.reload(); }
  else { showToast(j.error?.message || 'Failed', 'danger'); }
}

async function deleteConnectorType(id) {
  if (!confirm('Delete this connector type and all its bindings? This cannot be undone.')) return;
  const j = await apiFetch('/api/connector-types/' + id, { method: 'DELETE' });
  if (j.ok) { showToast('Deleted', 'success'); location.reload(); }
  else { showToast(j.error?.message || 'Failed', 'danger'); }
}

async function testBinding(id) {
  const j = await apiFetch('/api/connector-bindings/' + id + '/test', { method: 'POST' });
  if (j.ok) {
    const r = j.data.result;
    const content = document.getElementById('test-result-content');
    if (r.success) {
      content.innerHTML = '<div class="alert alert-success">Connection successful!</div>';
    } else {
      content.innerHTML = '<div class="alert alert-danger">Connection failed: ' + escapeHtml(r.error || 'Unknown error') + '</div>';
    }
    openModal('test-result-modal');
  } else {
    showToast(j.error?.message || 'Failed to test binding', 'danger');
  }
}

async function authorizeBindingOAuth(id) {
  const j = await apiFetch('/api/connector-bindings/' + id + '/oauth/start', { method: 'POST' });
  if (!j.ok) {
    showToast(j.error?.message || 'Failed to start OAuth authorization', 'danger');
    return;
  }
  const callbackUrl = j.data.callback_url;
  pendingOAuthAuthorizationUrl = j.data.authorization_url;
  const urlEl = document.getElementById('oauth-redirect-url');
  if (urlEl) urlEl.value = callbackUrl;
  openModal('oauth-redirect-modal');
}

async function copyOAuthRedirectUrl() {
  const urlEl = document.getElementById('oauth-redirect-url');
  const text = urlEl ? urlEl.value : '';
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    showToast('Redirect URI copied', 'success');
  } catch (err) {
    if (urlEl) {
      urlEl.focus();
      urlEl.select();
      document.execCommand('copy');
      showToast('Redirect URI copied', 'success');
    }
  }
}

function continueOAuthAuthorization() {
  if (!pendingOAuthAuthorizationUrl) {
    showToast('OAuth authorization URL is missing', 'danger');
    return;
  }
  closeModal('oauth-redirect-modal');
  window.location.href = pendingOAuthAuthorizationUrl;
}

(function showOAuthResult() {
  const success = getUrlParam('oauth_success');
  const error = getUrlParam('oauth_error');
  if (success) {
    showToast('OAuth authorization completed. The binding is ready to test.', 'success');
    setUrlParam('oauth_success', null);
  } else if (error) {
    showToast('OAuth authorization failed: ' + error, 'danger');
    setUrlParam('oauth_error', null);
  }
})();

function renderHealthResult(data) {
  const total = data.total || 0;
  const passed = data.passed || 0;
  const failed = data.failed || 0;
  const content = document.getElementById('test-result-content');
  if (!content) return;
  const rows = (data.results || []).map(function(r) {
    const status = r.success
      ? '<span class="badge badge-success">Passed</span>'
      : '<span class="badge badge-danger">Failed</span>';
    return '<tr><td>' + escapeHtml(r.binding_name || r.binding_id || '') + '</td><td><code>' + escapeHtml(r.scope || '') + '</code></td><td>' + status + '</td><td>' + escapeHtml(r.error || '') + '</td></tr>';
  }).join('');
  content.innerHTML =
    '<div class="' + (failed ? 'alert alert-warning' : 'alert alert-success') + '">Checked ' + total + ' binding(s): ' + passed + ' passed, ' + failed + ' failed.</div>' +
    (rows ? '<table><thead><tr><th>Binding</th><th>Scope</th><th>Status</th><th>Error</th></tr></thead><tbody>' + rows + '</tbody></table>' : '<div class="empty">No enabled bindings to check.</div>');
  openModal('test-result-modal');
}

async function checkHealth(btn) {
  const label = btn ? btn.textContent : null;
  if (btn) { btn.disabled = true; btn.textContent = 'Checking...'; }
  const j = await apiFetch('/api/connector-types/health-check', { method: 'POST', body: JSON.stringify({}) });
  if (!j.ok) {
    if (btn) { btn.disabled = false; btn.textContent = label; }
    showToast(j.error?.message || 'Health check failed', 'danger');
    return;
  }
  // Persist the result and reload so the Service Catalog health badges reflect the
  // freshly stored test state; the result modal is shown again after reload.
  sessionStorage.setItem('connectorHealthResult', JSON.stringify(j.data));
  location.reload();
}

(function showPendingHealthResult() {
  const stored = sessionStorage.getItem('connectorHealthResult');
  if (!stored) return;
  sessionStorage.removeItem('connectorHealthResult');
  try { renderHealthResult(JSON.parse(stored)); } catch (e) { /* ignore malformed */ }
})();

async function viewExecutions(id) {
  const j = await apiFetch('/api/connector-bindings/' + id + '/executions');
  if (!j.ok) { showToast(j.error?.message || 'Error', 'danger'); return; }
  const execs = j.data.executions || [];
  const rows = execs.map(function(e) {
    return '<tr><td>' + escapeHtml(e.action || '') + '</td><td>' + escapeHtml(e.result_status || '') + '</td><td>' + escapeHtml(e.executed_at || '') + '</td><td>' + escapeHtml(e.error_message || '-') + '</td></tr>';
  }).join('');
  document.getElementById('executions-content').innerHTML = execs.length
    ? '<table><thead><tr><th>Action</th><th>Status</th><th>When</th><th>Error</th></tr></thead><tbody>' + rows + '</tbody></table>'
    : '<em>No executions yet.</em>';
  openModal('executions-modal');
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, function(c) {
    return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
  });
}

function openNewBinding(typeId, defaultName) {
  resetCreateBindingForm();
  const el = document.getElementById('binding-connector-type');
  if (el) {
    el.value = typeId;
  }
  updateBindingFormContext(defaultName);
  const guidance = parseBindingGuidance();
  const credentialMode = document.getElementById('binding-credential-mode');
  if (credentialMode) credentialMode.value = defaultCredentialModeForGuidance(guidance);
  toggleBindingCredentialMode(false);
  openModal('create-binding-modal');
}

function focusBinding(bindingId) {
  const row = document.querySelector('[data-binding-id="' + CSS.escape(String(bindingId)) + '"]');
  if (!row) return;
  row.scrollIntoView({ behavior: 'smooth', block: 'center' });
  row.classList.add('row-highlight');
  setTimeout(function() {
    row.classList.remove('row-highlight');
  }, 1800);
}

function focusConnectorBindings(connectorTypeId) {
  const rows = document.querySelectorAll('[data-connector-type-id="' + CSS.escape(String(connectorTypeId)) + '"]');
  if (!rows.length) return;
  rows[0].scrollIntoView({ behavior: 'smooth', block: 'center' });
  rows.forEach(function(row) {
    row.classList.add('row-highlight');
    setTimeout(function() {
      row.classList.remove('row-highlight');
    }, 1800);
  });
}

let importSpecPreviewState = null;

function resetImportPreview() {
  importSpecPreviewState = null;
  const preview = document.getElementById('import-spec-preview');
  const importBtn = document.getElementById('import-spec-import-btn');
  if (preview) {
    preview.style.display = 'none';
    preview.innerHTML = '';
  }
  if (importBtn) {
    importBtn.disabled = true;
  }
}

function handleSpecFile(e) {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = function(ev) {
    document.getElementById('import-spec-json').value = ev.target.result;
    resetImportPreview();
    showToast('File loaded', 'success');
  };
  reader.readAsText(file);
}

['import-spec-url', 'import-spec-json', 'import-spec-name'].forEach(function(id) {
  const el = document.getElementById(id);
  if (el) {
    el.addEventListener('input', resetImportPreview);
    el.addEventListener('change', resetImportPreview);
  }
});

async function previewSpec(e) {
  if (e) e.preventDefault();
  const url = document.getElementById('import-spec-url').value.trim();
  const specJson = document.getElementById('import-spec-json').value.trim();
  const displayName = document.getElementById('import-spec-name').value.trim();

  if (!url && !specJson) {
    showToast('Provide a URL or paste/upload a spec', 'danger');
    return;
  }

  const body = {};
  if (url) body.url = url;
  if (specJson) body.spec_json = specJson;
  if (displayName) body.display_name = displayName;

  const j = await apiFetch('/api/connector-types/preview', { method: 'POST', body: JSON.stringify(body) });
  if (!j.ok) {
    showToast(j.error?.message || 'Validation failed', 'danger');
    return;
  }

  const preview = j.data.preview || {};
  importSpecPreviewState = preview;
  const previewEl = document.getElementById('import-spec-preview');
  const importBtn = document.getElementById('import-spec-import-btn');
  if (previewEl) {
    const servers = (preview.servers || []).slice(0, 3).map(escapeHtml).join('<br>');
    const warnings = (preview.warnings || []).map(function(w) {
      return '<li>' + escapeHtml(w) + '</li>';
    }).join('');
    const actions = (preview.supported_actions || []).slice(0, 8).map(function(a) {
      return '<span class="badge" style="margin:0 6px 6px 0;display:inline-block">' + escapeHtml(a) + '</span>';
    }).join('');
    previewEl.innerHTML =
      '<h4 style="margin-top:0">Preview</h4>' +
      '<table style="width:100%">' +
        '<tr><td style="padding:4px 8px 4px 0;color:var(--muted)">Name</td><td>' + escapeHtml(preview.display_name || preview.connector_type_id || 'API') + '</td></tr>' +
        '<tr><td style="padding:4px 8px 4px 0;color:var(--muted)">Connector ID</td><td><code>' + escapeHtml(preview.connector_type_id || '-') + '</code></td></tr>' +
        '<tr><td style="padding:4px 8px 4px 0;color:var(--muted)">Auth</td><td>' + escapeHtml(preview.auth_type || 'none') + '</td></tr>' +
        '<tr><td style="padding:4px 8px 4px 0;color:var(--muted)">Servers</td><td style="word-break:break-word">' + (servers || '<em>none</em>') + '</td></tr>' +
        '<tr><td style="padding:4px 8px 4px 0;color:var(--muted)">Actions</td><td>' + escapeHtml(String(preview.operation_count || 0)) + '</td></tr>' +
      '</table>' +
      (actions ? '<div style="margin-top:10px">' + actions + '</div>' : '') +
      (warnings ? '<div style="margin-top:10px"><strong>Warnings</strong><ul style="margin:6px 0 0 18px">' + warnings + '</ul></div>' : '');
    previewEl.style.display = '';
  }
  if (importBtn) {
    importBtn.disabled = false;
  }
  const previewName = preview.display_name || preview.connector_type_id || 'API';
  const previewCount = preview.operation_count != null ? preview.operation_count : 0;
  if (previewEl && previewEl.scrollIntoView) {
    previewEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
  showToast('Validated ' + previewName + ' (' + previewCount + ' actions)', 'success');
}

async function importSpec(e) {
  if (e) e.preventDefault();
  if (!importSpecPreviewState) {
    showToast('Validate the spec before creating it', 'danger');
    return;
  }
  const url = document.getElementById('import-spec-url').value.trim();
  const specJson = document.getElementById('import-spec-json').value.trim();
  const displayName = document.getElementById('import-spec-name').value.trim();

  if (!url && !specJson) {
    showToast('Provide a URL or paste/upload a spec', 'danger');
    return;
  }

  const body = {};
  if (url) body.url = url;
  if (specJson) body.spec_json = specJson;
  if (displayName) body.display_name = displayName;

  const j = await apiFetch('/api/connector-types/import', { method: 'POST', body: JSON.stringify(body) });
  if (!j.ok) {
    showToast(j.error?.message || 'Import failed', 'danger');
    return;
  }

  const ct = j.data.connector_type || {};
  const actionCount = j.data.operation_count || (ct.supported_actions || []).length;
  showToast('Imported ' + (ct.display_name || 'API') + ' (' + actionCount + ' actions)', 'success');
  closeModal('import-spec-modal');
  document.getElementById('import-spec-form').reset();
  resetImportPreview();
  location.reload();
}

async function importMcpServer(e) {
  if (e) e.preventDefault();
  const prefix = document.getElementById('directory-import-mcp-url') ? 'directory-' : '';
  const url = document.getElementById(prefix + 'import-mcp-url').value.trim();
  const displayName = document.getElementById(prefix + 'import-mcp-name').value.trim();
  const transportType = document.getElementById(prefix + 'import-mcp-transport').value || 'streamable_http';
  const timeoutMs = parseInt(document.getElementById(prefix + 'import-mcp-timeout').value || '60000', 10);
  const authHeader = document.getElementById(prefix + 'import-mcp-auth-header')?.value.trim();
  const authValue = document.getElementById(prefix + 'import-mcp-auth-value')?.value.trim();
  const description = document.getElementById(prefix + 'import-mcp-description')?.value.trim();

  if (!url) {
    showToast('Provide an MCP server URL', 'danger');
    return;
  }

  const body = { url, transport_type: transportType, timeout_ms: timeoutMs };
  if (displayName) body.display_name = displayName;
  if (description) body.description = description;
  if (authHeader && authValue) body.headers_json = JSON.stringify({[authHeader]: authValue});

  const j = await apiFetch('/api/connector-types/import-mcp', { method: 'POST', body: JSON.stringify(body) });
  if (!j.ok) {
    showToast(j.error?.message || 'MCP import failed', 'danger');
    return;
  }

  closeModal(prefix + 'import-mcp-modal');
  showToast('Imported ' + (j.data.connector_type?.display_name || 'MCP server') + ' (' + j.data.tool_count + ' tools)', 'success');
  document.getElementById(prefix + 'import-mcp-form').reset();
  location.reload();
}

function updateHttpAuthFields() {
  const authType = document.getElementById('http-auth-type').value;
  const headerGroup = document.getElementById('http-auth-header-group');
  const schemeGroup = document.getElementById('http-auth-scheme-group');
  const headerLabel = headerGroup.querySelector('label');
  const headerHint = headerGroup.querySelector('.form-hint');
  if (authType === 'none') {
    headerGroup.style.display = 'none';
    schemeGroup.style.display = 'none';
  } else if (authType === 'query') {
    headerGroup.style.display = '';
    schemeGroup.style.display = 'none';
    headerLabel.textContent = 'Query Parameter Name';
    headerHint.textContent = 'Leave blank to use api_key';
    document.getElementById('http-auth-header').placeholder = 'api_key';
  } else {
    headerGroup.style.display = '';
    schemeGroup.style.display = authType === 'bearer' ? '' : 'none';
    headerLabel.textContent = 'Auth Header Name';
    headerHint.textContent = 'Leave blank to use Authorization';
    document.getElementById('http-auth-header').placeholder = 'Authorization';
  }
}

async function addHttpConnector(e) {
  e.preventDefault();
  const displayName = document.getElementById('http-display-name').value.trim();
  const baseUrl = document.getElementById('http-base-url').value.trim();
  const authType = document.getElementById('http-auth-type').value;
  const authHeader = document.getElementById('http-auth-header').value.trim();
  const authScheme = document.getElementById('http-auth-scheme').value.trim();
  const headersJson = document.getElementById('http-extra-headers').value.trim();

  if (!displayName || !baseUrl) {
    showToast('Display name and base URL are required', 'danger');
    return;
  }

  const body = { display_name: displayName, base_url: baseUrl, auth_type: authType };
  if (authHeader) body.auth_header = authHeader;
  if (authScheme) body.auth_scheme = authScheme;
  if (headersJson) body.headers_json = headersJson;

  const j = await apiFetch('/api/connector-types/create-http', { method: 'POST', body: JSON.stringify(body) });
  if (!j.ok) {
    showToast(j.error?.message || 'Failed to create connector', 'danger');
    return;
  }

  closeModal('add-http-modal');
  showToast('Created ' + (j.data.connector_type?.display_name || displayName), 'success');
  document.getElementById('add-http-form').reset();
  updateHttpAuthFields();
  location.reload();
}

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

let actionsState = { ctId: null, offset: 0, all: [] };

async function viewActions(ctId, displayName, totalCount) {
  actionsState = { ctId: ctId, offset: 0, all: [] };
  const title = document.getElementById('view-actions-title');
  if (title) {
    title.dataset.baseTitle = displayName + ' \u2014 ' + totalCount + ' Actions';
    title.textContent = title.dataset.baseTitle;
  }
  document.getElementById('view-actions-filter').value = '';
  document.getElementById('view-actions-content').innerHTML = '<em>Loading...</em>';
  openModal('view-actions-modal');
  await loadActionsBatch(ctId, totalCount);
}

async function loadActionsBatch(ctId, totalCount) {
  const j = await apiFetch('/api/connector-types/' + ctId + '/tools?include_disabled=1&limit=1000');
  if (!j.ok) {
    document.getElementById('view-actions-content').innerHTML = '<em>Could not load actions</em>';
    return;
  }
  actionsState.all = j.data.tools || [];
  const enabledCount = actionsState.all.filter(function(t) { return t.enabled; }).length;
  const title = document.getElementById('view-actions-title');
  if (title) {
    const baseTitle = title.dataset.baseTitle || title.textContent;
    title.textContent = baseTitle + ' (' + enabledCount + ' enabled)';
  }
  renderActions();
}

function renderActions() {
  const filter = (document.getElementById('view-actions-filter').value || '').toLowerCase();
  const filtered = actionsState.all.filter(function(t) {
    if (!filter) return true;
    return t.name.toLowerCase().includes(filter) ||
           (t.description || '').toLowerCase().includes(filter) ||
           (t.path || '').toLowerCase().includes(filter);
  });
  const html = filtered.length ? (
    '<table style="width:100%">' +
      '<thead><tr><th style="width:72px">Enable</th><th>Action</th><th>Details</th></tr></thead>' +
      '<tbody>' + filtered.map(function(t) {
        return '<tr>' +
          '<td><label class="checkbox-label" style="margin:0"><input type="checkbox" ' +
          'data-action="' + encodeURIComponent(t.action) + '" ' +
          (t.enabled ? 'checked ' : '') +
          'onchange="toggleActionEnabled(decodeURIComponent(this.dataset.action), this.checked)"></label></td>' +
          '<td><strong style="font-size:0.9em">' + escapeHtml(t.name) + '</strong></td>' +
          '<td>' +
            (t.method ? '<span class="badge" style="font-size:0.75em;margin-right:6px">' + escapeHtml(t.method) + '</span>' : '') +
            (t.path ? '<code style="font-size:0.8em">' + escapeHtml(t.path) + '</code>' : '') +
            (t.auth_summary ? '<div style="font-size:0.8em;color:var(--muted);margin-top:4px">Auth: ' + escapeHtml(t.auth_summary) + '</div>' : '') +
            (t.description ? '<div style="font-size:0.85em;color:var(--muted);margin-top:4px">' + escapeHtml(t.description) + '</div>' : '') +
            (!t.enabled ? '<div class="text-muted" style="font-size:0.8em;margin-top:4px">Disabled</div>' : '') +
          '</td>' +
        '</tr>';
      }).join('') + '</tbody>' +
    '</table>'
  ) : '<em>No actions found</em>';
  document.getElementById('view-actions-content').innerHTML = html;
}

function filterActions() {
  renderActions();
}

function bulkSetActions(enabled) {
  const filter = (document.getElementById('view-actions-filter').value || '').toLowerCase();
  actionsState.all.forEach(function(t) {
    if (!filter || 
        t.name.toLowerCase().includes(filter) || 
        (t.description || '').toLowerCase().includes(filter) || 
        (t.path || '').toLowerCase().includes(filter)) {
      t.enabled = enabled;
    }
  });
  renderActions();
}

function toggleActionEnabled(actionId, enabled) {
  const item = actionsState.all.find(function(t) { return t.action === actionId; });
  if (item) {
    item.enabled = enabled;
  }
}

async function saveActionSettings() {
  if (!actionsState.ctId) return;
  const disabledActions = actionsState.all
    .filter(function(t) { return !t.enabled; })
    .map(function(t) { return t.action; });
  const j = await apiFetch('/api/connector-types/' + actionsState.ctId + '/actions', {
    method: 'PUT',
    body: JSON.stringify({ disabled_actions: disabledActions }),
  });
  if (j.ok) {
    showToast('Action settings saved', 'success');
    closeModal('view-actions-modal');
    location.reload();
  } else {
    showToast(j.error?.message || 'Failed to save actions', 'danger');
  }
}

window[window.AGENT_CORE_WINDOW_EVENT || "onAgentCoreEvent"] = function(event) {
  if (event.type !== 'connector_executed') return;
  var header = document.querySelector('#executions .section-header h3');
  if (!header || document.getElementById('executions-live-dot')) return;
  var dot = document.createElement('span');
  dot.id = 'executions-live-dot';
  dot.title = 'New execution recorded';
  dot.style.cssText = 'display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--accent,#4f8ef7);margin-left:8px;vertical-align:middle';
  header.appendChild(dot);
  setTimeout(function() {
    var el = document.getElementById('executions-live-dot');
    if (el) el.remove();
  }, 4000);
};

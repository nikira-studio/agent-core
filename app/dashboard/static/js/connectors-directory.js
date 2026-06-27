    let _dirPage = 1;
    let _dirSearch = '';
    let _dirCategory = '';

    async function loadDirectory(page) {
      if (page !== undefined) _dirPage = page;
      const grid = document.getElementById('directory-grid');
      grid.innerHTML = '<em>Loading...</em>';

      const params = new URLSearchParams({ page: _dirPage, limit: 30 });
      if (_dirSearch) params.set('q', _dirSearch);
      if (_dirCategory) params.set('category', _dirCategory);

      const j = await apiFetch('/api/connector-types/directory?' + params.toString());
      if (!j.ok) {
        grid.innerHTML = '<em>Could not load directory. Try again later.</em>';
        return;
      }
      const entries = j.data.entries || [];
      _dirEntriesCache = entries;
      const total = j.data.total || 0;

      const catSel = document.getElementById('dir-category');
      if (catSel.options.length <= 1 && j.data.categories) {
        j.data.categories.forEach(function(c) {
          const opt = document.createElement('option');
          opt.value = c;
          opt.textContent = c.charAt(0).toUpperCase() + c.slice(1);
          catSel.appendChild(opt);
        });
      }

      if (!entries.length) {
        grid.innerHTML = '<em>No APIs found matching your search.</em>';
        document.getElementById('directory-pagination').innerHTML = '';
        return;
      }

      const cards = entries.map(function(e) {
        const btn = e.variant_count > 1
          ? '<button type="button" class="btn btn-sm btn-primary" onclick="showDirectoryDetail(&apos;' + escapeHtml(e.id) + '&apos;)">View Variants</button>'
          : (e.installed
            ? '<button type="button" class="btn btn-sm btn-secondary" disabled>Already imported</button>'
            : '<button type="button" class="btn btn-sm btn-primary" onclick="importFromDirectory(&apos;' + escapeHtml(e.id) + '&apos;)">Import</button>');
        return '<div class="connector-type-card">' +
          '<div class="connector-type-head"><div>' +
          '<div class="connector-type-name"><a href="#" onclick="event.preventDefault();showDirectoryDetail(&apos;' + escapeHtml(e.id) + '&apos;)" style="color:var(--text);text-decoration:none">' + escapeHtml(e.display_name) + '</a></div>' +
          '<div class="connector-type-desc">' + escapeHtml((e.description || '').substring(0, 150)) + '</div>' +
          '</div></div>' +
          '<div class="connector-type-meta">' +
          '<span class="badge badge-stale">' + escapeHtml(e.category || '') + '</span> ' +
          (e.provider ? '<span class="badge badge-info">' + escapeHtml(e.provider) + '</span>' : '') +
          (e.variant_count > 1 ? ' <span class="badge badge-ok">' + e.variant_count + ' variants</span>' : '') +
          '</div>' +
          '<div class="connector-type-footer">' + btn + '</div>' +
          '</div>';
      }).join('');
      grid.innerHTML = cards;

      const totalPages = Math.ceil(total / 30);
      const pag = document.getElementById('directory-pagination');
      if (totalPages <= 1) {
        pag.innerHTML = '<span class="page-info">' + total + ' APIs</span>';
      } else {
        pag.innerHTML =
          '<button ' + (_dirPage <= 1 ? 'disabled' : '') + ' onclick="loadDirectory(' + (_dirPage - 1) + ')">Prev</button>' +
          '<span class="page-info">Page ' + _dirPage + ' of ' + totalPages + ' (' + total + ' APIs)</span>' +
          '<button ' + (_dirPage >= totalPages ? 'disabled' : '') + ' onclick="loadDirectory(' + (_dirPage + 1) + ')">Next</button>';
      }
    }

    let _dirSearchTimer = null;
    document.getElementById('dir-search').addEventListener('input', function(e) {
      _dirSearch = e.target.value.trim();
      clearTimeout(_dirSearchTimer);
      _dirSearchTimer = setTimeout(function() { loadDirectory(1); }, 300);
    });

    document.getElementById('dir-category').addEventListener('change', function(e) {
      _dirCategory = e.target.value;
      loadDirectory(1);
    });

    let _dirEntriesCache = [];

    async function showDirectoryDetail(entryId) {
      let entry = (_dirEntriesCache || []).find(function(e) { return e.id === entryId; });
      if (!entry) {
        const j = await apiFetch('/api/connector-types/directory?q=' + encodeURIComponent(entryId) + '&limit=100');
        if (j.ok) entry = (j.data.entries || []).find(function(e) { return e.id === entryId; });
      }
      if (!entry) { showToast('API not found', 'danger'); return; }

      const el = document.getElementById('dir-detail-content');
      const logo = entry.logo_url ? '<img src="' + escapeHtml(entry.logo_url) + '" style="max-height:40px;max-width:40px;border-radius:6px;margin-right:10px;vertical-align:middle" onerror="this.style.display=&apos;none&apos;">' : '';
      const desc = entry.description || 'No description available.';
      const cats = (entry.categories || [entry.category]).filter(Boolean).map(function(c) { return '<span class="badge badge-stale">' + escapeHtml(c) + '</span>'; }).join(' ');
      const provider = entry.provider ? '<span class="badge badge-info">' + escapeHtml(entry.provider) + '</span>' : '';
      const variants = entry.variant_count > 1 ? '<span class="badge badge-ok">' + entry.variant_count + ' variants (GHES, GHEC, etc.)</span>' : '';
      const variantRows = (entry.variants || []).map(function(v) {
        const installed = v.installed ? '<span class="badge badge-stale">Imported</span>' : '';
        const importBtn = v.installed
          ? '<button class="btn btn-sm btn-secondary" disabled>Already imported</button>'
          : '<button class="btn btn-sm btn-primary" onclick="startDirectoryImport(&apos;' + escapeHtml(v.id) + '&apos;, &apos;' + escapeHtml(v.spec_url) + '&apos;, &apos;' + escapeHtml(v.display_name) + '&apos;)">Import</button>';
        return '<tr>' +
          '<td><code>' + escapeHtml(v.id) + '</code></td>' +
          '<td>' + escapeHtml(v.version || '-') + '</td>' +
          '<td style="word-break:break-all">' + escapeHtml(v.spec_url || '-') + '</td>' +
          '<td>' + installed + '</td>' +
          '<td>' + importBtn + '</td>' +
        '</tr>';
      }).join('');

      el.innerHTML =
        '<div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">' +
          logo +
          '<h3 style="margin:0">' + escapeHtml(entry.display_name) + '</h3>' +
        '</div>' +
        '<div style="margin-bottom:10px">' + cats + ' ' + provider + ' ' + variants + '</div>' +
        '<div style="margin-bottom:10px;color:var(--muted);font-size:0.9em;white-space:pre-wrap;word-break:break-word;max-height:200px;overflow-y:auto">' + escapeHtml(desc) + '</div>' +
        '<table style="width:100%;font-size:0.85em">' +
          '<tr><td style="color:var(--muted);padding:4px 8px 4px 0;white-space:nowrap">Provider</td><td>' + escapeHtml(entry.provider || '-') + '</td></tr>' +
          '<tr><td style="color:var(--muted);padding:4px 8px 4px 0;white-space:nowrap">Version</td><td>' + escapeHtml(entry.version || '-') + '</td></tr>' +
          (entry.website ? '<tr><td style="color:var(--muted);padding:4px 8px 4px 0;white-space:nowrap">Website</td><td><a href="' + escapeHtml(entry.website) + '" target="_blank" rel="noopener">' + escapeHtml(entry.website) + '</a></td></tr>' : '') +
          (entry.origin_url ? '<tr><td style="color:var(--muted);padding:4px 8px 4px 0;white-space:nowrap">Spec source</td><td><a href="' + escapeHtml(entry.origin_url) + '" target="_blank" rel="noopener">' + escapeHtml(entry.origin_url.substring(0, 80)) + '</a></td></tr>' : '') +
          '<tr><td style="color:var(--muted);padding:4px 8px 4px 0;white-space:nowrap">Spec URL</td><td style="word-break:break-all">' + escapeHtml(entry.spec_url) + '</td></tr>' +
        '</table>';
      if (entry.variant_count > 1) {
        el.innerHTML +=
          '<h4 style="margin:16px 0 8px">Variants</h4>' +
          '<table style="width:100%;font-size:0.9em">' +
            '<thead><tr><th>Variant</th><th>Version</th><th>Spec URL</th><th>Status</th><th class="actions-cell">Actions</th></tr></thead>' +
            '<tbody>' + variantRows + '</tbody>' +
          '</table>';
      }

      const actions = document.getElementById('dir-detail-actions');
      if (entry.installed) {
        actions.innerHTML = '<button class="btn btn-secondary" disabled>Already imported</button> <button class="btn btn-secondary" onclick="closeModal(&apos;dir-detail-modal&apos;)">Close</button>';
      } else {
        actions.innerHTML = entry.variant_count > 1
          ? '<button class="btn btn-secondary" onclick="closeModal(&apos;dir-detail-modal&apos;)">Close</button>'
          : '<button class="btn" onclick="closeModal(&apos;dir-detail-modal&apos;);startDirectoryImport(&apos;' + escapeHtml(entry.id) + '&apos;, &apos;' + escapeHtml(entry.spec_url) + '&apos;, &apos;' + escapeHtml(entry.display_name) + '&apos;)">Import</button> <button class="btn btn-secondary" onclick="closeModal(&apos;dir-detail-modal&apos;)">Close</button>';
      }
      openModal('dir-detail-modal');
    }

    async function startDirectoryImport(entryId, specUrl, displayName) {
      document.getElementById('import-spec-url').value = specUrl || '';
      document.getElementById('import-spec-json').value = '';
      document.getElementById('import-spec-name').value = displayName || entryId || '';
      resetImportPreview();
      closeModal('dir-detail-modal');
      openModal('import-spec-modal');
    }

    async function importFromDirectory(entryId) {
      const params = new URLSearchParams({ page: _dirPage, limit: 30, q: _dirSearch, category: _dirCategory });
      const j = await apiFetch('/api/connector-types/directory?' + params.toString());
      if (!j.ok) { showToast('Failed to look up API', 'danger'); return; }
      const allPages = j.data.entries || [];
      let entry = allPages.find(function(e) { return e.id === entryId; });
      if (!entry) {
        const single = await apiFetch('/api/connector-types/directory?q=' + encodeURIComponent(entryId) + '&limit=100');
        if (single.ok) entry = (single.data.entries || []).find(function(e) { return e.id === entryId; });
      }
      if (!entry) { showToast('API not found', 'danger'); return; }

      startDirectoryImport(entry.id, entry.spec_url, entry.display_name);
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

      closeModal('import-spec-modal');
      showToast('Created ' + (j.data.connector_type?.display_name || 'spec') + ' (' + j.data.operation_count + ' actions)', 'success');
      document.getElementById('import-spec-url').value = '';
      document.getElementById('import-spec-json').value = '';
      document.getElementById('import-spec-name').value = '';
      resetImportPreview();
      loadDirectory();
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

      if (!url) {
        showToast('Provide an MCP server URL', 'danger');
        return;
      }

      const body = { url, transport_type: transportType, timeout_ms: timeoutMs };
      if (displayName) body.display_name = displayName;
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

    function escapeHtml(s) {
      if (!s) return '';
      return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }

    (typeof apiFetch !== 'undefined') ? loadDirectory() : document.addEventListener('DOMContentLoaded', function() { loadDirectory(); });

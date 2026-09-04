/* Agent Core Dashboard - JavaScript Utilities */

// ============================================================
// Theme Management
// ============================================================

(function() {
  const THEME_KEY = 'agent_core_theme';
  const THEMES = ['dark', 'light'];
  const STORAGE_KEY = THEME_KEY;

  function getPreferredTheme() {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored && THEMES.includes(stored)) return stored;
    if (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) {
      return 'light';
    }
    return 'dark';
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem(STORAGE_KEY, theme);
    updateThemeIcon(theme);
  }

  function toggleTheme() {
    const current = document.documentElement.getAttribute('data-theme') || 'dark';
    const next = current === 'dark' ? 'light' : 'dark';
    applyTheme(next);
  }

  function updateThemeIcon(theme) {
    const btn = document.querySelector('.theme-toggle');
    if (!btn) return;
    if (theme === 'dark') {
      btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>';
      btn.setAttribute('aria-label', 'Switch to light theme');
    } else {
      btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
      btn.setAttribute('aria-label', 'Switch to dark theme');
    }
  }

  function initTheme() {
    applyTheme(getPreferredTheme());
    document.addEventListener('DOMContentLoaded', function() {
      const btn = document.querySelector('.theme-toggle');
      if (btn) {
        btn.addEventListener('click', toggleTheme);
      }
    });
  }

  initTheme();
})();


// ============================================================
// Toast Notifications
// ============================================================

let toastContainer = null;

function getToastContainer() {
  if (!toastContainer) {
    toastContainer = document.createElement('div');
    toastContainer.className = 'toast-container';
    toastContainer.setAttribute('aria-live', 'polite');
    toastContainer.setAttribute('aria-relevant', 'additions');
    document.body.appendChild(toastContainer);
  }
  return toastContainer;
}

function showToast(message, type) {
  type = type || 'success';
  const container = getToastContainer();
  const toast = document.createElement('div');
  toast.className = 'toast ' + type;
  toast.setAttribute('role', type === 'danger' || type === 'error' ? 'alert' : 'status');
  toast.setAttribute('aria-atomic', 'true');
  toast.textContent = message;
  container.appendChild(toast);

  setTimeout(function() {
    toast.style.opacity = '0';
    toast.style.transform = 'translateX(100%)';
    setTimeout(function() {
      toast.remove();
      if (container.children.length === 0) {
        container.remove();
        toastContainer = null;
      }
    }, 300);
  }, 3000);
}


// ============================================================
// Modal Management
// ============================================================

var activeModalOverlay = null;
var modalReturnFocus = null;
var generatedFieldId = 0;

var FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])'
].join(',');

function visibleFocusableElements(root) {
  return Array.from(root.querySelectorAll(FOCUSABLE_SELECTOR)).filter(function(element) {
    return element.getClientRects().length > 0;
  });
}

function ensureElementId(element, prefix) {
  if (element.id) return element.id;
  do {
    generatedFieldId += 1;
    element.id = prefix + '-' + generatedFieldId;
  } while (document.getElementById(element.id) !== element);
  return element.id;
}

function elementsMatching(root, selector) {
  var matches = [];
  if (root.nodeType === Node.ELEMENT_NODE && root.matches(selector)) matches.push(root);
  return matches.concat(Array.from(root.querySelectorAll(selector)));
}

function associateFormLabels(root) {
  var container = root && root.querySelectorAll ? root : document;
  elementsMatching(container, '.form-group').forEach(function(group) {
    var label = Array.from(group.children).find(function(child) {
      return child.tagName === 'LABEL' && !child.classList.contains('checkbox-label');
    });
    if (!label) return;

    var controls = group.querySelectorAll('input:not([type="hidden"]), select, textarea');
    controls.forEach(function(control, index) {
      if ((control.labels && control.labels.length) ||
          control.hasAttribute('aria-label') ||
          control.hasAttribute('aria-labelledby')) return;

      var controlId = ensureElementId(control, 'dashboard-field');
      if (index === 0 && !label.htmlFor) {
        label.htmlFor = controlId;
      } else {
        control.setAttribute('aria-labelledby', ensureElementId(label, 'dashboard-label'));
      }
    });
  });
}

function prepareModal(overlay) {
  var dialog = overlay.querySelector('.modal') || overlay;
  dialog.setAttribute('role', 'dialog');
  dialog.setAttribute('aria-modal', 'true');
  if (!dialog.hasAttribute('aria-label') && !dialog.hasAttribute('aria-labelledby')) {
    var heading = dialog.querySelector('h1, h2, h3, h4');
    if (heading) {
      dialog.setAttribute('aria-labelledby', ensureElementId(heading, 'modal-title'));
    }
  }
  if (!dialog.hasAttribute('tabindex')) dialog.setAttribute('tabindex', '-1');
  associateFormLabels(overlay);
  return dialog;
}

function openModal(id) {
  var overlay = document.getElementById(id);
  if (overlay) {
    if (!activeModalOverlay) modalReturnFocus = document.activeElement;
    activeModalOverlay = overlay;
    var dialog = prepareModal(overlay);
    overlay.style.display = 'flex';
    overlay.classList.add('open');
    overlay.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
    var focusable = visibleFocusableElements(overlay);
    (focusable[0] || dialog).focus();
  }
}

function closeModal(id) {
  var overlay = document.getElementById(id);
  if (overlay) {
    overlay.classList.remove('open');
    overlay.style.display = 'none';
    overlay.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
    if (activeModalOverlay === overlay) {
      activeModalOverlay = null;
      if (modalReturnFocus && document.contains(modalReturnFocus)) modalReturnFocus.focus();
      modalReturnFocus = null;
    }
  }
}

function closeAllModals() {
  var modals = document.querySelectorAll('.modal-overlay.open');
  modals.forEach(function(m) {
    m.classList.remove('open');
    m.style.display = 'none';
    m.setAttribute('aria-hidden', 'true');
  });
  document.body.style.overflow = '';
  activeModalOverlay = null;
  if (modalReturnFocus && document.contains(modalReturnFocus)) modalReturnFocus.focus();
  modalReturnFocus = null;
}

// Close modal when clicking overlay
document.addEventListener('click', function(e) {
  if (e.target.classList.contains('modal-overlay')) {
    closeModal(e.target.id);
  }
});

// Keep keyboard focus inside the visible dialog and close it on Escape.
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    closeAllModals();
    return;
  }
  if (e.key !== 'Tab' || !activeModalOverlay) return;

  var focusable = visibleFocusableElements(activeModalOverlay);
  if (!focusable.length) {
    e.preventDefault();
    prepareModal(activeModalOverlay).focus();
    return;
  }

  var first = focusable[0];
  var last = focusable[focusable.length - 1];
  if (!activeModalOverlay.contains(document.activeElement)) {
    e.preventDefault();
    first.focus();
  } else if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
});

function enhanceDashboardAccessibility(root) {
  var container = root && root.querySelectorAll ? root : document;
  associateFormLabels(container);
  elementsMatching(container, '.modal-overlay').forEach(function(overlay) {
    prepareModal(overlay);
    if (!overlay.classList.contains('open')) overlay.setAttribute('aria-hidden', 'true');
  });
}

enhanceDashboardAccessibility(document);
new MutationObserver(function(mutations) {
  mutations.forEach(function(mutation) {
    mutation.addedNodes.forEach(function(node) {
      if (node.nodeType === Node.ELEMENT_NODE) enhanceDashboardAccessibility(node);
    });
  });
}).observe(document.body, {childList: true, subtree: true});


// ============================================================
// Clipboard Operations
// ============================================================

// Buttons that carry their text as data, so a secret never becomes part of an
// inline handler's source. `copyToClipboard` stays available for direct calls.
document.addEventListener('click', function(ev) {
  var btn = ev.target.closest('[data-copy-value]');
  if (!btn) return;
  ev.preventDefault();
  copyToClipboard(btn.dataset.copyValue, btn);
});

function copyToClipboard(text, button) {
  if (!button) return;
  text = String(text || '');
  var orig = button.textContent;
  function copied() {
    button.textContent = 'Copied!';
    button.classList.add('copied');
    setTimeout(function() {
      button.textContent = orig;
      button.classList.remove('copied');
    }, 1500);
  }
  function failed() {
    showToast('Failed to copy', 'danger');
  }
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(copied).catch(function() {
      fallbackCopyText(text) ? copied() : failed();
    });
  } else {
    fallbackCopyText(text) ? copied() : failed();
  }
}

function fallbackCopyText(text) {
  var textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.top = '-1000px';
  textarea.style.left = '-1000px';
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  var ok = false;
  try {
    ok = document.execCommand('copy');
  } catch (e) {
    ok = false;
  }
  textarea.remove();
  return ok;
}

function copyRef(refName, button) {
  copyToClipboard(refName, button);
}


// ============================================================
// API Fetch Helper
// ============================================================

async function apiFetch(url, options) {
  options = options || {};
  const opts = { ...options };
  if (!opts.headers) opts.headers = {};
  var headers = { 'Content-Type': 'application/json' };
  if (opts.headers) {
    Object.keys(opts.headers).forEach(function(k) {
      if (k !== 'Content-Type') headers[k] = opts.headers[k];
    });
  }
  Object.assign(opts, { headers: headers, credentials: 'same-origin' });
  var response = await fetch(url, opts);
  var data;
  try {
    data = await response.json();
  } catch (e) {
    data = { ok: false, error: { message: 'Invalid response' } };
  }
  if (!response.ok && !data.error) {
    data.error = { message: 'Request failed: ' + response.status };
  }
  return data;
}

function escapeHtml(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}


// ============================================================
// Form Helpers
// ============================================================

function clearFormErrors(form) {
  var errors = form.querySelectorAll('.form-error');
  errors.forEach(function(e) { e.style.display = 'none'; });
  var inputs = form.querySelectorAll('.error');
  inputs.forEach(function(i) { i.classList.remove('error'); });
}

function showFormError(form, fieldName, message) {
  var field = form.querySelector('[name="' + fieldName + '"]');
  if (field) {
    field.classList.add('error');
    var errorEl = field.parentNode.querySelector('.form-error');
    if (errorEl) {
      errorEl.textContent = message;
      errorEl.style.display = 'block';
    }
  }
}

function handleFormResponse(form, data) {
  if (data.ok) {
    showToast(data.data?.message || 'Success', 'success');
    return true;
  } else {
    var msg = data.error?.message || 'An error occurred';
    showToast(msg, 'error');
    return false;
  }
}


// ============================================================
// Confirmation Dialogs
// ============================================================

function confirmDelete(message) {
  message = message || 'Are you sure you want to delete this? This action cannot be undone.';
  return confirm(message);
}


// ============================================================
// Debounce Utility
// ============================================================

function debounce(func, wait) {
  var timeout;
  return function() {
    var args = arguments;
    clearTimeout(timeout);
    timeout = setTimeout(function() {
      func.apply(null, args);
    }, wait);
  };
}


// ============================================================
// URL Parameter Helpers
// ============================================================

function getUrlParam(name) {
  var params = new URLSearchParams(window.location.search);
  return params.get(name);
}

function setUrlParam(name, value) {
  var params = new URLSearchParams(window.location.search);
  if (value === null || value === undefined || value === '') {
    params.delete(name);
  } else {
    params.set(name, value);
  }
  var url = window.location.pathname + (params.toString() ? '?' + params.toString() : '');
  window.history.replaceState({}, '', url);
}


// ============================================================
// Relative Time Formatting
// ============================================================

function formatRelativeTime(dateString) {
  if (!dateString) return '';
  var date = new Date(dateString);
  var now = new Date();
  var diff = (now - date) / 1000;

  if (diff < 60) return 'just now';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  if (diff < 604800) return Math.floor(diff / 86400) + 'd ago';
  return date.toLocaleDateString();
}


// ============================================================
// Search Input Helper
// ============================================================

function initSearchInput(inputSelector, onSearch, debounceMs) {
  debounceMs = debounceMs || 300;
  var input = document.querySelector(inputSelector);
  if (!input) return;

  var searchFn = debounce(onSearch, debounceMs);
  input.addEventListener('input', searchFn);
  input.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') {
      e.preventDefault();
      searchFn();
    }
  });
}


// ============================================================
// Export/Import Helpers
// ============================================================

function downloadJson(data, filename) {
  var blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a');
  a.href = url;
  a.download = filename || 'export.json';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}


// ============================================================
// Accessibility Helpers
// ============================================================

function trapFocus(modal) {
  var focusable = modal.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
  if (focusable.length === 0) return;
  var first = focusable[0];
  var last = focusable[focusable.length - 1];

  modal.addEventListener('keydown', function(e) {
    if (e.key !== 'Tab') return;
    if (e.shiftKey) {
      if (document.activeElement === first) {
        e.preventDefault();
        last.focus();
      }
    } else {
      if (document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  });
}


// ============================================================
// Initialization
// ============================================================

document.addEventListener('DOMContentLoaded', function() {
  // Auto-dismiss alerts after 5 seconds
  var alerts = document.querySelectorAll('.alert[data-auto-dismiss]');
  alerts.forEach(function(alert) {
    setTimeout(function() {
      alert.style.opacity = '0';
      setTimeout(function() { alert.remove(); }, 300);
    }, 5000);
  });
});

// ============================================================
// Local time display
//
// All timestamps are stored and rendered in UTC. Each is emitted as
// <span class="local-dt" data-utc="<iso-utc>">UTC fallback</span>; here we
// convert every such element to the user's selected timezone (window.AC_USER_TZ),
// falling back to the browser zone. If the user has no saved zone yet, we detect
// the browser zone and persist it as their default.
// ============================================================

function acActiveTimezone() {
  if (window.AC_USER_TZ) return window.AC_USER_TZ;
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  } catch (e) {
    return 'UTC';
  }
}

function acFormatInstant(raw, dtStyle) {
  var date = new Date(raw);
  if (isNaN(date.getTime())) return null;
  var tz = acActiveTimezone();
  var opts;
  if (dtStyle === 'date') {
    opts = { timeZone: tz, year: 'numeric', month: 'short', day: 'numeric' };
  } else if (dtStyle === 'time') {
    opts = { timeZone: tz, hour: '2-digit', minute: '2-digit', timeZoneName: 'short' };
  } else {
    opts = { timeZone: tz, year: 'numeric', month: 'short', day: 'numeric',
             hour: '2-digit', minute: '2-digit', timeZoneName: 'short' };
  }
  try {
    return new Intl.DateTimeFormat(undefined, opts).format(date);
  } catch (e) {
    return null;
  }
}

function applyLocalTimes(root) {
  var scope = root || document;
  var nodes = scope.querySelectorAll
    ? scope.querySelectorAll('.local-dt[data-utc]:not([data-localized])')
    : [];
  nodes.forEach(function(node) {
    var raw = node.getAttribute('data-utc');
    if (!raw) return;
    var dtStyle = node.getAttribute('data-dt-style') || '';
    var formatted = acFormatInstant(raw, dtStyle);
    if (formatted) {
      node.textContent = formatted;
      node.setAttribute('title', raw);
      node.setAttribute('data-localized', '1');
    }
  });
}
window.applyLocalTimes = applyLocalTimes;

// Build markup for a client-rendered timestamp; the observer below converts it.
function localDt(utc, style) {
  if (!utc) return '—';
  var attr = style === 'date' ? ' data-dt-style="date"' : '';
  var safe = String(utc).replace(/"/g, '&quot;');
  return '<span class="local-dt" data-utc="' + safe + '"' + attr + '>' + safe + '</span>';
}
window.localDt = localDt;

// ============================================================
// Dropdown Menu (globally accessible for onclick handlers)
// ============================================================

function toggleDropdown(btn) {
  var menu = btn.nextElementSibling;
  if (!menu || !menu.classList.contains('dropdown-menu')) return;
  var isOpen = menu.style.display !== 'none';
  closeAllDropdowns();
  if (!isOpen) {
    menu.style.display = 'block';
  }
}
window.toggleDropdown = toggleDropdown;

function closeAllDropdowns() {
  var menus = document.querySelectorAll('.dropdown-menu');
  menus.forEach(function(m) { m.style.display = 'none'; });
}
window.closeAllDropdowns = closeAllDropdowns;

document.addEventListener('click', function(e) {
  if (!e.target.closest('.dropdown')) {
    closeAllDropdowns();
  }
});

document.addEventListener('DOMContentLoaded', function() {
  if (!window.AC_USER_TZ) {
    var detected = 'UTC';
    try {
      detected = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
    } catch (e) { detected = 'UTC'; }
    window.AC_USER_TZ = detected;
    // Persist the detected zone as the user's default; display works regardless.
    if (window.AC_AUTHENTICATED && typeof apiFetch === 'function') {
      apiFetch('/api/dashboard/user-settings', {
        method: 'POST',
        body: JSON.stringify({ timezone: detected }),
      }).catch(function() { /* non-blocking */ });
    }
  }
  applyLocalTimes(document);

  // Convert any timestamps inserted later by client-side rendering.
  if (window.MutationObserver) {
    var observer = new MutationObserver(function(mutations) {
      for (var i = 0; i < mutations.length; i++) {
        if (mutations[i].addedNodes && mutations[i].addedNodes.length) {
          applyLocalTimes(document);
          return;
        }
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }
});

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
    document.body.appendChild(toastContainer);
  }
  return toastContainer;
}

function showToast(message, type) {
  type = type || 'success';
  const container = getToastContainer();
  const toast = document.createElement('div');
  toast.className = 'toast ' + type;
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

function openModal(id) {
  var modal = document.getElementById(id);
  if (modal) {
    modal.style.display = 'flex';
    modal.classList.add('open');
    document.body.style.overflow = 'hidden';
  }
}

function closeModal(id) {
  var modal = document.getElementById(id);
  if (modal) {
    modal.classList.remove('open');
    modal.style.display = 'none';
    document.body.style.overflow = '';
  }
}

function closeAllModals() {
  var modals = document.querySelectorAll('.modal-overlay.open');
  modals.forEach(function(m) {
    m.classList.remove('open');
    m.style.display = 'none';
  });
  document.body.style.overflow = '';
}

// Close modal when clicking overlay
document.addEventListener('click', function(e) {
  if (e.target.classList.contains('modal-overlay')) {
    e.target.classList.remove('open');
    e.target.style.display = 'none';
    document.body.style.overflow = '';
  }
});

// Close modal on Escape key
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    closeAllModals();
  }
});


// ============================================================
// Clipboard Operations
// ============================================================

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
// Pagination Helpers
// ============================================================

function buildPagination(total, page, perPage, onPageChange) {
  var totalPages = Math.ceil(total / perPage);
  if (totalPages <= 1) return '';

  var html = '<div class="pagination">';
  if (page > 1) {
    html += '<button class="btn btn-sm" onclick="' + onPageChange + '(' + (page - 1) + ')">&laquo; Prev</button>';
  }
  html += '<span class="page-info">Page ' + page + ' of ' + totalPages + '</span>';
  if (page < totalPages) {
    html += '<button class="btn btn-sm" onclick="' + onPageChange + '(' + (page + 1) + ')">Next &raquo;</button>';
  }
  html += '</div>';
  return html;
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

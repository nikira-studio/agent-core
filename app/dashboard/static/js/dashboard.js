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
  toast.dataset.shown = String(Date.now());
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

// One dialog pattern for every pop-up: a fixed header (title and a close X), a body that scrolls, and a fixed
// footer with the actions. Existing markup (a heading, content, a .modal-footer or a trailing row of buttons) is
// reshaped once, the first time a dialog is prepared, so each pop-up does not need its own layout.
function buildModalParts(dialog, overlay) {
  if (dialog.dataset.modalReady) return;
  dialog.dataset.modalReady = '1';
  var heading = Array.from(dialog.children).find(function(c) { return /^H[1-4]$/.test(c.tagName); }) ||
    dialog.querySelector('h1, h2, h3, h4');
  var header = document.createElement('div');
  header.className = 'modal-header';
  var title = document.createElement('div');
  title.className = 'modal-title';
  if (heading) {
    ensureElementId(heading, 'modal-title');
    title.appendChild(heading);
  }
  header.appendChild(title);
  var x = document.createElement('button');
  x.type = 'button';
  x.className = 'modal-x';
  x.setAttribute('aria-label', 'Close');
  x.setAttribute('data-modal-close', '');
  x.innerHTML = '&times;';
  header.appendChild(x);

  // A trailing run of plain buttons (a lone Close, or a form's Create and Cancel) becomes the footer.
  function wrapTrailingButtons(container) {
    var kids = Array.from(container.children);
    var trailing = [];
    for (var k = kids.length - 1; k >= 0; k--) {
      var kid = kids[k];
      if (kid.tagName === 'BUTTON') trailing.unshift(kid); else break;
    }
    if (!trailing.length) return;
    var foot = document.createElement('div');
    foot.className = 'modal-footer';
    trailing[0].before(foot);
    trailing.forEach(function(b) { foot.appendChild(b); });
  }
  // A last row that holds only buttons (a hand-styled div) is the footer too.
  function markButtonRow(container) {
    var last = container.lastElementChild;
    if (last && last.tagName === 'DIV' && !last.classList.contains('modal-footer') && last.children.length &&
        Array.from(last.children).every(function(c) { return c.tagName === 'BUTTON' || c.tagName === 'A'; })) {
      last.className = 'modal-footer';
      last.removeAttribute('style');
    }
  }
  markButtonRow(dialog);
  dialog.querySelectorAll('form').forEach(markButtonRow);
  wrapTrailingButtons(dialog);
  dialog.querySelectorAll('form').forEach(wrapTrailingButtons);
  dialog.querySelectorAll('.modal-actions').forEach(function(a) { a.classList.add('modal-footer'); });
  // The main action sits on the right: secondary buttons (Cancel, Close) first, the rest after, in their own order.
  dialog.querySelectorAll('.modal-footer').forEach(function(foot) {
    var buttons = Array.from(foot.children).filter(function(c) { return c.tagName === 'BUTTON' || c.tagName === 'A'; });
    var secondary = buttons.filter(function(b) { return b.classList.contains('btn-secondary'); });
    var rest = buttons.filter(function(b) { return !b.classList.contains('btn-secondary'); });
    secondary.concat(rest).forEach(function(b) { foot.appendChild(b); });
  });

  var scroller = document.createElement('div');
  scroller.className = 'modal-scroll';
  while (dialog.firstChild) scroller.appendChild(dialog.firstChild);
  dialog.appendChild(header);
  dialog.appendChild(scroller);
}

function modalScroller(overlay) {
  return overlay.querySelector('.modal-scroll');
}

function prepareModal(overlay) {
  var dialog = overlay.querySelector('.modal') || overlay;
  buildModalParts(dialog, overlay);
  dialog.setAttribute('role', 'dialog');
  dialog.setAttribute('aria-modal', 'true');
  if (!dialog.hasAttribute('aria-label') && !dialog.hasAttribute('aria-labelledby')) {
    var heading = dialog.querySelector('.modal-title h1, .modal-title h2, .modal-title h3, .modal-title h4, h1, h2, h3, h4');
    if (heading) {
      dialog.setAttribute('aria-labelledby', ensureElementId(heading, 'modal-title'));
    }
  }
  if (!dialog.hasAttribute('tabindex')) dialog.setAttribute('tabindex', '-1');
  associateFormLabels(overlay);
  return dialog;
}

// Set a dialog's title (for a pop-up whose title is the thing it shows, such as a memory's topic).
function setModalTitle(id, text) {
  var overlay = document.getElementById(id);
  if (!overlay) return;
  prepareModal(overlay);
  var heading = overlay.querySelector('.modal-title h1, .modal-title h2, .modal-title h3, .modal-title h4');
  if (heading) heading.textContent = text;
}

function openModal(id) {
  var overlay = document.getElementById(id);
  if (overlay) {
    if (!activeModalOverlay) modalReturnFocus = document.activeElement;
    activeModalOverlay = overlay;
    var dialog = prepareModal(overlay);
    overlay.dataset.dirty = '';
    overlay.style.display = 'flex';
    overlay.classList.add('open');
    overlay.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
    // Always open at the top of the content, with focus inside: the first field of a form, otherwise the dialog
    // itself (so nothing scrolls it part-way down to reach a button).
    var scroller = modalScroller(overlay);
    if (scroller) scroller.scrollTop = 0;
    overlay.scrollTop = 0;
    var field = scroller && scroller.querySelector('input:not([type="hidden"]):not([disabled]), select:not([disabled]), textarea:not([disabled])');
    var target = field && field.getClientRects().length ? field : dialog;
    target.focus({ preventScroll: true });
    if (scroller) scroller.scrollTop = 0;
  }
}

function closeModal(id) {
  var overlay = document.getElementById(id);
  if (overlay) {
    overlay.classList.remove('open');
    overlay.style.display = 'none';
    overlay.setAttribute('aria-hidden', 'true');
    overlay.dataset.dirty = '';
    if (activeModalOverlay === overlay) {
      activeModalOverlay = null;
      document.body.style.overflow = '';
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
    m.dataset.dirty = '';
  });
  document.body.style.overflow = '';
  activeModalOverlay = null;
  if (modalReturnFocus && document.contains(modalReturnFocus)) modalReturnFocus.focus();
  modalReturnFocus = null;
}

// Typing into a form marks its dialog as having unsaved input; closing it by accident (a click outside, Escape)
// then asks first instead of discarding silently.
document.addEventListener('input', function(e) {
  var overlay = e.target.closest && e.target.closest('.modal-overlay');
  if (overlay && !overlay.hasAttribute('data-confirm-dialog')) overlay.dataset.dirty = '1';
});

// Close a dialog the way a person would by accident (backdrop, Escape): ask first when there is unsaved input.
async function requestCloseModal(overlay) {
  if (overlay.dataset.dirty === '1') {
    var ok = await confirmDialog('Discard what you typed?', { confirmLabel: 'Discard', cancelLabel: 'Keep editing', title: 'Discard what you typed?', plain: true });
    if (!ok) return;
  }
  closeModal(overlay.id);
}

// Close a dialog from its X, or by clicking the dimmed area around it.
document.addEventListener('click', function(e) {
  var x = e.target.closest && e.target.closest('[data-modal-close]');
  if (x) {
    var overlay = x.closest('.modal-overlay');
    if (overlay) { closeModal(overlay.id); return; }
  }
  if (e.target.classList.contains('modal-overlay') && !e.target.hasAttribute('data-confirm-dialog')) {
    requestCloseModal(e.target);
  }
});

// Keep keyboard focus inside the visible dialog and close it on Escape.
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    if (activeModalOverlay && !activeModalOverlay.hasAttribute('data-confirm-dialog')) {
      requestCloseModal(activeModalOverlay);
    } else if (!activeModalOverlay) {
      closeAllModals();
    }
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

// A confirmation in the same dialog as everything else, one step. Resolves true or false. The question is the text
// up to the first "?" and becomes the title (it names what will be changed); the rest is the explanation. A
// destructive question gets a red button labelled with its verb.
var CONFIRM_VERBS = ['Delete', 'Remove', 'Retract', 'Restore', 'Decline', 'Cancel', 'Deactivate', 'Reactivate', 'Reset',
  'Rotate', 'Generate', 'Permanently', 'Prune', 'Re-embed', 'Turn', 'Discard'];
var CONFIRM_DANGER = /^(delete|remove|retract|decline|cancel|deactivate|reset|rotate|permanently|prune|turn|discard)/i;
var confirmDialogQueue = Promise.resolve();

function confirmDialog(message, options) {
  options = options || {};
  var run = function() {
    return new Promise(function(resolve) {
      message = String(message || 'Are you sure?');
      var q = message.indexOf('?');
      var title = options.title || (q >= 0 ? message.slice(0, q + 1) : message);
      var detail = options.title ? message : (q >= 0 ? message.slice(q + 1).trim() : '');
      var first = (title.split(/\s+/)[0] || '').replace(/[^A-Za-z-]/g, '');
      var verb = options.confirmLabel || (CONFIRM_VERBS.indexOf(first.charAt(0).toUpperCase() + first.slice(1).toLowerCase()) >= 0
        ? (first === 'Permanently' ? 'Delete' : (first === 'Turn' ? 'Turn off' : first.charAt(0).toUpperCase() + first.slice(1).toLowerCase()))
        : 'Confirm');
      var danger = options.danger !== undefined ? options.danger : (!options.plain && CONFIRM_DANGER.test(first));
      var overlay = document.createElement('div');
      overlay.className = 'modal-overlay';
      overlay.id = 'confirm-dialog';
      overlay.setAttribute('data-confirm-dialog', '');
      overlay.style.display = 'none';
      var dialog = document.createElement('div');
      dialog.className = 'modal modal-confirm';
      var h = document.createElement('h2');
      h.textContent = title;
      dialog.appendChild(h);
      if (detail) {
        var p = document.createElement('p');
        p.className = 'confirm-detail';
        p.textContent = detail;
        dialog.appendChild(p);
      }
      var cancel = document.createElement('button');
      cancel.type = 'button';
      cancel.className = 'btn btn-secondary';
      cancel.textContent = options.cancelLabel || 'Cancel';
      var ok = document.createElement('button');
      ok.type = 'button';
      ok.className = 'btn ' + (danger ? 'btn-danger' : '');
      ok.textContent = verb;
      var field = null;
      if (options.input) {
        field = document.createElement('input');
        field.type = 'text';
        field.className = 'confirm-input';
        field.autocomplete = 'off';
        field.value = options.input.value || '';
        if (options.input.label) field.setAttribute('aria-label', options.input.label);
        if (options.input.placeholder) field.placeholder = options.input.placeholder;
        if (options.input.label) {
          var lab = document.createElement('label');
          lab.className = 'form-hint';
          lab.style.display = 'block';
          lab.textContent = options.input.label;
          dialog.appendChild(lab);
        }
        dialog.appendChild(field);
      }
      dialog.appendChild(cancel);
      dialog.appendChild(ok);
      overlay.appendChild(dialog);
      document.body.appendChild(overlay);
      var below = activeModalOverlay;       // a dialog this one is stacked on stays open underneath
      var opener = document.activeElement;
      var done = false;
      function finish(result) {
        if (done) return;
        done = true;
        document.removeEventListener('keydown', onKey, true);
        overlay.remove();
        activeModalOverlay = below;
        if (!below) document.body.style.overflow = '';
        if (opener && document.contains(opener)) opener.focus({ preventScroll: true });
        resolve(field ? (result ? field.value : null) : result);
      }
      function onKey(e) {
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finish(false); }
        else if (e.key === 'Tab') {
          var items = field ? [field, cancel, ok] : [cancel, ok];
          var i = items.indexOf(document.activeElement);
          e.preventDefault();
          items[(i + (e.shiftKey ? -1 : 1) + items.length) % items.length].focus();
        }
      }
      document.addEventListener('keydown', onKey, true);
      cancel.addEventListener('click', function() { finish(false); });
      if (field) field.addEventListener('keydown', function(e) { if (e.key === 'Enter') { e.preventDefault(); finish(true); } });
      ok.addEventListener('click', function() { finish(true); });
      overlay.addEventListener('click', function(e) { if (e.target === overlay) finish(false); });
      prepareModal(overlay);
      activeModalOverlay = overlay;
      overlay.style.display = 'flex';
      overlay.classList.add('open');
      document.body.style.overflow = 'hidden';
      // Focus the safe choice when the action is destructive, so a stray Enter does not delete.
      (field ? field : (danger ? cancel : ok)).focus({ preventScroll: true });
      if (field) field.select();
    });
  };
  confirmDialogQueue = confirmDialogQueue.then(run, run);
  return confirmDialogQueue;
}

// Ask for a line of text in the same dialog. Resolves the text, or null when cancelled.
function promptDialog(message, options) {
  options = options || {};
  return confirmDialog(message, Object.assign({ input: {}, plain: true }, options));
}

function confirmDelete(message) {
  return confirmDialog(message || 'Delete this? This cannot be undone.');
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


// Phone menu button: shows and hides the navigation.
document.addEventListener('click', function (ev) {
  var button = ev.target.closest && ev.target.closest('.nav-toggle');
  if (!button) return;
  var sidebar = button.closest('.sidebar');
  var open = sidebar.classList.toggle('nav-open');
  button.setAttribute('aria-expanded', open ? 'true' : 'false');
});


// A toast shown just before a page reload or navigation is shown again on the next page, so feedback is not lost.
window.addEventListener('beforeunload', function() {
  try {
    // Only toasts shown a moment ago, and not ones already carried over once.
    var shown = Array.prototype.filter.call(document.querySelectorAll('.toast-container .toast'), function(t) {
      return !t.dataset.replayed && Date.now() - Number(t.dataset.shown || 0) < 2500;
    }).map(function(t) {
      return { message: t.textContent, type: (t.className.replace('toast', '').trim() || 'success') };
    });
    if (shown.length) sessionStorage.setItem('ac-pending-toasts', JSON.stringify({ at: Date.now(), toasts: shown }));
  } catch (e) {}
});
document.addEventListener('DOMContentLoaded', function() {
  try {
    var raw = sessionStorage.getItem('ac-pending-toasts');
    if (!raw) return;
    sessionStorage.removeItem('ac-pending-toasts');
    var pending = JSON.parse(raw);
    if (!pending || Date.now() - pending.at > 5000) return;
    pending.toasts.forEach(function(t) {
      showToast(t.message, t.type);
      var all = document.querySelectorAll('.toast-container .toast');
      if (all.length) all[all.length - 1].dataset.replayed = '1';
    });
  } catch (e) {}
});

// "More" menus on rows: a floating menu anchored to its button. It leaves the row's layout alone, follows its button when the page scrolls and closes on an outside click,
// Escape, a choice or a resize, and the arrow keys move between its items.
(function() {
  function open() { return document.querySelector('details.row-more[open]'); }
  function place(details) {
    var summary = details.querySelector('summary'), list = details.querySelector('.row-more-list');
    if (!summary || !list) return;
    var s = summary.getBoundingClientRect();
    list.style.visibility = 'hidden';
    var w = list.offsetWidth, h = list.offsetHeight, gap = 4;
    var left = Math.min(Math.max(8, s.right - w), window.innerWidth - w - 8);
    var top = s.bottom + gap;
    if (top + h > window.innerHeight - 8 && s.top - gap - h >= 8) top = s.top - gap - h;
    list.style.left = left + 'px';
    list.style.top = top + 'px';
    list.style.visibility = '';
  }
  function close(details, refocus) {
    if (!details) return;
    details.removeAttribute('open');
    if (refocus) { var s = details.querySelector('summary'); if (s) s.focus(); }
  }
  document.addEventListener('toggle', function(e) {
    var d = e.target;
    if (!d.classList || !d.classList.contains('row-more') || !d.open) return;
    document.querySelectorAll('details.row-more[open]').forEach(function(o) { if (o !== d) close(o); });
    place(d);
  }, true);
  document.addEventListener('click', function(e) {
    var d = open();
    if (!d) return;
    if (!d.contains(e.target)) { close(d); return; }
    if (e.target.closest('.row-more-list a, .row-more-list button')) setTimeout(function() { close(d); }, 0);
  });
  document.addEventListener('keydown', function(e) {
    var d = open();
    if (!d) return;
    if (e.key === 'Escape') { e.preventDefault(); close(d, true); return; }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      var items = Array.prototype.slice.call(d.querySelectorAll('.row-more-list a, .row-more-list button')).filter(function(x) { return !x.disabled; });
      if (!items.length) return;
      e.preventDefault();
      var i = items.indexOf(document.activeElement);
      i = e.key === 'ArrowDown' ? (i + 1) % items.length : (i <= 0 ? items.length - 1 : i - 1);
      items[i].focus();
    }
  });
  window.addEventListener('resize', function() { close(open()); });
  window.addEventListener('scroll', function() {
    var d = open();
    if (!d) return;
    var r = d.querySelector('summary').getBoundingClientRect();
    if (r.bottom < 0 || r.top > window.innerHeight) close(d); else place(d);
  }, true);
})();

// A tab row too wide for a phone scrolls sideways: bring the current tab into view and fade the edge while more is hidden.
(function () {
  function setup(row) {
    function update() {
      var more = row.scrollWidth > row.clientWidth + 1;
      row.classList.toggle('scrolls', more);
      row.classList.toggle('at-end', !more || row.scrollLeft + row.clientWidth >= row.scrollWidth - 2);
    }
    var on = row.querySelector('a.active');
    if (on && row.scrollWidth > row.clientWidth) {
      row.scrollLeft = Math.max(0, on.offsetLeft - (row.clientWidth - on.offsetWidth) / 2);
    }
    row.addEventListener('scroll', update, { passive: true });
    window.addEventListener('resize', update);
    update();
  }
  document.querySelectorAll('.page-tabs').forEach(setup);
})();

// ============================================================
// Saved secrets: show that one exists (never its value)
// ============================================================

var SECRET_SAVED_PLACEHOLDER = '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022 saved; type to replace';

// Put a plain "A key is saved" (or "No key saved") line above a secret field and set its placeholder.
// `savedNoun` completes "A ___ is saved". `when` is an optional timestamp; only its date is shown.
function setSecretState(inputId, saved, savedNoun, noneText, when, noneHint) {
  var input = typeof inputId === 'string' ? document.getElementById(inputId) : inputId;
  if (!input) return;
  var state = input.parentNode.querySelector(':scope > .secret-state');
  if (!state) {
    state = document.createElement('div');
    state.className = 'secret-state';
    input.parentNode.insertBefore(state, input);
  }
  state.textContent = '';
  var badge = document.createElement('span');
  badge.className = 'badge ' + (saved ? 'badge-success' : 'badge-stale');
  badge.textContent = saved ? 'A ' + savedNoun + ' is saved' : noneText;
  state.appendChild(badge);
  if (saved && when) {
    var note = document.createElement('span');
    note.className = 'form-hint';
    note.innerHTML = ' saved ' + localDt(when, 'date');
    state.appendChild(note);
    if (typeof applyLocalTimes === 'function') applyLocalTimes(note);
  }
  input.placeholder = saved ? SECRET_SAVED_PLACEHOLDER : (noneHint || '');
}

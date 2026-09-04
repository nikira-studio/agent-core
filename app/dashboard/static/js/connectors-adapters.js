function filterAdapters() {
  const q = (document.getElementById('adapter-search').value || '').trim().toLowerCase();
  document.querySelectorAll('[data-adapter-card]').forEach(function(card) {
    const hay = (card.dataset.searchText || '').toLowerCase();
    card.style.display = !q || hay.includes(q) ? '' : 'none';
  });
}

document.getElementById('adapter-search').addEventListener('input', filterAdapters);


// Row actions carry their id as data. Interpolating it into an inline handler
// puts a value into JavaScript source, where HTML escaping does not protect it:
// entities are decoded before the handler is parsed.
document.addEventListener('click', function(ev) {
  const install = ev.target.closest('[data-adapter-install]');
  if (install) { ev.preventDefault(); installAdapter(install, install.dataset.adapterInstall); return; }
  const update = ev.target.closest('[data-adapter-update]');
  if (update) { ev.preventDefault(); updateAdapter(update, update.dataset.adapterUpdate); return; }
  const uninstall = ev.target.closest('[data-adapter-uninstall]');
  if (uninstall) { ev.preventDefault(); uninstallAdapter(uninstall, uninstall.dataset.adapterUninstall); return; }
});

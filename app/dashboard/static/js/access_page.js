// The Access page: every way something reaches something, when it was last used, and what nothing uses.
//
// Everything the server sends goes in through textContent, never as markup: an agent's name, a
// credential's name and a capability's name are typed by people, and they end up on an admin's screen.
//
// The answers come from GET /api/access/overview, the same list the Access API serves. Nothing here
// decides who may reach what; it only shows the answer and lets a person narrow it down.

(function () {
  'use strict';

  // The four ways something reaches something, in the order they are shown.
  var KIND_ORDER = ['capability_credential', 'agent_capability', 'agent_credential', 'service_credential'];

  // Where a name takes you: a credential, a capability, a service or an agent.
  var LINKS = {
    credential: '/credentials',
    connection: '/connectors',
    service: '/services',
    agent: '/agents',
  };

  // The three pickers and the kind of party each one lists.
  var PICKERS = [
    { id: 'access-filter-agent', type: 'agent' },
    { id: 'access-filter-capability', type: 'connection' },
    { id: 'access-filter-service', type: 'service' },
  ];

  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  // What kind of thing a group header names, in the order the groups are shown.
  var GROUP_ORDER = ['agent', 'service', 'connection'];
  var GROUP_KIND = { agent: 'agent', service: 'service', connection: 'connection' };

  // open: the groups a person opened or closed by hand (key -> true or false). Until then every group is closed,
  // unless a filter is on: then the groups that hold a match are open.
  var state = { rows: [], assignments: {}, unused: {}, kindOrder: KIND_ORDER.slice(), open: {} };

  // ── small helpers ───────────────────────────────────────────────────────────

  function el(tag, text, cls) {
    var node = document.createElement(tag);
    if (text !== null && text !== undefined) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  }

  function byId(id) {
    return document.getElementById(id);
  }

  function nameOf(party) {
    return party && party.name ? String(party.name) : '';
  }

  // A date the way a person writes it: 2 Nov 2026. The value is a UTC timestamp, so the day is the
  // one in UTC: the page never shifts a date to another timezone.
  function accessDate(value) {
    if (!value) return null;
    var parts = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value));
    if (!parts) return String(value);
    return Number(parts[3]) + ' ' + MONTHS[Number(parts[2]) - 1] + ' ' + parts[1];
  }

  function kindIndex(kind) {
    var index = state.kindOrder.indexOf(kind);
    return index < 0 ? state.kindOrder.length : index;
  }

  // ── the table ───────────────────────────────────────────────────────────────

  // The name of one party, linked to the page that shows it.
  function partyCell(party, label) {
    var cell = el('td');
    cell.setAttribute('data-label', label);
    var name = nameOf(party);
    if (!name) return cell;
    if (party.type && LINKS[party.type]) {
      var link = el('a', name);
      link.href = LINKS[party.type];
      cell.appendChild(link);
    } else {
      cell.textContent = name;
    }
    return cell;
  }

  // What the row says about itself: the reason, what it still waits for, and who owns it.
  function detailCell(row) {
    var cell = el('td');
    cell.setAttribute('data-label', 'Detail');
    if (row.detail) cell.appendChild(el('div', row.detail));
    if (row.status === 'needs_permission' && row.needs_label) {
      var chip = el('span', 'needs permission: ' + row.needs_label, 'badge badge-warning');
      chip.style.display = 'inline-block';
      chip.style.marginTop = '4px';
      var chipLine = el('div');
      chipLine.appendChild(chip);
      cell.appendChild(chipLine);
    }
    if (row.assignment === 'capability_credential' && row.owner) {
      cell.appendChild(el('div', 'owned by ' + row.owner, 'text-muted'));
    }
    if (row.approval && row.approval.length) {
      cell.appendChild(el('div', 'Waits for a person: ' + row.approval.join(', '), 'text-muted'));
    }
    return cell;
  }

  function buildRow(row) {
    var tr = el('tr');
    // Inside a group the header already names who or what, so this cell stays empty: the columns
    // still line up, and the name is not repeated on every row of the group.
    var who = el('td');
    who.setAttribute('data-label', 'Who or what');
    tr.appendChild(who);
    tr.appendChild(partyCell(row.to, 'Reaches'));
    var how = el('td', state.assignments[row.assignment] || '');
    how.setAttribute('data-label', 'How');
    tr.appendChild(how);
    tr.appendChild(detailCell(row));
    var used = accessDate(row.last_used);
    var when = el('td', used || 'never', used ? 'access-last-used' : 'access-last-used text-muted');
    when.setAttribute('data-label', 'Last used');
    tr.appendChild(when);
    return tr;
  }

  // Inside a group: the kind of assignment first, then the name of what it reaches.
  function sortedRows(rows) {
    return rows.slice().sort(function (a, b) {
      var left = kindIndex(a.assignment);
      var right = kindIndex(b.assignment);
      if (left !== right) return left - right;
      var to = nameOf(a.to).toLowerCase();
      var reach = nameOf(b.to).toLowerCase();
      if (to !== reach) return to < reach ? -1 : 1;
      return 0;
    });
  }

  // ── the groups: one header for each "Who or what" ───────────────────────────

  function groupKey(party) {
    return (party && party.type ? party.type : '') + ':' + (party && party.id ? party.id : nameOf(party));
  }

  function groupRank(type) {
    var index = GROUP_ORDER.indexOf(type);
    return index < 0 ? GROUP_ORDER.length : index;
  }

  // The rows a filter let through, split into one group for each party that reaches, in a fixed order.
  function groupRows(rows) {
    var groups = {};
    var list = [];
    rows.forEach(function (row) {
      var key = groupKey(row.from);
      if (!groups[key]) {
        groups[key] = { key: key, party: row.from || {}, rows: [] };
        list.push(groups[key]);
      }
      groups[key].rows.push(row);
    });
    list.sort(function (a, b) {
      var left = groupRank(a.party.type);
      var right = groupRank(b.party.type);
      if (left !== right) return left - right;
      var from = nameOf(a.party).toLowerCase();
      var other = nameOf(b.party).toLowerCase();
      if (from !== other) return from < other ? -1 : 1;
      return 0;
    });
    list.forEach(function (group) {
      group.rows = sortedRows(group.rows);
    });
    return list;
  }

  function filtersAreOn(filters) {
    return !!(filters.kind || filters.name || filters.agent || filters.capability || filters.service);
  }

  // Show or hide the rows under one header, and say so for anyone who cannot see the arrow.
  function setGroupOpen(head, open) {
    var button = head.querySelector('button');
    button.setAttribute('aria-expanded', open ? 'true' : 'false');
    head._rows.forEach(function (tr) {
      if (open) tr.classList.remove('is-closed');
      else tr.classList.add('is-closed');
    });
  }

  function groupHead(group, open) {
    var tr = el('tr', null, 'access-group-head');
    var cell = el('td');
    cell.colSpan = 5;
    var button = el('button', null, 'access-group-toggle');
    button.type = 'button';
    button.appendChild(el('span', '\u25B8', 'access-chevron'));
    button.lastChild.setAttribute('aria-hidden', 'true');
    button.appendChild(el('strong', nameOf(group.party) || 'Unnamed'));
    button.appendChild(el('span', GROUP_KIND[group.party.type] || '', 'text-muted'));
    var count = group.rows.length + (group.rows.length === 1 ? ' row' : ' rows');
    button.appendChild(el('span', count, 'text-muted access-group-count'));
    cell.appendChild(button);
    tr.appendChild(cell);
    tr._rows = [];
    function toggle() {
      var now = button.getAttribute('aria-expanded') === 'true';
      state.open[group.key] = !now;
      setGroupOpen(tr, !now);
    }
    button.addEventListener('click', function (ev) {
      // The row below is a control too, so the click must not toggle it a second time.
      ev.stopPropagation();
      toggle();
    });
    // Clicking anywhere on the header row opens or closes the group.
    tr.addEventListener('click', function (ev) {
      if (ev.target.closest && ev.target.closest('button')) return;
      toggle();
    });
    return tr;
  }

  // ── the filters ─────────────────────────────────────────────────────────────

  function currentFilters() {
    return {
      kind: byId('access-show').value,
      name: (byId('access-filter-name').value || '').trim().toLowerCase(),
      agent: byId('access-filter-agent').value,
      capability: byId('access-filter-capability').value,
      service: byId('access-filter-service').value,
    };
  }

  // Every filter has to pass. A chosen party keeps the row when it is on either side of it.
  function matches(row, filters) {
    if (filters.kind && row.assignment !== filters.kind) return false;
    if (filters.name) {
      var from = nameOf(row.from).toLowerCase();
      var to = nameOf(row.to).toLowerCase();
      if (from.indexOf(filters.name) < 0 && to.indexOf(filters.name) < 0) return false;
    }
    var picks = [
      { value: filters.agent, type: 'agent' },
      { value: filters.capability, type: 'connection' },
      { value: filters.service, type: 'service' },
    ];
    for (var i = 0; i < picks.length; i++) {
      var pick = picks[i];
      if (!pick.value) continue;
      var isFrom = row.from && row.from.type === pick.type && nameOf(row.from) === pick.value;
      var isTo = row.to && row.to.type === pick.type && nameOf(row.to) === pick.value;
      if (!isFrom && !isTo) return false;
    }
    return true;
  }

  // The names that occur in the rows, one list for each picker.
  function namesOfType(type) {
    var names = [];
    state.rows.forEach(function (row) {
      [row.from, row.to].forEach(function (party) {
        if (!party || party.type !== type) return;
        var name = nameOf(party);
        if (name && names.indexOf(name) < 0) names.push(name);
      });
    });
    names.sort(function (a, b) {
      var left = a.toLowerCase();
      var right = b.toLowerCase();
      if (left === right) return 0;
      return left < right ? -1 : 1;
    });
    return names;
  }

  function fillPickers() {
    PICKERS.forEach(function (picker) {
      var select = byId(picker.id);
      namesOfType(picker.type).forEach(function (name) {
        var option = document.createElement('option');
        option.value = name;
        option.textContent = name;
        select.appendChild(option);
      });
    });
  }

  // The one option per assignment, worded the way the server words it.
  function fillKinds() {
    var select = byId('access-show');
    var kinds = Object.keys(state.assignments);
    state.kindOrder = kinds.length ? kinds.slice() : KIND_ORDER.slice();
    KIND_ORDER.forEach(function (kind) {
      if (state.kindOrder.indexOf(kind) < 0) state.kindOrder.push(kind);
    });
    kinds.forEach(function (kind) {
      var option = document.createElement('option');
      option.value = kind;
      option.textContent = state.assignments[kind];
      select.appendChild(option);
    });
  }

  function renderRows() {
    var filters = currentFilters();
    var rows = state.rows.filter(function (row) {
      return matches(row, filters);
    });
    var groups = groupRows(rows);
    var searching = filtersAreOn(filters);

    var body = byId('access-rows');
    body.textContent = '';
    groups.forEach(function (group) {
      // A group is closed until a person opens it; a filter opens the groups that hold a match.
      var open = state.open[group.key] !== undefined ? state.open[group.key] : searching;
      var head = groupHead(group, open);
      body.appendChild(head);
      group.rows.forEach(function (row) {
        var tr = buildRow(row);
        tr.classList.add('access-group-row');
        head._rows.push(tr);
        body.appendChild(tr);
      });
      setGroupOpen(head, open);
    });

    var count = byId('access-row-count');
    count.textContent = rows.length + (rows.length === 1 ? ' row' : ' rows')
      + ' in ' + groups.length + (groups.length === 1 ? ' group' : ' groups');

    var empty = byId('access-empty');
    var none = byId('access-no-rows');
    if (!state.rows.length) {
      empty.style.display = 'none';
      none.style.display = 'block';
    } else if (!rows.length) {
      empty.style.display = 'block';
      none.style.display = 'none';
    } else {
      empty.style.display = 'none';
      none.style.display = 'none';
    }
  }

  // ── what nothing uses ───────────────────────────────────────────────────────

  function unusedItem(name, href, lines) {
    var item = el('li');
    if (href) {
      var link = el('a', name);
      link.href = href;
      item.appendChild(link);
    } else {
      item.appendChild(el('span', name));
    }
    lines.forEach(function (line) {
      item.appendChild(el('div', line, 'text-muted'));
    });
    return item;
  }

  function renderUnused() {
    var unused = state.unused || {};
    var groups = [
      { id: 'access-unused-credentials', items: unused.credentials || [], href: '/credentials' },
      { id: 'access-unused-connections', items: unused.connections || [], href: '/connectors' },
      { id: 'access-unused-services', items: unused.services || [], href: '/services' },
      { id: 'access-unused-left-behind', items: unused.left_behind || [], href: '/agents' },
    ];

    var any = false;
    groups.forEach(function (group) {
      var container = byId(group.id);
      var list = byId(group.id + '-list');
      var counter = byId(group.id + '-count');
      list.textContent = '';
      if (!group.items.length) {
        container.style.display = 'none';
        return;
      }
      any = true;
      container.style.display = 'block';
      counter.textContent = '(' + group.items.length + ')';
      group.items.forEach(function (item) {
        // The name, then the one line the server writes about it: nothing else.
        var lines = item.why ? [item.why] : [];
        list.appendChild(unusedItem(item.name || item.id, group.href, lines));
      });
    });
    byId('access-nothing-unused').style.display = any ? 'none' : 'block';
  }

  // ── loading ─────────────────────────────────────────────────────────────────

  async function loadAccess() {
    var j = await apiFetch('/api/access/overview');
    if (!j.ok) {
      // The table and the unused list stay hidden; both cards say why instead of looking empty.
      var failure = (j.error && j.error.message) || 'The access list could not be loaded';
      byId('access-loading').textContent = failure;
      byId('access-unused-loading').textContent = failure;
      return;
    }
    byId('access-loading').style.display = 'none';
    byId('access-unused-loading').style.display = 'none';
    byId('access-table-wrap').style.display = '';
    var data = j.data || {};
    state.rows = data.rows || [];
    state.assignments = data.assignments || {};
    state.unused = data.unused || {};
    fillKinds();
    fillPickers();
    renderRows();
    renderUnused();
  }

  // A new filter starts over: the groups that hold a match open by themselves, and nothing a person closed hides it.
  function filtersChanged() {
    state.open = {};
    renderRows();
  }

  function start() {
    byId('access-show').addEventListener('change', filtersChanged);
    byId('access-filter-name').addEventListener('input', filtersChanged);
    PICKERS.forEach(function (picker) {
      byId(picker.id).addEventListener('change', filtersChanged);
    });
    loadAccess();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();

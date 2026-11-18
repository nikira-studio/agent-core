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

  var state = { rows: [], assignments: {}, unused: {}, kindOrder: KIND_ORDER.slice() };

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
  function partyCell(party) {
    var cell = el('td');
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
    tr.appendChild(partyCell(row.from));
    tr.appendChild(partyCell(row.to));
    tr.appendChild(el('td', state.assignments[row.assignment] || ''));
    tr.appendChild(detailCell(row));
    var used = accessDate(row.last_used);
    var when = el('td', used || 'never seen', used ? '' : 'text-muted');
    tr.appendChild(when);
    return tr;
  }

  // Assignment kind first, then the name of the party that reaches.
  function sortedRows(rows) {
    return rows.slice().sort(function (a, b) {
      var left = kindIndex(a.assignment);
      var right = kindIndex(b.assignment);
      if (left !== right) return left - right;
      var from = nameOf(a.from).toLowerCase();
      var other = nameOf(b.from).toLowerCase();
      if (from !== other) return from < other ? -1 : 1;
      var to = nameOf(a.to).toLowerCase();
      var reach = nameOf(b.to).toLowerCase();
      if (to !== reach) return to < reach ? -1 : 1;
      return 0;
    });
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
    var rows = sortedRows(state.rows.filter(function (row) {
      return matches(row, filters);
    }));

    var body = byId('access-rows');
    body.textContent = '';
    rows.forEach(function (row) {
      body.appendChild(buildRow(row));
    });

    var count = byId('access-row-count');
    count.textContent = rows.length + (rows.length === 1 ? ' row' : ' rows');

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
      {
        id: 'access-unused-credentials',
        items: unused.credentials || [],
        href: '/credentials',
        extra: function (item) {
          return item.owner ? 'owned by ' + item.owner : '';
        },
      },
      {
        id: 'access-unused-connections',
        items: unused.connections || [],
        href: '/connectors',
        extra: function () {
          return '';
        },
      },
      {
        id: 'access-unused-services',
        items: unused.services || [],
        href: '/services',
        extra: function (item) {
          var last = accessDate(item.last_signed_in);
          return last ? 'last signed in ' + last : 'never';
        },
      },
      {
        id: 'access-unused-left-behind',
        items: unused.left_behind || [],
        href: '/agents',
        extra: function () {
          return '';
        },
      },
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
        var lines = [];
        var extra = group.extra(item);
        if (extra) lines.push(extra);
        if (item.why) lines.push(item.why);
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

  function start() {
    byId('access-show').addEventListener('change', renderRows);
    byId('access-filter-name').addEventListener('input', renderRows);
    PICKERS.forEach(function (picker) {
      byId(picker.id).addEventListener('change', renderRows);
    });
    loadAccess();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();

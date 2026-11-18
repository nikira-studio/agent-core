"""The Access page: every way something reaches something, when it was last used, and what nothing uses.

Admin only, like the other pages about access. The page carries no data of its own: the browser asks
for the overview (``GET /api/access/overview``) and builds the table and the Unused list from the
answer, so the page and the API can never disagree about what reaches what.

Everything a person typed - an agent's name, a credential's name, a capability's name - is put on the
page with textContent, never as markup. See ``app/dashboard/static/js/access_page.js``.
"""

from fastapi import APIRouter, Depends, Request

from app.routes.dashboard_shared import render_page, require_auth

router = APIRouter()


@router.get("/access")
def access_page(request: Request, session: dict = Depends(require_auth)):
    if session.get("role") != "admin":
        return render_page(
            "Admin Required",
            """
    <div class="page-header"><h1>Admin Access Required</h1></div>
    <div class="card">
      <p class="text-muted">Access is managed by administrators.</p>
      <a href="/" class="btn btn-secondary">Back to Needs you</a>
    </div>
    """,
            "/",
            session=session,
            status_code=403,
        )

    body = """
<div class="page-header"><h1>Access</h1></div>
<p class="text-muted" style="max-width:760px;margin-top:8px">Every way a credential, a capability or an agent reaches something, and when it was last used.</p>
<div class="card">
  <h2>Who reaches what</h2>
  <div class="filter-bar">
    <div class="form-group" style="margin:0">
      <label for="access-show">Show</label>
      <select id="access-show"><option value="">All</option></select>
    </div>
    <div class="form-group" style="margin:0">
      <label for="access-filter-name">Filter by name</label>
      <input type="text" id="access-filter-name" class="search-input" placeholder="Filter by name" autocomplete="off">
    </div>
    <div class="form-group" style="margin:0">
      <label for="access-filter-agent">Agent</label>
      <select id="access-filter-agent"><option value="">All</option></select>
    </div>
    <div class="form-group" style="margin:0">
      <label for="access-filter-capability">Capability</label>
      <select id="access-filter-capability"><option value="">All</option></select>
    </div>
    <div class="form-group" style="margin:0">
      <label for="access-filter-service">Service</label>
      <select id="access-filter-service"><option value="">All</option></select>
    </div>
  </div>
  <p class="text-muted" id="access-loading" role="status">Loading...</p>
  <p class="text-muted" id="access-row-count" style="font-size:0.85rem;margin:0 0 8px"></p>
  <div style="overflow-x:auto;display:none" id="access-table-wrap">
  <table>
    <thead><tr>
      <th>Who or what</th>
      <th>Reaches</th>
      <th>How</th>
      <th>Detail</th>
      <th>Last used</th>
    </tr></thead>
    <tbody id="access-rows"></tbody>
  </table>
  </div>
  <p class="empty" id="access-empty" style="display:none">Nothing matches these filters.</p>
  <p class="empty" id="access-no-rows" style="display:none">No access is set up yet. Start with a credential on the Credentials tab.</p>
</div>
<div class="card">
  <h2>Unused</h2>
  <p class="text-muted" style="font-size:0.85rem">Things nothing uses. Check each before you delete it.</p>
  <p class="text-muted" id="access-unused-loading" role="status">Loading...</p>
  <div id="access-unused-credentials" style="display:none">
    <h3>Credentials nothing uses <span class="text-muted" id="access-unused-credentials-count"></span></h3>
    <ul style="margin:0 0 12px;padding-left:18px" id="access-unused-credentials-list"></ul>
  </div>
  <div id="access-unused-connections" style="display:none">
    <h3>Capabilities nobody can use <span class="text-muted" id="access-unused-connections-count"></span></h3>
    <ul style="margin:0 0 12px;padding-left:18px" id="access-unused-connections-list"></ul>
  </div>
  <div id="access-unused-services" style="display:none">
    <h3>Services that look idle <span class="text-muted" id="access-unused-services-count"></span></h3>
    <ul style="margin:0 0 12px;padding-left:18px" id="access-unused-services-list"></ul>
  </div>
  <div id="access-unused-left-behind" style="display:none">
    <h3>Left behind by a retired agent or service <span class="text-muted" id="access-unused-left-behind-count"></span></h3>
    <ul style="margin:0 0 12px;padding-left:18px" id="access-unused-left-behind-list"></ul>
  </div>
  <p class="empty" id="access-nothing-unused" style="display:none">Nothing is unused.</p>
</div>
"""

    js = '<script src="/static/js/access_page.js?v=20261118a"></script>'
    return render_page("Access", body, "/access", js, session=session)

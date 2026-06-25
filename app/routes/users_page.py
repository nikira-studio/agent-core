"""Users dashboard page (admin-only). Split from dashboard.py — see
private/dashboard-split-plan.md."""

import json

from fastapi import APIRouter, Request, Depends

from app.routes.dashboard_shared import (
    render_page,
    require_auth,
    escape_html,
    local_dt,
    get_icon,
)

router = APIRouter()


@router.get("/users")
async def users_page(request: Request, session: dict = Depends(require_auth)):
    from app.services.auth_service import list_users

    if session.get("role") != "admin":
        return render_page(
            "Admin Required",
            """
    <div class="page-header"><h1>Admin Access Required</h1></div>
    <div class="card">
      <p class="text-muted">Users are managed by administrators.</p>
      <a href="/" class="btn btn-secondary">Back to Overview</a>
    </div>
    """,
            "/",
            session=session,
            status_code=403,
        )

    users = list_users()
    current_user_id = session["user_id"]

    def user_row(u):
        otp = (
            "<span class='badge badge-active'>enrolled</span>"
            if u.get("otp_enrolled")
            else "<span class='badge badge-inactive'>none</span>"
        )
        is_self = u["id"] == current_user_id
        user_payload = escape_html(
            json.dumps(
                {
                    "id": u["id"],
                    "email": u.get("email", ""),
                    "display_name": u.get("display_name", ""),
                    "role": u.get("role", "user"),
                }
            )
        )
        delete_action = (
            "<span class='text-muted' style='font-size:0.8rem'>current session</span>"
            if is_self
            else f"<button type='button' class='btn btn-sm btn-danger icon-delete-btn' onclick=\"deleteUser('{u['id']}', '{escape_html(u['display_name'])}')\" title='Delete user' aria-label='Delete user'>{get_icon('delete')}</button>"
        )
        actions = f"<div class='actions-cell'><button type='button' class='btn btn-sm btn-secondary' data-user='{user_payload}' onclick=\"editUser(this)\">Edit</button>{delete_action}</div>"
        return (
            f"<tr>"
            f"<td>{escape_html(u.get('display_name', ''))}</td>"
            f"<td><code>{u['id']}</code></td>"
            f"<td>{escape_html(u.get('email', ''))}</td>"
            f"<td><span class='badge badge-{'active' if u.get('role') == 'admin' else 'inactive'}'>{u.get('role', 'user')}</span></td>"
            f"<td>{otp}</td>"
            f"<td>{local_dt(u.get('created_at'), style='date')}</td>"
            f"<td>{actions}</td>"
            f"</tr>"
        )

    rows = (
        "".join(user_row(u) for u in users)
        or "<tr><td colspan=7 class=empty>No users.</td></tr>"
    )

    js = """
    <script>
    async function createUser(e) {
      e.preventDefault();
      const body = Object.fromEntries(new FormData(e.target));
      const j = await apiFetch('/api/auth/users', { method: 'POST', body: JSON.stringify(body) });
      if (j.ok) {
        showToast('User created');
        closeModal('create-user-modal');
        location.reload();
      } else { showToast(j.error?.message || 'Failed', 'danger'); }
    }
    function editUser(btn) {
      const u = JSON.parse(btn.getAttribute('data-user'));
      document.getElementById('eu-id').value = u.id;
      document.getElementById('eu-display-name').value = u.display_name || '';
      document.getElementById('eu-email').value = u.email || '';
      document.getElementById('eu-role').value = u.role || 'user';
      document.getElementById('eu-password').value = '';
      openModal('edit-user-modal');
    }
    async function submitEditUser(e) {
      e.preventDefault();
      const id = document.getElementById('eu-id').value;
      const body = {
        display_name: document.getElementById('eu-display-name').value,
        email: document.getElementById('eu-email').value,
        role: document.getElementById('eu-role').value,
      };
      const password = document.getElementById('eu-password').value;
      if (password) body.password = password;
      const j = await apiFetch('/api/auth/users/' + id, { method: 'PUT', body: JSON.stringify(body) });
      if (j.ok) {
        showToast('User updated');
        closeModal('edit-user-modal');
        location.reload();
      } else { showToast(j.error?.message || 'Failed', 'danger'); }
    }
    async function deleteUser(id, name) {
      if (!confirm('Delete user "' + name + '"? This cannot be undone.')) return;
      const j = await apiFetch('/api/auth/users/' + id, { method: 'DELETE' });
      if (j.ok) { showToast('User deleted'); location.reload(); }
      else { showToast(j.error?.message || 'Failed', 'danger'); }
    }
    </script>"""

    return render_page(
        "Users",
        f"""
    <div class="page-header"><h1>Users</h1><div class="page-actions">
      <button class="btn" onclick="openModal('create-user-modal')">+ Add User</button>
    </div></div>
    <div class="card">
      <h3>All Users</h3>
      <p class="text-muted" style="margin-bottom:12px">Admin-only view. First-run registration creates the initial admin; after that, admins create users here and assign roles.</p>
      <table><thead><tr><th>Name</th><th>ID</th><th>Email</th><th>Role</th><th>OTP</th><th>Created</th><th class="actions-cell">Actions</th></tr></thead>
      <tbody>{rows}</tbody></table>
    </div>
    <div class="modal-overlay" id="create-user-modal" style="display:none">
      <div class="modal">
        <h3>Add User</h3>
        <form id="create-user-form" onsubmit="createUser(event)">
          <div class="form-group"><label>Display Name</label><input type="text" name="display_name" autocomplete="name" required></div>
          <div class="form-group"><label>Email</label><input type="email" name="email" autocomplete="email" required></div>
          <div class="form-row">
            <div class="form-group"><label>Role</label><select name="role"><option value="user">User</option><option value="admin">Admin</option></select></div>
            <div class="form-group"><label>Initial Password</label><input type="password" name="password" minlength="8" autocomplete="new-password" required></div>
          </div>
          <p class="form-hint">Users can change their password after signing in. Admin role grants full dashboard access.</p>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('create-user-modal')">Cancel</button>
            <button type="submit" class="btn">Create</button>
          </div>
        </form>
      </div>
    </div>
    <div class="modal-overlay" id="edit-user-modal" style="display:none">
      <div class="modal">
        <h3>Edit User</h3>
        <form id="edit-user-form" onsubmit="submitEditUser(event)">
          <input type="hidden" id="eu-id">
          <div class="form-group"><label>Display Name</label><input type="text" id="eu-display-name" autocomplete="name" required></div>
          <div class="form-group"><label>Email</label><input type="email" id="eu-email" autocomplete="email" required></div>
          <div class="form-row">
            <div class="form-group"><label>Role</label><select id="eu-role"><option value="user">User</option><option value="admin">Admin</option></select></div>
            <div class="form-group"><label>New Password</label><input type="password" id="eu-password" minlength="8" autocomplete="new-password" placeholder="Leave unchanged"></div>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('edit-user-modal')">Cancel</button>
            <button type="submit" class="btn">Save</button>
          </div>
        </form>
      </div>
    </div>
    """,
        "/users",
        js,
        session=session,
    )

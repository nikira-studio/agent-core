"""Login / OTP / logout dashboard pages. Split from dashboard.py — see
private/dashboard-split-plan.md."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.services.auth_service import count_users
from app.routes.dashboard_shared import render_page

router = APIRouter()


@router.get("/login")
async def login_page(request: Request):
    user_count = count_users()
    if user_count == 0:
        return render_page(
            "Setup",
            """
    <div class="card">
      <h3>Welcome to {APP_NAME}</h3>
      <p class="text-muted" style="margin-bottom:20px">Create your admin account to get started.</p>
      <form id="setup-form" onsubmit="submitSetup(event)">
        <div class="form-group">
          <label>Email</label>
          <input type="email" name="email" autocomplete="email" required>
        </div>
        <div class="form-group">
          <label>Display Name</label>
          <input type="text" name="display_name" autocomplete="name" required>
        </div>
        <div class="form-group">
          <label>Password</label>
          <input type="password" name="password" minlength="8" autocomplete="new-password" required>
        </div>
        <button type="submit" class="btn">Create Account</button>
      </form>
    </div>
    <script>
    async function submitSetup(e) {
      e.preventDefault();
      const fd = new FormData(e.target);
      const r = await fetch('/api/auth/register', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(Object.fromEntries(fd))});
      const j = await r.json();
      if (j.ok) { showToast('Account created. Set up two-factor authentication next.', 'success'); window.location.href = '/settings/otp?first_run=1'; }
      else { showToast(j.error.message || 'Error', 'danger'); }
    }
    </script>
    """,
            "",
            show_sidebar=False,
        )
    return render_page(
        "Login",
        """
    <div class="card" style="max-width:400px;margin:60px auto">
      <h3>Sign In</h3>
      <form id="login-form" onsubmit="submitLogin(event)">
        <div class="form-group">
          <label>Email</label>
          <input type="email" name="email" autocomplete="email" required>
        </div>
        <div class="form-group">
          <label>Password</label>
          <input type="password" name="password" autocomplete="current-password" required>
        </div>
        <button type="submit" class="btn">Login</button>
      </form>
    </div>
    <script>
    async function submitLogin(e) {
      e.preventDefault();
      const fd = new FormData(e.target);
      const r = await fetch('/api/auth/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(Object.fromEntries(fd))});
      const j = await r.json();
      if (j.ok) {
        if (j.data.requires_otp) {
          sessionStorage.setItem('temp_session_id', j.data.session_id);
          window.location.href = '/otp';
        } else {
          window.location.href = '/';
        }
      } else { showToast(j.error.message || 'Login failed', 'danger'); }
    }
    </script>
    """,
        "",
        show_sidebar=False,
    )


@router.get("/otp")
async def otp_page(request: Request):
    return render_page(
        "OTP Verification",
        """
    <div class="card" style="max-width:400px;margin:60px auto">
      <h3>Two-Factor Authentication</h3>
      <p class="text-muted" style="margin-bottom:16px">Enter the 6-digit code from your authenticator app.</p>
      <form id="otp-form" onsubmit="submitOtp(event)">
        <div class="form-group">
          <input type="text" name="otp_code" placeholder="123456" autocomplete="one-time-code" style="width:260px;font-size:1rem;text-align:center">
        </div>
        <button type="submit" class="btn">Verify</button>
      </form>
    </div>
    <script>
    async function submitOtp(e) {
      e.preventDefault();
      const fd = new FormData(e.target);
      fd.set('session_id', sessionStorage.getItem('temp_session_id') || '');
      const r = await fetch('/api/auth/otp/verify', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(Object.fromEntries(fd))});
      const j = await r.json();
      if (j.ok) { sessionStorage.removeItem('temp_session_id'); window.location.href = '/'; }
      else { showToast(j.error.message || 'Invalid code', 'danger'); }
    }
    </script>
    """,
        "",
        show_sidebar=False,
    )


@router.get("/logout")
async def logout_page(request: Request):
    return HTMLResponse("""<html><body>
<script>fetch('/api/auth/logout',{method:'POST'}).finally(()=>{window.location.href='/login'});</script>
<p>Logging out...</p></body></html>""")



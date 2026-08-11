"""AutoClip authentication module.

Handles Google Sign-In OAuth flow, Flask session cookies, and access decorators.

Login flow:
  1. User visits /login, clicks "Sign in with Google"
  2. Redirect to Google OAuth (openid + email + profile scopes)
  3. Google redirects back to /auth/google/callback with a code
  4. Exchange code -> id_token, extract user info
  5. Create or update user in DB
  6. Set Flask session cookie with user_id
  7. Redirect based on user status:
     - Not approved: /pending-approval
     - Approved: /dashboard
"""
import os
import json
import secrets
import functools
from pathlib import Path
from urllib.parse import urlencode

from flask import (
    Blueprint, request, redirect, url_for, session, jsonify, abort, current_app
)
import requests
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests

import db

# ============================================================================
# Config
# ============================================================================

BOOTSTRAP_ADMIN_EMAIL = "harlanhgharris@gmail.com"

CREDS_DIR = Path.home() / "cfb_clip_studio" / "credentials"
SIGNIN_CLIENT_FILE = CREDS_DIR / "autoclip_signin_client.json"

# Where to save YouTube channel tokens (per-channel).
TOKENS_DIR = CREDS_DIR / "tokens"
TOKENS_DIR.mkdir(parents=True, exist_ok=True)


def _load_client_config():
    """Load OAuth client config JSON downloaded from Google Cloud Console."""
    with open(SIGNIN_CLIENT_FILE) as f:
        data = json.load(f)
    # Google gives either 'web' or 'installed' shape; we want 'web'
    return data.get('web') or data.get('installed') or data


SIGNIN_SCOPES = ["openid", "email", "profile"]
SIGNIN_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
SIGNIN_TOKEN_URI = "https://oauth2.googleapis.com/token"
SIGNIN_REDIRECT_URI = "https://autoclip.cloud/auth/google/callback"


# ============================================================================
# Blueprint
# ============================================================================

bp = Blueprint('auth', __name__)


@bp.route('/login')
def login():
    """Renders the sign-in page."""
    from flask import render_template
    if session.get('user_id'):
        return redirect(url_for('dashboard'))
    return render_template('login.html')


@bp.route('/login/google')
def login_google():
    """Kick off Google OAuth."""
    cfg = _load_client_config()
    state = secrets.token_urlsafe(32)
    session['oauth_state'] = state

    params = {
        'client_id': cfg['client_id'],
        'redirect_uri': SIGNIN_REDIRECT_URI,
        'response_type': 'code',
        'scope': ' '.join(SIGNIN_SCOPES),
        'access_type': 'online',
        'state': state,
        'prompt': 'select_account',  # let user pick account each time
    }
    return redirect(f"{SIGNIN_AUTH_URI}?{urlencode(params)}")


@bp.route('/auth/google/callback')
def google_callback():
    """Handle the OAuth redirect from Google."""
    from flask import render_template

    # Verify state to prevent CSRF
    state = request.args.get('state')
    if not state or state != session.pop('oauth_state', None):
        return "Invalid state parameter. Please try signing in again.", 400

    code = request.args.get('code')
    if not code:
        error = request.args.get('error', 'unknown')
        return f"Sign-in was cancelled or failed: {error}", 400

    cfg = _load_client_config()

    # Exchange code for tokens
    token_resp = requests.post(SIGNIN_TOKEN_URI, data={
        'code': code,
        'client_id': cfg['client_id'],
        'client_secret': cfg['client_secret'],
        'redirect_uri': SIGNIN_REDIRECT_URI,
        'grant_type': 'authorization_code',
    })
    if not token_resp.ok:
        return f"Failed to exchange code with Google: {token_resp.text}", 500

    tokens = token_resp.json()
    id_token_jwt = tokens.get('id_token')
    if not id_token_jwt:
        return "Google did not return an ID token.", 500

    # Verify the id_token
    try:
        idinfo = google_id_token.verify_oauth2_token(
            id_token_jwt, google_requests.Request(), cfg['client_id']
        )
    except ValueError as e:
        return f"Invalid ID token: {e}", 400

    google_id = idinfo['sub']
    email = idinfo.get('email', '')
    name = idinfo.get('name', '')
    picture = idinfo.get('picture', '')

    # Create or update the user
    user = db.create_or_update_user(google_id, email, name, picture)

    # Set session
    session.permanent = True
    session['user_id'] = user['id']

    if not user['is_approved']:
        return redirect(url_for('auth.pending_approval'))

    return redirect(url_for('dashboard'))


@bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('landing'))


@bp.route('/pending-approval')
def pending_approval():
    from flask import render_template
    if not session.get('user_id'):
        return redirect(url_for('auth.login'))
    user = db.get_user_by_id(session['user_id'])
    if not user:
        session.clear()
        return redirect(url_for('auth.login'))
    if user['is_approved']:
        return redirect(url_for('dashboard'))
    return render_template('pending_approval.html', user=user)


# ============================================================================
# Decorators
# ============================================================================

def get_current_user():
    """Return current logged-in user dict, or None."""
    user_id = session.get('user_id')
    if not user_id:
        return None
    return db.get_user_by_id(user_id)


def login_required(f):
    """Route decorator: user must be signed in AND approved."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            # For API routes, return 401 JSON. For pages, redirect.
            if request.path.startswith('/api/'):
                return jsonify({'error': 'not authenticated'}), 401
            return redirect(url_for('auth.login', next=request.path))
        if not user['is_approved']:
            if request.path.startswith('/api/'):
                return jsonify({'error': 'pending approval'}), 403
            return redirect(url_for('auth.pending_approval'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Route decorator: user must be signed in, approved, and admin."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            if request.path.startswith('/api/'):
                return jsonify({'error': 'not authenticated'}), 401
            return redirect(url_for('auth.login', next=request.path))
        if not user['is_approved']:
            return redirect(url_for('auth.pending_approval'))
        if user['role'] != 'admin':
            if request.path.startswith('/api/'):
                return jsonify({'error': 'admin only'}), 403
            abort(403)
        return f(*args, **kwargs)
    return decorated

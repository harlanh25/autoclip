"""AutoClip database module.

SQLite-backed. Single-file database at ~/cfb_clip_studio/autoclip.db.
Uses per-request connections via Flask's g.
"""
import os
import sqlite3
from pathlib import Path
from flask import g

import pgcompat

DB_PATH = str(Path.home() / "cfb_clip_studio" / "autoclip.db")


def get_conn():
    """Standalone connection, no Flask involvement.

    For worker.py / podcast_sync_worker.py and anything outside a request.
    Caller owns the lifecycle and must close it.
    """
    return pgcompat.connect(sqlite_path=DB_PATH)


def is_postgres():
    return pgcompat.backend() == "postgres"


def get_db():
    """Get a per-request DB connection."""
    if 'db' not in g:
        g.db = get_conn()
        if not is_postgres():
            g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(exc=None):
    """Close the DB connection at end of request."""
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_app(app):
    """Register close_db teardown on the Flask app."""
    app.teardown_appcontext(close_db)


# --- User helpers ---

def get_user_by_google_id(google_id):
    row = get_db().execute(
        "SELECT * FROM users WHERE google_id = ?", (google_id,)
    ).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id):
    row = get_db().execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    return dict(row) if row else None


def get_user_by_email(email):
    row = get_db().execute(
        "SELECT * FROM users WHERE email = ?", (email,)
    ).fetchone()
    return dict(row) if row else None


def create_or_update_user(google_id, email, name, picture_url):
    """Create user if new, update profile if existing. Returns user dict."""
    db = get_db()
    existing = get_user_by_google_id(google_id)

    # Bootstrap: if no admins exist yet and this email is the bootstrap admin,
    # auto-create as admin + approved. See auth.py BOOTSTRAP_ADMIN_EMAIL.
    from auth import BOOTSTRAP_ADMIN_EMAIL
    is_bootstrap_admin = (
        email == BOOTSTRAP_ADMIN_EMAIL and
        db.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0] == 0
    )

    if existing:
        db.execute(
            "UPDATE users SET email=?, name=?, picture_url=?, last_login_at=CURRENT_TIMESTAMP WHERE id=?",
            (email, name, picture_url, existing['id'])
        )
        db.commit()
        return get_user_by_id(existing['id'])
    else:
        role = 'admin' if is_bootstrap_admin else 'member'
        is_approved = 1 if is_bootstrap_admin else 0
        trial_expr = ("CURRENT_TIMESTAMP + INTERVAL '7 days'"
                      if is_postgres() else "datetime('now', '+7 days')")
        sql = (
            "INSERT INTO users (google_id, email, name, picture_url, role, is_approved, last_login_at, "
            "trial_started_at, trial_expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP, " + trial_expr + ")"
        )
        params = (google_id, email, name, picture_url, role, is_approved)
        if is_postgres():
            new_id = db.insert_returning_id(sql, params)
        else:
            new_id = db.execute(sql, params).lastrowid
        db.commit()
        return get_user_by_id(new_id)


def list_users():
    rows = get_db().execute(
        "SELECT * FROM users ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def set_user_approved(user_id, approved=True):
    get_db().execute(
        "UPDATE users SET is_approved=? WHERE id=?", (1 if approved else 0, user_id)
    )
    get_db().commit()


def set_user_role(user_id, role):
    assert role in ('member', 'admin')
    get_db().execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
    get_db().commit()


# --- Channel helpers ---

def get_channel_by_youtube_id(youtube_channel_id):
    row = get_db().execute(
        "SELECT * FROM channels WHERE youtube_channel_id = ?", (youtube_channel_id,)
    ).fetchone()
    return dict(row) if row else None


def get_channel_by_id(channel_id):
    row = get_db().execute(
        "SELECT * FROM channels WHERE id = ?", (channel_id,)
    ).fetchone()
    return dict(row) if row else None


def create_channel(youtube_channel_id, title, handle, token_path, owner_user_id=None, long_uploads=False):
    db = get_db()
    sql = (
        "INSERT INTO channels (youtube_channel_id, title, handle, token_path, owner_user_id, long_uploads_enabled) "
        "VALUES (?, ?, ?, ?, ?, ?)"
    )
    params = (youtube_channel_id, title, handle, token_path, owner_user_id,
              1 if long_uploads else 0)
    if is_postgres():
        new_id = db.insert_returning_id(sql, params)
    else:
        new_id = db.execute(sql, params).lastrowid
    db.commit()
    return get_channel_by_id(new_id)


def update_channel_owner(channel_id, owner_user_id):
    db = get_db()
    db.execute(
        "UPDATE channels SET owner_user_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (owner_user_id, channel_id)
    )
    # Also add the owner to user_channels as 'owner' role
    if owner_user_id:
        db.execute(_upsert_user_channel_sql(), (owner_user_id, channel_id, 'owner'))
    db.commit()


def list_all_channels():
    rows = get_db().execute("SELECT * FROM channels ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def list_channels_for_user(user_id):
    """Channels the user has any access to (owner, uploader, or viewer)."""
    rows = get_db().execute(
        "SELECT c.*, uc.role AS user_role FROM channels c "
        "JOIN user_channels uc ON uc.channel_id = c.id "
        "WHERE uc.user_id = ? "
        "ORDER BY c.title",
        (user_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def user_has_channel_access(user_id, channel_id):
    """True if the user has any access to this channel."""
    row = get_db().execute(
        "SELECT 1 FROM user_channels WHERE user_id=? AND channel_id=?",
        (user_id, channel_id)
    ).fetchone()
    return row is not None


def user_has_channel_access_by_yt_id(user_id, youtube_channel_id):
    row = get_db().execute(
        "SELECT 1 FROM user_channels uc "
        "JOIN channels c ON c.id = uc.channel_id "
        "WHERE uc.user_id = ? AND c.youtube_channel_id = ?",
        (user_id, youtube_channel_id)
    ).fetchone()
    return row is not None


def _upsert_user_channel_sql():
    """Upsert on the (user_id, channel_id) primary key."""
    if is_postgres():
        return ("INSERT INTO user_channels (user_id, channel_id, role) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT (user_id, channel_id) "
                "DO UPDATE SET role = EXCLUDED.role")
    return ("INSERT OR REPLACE INTO user_channels (user_id, channel_id, role) "
            "VALUES (?, ?, ?)")


def grant_channel_access(user_id, channel_id, role='uploader'):
    assert role in ('owner', 'uploader', 'viewer')
    db = get_db()
    db.execute(_upsert_user_channel_sql(), (user_id, channel_id, role))
    db.commit()


def revoke_channel_access(user_id, channel_id):
    db = get_db()
    db.execute(
        "DELETE FROM user_channels WHERE user_id=? AND channel_id=?",
        (user_id, channel_id)
    )
    db.commit()


def list_users_for_channel(channel_id):
    rows = get_db().execute(
        "SELECT u.*, uc.role AS channel_role FROM users u "
        "JOIN user_channels uc ON uc.user_id = u.id "
        "WHERE uc.channel_id = ?",
        (channel_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# __SET_USER_TIER_V1__
def set_user_tier(user_id, video_tier=None, audio_tier=None):
    """Update a user's tier(s). Also toggles has_clipping/has_audio and applies cap defaults."""
    from datetime import datetime
    # Cap defaults per tier (must match plans.py)
    VIDEO_CAPS = {'demo': 3, 'tier1': 20, 'tier2': 50, 'tier3': 200}
    AUDIO_CAPS = {'demo': 999999, 'tier1': 30, 'tier2': 100, 'tier3': 500}
    updates = []
    values = []
    if video_tier:
        assert video_tier in VIDEO_CAPS, f'bad video_tier {video_tier}'
        updates += ['video_tier=?', 'clipping_monthly_cap=?', 'has_clipping=?']
        values += [video_tier, VIDEO_CAPS[video_tier], 1 if video_tier != 'demo' else 0]
    if audio_tier:
        assert audio_tier in AUDIO_CAPS, f'bad audio_tier {audio_tier}'
        updates += ['audio_tier=?', 'audio_monthly_cap=?', 'has_audio=?']
        values += [audio_tier, AUDIO_CAPS[audio_tier], 1 if audio_tier != 'demo' else 0]
    if not updates:
        return
    values.append(user_id)
    get_db().execute(f"UPDATE users SET {', '.join(updates)} WHERE id=?", values)
    get_db().commit()

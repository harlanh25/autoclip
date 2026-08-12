"""Single source of truth for AutoClip pricing and tier configuration.

Any code that needs to know what a tier costs, includes, or caps should
import from here. This is the file that changes when pricing changes.
"""

# ─────────────────────────────────────────────────────────────
# Pricing definitions
# ─────────────────────────────────────────────────────────────

VIDEO_TIERS = {
    'demo':  {'name': 'Demo',    'price_usd': 0,      'monthly_cap': 3,   'stripe_price_id': None},
    'tier1': {'name': 'Starter', 'price_usd': 49.99,  'monthly_cap': 20,  'stripe_price_id': None},
    'tier2': {'name': 'Pro',     'price_usd': 79.99,  'monthly_cap': 50,  'stripe_price_id': None},
    'tier3': {'name': 'Studio',  'price_usd': 149.99, 'monthly_cap': 200, 'stripe_price_id': None},
}

AUDIO_TIERS = {
    'demo':  {'name': 'Demo',    'price_usd': 0,     'monthly_cap': 3,   'stripe_price_id': None},
    'tier1': {'name': 'Starter', 'price_usd': 24.99, 'monthly_cap': 30,  'stripe_price_id': None},
    'tier2': {'name': 'Pro',     'price_usd': 39.99, 'monthly_cap': 100, 'stripe_price_id': None},
    'tier3': {'name': 'Studio',  'price_usd': 49.99, 'monthly_cap': 500, 'stripe_price_id': None},
}

BUNDLE_TIERS = {
    'tier1': {'name': 'Bundle Starter', 'price_usd': 59.99,  'video_cap': 20,  'audio_cap': 30,  'savings_usd': 15.00, 'stripe_price_id': None},
    'tier2': {'name': 'Bundle Pro',     'price_usd': 99.99,  'video_cap': 50,  'audio_cap': 100, 'savings_usd': 19.99, 'stripe_price_id': None},
    'tier3': {'name': 'Bundle Studio',  'price_usd': 159.99, 'video_cap': 200, 'audio_cap': 500, 'savings_usd': 40.00, 'stripe_price_id': None},
}

# __STRIPE_PRICE_WIRING__
# Populate the stripe_price_id stubs from stripe_config so there is exactly
# one place price IDs are declared. Import is lazy + guarded so plans.py
# still works standalone (tests, scripts) if stripe_config is absent.
try:
    import stripe_config as _sc
    for _tbl, _prod in ((VIDEO_TIERS, 'video'), (AUDIO_TIERS, 'audio'), (BUNDLE_TIERS, 'bundle')):
        for _tier in _tbl:
            _tbl[_tier]['stripe_price_id'] = _sc.price_for_plan(f'{_prod}_{_tier}')
except Exception:
    pass



# ─────────────────────────────────────────────────────────────
# Helpers to read a user's current plan
# ─────────────────────────────────────────────────────────────

def video_tier_info(tier_key):
    return VIDEO_TIERS.get(tier_key) or VIDEO_TIERS['demo']

def audio_tier_info(tier_key):
    return AUDIO_TIERS.get(tier_key) or AUDIO_TIERS['demo']

def video_cap_for_user(user):
    """Effective monthly cap for a user's video product access."""
    if not user or not user.get('has_clipping'):
        return 0
    if user.get('clipping_monthly_cap') is not None:
        return int(user['clipping_monthly_cap'])
    return video_tier_info(user.get('video_tier')).get('monthly_cap', 0)

def audio_cap_for_user(user):
    """Effective monthly cap for a user's audio product access."""
    if not user or not user.get('has_audio'):
        return 0
    if user.get('audio_monthly_cap') is not None:
        return int(user['audio_monthly_cap'])
    return audio_tier_info(user.get('audio_tier')).get('monthly_cap', 0)


def has_bundle(user):
    """A user is on the bundle if both product tiers exist and match."""
    if not user:
        return False
    v = user.get('video_tier')
    a = user.get('audio_tier')
    return bool(v and a and v != 'demo' and a != 'demo' and v == a)


def user_plan_summary(user):
    """Return a dict summarizing the user's current plan for display."""
    if not user:
        return {}
    v_key = user.get('video_tier', 'demo')
    a_key = user.get('audio_tier', 'demo')
    return {
        'video_tier_key': v_key,
        'video_tier_name': video_tier_info(v_key)['name'],
        'video_price': video_tier_info(v_key)['price_usd'],
        'video_cap': video_cap_for_user(user),
        'has_video': bool(user.get('has_clipping')),
        'audio_tier_key': a_key,
        'audio_tier_name': audio_tier_info(a_key)['name'],
        'audio_price': audio_tier_info(a_key)['price_usd'],
        'audio_cap': audio_cap_for_user(user),
        'has_audio': bool(user.get('has_audio')),
        'is_bundle': has_bundle(user),
    }


# ─────────────────────────────────────────────────────────────
# Usage counting (this month)
# ─────────────────────────────────────────────────────────────

def video_usage_this_month(db, user_id):
    """Count of published (not failed) video jobs for the current calendar month."""
    row = db.execute(
        "SELECT COUNT(*) FROM publish_jobs "
        "WHERE user_id=? AND status='done' "
        "AND strftime('%Y-%m', COALESCE(finished_at, created_at)) = strftime('%Y-%m', 'now')",
        (user_id,)
    ).fetchone()
    return row[0] if row else 0


def audio_usage_this_month(db, user_id):
    """Placeholder for audio usage — wire up when Phase 5 audio tables exist."""
    # TODO: when audio publish table exists, count from it
    return 0


# __TRIAL_ENFORCEMENT_V1__
# ─────────────────────────────────────────────────────────────
# Trial + cap enforcement
# ─────────────────────────────────────────────────────────────
from datetime import datetime, timezone

DEMO_SESSION_LIMIT = 3

def is_on_trial(user):
    """Is this user currently in an active demo trial?"""
    if not user:
        return False
    if user.get('video_tier') != 'demo' and user.get('audio_tier') != 'demo':
        return False
    if not user.get('trial_expires_at'):
        return False
    return True


def trial_days_remaining(user):
    """Return int days remaining in trial, or None if not on trial. 0 if expired."""
    if not is_on_trial(user):
        return None
    try:
        exp = datetime.fromisoformat(user['trial_expires_at'])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        remaining = (exp - now).total_seconds() / 86400
        return max(0, int(remaining) + (1 if remaining > 0 else 0))
    except Exception:
        return None


def is_trial_expired(user):
    """True if user was on trial and it has ended."""
    if not user or user.get('video_tier') != 'demo':
        return False  # Not a demo user, not applicable
    if not user.get('trial_expires_at'):
        return False  # No trial ever set
    try:
        exp = datetime.fromisoformat(user['trial_expires_at'])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > exp
    except Exception:
        return False


def can_create_session(user):
    """Return (ok, reason). Enforces demo session limit + trial expiration."""
    if not user:
        return False, 'Not logged in.'
    # Founders / paid tiers: no session limit
    if user.get('video_tier') != 'demo':
        return True, None
    # Demo user: check trial expiry
    if is_trial_expired(user):
        return False, 'Your 7-day trial has ended. Please upgrade to continue.'
    # Demo user: check session count
    used = user.get('sessions_created_count') or 0
    if used >= DEMO_SESSION_LIMIT:
        return False, f'Demo limit reached ({DEMO_SESSION_LIMIT} sessions). Please upgrade to create more.'
    return True, None


def can_publish_video(user, current_month_count):
    """Return (ok, reason). Enforces trial expiry + monthly cap."""
    if not user or not user.get('has_clipping'):
        return False, 'Video subscription required.'
    if user.get('video_tier') == 'demo' and is_trial_expired(user):
        return False, 'Your 7-day trial has ended. Please upgrade to continue publishing.'
    cap = video_cap_for_user(user)
    if cap and current_month_count >= cap:
        return False, f'Monthly cap reached ({cap} clips). Upgrade for more.'
    return True, None


def can_publish_audio(user, current_month_count):
    """Return (ok, reason)."""
    if not user or not user.get('has_audio'):
        return False, 'Audio subscription required.'
    if user.get('audio_tier') == 'demo' and is_trial_expired(user):
        return False, 'Your 7-day trial has ended. Please upgrade.'
    cap = audio_cap_for_user(user)
    if cap and cap < 999999 and current_month_count >= cap:
        return False, f'Monthly cap reached ({cap} syncs). Upgrade for more.'
    return True, None


# __TRIAL_DISPLAY_V1__
# ─────────────────────────────────────────────────────────────
# Display-layer trial/usage state (product-aware).
# Enforcement still lives in can_create_session / can_publish_video.
# These are for UI only — banners, usage bars, upgrade prompts.
# ─────────────────────────────────────────────────────────────

def _trial_days_left(user):
    """Raw days left on trial_expires_at, or None. Not product-aware."""
    if not user or not user.get('trial_expires_at'):
        return None
    try:
        exp = datetime.fromisoformat(user['trial_expires_at'])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        remaining = (exp - datetime.now(timezone.utc)).total_seconds() / 86400
        return max(0, int(remaining) + (1 if remaining > 0 else 0))
    except Exception:
        return None


def _has_any_paid_tier(user):
    """True if the user pays for at least one product."""
    if not user:
        return False
    return (user.get('video_tier') or 'demo') != 'demo' or \
           (user.get('audio_tier') or 'demo') != 'demo'


def video_trial_state(user):
    """Trial state for the VIDEO product only."""
    if not user or user.get('video_tier') != 'demo':
        return {'on_trial': False, 'expired': False, 'days_left': None}
    if not user.get('trial_expires_at'):
        return {'on_trial': False, 'expired': False, 'days_left': None}
    if _has_any_paid_tier(user):
        # Converted on some product -> trial is over, not "still trialing"
        return {'on_trial': False, 'expired': False, 'days_left': None}
    days = _trial_days_left(user)
    return {
        'on_trial': True,
        'expired': (days == 0),
        'days_left': days,
    }


def audio_trial_state(user):
    """Trial state for the AUDIO product only."""
    if not user or user.get('audio_tier') != 'demo':
        return {'on_trial': False, 'expired': False, 'days_left': None}
    if not user.get('trial_expires_at'):
        return {'on_trial': False, 'expired': False, 'days_left': None}
    if _has_any_paid_tier(user):
        # Converted on some product -> trial is over, not "still trialing"
        return {'on_trial': False, 'expired': False, 'days_left': None}
    days = _trial_days_left(user)
    return {
        'on_trial': True,
        'expired': (days == 0),
        'days_left': days,
    }


def usage_context(user, db=None):
    """
    Everything the UI needs about trial + usage, in one dict.
    Safe to call with db=None (usage counts come back as None).
    Never raises — UI must not 500 because a count failed.
    """
    if not user:
        return {'authed': False}

    vt = video_trial_state(user)
    at = audio_trial_state(user)

    videos_used = None
    if db is not None and user.get('has_clipping'):
        try:
            videos_used = video_usage_this_month(db, user['id'])
        except Exception:
            videos_used = None

    v_cap = video_cap_for_user(user)
    sessions_used = user.get('sessions_created_count') or 0
    is_demo_video = user.get('video_tier') == 'demo'

    return {
        'authed': True,
        'plan': user_plan_summary(user),

        'video_trial': vt,
        'audio_trial': at,
        # Show a banner only if at least one product is genuinely on trial
        'show_trial_banner': bool(vt['on_trial'] or at['on_trial']),
        # Hard block only when the video trial is done and they never paid
        'video_locked': bool(vt['expired']),

        'videos_used': videos_used,
        'videos_cap': v_cap,
        'videos_pct': (
            min(100, int(videos_used * 100 / v_cap))
            if (videos_used is not None and v_cap) else None
        ),

        'sessions_used': sessions_used,
        'sessions_cap': DEMO_SESSION_LIMIT if is_demo_video else None,
        'sessions_pct': (
            min(100, int(sessions_used * 100 / DEMO_SESSION_LIMIT))
            if is_demo_video else None
        ),
    }


# __THUMBNAIL_CAPS_V1__
# ─────────────────────────────────────────────────────────────
# Thumbnail generation caps.
#   monthly pool = 1.5x the tier's clip cap
#   per-segment  = 3 attempts, so a difficult clip can be retried
# gpt-image-2 at 1536x1024 high is ~$0.165/image, the single largest
# variable cost per clip, so these are real money not just abuse control.
# ─────────────────────────────────────────────────────────────

THUMB_POOL_MULTIPLIER = 1.5
THUMB_PER_SEGMENT_MAX = 3
THUMB_EST_COST_USD = 0.165
THUMB_DEMO_POOL = 9          # demo: 3 sessions x 3 attempts


def thumbnail_pool_for_user(user):
    """Monthly generation allowance. None = unlimited (founders/admin)."""
    if not user:
        return 0
    tier = user.get('video_tier') or 'demo'
    if tier == 'demo':
        return THUMB_DEMO_POOL
    cap = video_cap_for_user(user)
    if not cap:
        return None
    return int(cap * THUMB_POOL_MULTIPLIER)


def thumbnails_used_this_month(db, user_id):
    row = db.execute(
        "SELECT COUNT(*) FROM thumbnail_generations "
        "WHERE user_id=? AND strftime('%Y-%m', created_at) = strftime('%Y-%m','now')",
        (user_id,)
    ).fetchone()
    return row[0] if row else 0


def thumbnails_used_for_segment(db, session_id, segment_index):
    row = db.execute(
        "SELECT COUNT(*) FROM thumbnail_generations "
        "WHERE session_id=? AND segment_index=?",
        (session_id, segment_index)
    ).fetchone()
    return row[0] if row else 0


def can_generate_thumbnail(user, db, session_id=None, segment_index=None):
    """Return (ok, reason). Checks per-segment then monthly pool."""
    if not user:
        return False, 'Not logged in.'
    if user.get('role') == 'admin':
        return True, None

    if session_id is not None and segment_index is not None:
        used = thumbnails_used_for_segment(db, session_id, segment_index)
        if used >= THUMB_PER_SEGMENT_MAX:
            return False, (f'This clip has used all {THUMB_PER_SEGMENT_MAX} thumbnail '
                           f'generations. Upload your own image instead.')

    pool = thumbnail_pool_for_user(user)
    if pool is None:
        return True, None
    used = thumbnails_used_this_month(db, user['id'])
    if used >= pool:
        return False, (f'Monthly thumbnail limit reached ({pool}). '
                       f'Upgrade for more, or upload your own image.')
    return True, None


def log_thumbnail_generation(db, user_id, session_id=None, segment_index=None,
                             model=None, quality=None):
    """Record a generation for cap enforcement and cost tracking."""
    db.execute(
        "INSERT INTO thumbnail_generations "
        "(user_id, session_id, segment_index, model, quality, est_cost_usd) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, session_id, segment_index, model, quality, THUMB_EST_COST_USD)
    )
    db.commit()
    # Mirror into usage_events so the margin report sees it. Kept as two
    # tables because thumbnail_generations backs the per-segment cap.
    try:
        import costs as _costs
        _costs.record_image(db, user_id, model,
                            session_id=session_id, segment_index=segment_index)
    except Exception:
        pass

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
    'tier2': {'name': 'Bundle Pro',     'price_usd': 95.99,  'video_cap': 50,  'audio_cap': 100, 'savings_usd': 24.00, 'stripe_price_id': None},
    'tier3': {'name': 'Bundle Studio',  'price_usd': 159.99, 'video_cap': 200, 'audio_cap': 500, 'savings_usd': 40.00, 'stripe_price_id': None},
}


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

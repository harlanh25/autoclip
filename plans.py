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

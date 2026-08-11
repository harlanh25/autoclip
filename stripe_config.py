"""
Stripe price ID -> plan mapping.

TEST MODE IDs. Live-mode products are separate objects with different IDs;
swap this table (or branch on STRIPE_LIVE) at launch.

product: 'video' | 'audio' | 'bundle'
tier:    'tier1' (Starter) | 'tier2' (Pro) | 'tier3' (Studio)

A 'bundle' purchase sets BOTH video_tier and audio_tier to the same tier,
which is what plans.has_bundle() keys off of.
"""

STRIPE_TEST_MODE = True

# plan_key -> price id
PRICE_IDS = {
    'video_tier1':  'price_1U3OpmPEKVwfSYb5oW6Kx0Jf',   # Video Starter    49.99
    'video_tier2':  'price_1U3Oq4PEKVwfSYb5WAjt0K5n',   # Video Pro        79.99
    'video_tier3':  'price_1U3OqLPEKVwfSYb5ZWi47WIb',   # Video Studio    149.99

    'audio_tier1':  'price_1U3OqfPEKVwfSYb5DKe40cVe',   # Audio Starter    24.99
    'audio_tier2':  'price_1U3OqxPEKVwfSYb5ijSZXTeQ',   # Audio Pro        39.99
    'audio_tier3':  'price_1U3OrDPEKVwfSYb5dR1AWwl2',   # Audio Studio     49.99

    'bundle_tier1': 'price_1U3OrSPEKVwfSYb5buNtWkAO',   # Bundle Starter   59.99
    'bundle_tier2': 'price_1U3OriPEKVwfSYb5uvh0K33Z',   # Bundle Pro       99.99
    'bundle_tier3': 'price_1U3OrvPEKVwfSYb5BePAtJLB',   # Bundle Studio   159.99
}

# reverse: price id -> (product, tier). Built at import so the webhook is O(1).
PRICE_TO_PLAN = {}
for _k, _pid in PRICE_IDS.items():
    _product, _tier = _k.split('_', 1)
    PRICE_TO_PLAN[_pid] = (_product, _tier)

# Display amounts, for sanity-checking against plans.py
EXPECTED_AMOUNTS = {
    'video_tier1': 49.99,  'video_tier2': 79.99,  'video_tier3': 149.99,
    'audio_tier1': 24.99,  'audio_tier2': 39.99,  'audio_tier3': 49.99,
    'bundle_tier1': 59.99, 'bundle_tier2': 99.99, 'bundle_tier3': 159.99,
}


def plan_for_price(price_id):
    """Return (product, tier) for a Stripe price id, or (None, None)."""
    return PRICE_TO_PLAN.get(price_id, (None, None))


def price_for_plan(plan_key):
    """Return the Stripe price id for e.g. 'video_tier2', or None."""
    return PRICE_IDS.get(plan_key)


def tiers_for_purchase(product, tier):
    """
    Given a purchased (product, tier), return (video_tier, audio_tier) deltas.
    None means "leave this product's tier unchanged".
    """
    if product == 'video':
        return (tier, None)
    if product == 'audio':
        return (None, tier)
    if product == 'bundle':
        return (tier, tier)
    return (None, None)

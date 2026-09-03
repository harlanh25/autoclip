"""
Per-user variable cost tracking.

Everything except fixed platform cost (VM, disk, static IP, registry) is
attributable to a user. Rates are Aug 2026; update here, not at call sites.
"""
import logging

log = logging.getLogger(__name__)

# ---- unit rates (USD) ----
WHISPER_PER_MIN      = 0.006
GPT4O_IN_PER_TOKEN   = 2.50 / 1_000_000
GPT4O_OUT_PER_TOKEN  = 10.00 / 1_000_000
IMAGE_PER_GEN        = 0.165      # gpt-image-2 1536x1024 high
# Claude Sonnet 5 - $2/$10 per MTok. Anthropic made the introductory rate
# permanent on 2026-08-10 and cancelled the $3/$15 increase that had been
# scheduled for 2026-09-01. No change needed.
SONNET5_IN_PER_TOKEN  = 2.00 / 1_000_000
SONNET5_OUT_PER_TOKEN = 10.00 / 1_000_000
L4_GPU_PER_HOUR      = 0.71
GCS_STORE_PER_GB_MO  = 0.020
GCS_EGRESS_PER_GB    = 0.12

# Post-Cloud-Run-migration, Sep 2026. Built from component rates rather than
# a past bill, since the architecture changed on 2026-09-03 and no full month
# has been billed under it yet. Revisit against the September invoice.
#   e2-small VM (podcast sync only)      13.00
#   VM disk 30GB standard PD              1.20
#   static IP                             3.00
#   Cloud SQL db-f1-micro + 10GB          9.00
#   Cloud Run web + worker (usage-based)  8.00
#   Cloud Storage (~85GB, falls once the 30-day lifecycle bites)  2.00
#   Artifact Registry, Cloud Tasks, net   2.00
FIXED_MONTHLY = 38.00             # platform cost, not attributable to a user


def record(db, user_id, kind, cost_usd, quantity=None, unit=None,
           session_id=None, segment_index=None, detail=None, commit=True):
    """Insert one usage event. Never raises - cost tracking must not break a request."""
    try:
        db.execute(
            "INSERT INTO usage_events "
            "(user_id, kind, session_id, segment_index, detail, quantity, unit, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, kind, session_id, segment_index, detail,
             quantity, unit, round(cost_usd or 0, 6))
        )
        if commit:
            db.commit()
    except Exception:
        log.exception('usage_events insert failed (kind=%s user=%s)', kind, user_id)


def record_whisper(db, user_id, minutes, session_id=None, detail=None):
    record(db, user_id, 'whisper', minutes * WHISPER_PER_MIN,
           quantity=minutes, unit='min', session_id=session_id, detail=detail)


def record_gpt_text(db, user_id, response, model='gpt-4o',
                    session_id=None, segment_index=None, detail=None):
    """Pull real token counts off an OpenAI chat completion response."""
    try:
        u = response.usage
        tin, tout = u.prompt_tokens, u.completion_tokens
    except Exception:
        return
    cost = tin * GPT4O_IN_PER_TOKEN + tout * GPT4O_OUT_PER_TOKEN
    record(db, user_id, 'gpt_text', cost, quantity=tin + tout, unit='tok',
           session_id=session_id, segment_index=segment_index,
           detail=detail or f'{model} in={tin} out={tout}')


def record_claude_text(db, user_id, response, model='claude-sonnet-5',
                       session_id=None, segment_index=None, detail=None):
    """Pull real token counts off an Anthropic Messages response.

    Recorded under kind='gpt_text' so the admin dashboard aggregates AI text
    spend across providers without a schema or dashboard change. The model
    name in `detail` distinguishes them.
    """
    try:
        u = response.usage
        tin, tout = u.input_tokens, u.output_tokens
    except Exception:
        return
    cost = tin * SONNET5_IN_PER_TOKEN + tout * SONNET5_OUT_PER_TOKEN
    record(db, user_id, 'gpt_text', cost, quantity=tin + tout, unit='tok',
           session_id=session_id, segment_index=segment_index,
           detail=detail or f'{model} in={tin} out={tout}')
def record_image(db, user_id, model, session_id=None, segment_index=None):
    record(db, user_id, 'thumbnail', IMAGE_PER_GEN, quantity=1, unit='image',
           session_id=session_id, segment_index=segment_index, detail=model)


def record_gpu(db, user_id, gpu_seconds, session_id=None, segment_index=None):
    mins = gpu_seconds / 60.0
    record(db, user_id, 'gpu_compose', (gpu_seconds / 3600.0) * L4_GPU_PER_HOUR,
           quantity=mins, unit='gpu_min', session_id=session_id,
           segment_index=segment_index)


def record_egress(db, user_id, gb, session_id=None, segment_index=None, detail=None):
    record(db, user_id, 'egress', gb * GCS_EGRESS_PER_GB, quantity=gb, unit='gb',
           session_id=session_id, segment_index=segment_index, detail=detail)


def record_storage_snapshot(db, user_id, gb):
    """One row per user per month from the storage sweep job."""
    record(db, user_id, 'storage', gb * GCS_STORE_PER_GB_MO, quantity=gb, unit='gb')


# ---- reporting ----

def user_costs(db, user_id, month=None):
    """Cost breakdown by kind for a user. month='YYYY-MM', default current."""
    import db as _db
    where = "user_id=?"
    args = [user_id]
    if month:
        where += " AND %s=?" % _db.month_expr("created_at")
        args.append(month)
    else:
        where += " AND %s=%s" % (_db.month_expr("created_at"),
                                 _db.current_month_expr())
    rows = db.execute(
        f"SELECT kind, COUNT(*) n, SUM(quantity) qty, SUM(cost_usd) cost "
        f"FROM usage_events WHERE {where} GROUP BY kind ORDER BY cost DESC", args
    ).fetchall()
    out = [dict(r) for r in rows]
    return {'by_kind': out, 'total': sum(r['cost'] or 0 for r in out)}


def all_user_costs(db, month=None):
    """Every user's total for a month, most expensive first."""
    import db as _db
    clause = (("%s=?" % _db.month_expr("e.created_at")) if month
              else ("%s=%s" % (_db.month_expr("e.created_at"),
                               _db.current_month_expr())))
    args = [month] if month else []
    rows = db.execute(
        f"SELECT u.id, u.email, u.name, u.video_tier, u.audio_tier, "
        f"       COALESCE(SUM(e.cost_usd), 0) cost, COUNT(e.id) events "
        f"FROM users u LEFT JOIN usage_events e "
        f"  ON e.user_id = u.id AND {clause} "
        f"GROUP BY u.id ORDER BY cost DESC", args
    ).fetchall()
    return [dict(r) for r in rows]


def available_months(db):
    import db as _db
    rows = db.execute(
        "SELECT DISTINCT %s m FROM usage_events "
        "ORDER BY m DESC" % _db.month_expr("created_at")
    ).fetchall()
    return [r[0] for r in rows if r[0]]


# ---- margin ----

def _monthly_revenue(user, plans_mod):
    """What this user pays per month. Bundle counts once, not twice."""
    v = user.get('video_tier') or 'demo'
    a = user.get('audio_tier') or 'demo'
    if v != 'demo' and a != 'demo' and v == a:
        return plans_mod.BUNDLE_TIERS.get(v, {}).get('price_usd', 0) or 0
    rev = 0
    if v != 'demo':
        rev += plans_mod.VIDEO_TIERS.get(v, {}).get('price_usd', 0) or 0
    if a != 'demo':
        rev += plans_mod.AUDIO_TIERS.get(a, {}).get('price_usd', 0) or 0
    return rev


def margin_report(db, plans_mod, month=None):
    """
    Per-user revenue, variable cost, gross profit and margin, plus a
    platform-level summary that also subtracts fixed cost.
    """
    rows = all_user_costs(db, month)
    users = []
    for r in rows:
        rev = _monthly_revenue(r, plans_mod)
        cost = r['cost'] or 0
        profit = rev - cost
        users.append({
            **r,
            'revenue': rev,
            'cost': cost,
            'profit': profit,
            'margin_pct': (profit / rev * 100) if rev else None,
            'is_paying': rev > 0,
        })

    paying = [u for u in users if u['is_paying']]
    free = [u for u in users if not u['is_paying']]

    total_rev = sum(u['revenue'] for u in users)
    total_cost = sum(u['cost'] for u in users)
    gross = total_rev - total_cost
    net = gross - FIXED_MONTHLY

    return {
        'month': month or 'current',
        'users': users,
        'summary': {
            'revenue': total_rev,
            'variable_cost': total_cost,
            'gross_profit': gross,
            'gross_margin_pct': (gross / total_rev * 100) if total_rev else None,
            'fixed_cost': FIXED_MONTHLY,
            'net_profit': net,
            'net_margin_pct': (net / total_rev * 100) if total_rev else None,
            'paying_users': len(paying),
            'free_users': len(free),
            'free_user_cost': sum(u['cost'] for u in free),
            'breakeven_users': (FIXED_MONTHLY / (gross / len(paying))
                                if paying and gross > 0 else None),
        },
    }

"""
Stripe billing for AutoClip.

Model: trials are OURS (cardless, 7 days, enforced by the app gate).
Stripe only ever sees a paid subscription starting the moment someone pays.
No trial_period_days on any price.

Routes:
  POST /api/checkout/<plan_key>  -> Checkout session URL
  POST /api/billing_portal       -> Customer Portal URL
  POST /api/stripe/webhook       -> subscription lifecycle
"""
import os
import logging
from datetime import datetime, timezone

import stripe
from flask import Blueprint, jsonify, request, current_app

import db
import plans
import stripe_config

log = logging.getLogger(__name__)
bp = Blueprint('stripe_bp', __name__)

# Keep access while Stripe retries a failed payment (~2 weeks).
# Downgrade only on customer.subscription.deleted.
# Flip to False to cut access on the first decline.
GRACE_PERIOD_ON_PAST_DUE = True

STATUSES_WITH_ACCESS = {'active', 'trialing'}
if GRACE_PERIOD_ON_PAST_DUE:
    STATUSES_WITH_ACCESS |= {'past_due', 'unpaid'}


def _key():
    k = os.environ.get('STRIPE_SECRET_KEY', '')
    if not k:
        raise RuntimeError('STRIPE_SECRET_KEY not set')
    stripe.api_key = k
    return k


def _base_url():
    return os.environ.get('PUBLIC_BASE_URL', 'https://autoclip.cloud').rstrip('/')


def _ts(unix):
    if not unix:
        return None
    return datetime.fromtimestamp(unix, tz=timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────
# Checkout
# ─────────────────────────────────────────────────────────────

@bp.route('/api/checkout/<plan_key>', methods=['POST'])
def create_checkout(plan_key):
    import auth as autoclip_auth
    user = autoclip_auth.get_current_user()
    if not user:
        return jsonify({'error': 'not authenticated'}), 401

    price_id = stripe_config.price_for_plan(plan_key)
    if not price_id:
        return jsonify({'error': f'unknown plan: {plan_key}'}), 400

    try:
        _key()
        customer_id = user.get('stripe_customer_id')
        if not customer_id:
            cust = stripe.Customer.create(
                email=user['email'],
                name=user.get('name') or None,
                metadata={'autoclip_user_id': str(user['id'])},
            )
            customer_id = cust.id
            db.get_db().execute(
                "UPDATE users SET stripe_customer_id=? WHERE id=?",
                (customer_id, user['id'])
            )
            db.get_db().commit()

        session = stripe.checkout.Session.create(
            mode='subscription',
            customer=customer_id,
            line_items=[{'price': price_id, 'quantity': 1}],
            success_url=f'{_base_url()}/account?checkout=success',
            cancel_url=f'{_base_url()}/pricing?checkout=cancelled',
            allow_promotion_codes=True,
            # user id on BOTH objects: the session for checkout.session.completed,
            # the subscription for every later subscription.* event
            client_reference_id=str(user['id']),
            metadata={'autoclip_user_id': str(user['id']), 'plan_key': plan_key},
            subscription_data={
                'metadata': {'autoclip_user_id': str(user['id']), 'plan_key': plan_key}
            },
        )
        return jsonify({'url': session.url})
    except Exception as e:
        log.exception('checkout failed')
        return jsonify({'error': str(e)}), 500


@bp.route('/api/billing_portal', methods=['POST'])
def billing_portal():
    import auth as autoclip_auth
    user = autoclip_auth.get_current_user()
    if not user:
        return jsonify({'error': 'not authenticated'}), 401
    if not user.get('stripe_customer_id'):
        return jsonify({'error': 'no billing account yet'}), 400
    try:
        _key()
        sess = stripe.billing_portal.Session.create(
            customer=user['stripe_customer_id'],
            return_url=f'{_base_url()}/account',
        )
        return jsonify({'url': sess.url})
    except Exception as e:
        log.exception('portal failed')
        return jsonify({'error': str(e)}), 500


# ─────────────────────────────────────────────────────────────
# Webhook
# ─────────────────────────────────────────────────────────────

def _d(obj):
    """Stripe objects override __getattr__, so .get() raises KeyError('get').
    Normalize to a plain dict before any dict-style access."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    # stripe>=15: StripeObject has .to_dict(); dict(obj) raises KeyError.
    to_dict = getattr(obj, 'to_dict', None)
    if callable(to_dict):
        try:
            return to_dict()
        except Exception:
            pass
    try:
        return {k: obj[k] for k in obj.keys()}
    except Exception:
        return {}


def _user_for_event(obj):
    """Resolve a user from metadata first, then customer id."""
    obj = _d(obj)
    d = db.get_db()
    uid = (obj.get('metadata') or {}).get('autoclip_user_id')
    if uid:
        row = d.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if row:
            return dict(row)
    cust = obj.get('customer')
    if cust:
        row = d.execute("SELECT * FROM users WHERE stripe_customer_id=?", (cust,)).fetchone()
        if row:
            return dict(row)
    return None


def _apply_subscription(user, sub):
    """Write subscription state + tiers for one subscription object."""
    sub = _d(sub)
    d = db.get_db()
    status = sub.get('status')
    sub_id = sub.get('id')

    items = (_d(sub.get('items')).get('data')) or []
    price_id = (_d(_d(items[0]).get('price')).get('id')) if items else None
    product, tier = stripe_config.plan_for_price(price_id) if price_id else (None, None)

    # Newer Stripe API versions moved current_period_end onto the subscription
    # item; older ones keep it on the subscription. Check both.
    _item0 = _d(items[0]) if items else {}
    period_end = _ts(_item0.get('current_period_end') or sub.get('current_period_end'))

    d.execute(
        "UPDATE users SET stripe_subscription_id=?, subscription_status=?, "
        "current_period_end=?, stripe_customer_id=COALESCE(stripe_customer_id, ?) WHERE id=?",
        (sub_id, status, period_end, sub.get('customer'), user['id'])
    )
    d.commit()

    if not product:
        log.warning('unmapped price %s on sub %s', price_id, sub_id)
        return

    if status in STATUSES_WITH_ACCESS:
        v, a = stripe_config.tiers_for_purchase(product, tier)
    else:
        v, a = ('demo', 'demo') if product == 'bundle' else \
               (('demo', None) if product == 'video' else (None, 'demo'))

    if v or a:
        db.set_user_tier(user['id'], video_tier=v, audio_tier=a)

    log.info('user %s sub %s status=%s -> video=%s audio=%s',
             user['id'], sub_id, status, v, a)


@bp.route('/api/stripe/webhook', methods=['POST'])
def webhook():
    secret = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
    payload = request.get_data()
    sig = request.headers.get('Stripe-Signature', '')

    if not secret:
        log.error('STRIPE_WEBHOOK_SECRET not set - rejecting')
        return jsonify({'error': 'webhook not configured'}), 500

    try:
        event = stripe.Webhook.construct_event(payload, sig, secret)
    except Exception as e:
        log.warning('bad webhook signature: %s', e)
        return jsonify({'error': 'invalid signature'}), 400

    etype = event['type']
    obj = _d(event['data']['object'])
    log.info('stripe webhook: %s', etype)

    try:
        _key()
        if etype == 'checkout.session.completed':
            user = _user_for_event(obj)
            if not user and obj.get('client_reference_id'):
                row = db.get_db().execute(
                    "SELECT * FROM users WHERE id=?", (obj['client_reference_id'],)
                ).fetchone()
                user = dict(row) if row else None
            if user and obj.get('subscription'):
                sub = stripe.Subscription.retrieve(obj['subscription'])
                _apply_subscription(user, sub)

        elif etype in ('customer.subscription.created',
                       'customer.subscription.updated',
                       'customer.subscription.deleted'):
            user = _user_for_event(obj)
            if user:
                if etype == 'customer.subscription.deleted':
                    obj = dict(obj)
                    obj['status'] = 'canceled'
                _apply_subscription(user, obj)

        elif etype == 'invoice.payment_failed':
            user = _user_for_event(obj)
            if user:
                db.get_db().execute(
                    "UPDATE users SET subscription_status='past_due' WHERE id=?",
                    (user['id'],)
                )
                db.get_db().commit()
                log.warning('payment failed for user %s', user['id'])

    except Exception:
        # 500 makes Stripe retry, which is what we want on a transient failure
        log.exception('webhook handler error on %s', etype)
        return jsonify({'error': 'handler error'}), 500

    return jsonify({'received': True}), 200

# AutoClip Backlog

## Known issues
- [ ] Monetization checkbox not persisting to YouTube on publish. Currently must toggle in YT Studio manually. Fix: investigate monetizationDetails API endpoint or verify snippet payload structure.

## Phase 5 - Podcast side (mirror of video)
- Multi-tenant podcast connections
- Users add Buzzsprout/Transistor/Libsyn/RSS.com connections
- Upload audio from platform
- Insert ads from podcast platform APIs
- Manual ad insertion into audio
- Same async job architecture (Cloud Tasks + Cloud Run worker)

## Phase 4 remaining
- [ ] 4.3: Cloud SQL Postgres migration (kill SQLite, VM becomes disposable)
- [ ] 4.4: Cloud Run web tier (Dockerize Flask, kill VM entirely)

## Post-4.x enhancements
- Cost dashboards per user
- Auto-place segments to 8-min clips (guided vs auto toggle)
- Reference image persistence across page reloads
- Fair-use caps enforcement on clipping_monthly_cap / audio_monthly_cap
- Compose caching (skip ffmpeg if same inputs re-published)
- Priority queues (paid tier = faster workers)
- Better observability (Cloud Monitoring dashboards, alerting)

## Data lifecycle policy (Phase 5-ish)
- [ ] Session JSON: keep indefinitely (tiny, useful history)
- [ ] Source videos in GCS: auto-delete after N days (differentiate free vs paid)
- [ ] Cut clips in GCS: auto-delete after N days
- [ ] Composed intermediate files: already deleted post-upload ✓
- [ ] Soft-delete on user account deletion, hard-delete after 30 days
- [ ] Cost dashboard per user (storage GB + compute minutes)

## Trial + subscription UI polish (enhancement)
- [ ] Global trial banner: show "N days left in trial" on all pages for demo users
- [ ] Usage progress bar visible on dashboard (not just /account)
- [ ] Trial-expired screen with prominent upgrade CTA (currently just JSON 402)
- [ ] Client-side handling of 402 response: show upgrade modal instead of raw error
- [ ] "Sessions used: 2/3" indicator on session upload form
- [ ] Auto-set trial_started_at for NEW user signups (currently only grandfathered)

## Bundle handling (address at Stripe integration)
Right now bundles work by having video_tier == audio_tier (both non-demo). The pricing page displays bundle prices ($59.99/$99.99/$159.99), but caps and DB records are set separately per product.

When Stripe integration lands:
- [ ] Create dedicated Stripe products for each bundle tier (not two separate subscriptions)
- [ ] Admin UI needs one-click "Set to Bundle T1/T2/T3" that sets both video + audio tiers atomically
- [ ] Bundle upgrade/downgrade flow: transitioning from separate subscriptions to a bundle (or vice versa) needs a subscription swap in Stripe
- [ ] Bundle indicator badge in admin panel (small 🎁 Bundle tag when both tiers match and non-demo)
- [ ] Handle mid-cycle changes: if user upgrades video only, are they still on bundle or split?
- [ ] Emails/notifications should reflect "You're on Bundle Pro" not "You have Video Pro + Audio Pro"

## Bundle handling (address at Stripe integration)
Right now bundles work by having video_tier == audio_tier (both non-demo). The pricing page displays bundle prices ($59.99/$99.99/$159.99), but caps and DB records are set separately per product.

When Stripe integration lands:
- [ ] Create dedicated Stripe products for each bundle tier (not two separate subscriptions)
- [ ] Admin UI needs one-click "Set to Bundle T1/T2/T3" that sets both video + audio tiers atomically
- [ ] Bundle upgrade/downgrade flow: transitioning from separate subscriptions to a bundle (or vice versa) needs a subscription swap in Stripe
- [ ] Bundle indicator badge in admin panel (small 🎁 Bundle tag when both tiers match and non-demo)
- [ ] Handle mid-cycle changes: if user upgrades video only, are they still on bundle or split?
- [ ] Emails/notifications should reflect "You're on Bundle Pro" not "You have Video Pro + Audio Pro"

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

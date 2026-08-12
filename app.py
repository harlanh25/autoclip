#!/usr/bin/env python3
"""
CFB Clip Studio
A content production pipeline for College Football Addiction.
Upload a live show, clip segments, add ads, generate titles/descriptions, upload to YouTube.
"""

import os
import json
import uuid
import subprocess
from pathlib import Path
import re
import gcs_storage
import tempfile
import gcs_helper
import plans
import costs
from flask import Flask, render_template, request, jsonify, send_from_directory, redirect, url_for
from openai import OpenAI
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
import google.auth.transport.requests
import db as autoclip_db
import auth as autoclip_auth
from flask import session as flask_session

app = Flask(__name__)

# --- Multi-tenant additions ---
app.secret_key = os.environ.get('AUTOCLIP_SECRET_KEY', 'dev-key-CHANGE-IN-PROD')
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = 60 * 60 * 24 * 30  # 30 days
autoclip_db.init_app(app)
app.register_blueprint(autoclip_auth.bp)


PUBLIC_PATH_PREFIXES = ('/static/', '/login', '/logout', '/auth/', '/pending-approval')
PUBLIC_EXACT_PATHS = {'/', '/pricing'}

@app.before_request
def _enforce_auth_globally():
    from flask import request as _rq, redirect as _rd, url_for as _uf, jsonify as _jf
    path = _rq.path
    if path in PUBLIC_EXACT_PATHS:
        return
    if any(path.startswith(p) for p in PUBLIC_PATH_PREFIXES):
        return
    # Worker callbacks use X-Worker-Secret auth, not session cookies
    if path.startswith('/api/publish_jobs/') and path.endswith('/worker_update'):
        return
    # Stripe webhooks come from Stripe's servers - authenticated by signature
    if path == '/api/stripe/webhook':
        return
    user = autoclip_auth.get_current_user()
    if not user:
        if path.startswith('/api/'):
            return _jf({'error': 'not authenticated'}), 401
        return _rd(_uf('auth.login'))
    if not user['is_approved']:
        if path.startswith('/api/'):
            return _jf({'error': 'pending approval'}), 403
        return _rd(_uf('auth.pending_approval'))


# __TRIAL_UI_V1__
TRIAL_GATE_EXEMPT_EXACT = {'/pricing', '/account', '/logout', '/login', '/favicon.ico'}
TRIAL_GATE_EXEMPT_PREFIX = ('/static/', '/auth/', '/api/admin/', '/api/stripe/', '/api/checkout/', '/api/billing_portal')


@app.context_processor
def _inject_trial_context():
    """Make `trial` available in every template. Never raises."""
    try:
        import plans as _plans
        u = autoclip_auth.get_current_user()
        if not u:
            return {'trial': {'authed': False}}
        try:
            _db = autoclip_db.get_db()
        except Exception:
            _db = None
        return {'trial': _plans.usage_context(u, _db)}
    except Exception:
        return {'trial': {'authed': False}}


@app.before_request
def _gate_expired_trials():
    """Hard gate: expired demo users get the upgrade wall."""
    from flask import request as _rq, jsonify as _jf
    path = _rq.path
    if path in PUBLIC_EXACT_PATHS or path in TRIAL_GATE_EXEMPT_EXACT:
        return
    if any(path.startswith(p) for p in PUBLIC_PATH_PREFIXES):
        return
    if any(path.startswith(p) for p in TRIAL_GATE_EXEMPT_PREFIX):
        return
    if path.startswith('/api/publish_jobs/') and path.endswith('/worker_update'):
        return
    try:
        import plans as _plans
        u = autoclip_auth.get_current_user()
        if not u or u.get('role') == 'admin':
            return
        if not _plans.video_trial_state(u)['expired']:
            return
        if path.startswith('/api/'):
            return _jf({'error': 'Your free trial has ended. Please upgrade to continue.',
                        'upgrade_url': '/pricing'}), 402
        return render_template('trial_expired.html'), 402
    except Exception:
        return  # never lock anyone out on an internal error



# __STRIPE_BP__
try:
    import stripe_integration
    app.register_blueprint(stripe_integration.bp)
    app.logger.info('stripe blueprint registered')
except Exception as _e:
    app.logger.error('stripe blueprint NOT registered: %s', _e)

# --- End multi-tenant additions ---

app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024 * 1024  # 10GB max upload

# ─────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────
OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY", "")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY environment variable not set — configure it in systemd unit or /etc/environment")
# gpt-image-1 is removed from the OpenAI API on 2026-10-23.
# gpt-image-2 at 1536x1024/high is ~1.9x slower (95s vs 51s) but clearly
# better, especially at rendering text. ~$0.165/image.
# Note: gpt-image-2 rejects input_fidelity - it always uses high fidelity.
# Founder/admin accounts excluded from revenue totals by default -
# they pay nothing, so counting their tier as revenue would be fiction.
ADMIN_USER_IDS = {1, 2}
IMAGE_MODEL = os.environ.get('AUTOCLIP_IMAGE_MODEL', 'gpt-image-2')
YOUTUBE_API_KEY  = "AIzaSyAO6a2xvRYpaVKD27hDzHWPvfxyk7vmB4s"

BASE_DIR         = Path(__file__).parent
UPLOAD_DIR       = BASE_DIR / "static" / "uploads"
ADS_DIR          = BASE_DIR / "static" / "ads"
THUMBS_DIR       = BASE_DIR / "static" / "thumbnails"
CLIPS_DIR        = BASE_DIR / "static" / "clips"
SESSIONS_DIR     = BASE_DIR / "sessions"

for d in [UPLOAD_DIR, ADS_DIR, THUMBS_DIR, CLIPS_DIR, SESSIONS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

client = OpenAI(api_key=OPENAI_API_KEY)

# Title style guide for AI generation
TITLE_STYLE_GUIDE = """
Generate YouTube titles for a college football podcast channel. Follow these rules exactly:
- Use ALL CAPS for key words (team names, power words)
- Use power words like: ADMITS, REVEALS, SECRET, BOMBSHELL, MUST SEE, TRUTH, LEAKS, EXPOSED, SHOCKING
- Include the team or conference name
- End with ! or ?
- Keep under 70 characters
- Examples:
  "LSU Expert Says Tigers' SECRET WEAPON Will HELP Lane Kiffin!"
  "Miami Hurricanes KEEPING A SECRET on ELITE Recruit? 👀"
  "Clemson RIVAL Admits Something EVERY Tiger MUST SEE!"
  "BYU Source LEAKS BOMBSHELL That Changed EVERYTHING for the Cougars!"
"""


# ─────────────────────────────────────────
#  ROUTES — Pages
# ─────────────────────────────────────────

@app.route('/')
def landing():
    """Public marketing page. Signed-in users are redirected to dashboard."""
    user = autoclip_auth.get_current_user()
    if user and user['is_approved']:
        return redirect(url_for('dashboard'))
    return render_template('landing.html')

@app.route('/dashboard')
def dashboard():
    """Signed-in dashboard — the AutoClip upload page."""
    user = autoclip_auth.get_current_user()
    channels = autoclip_db.list_channels_for_user(user['id']) if user['role'] != 'admin' else autoclip_db.list_all_channels()
    return render_template('index.html', current_user=user, channels=channels)

# __AUTOCLIP_MY_SESSIONS_V42__
@app.route('/sessions')
def list_sessions():
    """Dashboard of all sessions owned by the current user."""
    import glob
    import os
    user = autoclip_auth.get_current_user()
    if not user:
        return redirect(url_for('auth.login'))

    db = autoclip_db.get_db()

    # Load and enrich each session
    rows = []
    for fp in glob.glob('sessions/*.json'):
        try:
            with open(fp) as f:
                s = json.load(f)
        except Exception:
            continue

        # Ownership check: admin sees all, users see their own
        owner = s.get('owner_user_id')
        if user['role'] != 'admin' and owner != user['id']:
            continue

        segments = s.get('segments', [])
        published = sum(1 for seg in segments if seg.get('youtube_video_id'))

        # Channel display name
        chan_yt_id = s.get('channel_youtube_id')
        chan_name = '(no channel)'
        if chan_yt_id:
            _cr = db.execute(
                "SELECT title FROM channels WHERE youtube_channel_id=?",
                (chan_yt_id,)
            ).fetchone()
            if _cr:
                chan_name = _cr['title']

        rows.append({
            'id': s.get('id'),
            'show_name': s.get('show_name') or 'Untitled show',
            'channel_name': chan_name,
            'segment_count': len(segments),
            'published_count': published,
            'mtime': os.path.getmtime(fp),
        })

    # Sort by mtime desc (most recent first)
    rows.sort(key=lambda r: r['mtime'], reverse=True)

    return render_template('sessions.html', sessions=rows, current_user=user)


@app.route('/studio/<session_id>')
def studio(session_id):
    session = load_session(session_id)
    if not session:
        return "Session not found", 404
    return render_template('studio.html', session=session, session_id=session_id)

@app.route('/clips/<session_id>')
def clips_page(session_id):
    session = load_session(session_id)
    if not session:
        return "Session not found", 404
    return render_template('clips.html', session=session, session_id=session_id)

@app.route('/ads')
def ads_page():
    user = autoclip_auth.get_current_user()
    db = autoclip_db.get_db()
    if user['role'] == 'admin':
        channels = db.execute(
            "SELECT id, youtube_channel_id, title, handle FROM channels ORDER BY title"
        ).fetchall()
    else:
        channels = db.execute(
            "SELECT c.id, c.youtube_channel_id, c.title, c.handle "
            "FROM channels c "
            "JOIN user_channels uc ON uc.channel_id = c.id "
            "WHERE uc.user_id = ? "
            "ORDER BY c.title",
            (user['id'],)
        ).fetchall()
    return render_template(
        'ads.html',
        current_user=user,
        channels=[dict(c) for c in channels]
    )

@app.route('/thumbnails')
def thumbnails_page():
    user = autoclip_auth.get_current_user()
    db = autoclip_db.get_db()
    if user['role'] == 'admin':
        channels = db.execute(
            "SELECT id, youtube_channel_id, title, handle FROM channels ORDER BY title"
        ).fetchall()
    else:
        channels = db.execute(
            "SELECT c.id, c.youtube_channel_id, c.title, c.handle "
            "FROM channels c "
            "JOIN user_channels uc ON uc.channel_id = c.id "
            "WHERE uc.user_id = ? "
            "ORDER BY c.title",
            (user['id'],)
        ).fetchall()
    return render_template(
        'thumbnails.html',
        current_user=user,
        channels=[dict(c) for c in channels]
    )


# ─────────────────────────────────────────
#  ROUTES — API
# ─────────────────────────────────────────


@app.route('/api/pull_latest_video', methods=['POST'])
def pull_latest_video():
    import subprocess
    req_data = request.json or {}
    channel_id = req_data.get('channel_id', 'UC3llcEBdtsSi0qTDtKs1QpA')
    CHANNEL_HANDLES = {
        'UCEsOcvBbXtO8AyyY2tZYJpg': '@PowerTwoCFB',
        'UC3llcEBdtsSi0qTDtKs1QpA': '@CFBAddiction',
        'UCrZfTzLVUeFiYL65Q2ohGGQ': '@ACCFootballAddiction',
        'UCZNyN9HE6dEODo34dU-LVfg': '@SECFootballAddiction',
        'UCM1psK9YLuXtW1La8yJGa6Q': '@Big10FootballAddiction',
        'UCaAn3sLjMU5rdA3A7cW5nsA': '@Big12FootballAddiction',
    }
    handle = CHANNEL_HANDLES.get(channel_id)
    channel_url = f'https://www.youtube.com/{handle}' if handle else f'https://www.youtube.com/channel/{channel_id}'
    result = subprocess.run(
        ['yt-dlp', '--flat-playlist', '--playlist-end', '1',
         '--print', '%(id)s|||%(title)s|||%(duration)s', channel_url],
        capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0 or not result.stdout.strip():
        return jsonify({'error': 'Could not fetch channel videos', 'detail': result.stderr}), 500
    line = result.stdout.strip().split("\n")[0]
    parts = line.split("|||")
    video_id = parts[0].strip()
    video_title = parts[1].strip() if len(parts) > 1 else 'Latest Video'
    duration = parts[2].strip() if len(parts) > 2 else 'unknown'
    video_url = f'https://www.youtube.com/watch?v={video_id}'
    session_id = str(uuid.uuid4())[:8]
    filename = f"{session_id}.mp4"
    filepath = UPLOAD_DIR / filename
    dl = subprocess.run(
        ['yt-dlp', '-f', 'best[ext=mp4]/best', '-o', str(filepath), video_url],
        capture_output=True, text=True, timeout=3600
    )
    if dl.returncode != 0:
        return jsonify({'error': 'Download failed', 'detail': dl.stderr}), 500
    session = {
        'id': session_id,
        'show_name': video_title,
        'video_file': filename,
        'video_path': str(filepath),
        'video_url': video_url,
        'video_id': video_id,
        'transcript': None,
        'segments': [],
        'clips': []
    }
    save_session(session_id, session)
    return jsonify({
        'session_id': session_id,
        'video_id': video_id,
        'title': video_title,
        'duration': duration,
        'status': 'downloaded'
    })

@app.route('/api/upload/signed-url', methods=['POST'])
def get_upload_signed_url():
    """Step 1: browser asks for a signed URL to PUT the video directly to GCS."""
    data = request.get_json() or {}
    filename = data.get('filename', 'video.mp4')
    content_type = data.get('content_type', 'video/mp4')
    session_id = str(uuid.uuid4())[:8]
    ext = Path(filename).suffix or '.mp4'
    gcs_key = f"uploads/{session_id}{ext}"
    signed_url = gcs_helper.generate_upload_url(gcs_key, content_type)
    return jsonify({
        'session_id': session_id,
        'gcs_key': gcs_key,
        'signed_url': signed_url,
    })

@app.route('/api/upload/complete', methods=['POST'])
def complete_upload():
    """Step 2: browser confirms upload done, we create the session."""
    user = autoclip_auth.get_current_user()
    if not user['has_clipping']:
        return jsonify({'error': 'clipping subscription required'}), 403

    # __AUTOCLIP_TRIAL_GATE_V1__
    # Enforce demo session limit + trial expiration
    ok, reason = plans.can_create_session(user)
    if not ok:
        return jsonify({'error': reason, 'upgrade_url': '/pricing'}), 402  # 402 Payment Required

    data = request.get_json() or {}
    session_id = data.get('session_id')
    gcs_key = data.get('gcs_key')
    show_name = data.get('show_name', 'Live Show')
    channel_db_id = data.get('channel_id')  # DB primary key from channels table
    if not session_id or not gcs_key:
        return jsonify({'error': 'session_id and gcs_key required'}), 400

    # Verify user has access to selected channel (if any)
    channel_yt_id = None
    if channel_db_id:
        ch = autoclip_db.get_channel_by_id(int(channel_db_id))
        if not ch:
            return jsonify({'error': 'channel not found'}), 400
        if user['role'] != 'admin' and not autoclip_db.user_has_channel_access(user['id'], ch['id']):
            return jsonify({'error': 'no access to selected channel'}), 403
        channel_yt_id = ch['youtube_channel_id']

    session = {
        'id': session_id,
        'show_name': show_name,
        'gcs_key': gcs_key,
        'gcs_uri': f"gs://{gcs_helper.BUCKET_NAME}/{gcs_key}",
        'owner_user_id': user['id'],
        'channel_id': int(channel_db_id) if channel_db_id else None,
        'channel_youtube_id': channel_yt_id,
        'transcript': None,
        'segments': [],
        'clips': []
    }
    save_session(session_id, session)

    # Increment session counter for demo cap tracking
    try:
        _db = autoclip_db.get_db()
        _db.execute(
            "UPDATE users SET sessions_created_count = COALESCE(sessions_created_count, 0) + 1 WHERE id=?",
            (user['id'],)
        )
        _db.commit()
    except Exception as _ce:
        app.logger.warning(f'Failed to bump sessions_created_count for user {user["id"]}: {_ce}')

    return jsonify({'session_id': session_id, 'status': 'uploaded'})


@app.route('/api/transcribe/<session_id>', methods=['POST'])
def transcribe(session_id):
    """Transcribe the video using OpenAI Whisper API with chunking for large files."""
    session = load_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404

    if 'gcs_key' in session:
        ext = Path(session['gcs_key']).suffix or '.mp4'
        video_path = str(UPLOAD_DIR / f"{session_id}{ext}")
        if not Path(video_path).exists():
            gcs_helper.download_file(session['gcs_key'], video_path)
    else:
        video_path = session['video_path']

    # Extract audio using ffmpeg - low bitrate mono to minimize file size
    audio_path = str(UPLOAD_DIR / f"{session_id}_audio.mp3")
    subprocess.run([
        'ffmpeg', '-i', video_path,
        '-vn', '-acodec', 'mp3', '-ab', '32k',
        '-ac', '1',
        '-ar', '16000',
        '-y', audio_path
    ], capture_output=True)

    # Get audio duration in seconds
    probe = subprocess.run([
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1', audio_path
    ], capture_output=True, text=True)
    duration = float(probe.stdout.strip())

    # Split into 20-minute chunks to stay well under 25MB
    chunk_duration = 1200
    chunks = []
    start = 0
    chunk_index = 0

    while start < duration:
        chunk_path = str(UPLOAD_DIR / f"{session_id}_chunk_{chunk_index}.mp3")
        subprocess.run([
            'ffmpeg', '-i', audio_path,
            '-ss', str(start),
            '-t', str(chunk_duration),
            '-c', 'copy',
            '-y', chunk_path
        ], capture_output=True)
        chunks.append((chunk_path, start))
        start += chunk_duration
        chunk_index += 1

    # Transcribe each chunk and offset timestamps
    all_segments = []
    full_text = ""

    for chunk_path, time_offset in chunks:
        with open(chunk_path, 'rb') as f:
            response = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="verbose_json",
                timestamp_granularities=["segment"]
            )
        for seg in response.segments:
            all_segments.append({
                'start': seg.start + time_offset,
                'end': seg.end + time_offset,
                'text': seg.text.strip()
            })
        full_text += response.text + " "
        os.remove(chunk_path)

    os.remove(audio_path)

    session['transcript'] = all_segments
    session['full_text'] = full_text.strip()

    # Cost: whisper bills per minute; last segment end == audio duration
    try:
        _owner = session.get('owner_user_id')
        if _owner and all_segments:
            _mins = max(float(sg['end']) for sg in all_segments) / 60.0
            costs.record_whisper(autoclip_db.get_db(), _owner, _mins,
                                 session_id=session_id, detail='whisper-1')
    except Exception:
        app.logger.exception('whisper cost record failed')
    save_session(session_id, session)

    return jsonify({
        'status': 'transcribed',
        'segments_count': len(all_segments),
        'transcript': all_segments
    })


@app.route('/api/detect_segments/<session_id>', methods=['POST'])
def detect_segments(session_id):
    """
    Detect segments by having GPT-4o read the full timestamped transcript.

    Request body may include:
      { "topics": ["Ohio State season outlook", "Miami football preview", ...] }
    If topics are provided, GPT is asked to locate each one. If omitted, GPT
    is asked to auto-discover the show's segments.
    """
    session = load_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404

    transcript_segments = session.get('transcript', [])
    if not transcript_segments:
        return jsonify({'error': 'No transcript available'}), 400

    body = request.get_json(silent=True) or {}
    topics = [t.strip() for t in body.get('topics', []) if t and t.strip()]

    # Build a compact timestamped transcript for the model.
    # Format: "[MM:SS] text" per line. Keeps tokens down vs. JSON.
    def fmt_ts(sec):
        m = int(sec) // 60
        s = int(sec) % 60
        return f"{m:02d}:{s:02d}"

    lines = []
    for seg in transcript_segments:
        lines.append(f"[{fmt_ts(seg['start'])}] {seg['text'].strip()}")
    transcript_text = "\n".join(lines)

    total_duration = transcript_segments[-1]['end']
    total_mmss = fmt_ts(total_duration)

    # Two prompt variants based on whether topics were provided.
    if topics:
        topics_list = "\n".join(f"- {t}" for t in topics)
        prompt = f"""You are analyzing a college football podcast transcript. Each line is prefixed with the timestamp when it was spoken, in [MM:SS] format. Total duration: {total_mmss}.

The user has listed the topics that were discussed, in the order they appear in the show. For EACH topic, find:
1. The moment the host begins discussing it (start_time in seconds).
2. The moment the host stops discussing it and moves to the next topic (end_time in seconds).

Rules:
- The start of topic N is the end of topic N-1.
- STRONG SIGNAL: the hosts on this show reliably mark segment boundaries with phrases like "transition to", "let's transition", "transition over to", "our next segment", "final segment", "let's talk about", "let's jump to", "moving on to". When you see any of these, treat the timestamp AFTER the transition phrase completes as a very likely segment boundary.
- If a strong signal phrase doesn't appear near where you'd expect a topic to start, fall back to content-based judgment (a shift in team names, subject, or discussion focus).
- Ignore ad reads, sponsor mentions, and channel promos - these are not topics.
- Timestamps must be in seconds (integers). Convert from [MM:SS] format: 2:06 = 126, 20:47 = 1247, etc.
- The final topic ends at the total show duration ({int(total_duration)} seconds) or when the host says goodbye.

Topics to locate (in order):
{topics_list}

Transcript:
{transcript_text}

Return ONLY a JSON object with this structure:
{{
  "segments": [
    {{"topic": "...", "start_time": 0, "end_time": 126, "summary": "2-3 sentence summary of what was actually discussed"}}
  ]
}}

Return exactly {len(topics)} segments, one per topic listed, in the same order."""
    else:
        prompt = f"""You are analyzing a college football podcast transcript. Each line is prefixed with the timestamp when it was spoken, in [MM:SS] format. Total duration: {total_mmss}.

Identify the distinct topical segments of the show. A segment is a sustained discussion of one team, matchup, story, or theme. Skip:
- Intros, outros, and channel promos
- Ad reads and sponsor mentions
- Brief tangents that return to the main topic

For each real segment, provide:
1. A short topic name (3-6 words)
2. start_time in seconds (integer)
3. end_time in seconds (integer)
4. A 2-3 sentence summary

Transcript:
{transcript_text}

Return ONLY a JSON object:
{{
  "segments": [
    {{"topic": "Short topic name", "start_time": 0, "end_time": 126, "summary": "..."}}
  ]
}}"""

    try:
        response = tracked_chat(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        result = json.loads(response.choices[0].message.content)
        raw_segs = result.get('segments', [])
    except Exception as e:
        return jsonify({'error': f'AI detection failed: {str(e)}'}), 500

    # Normalize and generate a suggested title per segment (second, cheap call each).
    segments = []
    for i, seg in enumerate(raw_segs):
        try:
            start_time = float(seg.get('start_time', 0))
            end_time = float(seg.get('end_time', start_time + 60))
        except (TypeError, ValueError):
            start_time = 0.0
            end_time = 60.0
        topic = seg.get('topic', f'Segment {i+1}')
        summary = seg.get('summary', '')

        # Generate title from topic + summary.
        title_prompt = f"""Generate a YouTube video title for a college football podcast segment.

Topic: {topic}
Summary: {summary}

Style: power words in ALL CAPS for emphasis, team names included, ends with ! or ? for engagement. 50-70 characters ideal.

Return ONLY JSON: {{"title": "..."}}"""
        try:
            title_resp = tracked_chat(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": title_prompt}],
                response_format={"type": "json_object"},
                temperature=0.7,
            )
            title = json.loads(title_resp.choices[0].message.content).get('title', topic)
        except Exception:
            title = topic

        segments.append({
            'topic': topic,
            'start_time': start_time,
            'end_time': end_time,
            'summary': summary,
            'title': title,
        })

    session['segments'] = segments
    save_session(session_id, session)
    return jsonify({'status': 'detected', 'segments': segments})


@app.route('/api/generate_metadata/<session_id>', methods=['POST'])
def generate_metadata(session_id):
    """Generate title and description for a specific segment."""
    session = load_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404

    data = request.json
    segment_index = data.get('segment_index', 0)
    segment = session['segments'][segment_index]

    prompt = f"""You are a YouTube content creator for a college football podcast.

{TITLE_STYLE_GUIDE}

Segment topic: {segment.get('topic', '')}
Segment summary: {segment.get('summary', '')}

Generate a title and description. Return ONLY JSON:
{{
  "title": "...",
  "description": "..."
}}

For the description:
- Start with a compelling 2-3 sentence hook about the topic
- Include relevant hashtags at the end (#CollegeFootball #CFB etc)
- Mention to like, subscribe, and turn on notifications
- Keep it under 500 words"""

    response = tracked_chat(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"}
    )

    result = json.loads(response.choices[0].message.content)

    session['segments'][segment_index]['title'] = result.get('title', '')
    session['segments'][segment_index]['description'] = result.get('description', '')
    save_session(session_id, session)

    return jsonify({'status': 'generated', 'title': result.get('title'), 'description': result.get('description')})


def get_local_video_path(session):
    """Return a local filesystem path to the session's video, downloading from GCS if needed."""
    if 'gcs_key' in session:
        ext = Path(session['gcs_key']).suffix or '.mp4'
        local = str(UPLOAD_DIR / f"{session['id']}{ext}")
        if not Path(local).exists():
            gcs_helper.download_file(session['gcs_key'], local)
        _sweep_uploads_dir()  # opportunistic LRU sweep
        return local
    return session['video_path']


# __AUTOCLIP_UPLOADS_SWEEP_V42__
def _sweep_uploads_dir(max_age_hours=1, min_free_gb=2.0):
    """Delete files in UPLOAD_DIR older than max_age_hours.

    Also does an emergency sweep (delete ANY files) if disk free is under min_free_gb.
    Silent — never raises. Runs on every session-video-fetch.
    """
    import time
    import shutil as _sh
    try:
        cutoff = time.time() - (max_age_hours * 3600)
        # Emergency mode: if free space is critical, be more aggressive
        try:
            free_gb = _sh.disk_usage(str(UPLOAD_DIR)).free / (1024 ** 3)
        except Exception:
            free_gb = 999.0
        emergency = free_gb < min_free_gb
        for f in Path(UPLOAD_DIR).iterdir():
            if not f.is_file():
                continue
            try:
                mtime = f.stat().st_mtime
                if emergency or mtime < cutoff:
                    f.unlink()
                    app.logger.info(f'uploads sweep: removed {f.name} (age={int((time.time()-mtime)/60)}min emergency={emergency})')
            except Exception as _e:
                app.logger.warning(f'uploads sweep: failed to remove {f.name}: {_e}')
    except Exception as _e:
        app.logger.warning(f'uploads sweep failed: {_e}')

@app.route('/api/clip/<session_id>', methods=['POST'])
def create_clip(session_id):
    """Cut a clip from the source. Writes to GCS, deletes local temp."""
    session = load_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404
    data = request.get_json() or {}
    segment_index = int(data.get('segment_index', 0))
    if segment_index >= len(session.get('segments', [])):
        return jsonify({'error': 'segment_index out of range'}), 400

    segment = session['segments'][segment_index]
    yt_channel_id = session.get('channel_youtube_id')
    if not yt_channel_id:
        return jsonify({'error': 'Session has no channel_youtube_id. Legacy session — reupload to a channel.'}), 400

    start = float(segment.get('start_time') or segment.get('start') or 0)
    end = float(segment.get('end_time') or segment.get('end') or 0)
    duration = end - start

    # Cut into a temp file, then upload to GCS, then clean up local.
    with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as tmp:
        clip_tmp = tmp.name
    try:
        result = subprocess.run([
            'ffmpeg',
            '-ss', str(start),
            '-i', get_local_video_path(session),
            '-t', str(duration),
            '-c:v', 'copy',
            '-c:a', 'copy',
            '-avoid_negative_ts', 'make_zero',
            '-y', clip_tmp
        ], capture_output=True, text=True)
        if result.returncode != 0:
            return jsonify({'error': f'ffmpeg failed: {result.stderr[-500:]}'}), 500

        # Upload to GCS
        gcs_key = gcs_storage.clip_key(yt_channel_id, session_id, segment_index)
        gcs_storage.upload_file_to_gcs(clip_tmp, gcs_key, content_type='video/mp4')

        segment['clip_gcs_key'] = gcs_key
        segment['clip_file'] = f'{session_id}_clip_{segment_index}.mp4'  # kept for backward-compat URL routing
        # Retire local clip_path — no longer stored locally
        segment['clip_path'] = None
    finally:
        try:
            os.unlink(clip_tmp)
        except Exception:
            pass

    save_session(session_id, session)

    # Bump usage counter (same logic as before)
    try:
        _u = autoclip_auth.get_current_user()
        if _u:
            _period = _current_period()
            _db = autoclip_db.get_db()
            _db.execute(
                'INSERT INTO usage_monthly (user_id, period, clipping_clips) VALUES (?, ?, 1) '
                'ON CONFLICT(user_id, period) DO UPDATE SET clipping_clips = clipping_clips + 1',
                (_u['id'], _period)
            )
            _db.commit()
    except Exception:
        pass

    return jsonify({'status': 'clipped', 'clip_file': segment['clip_file']})

@app.route('/api/insert_ad/<session_id>', methods=['POST'])
def insert_ad(session_id):
    """Insert an ad read into a clip at the best spot in the first 3 minutes."""
    session = load_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404

    data = request.json
    segment_index = data.get('segment_index', 0)
    ad_filename = data.get('ad_filename')

    segment = session['segments'][segment_index]
    clip_path = segment.get('clip_path')
    ad_path = str(ADS_DIR / ad_filename)

    if not clip_path or not os.path.exists(clip_path):
        return jsonify({'error': 'Clip not found. Create clip first.'}), 400

    if not os.path.exists(ad_path):
        return jsonify({'error': 'Ad file not found'}), 400

    # Find best insertion point using silence detection in first 3 minutes
    silence_result = subprocess.run([
        'ffmpeg', '-i', clip_path,
        '-t', '180',  # First 3 minutes
        '-af', 'silencedetect=noise=-30dB:d=0.5',
        '-f', 'null', '-'
    ], capture_output=True, text=True)

    # Parse silence points from ffmpeg output
    import re
    silence_ends = re.findall(r'silence_end: (\d+\.?\d*)', silence_result.stderr)
    
    # Pick the last silence point in first 3 min, default to 60s
    insert_point = 60.0
    if silence_ends:
        candidates = [float(t) for t in silence_ends if float(t) < 170]
        if candidates:
            insert_point = candidates[-1]

    # Build the final clip with ad inserted using ffmpeg concat
    output_filename = f"{session_id}_clip_{segment_index}_with_ad.mp4"
    output_path = str(CLIPS_DIR / output_filename)

    # Split clip at insert point, concatenate with ad
    part1 = str(CLIPS_DIR / f"temp_{session_id}_{segment_index}_part1.mp4")
    part2 = str(CLIPS_DIR / f"temp_{session_id}_{segment_index}_part2.mp4")
    concat_list = str(CLIPS_DIR / f"temp_{session_id}_{segment_index}_concat.txt")

    # Cut part 1
    subprocess.run(['ffmpeg', '-i', clip_path, '-t', str(insert_point), '-c', 'copy', '-y', part1], capture_output=True)
    # Cut part 2
    subprocess.run(['ffmpeg', '-i', clip_path, '-ss', str(insert_point), '-c', 'copy', '-y', part2], capture_output=True)

    # Write concat file
    with open(concat_list, 'w') as f:
        f.write(f"file '{part1}'\n")
        f.write(f"file '{ad_path}'\n")
        f.write(f"file '{part2}'\n")

    # Concatenate
    result = subprocess.run([
        'ffmpeg', '-f', 'concat', '-safe', '0',
        '-i', concat_list,
        '-c:v', 'libx264', '-c:a', 'aac',
        '-y', output_path
    ], capture_output=True, text=True)

    # Clean up temp files
    for f in [part1, part2, concat_list]:
        if os.path.exists(f):
            os.remove(f)

    if result.returncode != 0:
        return jsonify({'error': f'Ad insertion failed: {result.stderr}'}), 500

    segment['clip_with_ad'] = output_filename
    segment['clip_with_ad_path'] = output_path
    segment['ad_insert_point'] = insert_point
    save_session(session_id, session)

    return jsonify({
        'status': 'ad_inserted',
        'insert_point': insert_point,
        'output_file': output_filename
    })




# ============================================================================
# YouTube publish settings helper
# ============================================================================

# Standard YouTube category IDs. Full list at:
# https://developers.google.com/youtube/v3/docs/videoCategories/list
YT_CATEGORIES = {
    '17': 'Sports',
    '24': 'Entertainment',
    '25': 'News & Politics',
    '27': 'Education',
    '20': 'Gaming',
    '22': 'People & Blogs',
    '23': 'Comedy',
    '10': 'Music',
    '15': 'Pets & Animals',
    '19': 'Travel & Events',
    '28': 'Science & Technology',
}


def _apply_publish_settings(data, snippet, status_dict):
    """Mutate snippet and status_dict based on user-provided publish settings.

    Data fields consumed:
      - category_id (string, default '17')
      - tags (list of strings, or comma-separated string)
      - made_for_kids (bool)
      - publish_at (ISO 8601 datetime string, e.g. '2026-08-15T14:00:00Z')
      - monetize (bool)

    Returns a dict of "post-upload actions" to run after upload:
      - playlist_ids (list of str)
    """
    # Category
    cat = str(data.get('category_id', '17')).strip()
    if cat and cat in YT_CATEGORIES:
        snippet['categoryId'] = cat
    else:
        snippet['categoryId'] = '17'  # Sports default

    # Tags — accept list or comma-separated string
    tags = data.get('tags', [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(',') if t.strip()]
    if tags:
        snippet['tags'] = tags[:500]  # YouTube max 500 char total, ~15 tags realistically

    # Made for Kids
    status_dict['selfDeclaredMadeForKids'] = bool(data.get('made_for_kids', False))

    # Scheduled publish — if set, force privacy to private and set publishAt
    publish_at = data.get('publish_at', '').strip() if isinstance(data.get('publish_at'), str) else None
    if publish_at:
        status_dict['privacyStatus'] = 'private'
        status_dict['publishAt'] = publish_at

    # Monetization — API sets monetizationDetails.access; server-side review still runs
    # Only relevant for channels in YPP.
    post_actions = {
        'playlist_ids': data.get('playlist_ids', []) or [],
        'monetize': bool(data.get('monetize', False)),
    }
    return post_actions


# ============================================================================
# __AUTOCLIP_COMPOSE_HELPERS_V3B__
# Ad composition at publish time. Called by /api/upload_youtube.
# ============================================================================

def _probe_has_video(media_path):
    """Return True if the file has a video stream (not audio-only)."""
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=codec_type', '-of', 'csv=p=0', media_path],
            capture_output=True, text=True, timeout=15
        )
        return r.stdout.strip() == 'video'
    except Exception:
        return False


def _wrap_audio_as_video(audio_path, out_path, target_w=1920, target_h=1080, target_fps=30):
    """Convert an audio-only ad to a black-background video for concat compatibility."""
    subprocess.run([
        'ffmpeg',
        '-f', 'lavfi', '-i', f'color=c=black:s={target_w}x{target_h}:r={target_fps}',
        '-i', audio_path,
        '-map', '0:v', '-map', '1:a',
        '-c:v', 'libx264', '-preset', 'medium', '-crf', '23',
        '-c:a', 'aac', '-b:a', '192k',
        '-pix_fmt', 'yuv420p',
        '-shortest', '-y', out_path
    ], capture_output=True, check=True, timeout=300)
    return out_path


def _find_silence_split(clip_path, window_secs=180, fallback=60.0):
    """Find the last silence-end point in the first `window_secs` of the clip.
    Falls back to 60s if no silence found or ffmpeg errors."""
    import re as _re
    try:
        r = subprocess.run(
            ['ffmpeg', '-i', clip_path,
             '-t', str(window_secs),
             '-af', 'silencedetect=noise=-30dB:d=0.5',
             '-f', 'null', '-'],
            capture_output=True, text=True, timeout=120
        )
        ends = _re.findall(r'silence_end: (\d+\.?\d*)', r.stderr or '')
        candidates = [float(t) for t in ends if float(t) < window_secs - 10]
        if candidates:
            return candidates[-1]
    except Exception:
        pass
    return fallback


def _compose_clip_with_ads(session, segment_index, clip_path, work_dir):
    """If the segment has intro/outro/mid ad IDs set, produce a composed MP4
    with the ads baked in and return its path. Return None if no ads set.

    Raises RuntimeError with a truncated ffmpeg stderr on failure.
    """
    segment = session['segments'][segment_index]
    intro_ad_id = segment.get('intro_ad_id')
    outro_ad_id = segment.get('outro_ad_id')
    mid_ad_id = segment.get('mid_ad_id')
    mid_pos = segment.get('mid_ad_position_sec')

    if not (intro_ad_id or outro_ad_id or mid_ad_id):
        return None

    if not os.path.exists(clip_path):
        raise RuntimeError(f'clip file not found: {clip_path}')

    os.makedirs(work_dir, exist_ok=True)
    db = autoclip_db.get_db()

    def _fetch_and_normalize(ad_id, tag):
        if not ad_id:
            return None
        row = db.execute("SELECT * FROM ads WHERE id=?", (ad_id,)).fetchone()
        if not row:
            raise RuntimeError(f'{tag} ad_id={ad_id} not found in DB')
        gcs_key = row['gcs_key']
        ext = os.path.splitext(gcs_key)[1] or '.mp4'
        raw = os.path.join(work_dir, f'ad_{tag}_raw{ext}')
        try:
            gcs_storage.download_from_gcs(gcs_key, raw)
        except Exception as e:
            raise RuntimeError(f'{tag} GCS download failed: {e}')
        if _probe_has_video(raw):
            return raw
        video_path = os.path.join(work_dir, f'ad_{tag}_video.mp4')
        try:
            _wrap_audio_as_video(raw, video_path)
        except subprocess.CalledProcessError as e:
            tail = (e.stderr or b'')[-500:].decode('utf-8', errors='replace')
            raise RuntimeError(f'{tag} audio-to-video failed: {tail}')
        return video_path

    intro_path = _fetch_and_normalize(intro_ad_id, 'intro')
    mid_path   = _fetch_and_normalize(mid_ad_id, 'mid')
    outro_path = _fetch_and_normalize(outro_ad_id, 'outro')

    # Determine mid split position
    if mid_path and (mid_pos is None or mid_pos == ''):
        mid_pos = _find_silence_split(clip_path)

    # Split the clip if mid ad present; re-encode both halves for clean cuts
    if mid_path:
        part1 = os.path.join(work_dir, 'clip_part1.mp4')
        part2 = os.path.join(work_dir, 'clip_part2.mp4')
        try:
            subprocess.run(
                ['ffmpeg', '-i', clip_path, '-t', str(mid_pos),
                 '-c:v', 'libx264', '-preset', 'medium', '-crf', '23',
                 '-c:a', 'aac', '-b:a', '192k',
                 '-pix_fmt', 'yuv420p',
                 '-y', part1],
                capture_output=True, check=True, timeout=1800
            )
            subprocess.run(
                ['ffmpeg', '-i', clip_path, '-ss', str(mid_pos),
                 '-c:v', 'libx264', '-preset', 'medium', '-crf', '23',
                 '-c:a', 'aac', '-b:a', '192k',
                 '-pix_fmt', 'yuv420p',
                 '-y', part2],
                capture_output=True, check=True, timeout=1800
            )
        except subprocess.CalledProcessError as e:
            tail = (e.stderr or b'')[-500:].decode('utf-8', errors='replace')
            raise RuntimeError(f'clip split failed: {tail}')
        clip_segments = [part1, mid_path, part2]
    else:
        clip_segments = [clip_path]

    ordered = []
    if intro_path: ordered.append(intro_path)
    ordered.extend(clip_segments)
    if outro_path: ordered.append(outro_path)

    if len(ordered) <= 1:
        # Nothing to compose (shouldn't happen given the early-return above, but safe)
        return None

    output_path = os.path.join(work_dir, 'composed.mp4')
    input_args = []
    for p in ordered:
        input_args.extend(['-i', p])
    n = len(ordered)
    stream_pairs = ''.join(f'[{i}:v:0][{i}:a:0]' for i in range(n))
    filter_str = f'{stream_pairs}concat=n={n}:v=1:a=1[outv][outa]'

    try:
        subprocess.run(
            ['ffmpeg', *input_args,
             '-filter_complex', filter_str,
             '-map', '[outv]', '-map', '[outa]',
             '-c:v', 'libx264', '-preset', 'medium', '-crf', '23',
             '-c:a', 'aac', '-b:a', '192k',
             '-pix_fmt', 'yuv420p',
             '-y', output_path],
            capture_output=True, check=True, timeout=1800
        )
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or b'')[-500:].decode('utf-8', errors='replace')
        raise RuntimeError(f'ad+clip concat failed: {tail}')

    return output_path





# __AUTOCLIP_YT_FINISHER_V42__
# ============================================================================
# Phase 4.2 — Background YouTube finisher
# ============================================================================
# The Cloud Run worker composes the video (with NVENC on GPU) but can't
# upload to YouTube because YouTube OAuth tokens live only on the VM.
# So the worker marks the job stage='compose_done' and uploads the composed
# mp4 to GCS. This background thread watches for those jobs and finishes
# them: downloads composed, uploads to YouTube, updates DB, cleans up GCS.

import threading as _threading_v42y
import time as _time_v42y
import tempfile as _tempfile_v42y
import os as _os_v42y
import json as _json_v42y
import traceback as _traceback_v42y

_YT_FINISHER_STARTED = False
_YT_FINISHER_LOCK = _threading_v42y.Lock()


def _finish_publish_job(job_id):
    """Download composed mp4 from GCS, upload to YouTube, mark job done."""
    with app.app_context():
        db = autoclip_db.get_db()
        row = db.execute(
            "SELECT id, session_id, segment_index, user_id, privacy, publish_payload, "
            "       composed_gcs_key, composed_gcs_bucket "
            "FROM publish_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if not row:
            app.logger.warning(f'finisher: job {job_id} vanished')
            return
        job = dict(row)

        composed_key = job['composed_gcs_key']
        composed_bucket = job['composed_gcs_bucket'] or 'autoclip-uploads'
        if not composed_key:
            db.execute(
                "UPDATE publish_jobs SET status='failed', error=?, finished_at=CURRENT_TIMESTAMP WHERE id=?",
                ('No composed_gcs_key on job', job_id))
            db.commit()
            return

        try:
            session = load_session(job['session_id'])
            segment = session['segments'][job['segment_index']]
            payload = _json_v42y.loads(job['publish_payload'] or '{}')

            # Download the composed file
            db.execute("UPDATE publish_jobs SET stage='downloading_composed', progress_pct=93, heartbeat_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
            db.commit()

            _tmp = _tempfile_v42y.mkdtemp(prefix='autoclip_finisher_')
            local_composed = _os_v42y.path.join(_tmp, 'composed.mp4')
            gcs_storage.download_from_gcs(composed_key, local_composed)

            # Cost: pulling the composed file out of GCS is billable egress
            try:
                _gb = _os_v42y.path.getsize(local_composed) / (1024 ** 3)
                _uid = job.get('user_id') if hasattr(job, 'get') else job['user_id']
                if _uid:
                    costs.record_egress(db, _uid, _gb,
                                        session_id=job['session_id'],
                                        segment_index=job['segment_index'],
                                        detail='composed->youtube')
            except Exception:
                app.logger.exception('egress cost record failed')

            # Do the YouTube upload — mirror worker.py logic
            db.execute("UPDATE publish_jobs SET stage='uploading_youtube', progress_pct=95, heartbeat_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
            db.commit()

            import googleapiclient.discovery
            import google.oauth2.credentials
            import google.auth.transport.requests
            from googleapiclient.http import MediaFileUpload

            yt_channel_id = session.get('channel_youtube_id')
            if not yt_channel_id:
                raise RuntimeError('Session has no channel_youtube_id')

            ch = db.execute(
                "SELECT * FROM channels WHERE youtube_channel_id=?", (yt_channel_id,)
            ).fetchone()
            if not ch:
                raise RuntimeError(f'Channel {yt_channel_id} not found')
            token_path = _os_v42y.path.join(_os_v42y.path.dirname(_os_v42y.path.abspath(__file__)), ch['token_path'])
            if not _os_v42y.path.exists(token_path):
                raise RuntimeError(f'Token file missing: {token_path}')
            with open(token_path) as _f:
                token_data = _json_v42y.load(_f)
            creds = google.oauth2.credentials.Credentials(
                token=token_data.get('token'),
                refresh_token=token_data.get('refresh_token'),
                token_uri=token_data.get('token_uri'),
                client_id=token_data.get('client_id'),
                client_secret=token_data.get('client_secret'),
                scopes=token_data.get('scopes'),
            )
            if not creds.valid:
                creds.refresh(google.auth.transport.requests.Request())
                token_data['token'] = creds.token
                with open(token_path, 'w') as _f:
                    _json_v42y.dump(token_data, _f)

            yt = googleapiclient.discovery.build('youtube', 'v3', credentials=creds)

            # __AUTOCLIP_FINISHER_FULL_YT_V42__
            # Pull the rich YT settings the frontend saved on the segment
            _yt = segment.get('youtube', {}) or {}

            title = payload.get('title') or _yt.get('title') or segment.get('title') or 'Untitled'
            # AI/user text, then compose with ad blurbs + channel base description
            _ai_desc = payload.get('description') or _yt.get('description') or segment.get('description') or ''
            try:
                description = compose_youtube_description(session, segment, ai_text=_ai_desc)
            except Exception as _e:
                app.logger.error('description compose failed, using raw: %s', _e)
                description = _ai_desc
            privacy = payload.get('privacy') or _yt.get('privacy') or job.get('privacy') or 'private'
            tags = payload.get('tags') or _yt.get('tags') or []
            category_id = str(payload.get('category_id') or _yt.get('category_id') or 22)
            made_for_kids = bool(_yt.get('made_for_kids', False))
            monetize = bool(_yt.get('monetize', False))
            publish_at = _yt.get('publish_at')  # ISO-8601 string like "2025-08-15T12:00:00Z"
            _playlist_ids_final = _yt.get('playlist_ids') or []

            body = {
                'snippet': {
                    'title': title[:100],
                    'description': description,
                    'tags': tags,
                    'categoryId': category_id,
                },
                'status': {
                    'privacyStatus': privacy,
                    'selfDeclaredMadeForKids': made_for_kids,
                }
            }
            # Scheduled publish: only meaningful when video is Private and publish_at is set
            if publish_at and privacy == 'private':
                body['status']['publishAt'] = publish_at
            # Monetization is NOT settable via videos.insert. Sending
            # monetizationDetails makes the client library derive
            # part=...,monetization_details, which the API rejects with
            # 400 unexpectedPart and fails the whole upload. It is a
            # YouTube Studio / Content Owner setting. We record the user's
            # intent and surface it in the UI instead.
            if monetize:
                app.logger.info(
                    'Job %s: monetize requested - not settable via API, '
                    'user must enable it in YouTube Studio', job_id)

            media = MediaFileUpload(local_composed, mimetype='video/mp4', resumable=True, chunksize=8*1024*1024)
            req = yt.videos().insert(part='snippet,status', body=body, media_body=media)

            response = None
            last_pct = 95
            while response is None:
                status_, response = req.next_chunk()
                if status_:
                    p = int(95 + status_.progress() * 4)
                    if p != last_pct:
                        db.execute("UPDATE publish_jobs SET progress_pct=?, heartbeat_at=CURRENT_TIMESTAMP WHERE id=?", (p, job_id))
                        db.commit()
                        last_pct = p

            video_id = response.get('id')
            if not video_id:
                raise RuntimeError(f'YouTube returned no video id: {response}')

            # Persist video_id on segment
            session = load_session(job['session_id'])
            session['segments'][job['segment_index']]['youtube_video_id'] = video_id
            save_session(job['session_id'], session)

            # Set the custom thumbnail. YouTube requires a separate API call
            # after upload - this was never implemented in the async path, so
            # every clip published with the auto-generated YouTube frame.
            try:
                _seg_t = session['segments'][job['segment_index']]
                _tkey = _seg_t.get('thumbnail_gcs_key')
                if _tkey:
                    _tdir = _tempfile_v42y.mkdtemp(prefix='autoclip_thumb_')
                    _tlocal = _os_v42y.path.join(
                        _tdir, 'thumb' + (_os_v42y.path.splitext(_tkey)[1] or '.png'))
                    gcs_storage.download_from_gcs(_tkey, _tlocal)
                    yt.thumbnails().set(
                        videoId=video_id,
                        media_body=MediaFileUpload(_tlocal)
                    ).execute()
                    app.logger.info('Finisher: thumbnail set on %s from %s',
                                    video_id, _tkey)
                    try:
                        import shutil as _sh_t
                        _sh_t.rmtree(_tdir, ignore_errors=True)
                    except Exception:
                        pass
                else:
                    app.logger.info('Finisher: no thumbnail_gcs_key on segment, skipping')
            except Exception as _te:
                app.logger.error('Finisher: thumbnail set failed for %s: %s', video_id, _te)

            db.execute(
                "UPDATE publish_jobs "
                "SET status='done', stage='complete', progress_pct=100, "
                "    youtube_video_id=?, finished_at=CURRENT_TIMESTAMP, heartbeat_at=CURRENT_TIMESTAMP "
                "WHERE id=?", (video_id, job_id))
            db.commit()
            app.logger.info(f'Finisher: job {job_id} done, video_id={video_id}')

            # Add to playlists (best-effort, ignore failures)
            if _playlist_ids_final:
                for _pid in _playlist_ids_final:
                    try:
                        yt.playlistItems().insert(
                            part='snippet',
                            body={
                                'snippet': {
                                    'playlistId': _pid,
                                    'resourceId': {
                                        'kind': 'youtube#video',
                                        'videoId': video_id,
                                    }
                                }
                            }
                        ).execute()
                        app.logger.info(f'Added job {job_id} video {video_id} to playlist {_pid}')
                    except Exception as _pe:
                        app.logger.warning(f'Failed to add to playlist {_pid}: {_pe}')

            # Cleanup GCS temp composed
            try:
                gcs_storage.delete_from_gcs(composed_key, bucket_name=composed_bucket)
            except AttributeError:
                # If delete helper doesn't exist, cleanup manually via client
                try:
                    from google.cloud import storage as _storage
                    _storage.Client().bucket(composed_bucket).blob(composed_key).delete()
                except Exception as _de:
                    app.logger.warning(f'Failed to delete GCS temp {composed_key}: {_de}')
            except Exception as _de:
                app.logger.warning(f'Failed to delete GCS temp: {_de}')

            # Cleanup local temp
            try:
                import shutil as _sh
                _sh.rmtree(_tmp, ignore_errors=True)
            except Exception:
                pass

        except Exception as _e:
            tb = _traceback_v42y.format_exc()
            app.logger.exception(f'Finisher: job {job_id} FAILED')
            try:
                db.execute(
                    "UPDATE publish_jobs SET status='failed', error=?, finished_at=CURRENT_TIMESTAMP WHERE id=?",
                    (f'YouTube upload failed: {_e}\n{tb[-1500:]}', job_id))
                db.commit()
            except Exception:
                app.logger.exception('Also failed to update job status')


def _yt_finisher_loop():
    """Poll for compose_done jobs, run finisher on each."""
    app.logger.info('YT finisher thread started')
    while True:
        try:
            with app.app_context():
                db = autoclip_db.get_db()
                row = db.execute(
                    "SELECT id FROM publish_jobs "
                    "WHERE stage='compose_done' AND status='running' "
                    "ORDER BY created_at ASC LIMIT 1"
                ).fetchone()
                if row:
                    _finish_publish_job(row['id'])
                else:
                    _time_v42y.sleep(5)
        except Exception:
            app.logger.exception('YT finisher loop error')
            _time_v42y.sleep(10)


def _start_yt_finisher_once():
    global _YT_FINISHER_STARTED
    with _YT_FINISHER_LOCK:
        if _YT_FINISHER_STARTED:
            return
        _YT_FINISHER_STARTED = True
        t = _threading_v42y.Thread(target=_yt_finisher_loop, name='yt-finisher', daemon=True)
        t.start()


# Start the finisher on first HTTP request (Flask 3.x compatible pattern)
@app.before_request
def _yt_finisher_startup_hook():
    _start_yt_finisher_once()


# __AUTOCLIP_CLOUD_TASKS_V42__
# ============================================================================
# Phase 4.2 — Cloud Tasks + Cloud Run worker integration
# ============================================================================
import os as _os_v42
import json as _json_v42


def _enqueue_publish_task(job_id, session_id, segment_index, segment, session):
    """Push a task to Cloud Tasks that will call our Cloud Run worker.

    The task payload contains all GCS keys the worker needs to fetch inputs.
    """
    from google.cloud import tasks_v2

    project = _os_v42.environ.get('CLOUD_TASKS_PROJECT', 'youtube-podcast-sync-502121')
    location = _os_v42.environ.get('CLOUD_TASKS_LOCATION', 'us-east4')
    queue = _os_v42.environ.get('CLOUD_TASKS_QUEUE', 'autoclip-jobs')
    worker_url = _os_v42.environ.get('CLOUD_RUN_WORKER_URL', '').rstrip('/')
    invoker_sa = _os_v42.environ.get('CLOUD_TASKS_INVOKER_SA', '')

    if not worker_url or not invoker_sa:
        raise RuntimeError('CLOUD_RUN_WORKER_URL and CLOUD_TASKS_INVOKER_SA env vars required')

    # Resolve ad GCS keys via DB
    db = autoclip_db.get_db()
    def _ad_key(ad_id):
        if not ad_id:
            return None
        row = db.execute("SELECT gcs_key FROM ads WHERE id=?", (ad_id,)).fetchone()
        return row['gcs_key'] if row else None

    payload = {
        'job_id': job_id,
        'session_id': session_id,
        'segment_index': segment_index,
        'clip_gcs_key': segment.get('clip_gcs_key'),
        'intro_ad_gcs_key': _ad_key(segment.get('intro_ad_id')),
        'mid_ad_gcs_key': _ad_key(segment.get('mid_ad_id')),
        'outro_ad_gcs_key': _ad_key(segment.get('outro_ad_id')),
        'mid_ad_position_sec': segment.get('mid_ad_position_sec'),
        'extra_mid_ad_gcs_keys': [
            _ad_key(a) for a in (segment.get('extra_mid_ad_ids') or [])
        ],
        'extra_mid_positions': segment.get('extra_mid_positions') or [],
    }

    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(project, location, queue)

    task = {
        'http_request': {
            'http_method': tasks_v2.HttpMethod.POST,
            'url': worker_url + '/',
            'headers': {'Content-Type': 'application/json'},
            'body': _json_v42.dumps(payload).encode('utf-8'),
            'oidc_token': {
                'service_account_email': invoker_sa,
                'audience': worker_url,
            },
        },
        'dispatch_deadline': {'seconds': 1800},
    }

    resp = client.create_task(parent=parent, task=task)
    app.logger.info(f'Cloud Task enqueued: {resp.name} for job {job_id}')
    return resp.name


@app.route('/api/publish_jobs/<int:job_id>/worker_update', methods=['POST'])
def publish_job_worker_update(job_id):
    """Callback endpoint the Cloud Run worker uses to report status.

    Auth via X-Worker-Secret header (matches env var WORKER_SHARED_SECRET).
    """
    secret = _os_v42.environ.get('WORKER_SHARED_SECRET', '')
    got = request.headers.get('X-Worker-Secret', '')
    if not secret or got != secret:
        app.logger.warning(f'Rejected worker_update for job {job_id}: bad secret')
        return jsonify({'error': 'unauthorized'}), 401

    fields = request.get_json(silent=True) or {}
    allowed = {'status', 'stage', 'progress_pct', 'error',
               'composed_gcs_key', 'composed_gcs_bucket',
               'youtube_video_id', 'heartbeat_at'}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return jsonify({'error': 'no valid fields'}), 400

    # Always bump heartbeat
    updates['heartbeat_at'] = 'CURRENT_TIMESTAMP'

    # Auto-set finished_at when reaching a terminal status
    if updates.get('status') in ('done', 'failed'):
        updates['finished_at'] = 'CURRENT_TIMESTAMP'

    db = autoclip_db.get_db()
    pieces = []
    vals = []
    for k, v in updates.items():
        if v == 'CURRENT_TIMESTAMP':
            pieces.append(f'{k}=CURRENT_TIMESTAMP')
        else:
            pieces.append(f'{k}=?')
            vals.append(v)
    vals.append(job_id)
    n = db.execute(
        f'UPDATE publish_jobs SET {", ".join(pieces)} WHERE id=?',
        vals
    ).rowcount
    db.commit()

    if n == 0:
        return jsonify({'error': 'job not found'}), 404

    app.logger.info(f'worker_update job {job_id}: {list(updates.keys())}')
    return jsonify({'ok': True})


# __AUTOCLIP_ASYNC_PUBLISH_V41__
# ============================================================================
# Phase 4.1 — Async publish endpoints
# ============================================================================

@app.route('/api/publish_async/<session_id>', methods=['POST'])
def publish_async_create(session_id):
    """Enqueue a publish job. Returns {job_id} immediately.

    Body (JSON, all optional):
      segment_index (default 0)
      title, description, tags, category_id, privacy
    """
    import json as _json
    user = autoclip_auth.get_current_user()

    # __AUTOCLIP_TRIAL_GATE_PUBLISH_V1__
    _db_gate = autoclip_db.get_db()
    _month_count = plans.video_usage_this_month(_db_gate, user["id"])
    _ok, _reason = plans.can_publish_video(user, _month_count)
    if not _ok:
        return jsonify({"error": _reason, "upgrade_url": "/pricing"}), 402

    payload = request.get_json(silent=True) or {}
    segment_index = int(payload.get('segment_index', 0))

    session = load_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404
    if segment_index < 0 or segment_index >= len(session.get('segments', [])):
        return jsonify({'error': 'segment_index out of range'}), 400

    # Access check via channel
    channel_yt_id = session.get('channel_youtube_id')
    if channel_yt_id and user['role'] != 'admin':
        _db = autoclip_db.get_db()
        _ch = _db.execute(
            "SELECT id FROM channels WHERE youtube_channel_id=?", (channel_yt_id,)
        ).fetchone()
        if _ch and not autoclip_db.user_has_channel_access(user['id'], _ch['id']):
            return jsonify({'error': 'forbidden'}), 403

    # Persist the YouTube publish settings onto the segment so the finisher
    # can read them. publishAsync used to send only title/description/privacy,
    # so segment['youtube'] stayed empty and monetization, playlists,
    # category, tags and scheduling were all silently dropped.
    try:
        _seg = session['segments'][segment_index]
        _ytset = dict(_seg.get('youtube') or {})
        if 'title' in payload and payload['title'] is not None:
            _ytset['title'] = payload['title']
        if 'description' in payload and payload['description'] is not None:
            _ytset['description'] = payload['description']
        if payload.get('privacy'):
            _ytset['privacy'] = payload['privacy']
        if payload.get('category_id'):
            _ytset['category_id'] = str(payload['category_id'])
        if 'made_for_kids' in payload:
            _ytset['made_for_kids'] = bool(payload['made_for_kids'])
        if 'monetize' in payload:
            _ytset['monetize'] = bool(payload['monetize'])
        if payload.get('publish_at'):
            _ytset['publish_at'] = payload['publish_at']
        if 'playlist_ids' in payload:
            _ytset['playlist_ids'] = [p for p in (payload.get('playlist_ids') or []) if p]
        if 'tags' in payload:
            _raw = payload.get('tags')
            if isinstance(_raw, str):
                _raw = [t.strip() for t in _raw.split(',')]
            _ytset['tags'] = [t for t in (_raw or []) if t][:15]
        _seg['youtube'] = _ytset
        save_session(session_id, session)
    except Exception:
        app.logger.exception('failed to persist youtube settings on segment')

    db = autoclip_db.get_db()

    # Reject duplicate: same segment already pending/running
    existing = db.execute(
        "SELECT id, status FROM publish_jobs "
        "WHERE session_id=? AND segment_index=? "
        "  AND status IN ('pending','running')",
        (session_id, segment_index)
    ).fetchone()
    if existing:
        return jsonify({
            'error': 'A publish job is already in progress for this segment',
            'existing_job_id': existing['id'],
            'existing_status': existing['status']
        }), 409

    cur = db.execute(
        "INSERT INTO publish_jobs "
        "(session_id, segment_index, user_id, status, privacy, publish_payload) "
        "VALUES (?, ?, ?, 'pending', ?, ?)",
        (
            session_id,
            segment_index,
            user['id'],
            payload.get('privacy', 'private'),
            _json.dumps(payload)
        )
    )
    db.commit()
    job_id = cur.lastrowid
    app.logger.info(f'Inserted publish job {job_id} for session {session_id} segment {segment_index}')

    # Enqueue Cloud Task so GPU worker picks it up
    segment = session['segments'][segment_index]
    try:
        _enqueue_publish_task(job_id, session_id, segment_index, segment, session)
    except Exception as _te:
        db.execute(
            "UPDATE publish_jobs SET status='failed', error=?, finished_at=CURRENT_TIMESTAMP WHERE id=?",
            (f'Failed to enqueue Cloud Task: {_te}', job_id)
        )
        db.commit()
        app.logger.exception(f'Failed to enqueue Cloud Task for job {job_id}')
        return jsonify({'error': f'Enqueue failed: {_te}', 'job_id': job_id}), 500

    return jsonify({'job_id': job_id, 'status': 'pending'})


@app.route('/api/publish_jobs/<int:job_id>')
def publish_job_status(job_id):
    """Return status of a single publish job."""
    user = autoclip_auth.get_current_user()
    db = autoclip_db.get_db()
    row = db.execute(
        "SELECT id, session_id, segment_index, user_id, status, stage, "
        "       progress_pct, error, youtube_video_id, privacy, "
        "       created_at, started_at, heartbeat_at, finished_at "
        "FROM publish_jobs WHERE id=?",
        (job_id,)
    ).fetchone()
    if not row:
        return jsonify({'error': 'not found'}), 404
    d = dict(row)
    # Admin sees all; regular user must own the job
    if user['role'] != 'admin' and d.get('user_id') != user['id']:
        return jsonify({'error': 'forbidden'}), 403
    return jsonify(d)


@app.route('/api/publish_jobs')
def publish_jobs_list():
    """List publish jobs. Optional filter by session_id."""
    user = autoclip_auth.get_current_user()
    session_id = request.args.get('session_id')
    db = autoclip_db.get_db()

    where = []
    params = []
    if user['role'] != 'admin':
        where.append("user_id = ?")
        params.append(user['id'])
    if session_id:
        where.append("session_id = ?")
        params.append(session_id)
    sql = (
        "SELECT id, session_id, segment_index, user_id, status, stage, "
        "       progress_pct, error, youtube_video_id, privacy, "
        "       created_at, started_at, heartbeat_at, finished_at "
        "FROM publish_jobs"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT 50"
    rows = db.execute(sql, params).fetchall()
    return jsonify({'jobs': [dict(r) for r in rows]})


@app.route('/api/upload_youtube/<session_id>', methods=['POST'])
def upload_to_youtube(session_id):
    """Upload a clip to YouTube."""

    # Read new YouTube publish settings from request (called for side effect below)
    _yt_data = request.get_json() or {}
    _yt_category = str(_yt_data.get('category_id', '17'))
    _yt_tags = _yt_data.get('tags', [])
    if isinstance(_yt_tags, str):
        _yt_tags = [t.strip() for t in _yt_tags.split(',') if t.strip()]
    _yt_made_for_kids = bool(_yt_data.get('made_for_kids', False))
    _yt_publish_at = str(_yt_data.get('publish_at', '')).strip() or None
    _yt_playlist_ids = _yt_data.get('playlist_ids', []) or []
    _yt_monetize = bool(_yt_data.get('monetize', False))
    # Download clip from GCS to /tmp if not already local (multi-tenant GCS storage)
    session_data_gcs = load_session(session_id)
    if session_data_gcs:
        _data_gcs = request.get_json() or {}
        _seg_idx_gcs = int(_data_gcs.get('segment_index', 0))
        if _seg_idx_gcs < len(session_data_gcs.get('segments', [])):
            _seg_gcs = session_data_gcs['segments'][_seg_idx_gcs]
            _gcs_key = _seg_gcs.get('clip_with_ad_gcs_key') or _seg_gcs.get('clip_gcs_key')
            if _gcs_key:
                _tmp_dir = tempfile.mkdtemp(prefix='autoclip_pub_')
                _local = os.path.join(_tmp_dir, f'{session_id}_clip_{_seg_idx_gcs}.mp4')
                gcs_storage.download_from_gcs(_gcs_key, _local)
                # Set clip_path so the rest of the route finds it
                _seg_gcs['clip_path'] = _local
                _seg_gcs['_tmp_dir_for_publish'] = _tmp_dir
                save_session(session_id, session_data_gcs)
    session = load_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404

    data = request.json
    segment_index = data.get('segment_index', 0)
    privacy_status = data.get('privacy_status', 'private')
    scheduled_time = data.get('scheduled_time', None)
    channel_id = data.get('channel_id', 'UCEsOcvBbXtO8AyyY2tZYJpg')  # Default: Power 2

    segment = session['segments'][segment_index]

    # Use clip with ad if available, otherwise use regular clip
    video_path = segment.get('clip_with_ad_path') or segment.get('clip_path')
    if not video_path or not os.path.exists(video_path):
        return jsonify({'error': 'No clip found. Create clip first.'}), 400

    # __AUTOCLIP_COMPOSE_CALL_V3B__
    # If any per-segment ad slot is set, compose intro+clip+mid+outro into a
    # single MP4 and publish that instead of the raw clip. No-op otherwise.
    if any(segment.get(k) for k in ('intro_ad_id', 'outro_ad_id', 'mid_ad_id')):
        _compose_work = os.path.dirname(video_path)
        if not _compose_work or not os.path.isdir(_compose_work):
            _compose_work = tempfile.mkdtemp(prefix='autoclip_compose_')
        try:
            _composed = _compose_clip_with_ads(session, segment_index, video_path, _compose_work)
            if _composed:
                app.logger.info(f'Publishing composed clip (ads baked in): {_composed}')
                video_path = _composed
        except Exception as _ce:
            app.logger.exception('ad composition failed')
            return jsonify({'error': f'Ad composition failed: {str(_ce)[:500]}'}), 500

    title = segment.get('title', 'Untitled')
    description = segment.get('description', '')

    # Multi-tenant: load token for the session's channel
    _channel_yt_id = session.get('channel_youtube_id')
    if _channel_yt_id:
        creds_path = BASE_DIR / 'credentials' / 'tokens' / f'{_channel_yt_id}.json'
    else:
        # Legacy fallback for pre-multitenant sessions
        creds_path = BASE_DIR / 'credentials' / 'power2_token.json'
    if not creds_path.exists():
        return jsonify({'error': f'YouTube credentials not found for this channel ({_channel_yt_id or "none"}). Visit /channels to connect it.'}), 400

    with open(creds_path) as f:
        creds_data = json.load(f)

    creds = Credentials(
        token=creds_data['token'],
        refresh_token=creds_data['refresh_token'],
        token_uri='https://oauth2.googleapis.com/token',
        client_id=creds_data['client_id'],
        client_secret=creds_data['client_secret']
    )

    if creds.expired:
        creds.refresh(google.auth.transport.requests.Request())
        with open(creds_path, 'w') as f:
            json.dump({
                'token': creds.token,
                'refresh_token': creds.refresh_token,
                'client_id': creds.client_id,
                'client_secret': creds.client_secret
            }, f)

    youtube = build('youtube', 'v3', credentials=creds)

    body = {
        'snippet': {
            'title': title,
            'description': description,
            'categoryId': '17'  # Sports
        },
        'status': {
            'privacyStatus': privacy_status
        }
    }

    if scheduled_time and privacy_status == 'private':
        body['status']['publishAt'] = scheduled_time
        body['status']['privacyStatus'] = 'private'

    # Apply the extra YouTube publish settings from the request
    body['snippet']['categoryId'] = _yt_category
    if _yt_tags:
        body['snippet']['tags'] = _yt_tags[:15]
    body['status']['selfDeclaredMadeForKids'] = _yt_made_for_kids
    if _yt_publish_at:
        body['status']['privacyStatus'] = 'private'
        body['status']['publishAt'] = _yt_publish_at
    media = MediaFileUpload(video_path, mimetype='video/mp4', resumable=True)

    request_yt = youtube.videos().insert(
        part='snippet,status',
        body=body,
        media_body=media
    )

    response = request_yt.execute()
    video_id = response['id']

    segment['youtube_video_id'] = video_id
    segment['youtube_url'] = f"https://www.youtube.com/watch?v={video_id}"
    save_session(session_id, session)

    # Add to playlists after upload (uses same yt client if in scope)
    try:
        if _yt_playlist_ids:
            _uploaded_video_id = None
            # Try common variable names for the upload response
            for _v in ('response', 'video', 'upload_response', 'result'):
                _r = locals().get(_v)
                if isinstance(_r, dict) and 'id' in _r:
                    _uploaded_video_id = _r['id']
                    break
            _yt_client = locals().get('yt') or locals().get('youtube')
            if _uploaded_video_id and _yt_client:
                for _pid in _yt_playlist_ids:
                    try:
                        _yt_client.playlistItems().insert(
                            part='snippet',
                            body={
                                'snippet': {
                                    'playlistId': _pid,
                                    'resourceId': {
                                        'kind': 'youtube#video',
                                        'videoId': _uploaded_video_id,
                                    }
                                }
                            }
                        ).execute()
                    except Exception as _pe:
                        app.logger.warning(f'Failed to add to playlist {_pid}: {_pe}')
    except Exception as _e:
        app.logger.warning(f'Playlist post-actions failed: {_e}')
    return jsonify({
        'status': 'uploaded',
        'video_id': video_id,
        'url': f"https://www.youtube.com/watch?v={video_id}"
    })



@app.route('/api/youtube_channels', methods=['GET'])
def get_youtube_channels():
    """List all YouTube channels accessible with the current token."""
    creds_path = BASE_DIR / 'credentials' / 'power2_token.json'
    if not creds_path.exists():
        return jsonify({'error': 'YouTube credentials not found. Visit /authorize first.'}), 400
    with open(creds_path) as f:
        creds_data = json.load(f)
    creds = Credentials(
        token=creds_data['token'],
        refresh_token=creds_data['refresh_token'],
        token_uri='https://oauth2.googleapis.com/token',
        client_id=creds_data['client_id'],
        client_secret=creds_data['client_secret']
    )
    if creds.expired:
        creds.refresh(google.auth.transport.requests.Request())
    youtube = build('youtube', 'v3', credentials=creds)
    response = youtube.channels().list(part='snippet', mine=True).execute()
    channels = [
        {
            'id': ch['id'],
            'title': ch['snippet']['title'],
            'thumbnail': ch['snippet']['thumbnails']['default']['url']
        }
        for ch in response.get('items', [])
    ]
    return jsonify({'channels': channels})

# ─────────────────────────────────────────
#  ROUTES — Ad Library
# ─────────────────────────────────────────

@app.route('/api/ads', methods=['GET'])
def get_ads():
    return jsonify(load_ads())

@app.route('/api/ads/upload', methods=['POST'])
def upload_ad():
    if 'ad' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['ad']
    name = request.form.get('name', file.filename)
    sponsor = request.form.get('sponsor', '')

    filename = f"{uuid.uuid4()[:8]}_{file.filename}"
    file.save(str(ADS_DIR / filename))

    ads = load_ads()
    ads.append({'name': name, 'sponsor': sponsor, 'filename': filename})
    save_ads(ads)

    return jsonify({'status': 'uploaded', 'filename': filename})

@app.route('/api/ads/delete', methods=['POST'])
def delete_ad():
    data = request.json
    filename = data.get('filename')
    ad_path = ADS_DIR / filename
    if ad_path.exists():
        ad_path.unlink()
    ads = [a for a in load_ads() if a['filename'] != filename]
    save_ads(ads)
    return jsonify({'status': 'deleted'})


# ─────────────────────────────────────────
#  ROUTES — Thumbnails
# ─────────────────────────────────────────

@app.route('/api/thumbnails/upload', methods=['POST'])
def upload_thumbnail():
    if 'thumbnail' not in request.files:
        return jsonify({'error': 'No file'}), 400
    file = request.files['thumbnail']
    file.save(str(THUMBS_DIR / file.filename))
    return jsonify({'status': 'uploaded'})


# __AUTOCLIP_THUMB_RESIZE_V2_NOCROP__
def _resize_thumbnail_to_youtube(image_bytes):
    """Downscale to YouTube 1280x720 with NO cropping.

    gpt-image-1 outputs 1536x1024. YouTube wants 16:9 (1280x720).
    We downscale directly — aspect changes 1.5 -> 1.78, ~19% horizontal
    stretch. Deterministic and content-preserving, which is the right
    tradeoff for a commercial product: never chop text.
    """
    try:
        from PIL import Image
        import io as _io
        src_img = Image.open(_io.BytesIO(image_bytes))
        resized = src_img.resize((1280, 720), Image.LANCZOS)
        out = _io.BytesIO()
        resized.save(out, format='PNG', optimize=True)
        return out.getvalue()
    except Exception as _e:
        app.logger.warning(f'thumbnail resize failed, using original bytes: {_e}')
        return image_bytes


@app.route('/api/generate_thumbnail', methods=['POST'])
def generate_thumbnail():
    """Generate a thumbnail image via gpt-image-1, save to GCS, register in library."""
    data = request.json or {}
    title = data.get('title', '')
    topic = data.get('topic', '')
    guidance = (data.get('guidance') or '').strip()
    session_id = data.get('session_id')
    segment_index = data.get('segment_index')

    _u = autoclip_auth.get_current_user()
    _db = autoclip_db.get_db()
    _ok, _why = plans.can_generate_thumbnail(
        _u, _db, session_id,
        int(segment_index) if segment_index is not None else None)
    if not _ok:
        return jsonify({'error': _why, 'upgrade_url': '/pricing'}), 402

    yt_channel_id = None
    if session_id and segment_index is not None:
        sess = load_session(session_id)
        if sess and int(segment_index) < len(sess.get('segments', [])):
            seg = sess['segments'][int(segment_index)]
            title = title or seg.get('title', '')
            topic = topic or seg.get('description', '')[:200]
            yt_channel_id = sess.get('channel_youtube_id')

    base_prompt = (
        'Generate a YouTube thumbnail for a college football podcast video.\n'
        f'Title: {title}\n'
        f'Topic: {topic}\n'
        'Style: Bold, high energy sports thumbnail. Dark or team-colored background. '
        'Dramatic lighting. Text overlay space. No people required, focus on energy and drama. '
        'Make it look like a premium college football YouTube thumbnail.\n'
        'IMPORTANT LAYOUT: Keep ALL text and important faces inside the center 87% '
        'horizontally and 87% vertically - the image will be cropped to 16:9 for YouTube, '
        'so anything within ~7% of any edge WILL be cut off. Leave those edges as background.'
    )
    prompt = (guidance + '. ' + base_prompt) if guidance else base_prompt

    response = client.images.generate(
        model=IMAGE_MODEL,
        prompt=prompt,
        size='1536x1024',
        quality='high',
        n=1
    )
    plans.log_thumbnail_generation(
        _db, _u['id'], session_id,
        int(segment_index) if segment_index is not None else None,
        IMAGE_MODEL, 'high')
    import base64
    image_b64 = response.data[0].b64_json
    image_bytes = base64.b64decode(image_b64)
    image_bytes = _resize_thumbnail_to_youtube(image_bytes)  # 1280x720 for YouTube

    # If we know the channel, save to GCS; else fall back to legacy local save
    if yt_channel_id:
        import hashlib
        digest = hashlib.md5(image_bytes).hexdigest()[:12]
        gcs_key = gcs_storage.thumbnail_key(yt_channel_id, digest, ext='png')
        gcs_storage.upload_bytes_to_gcs(image_bytes, gcs_key, content_type='image/png')
        thumbnail_url = f'/api/thumbnail-url?key={gcs_key}'

        # Register in library
        try:
            internal_ch = autoclip_db.get_db().execute(
                'SELECT id FROM channels WHERE youtube_channel_id=?', (yt_channel_id,)
            ).fetchone()
            if internal_ch:
                _u = autoclip_auth.get_current_user()
                autoclip_db.get_db().execute(
                    'INSERT OR IGNORE INTO thumbnails_library (channel_id, gcs_key, source_type, source_session_id, source_segment_index, created_by_user_id) '
                    'VALUES (?, ?, ?, ?, ?, ?)',
                    (internal_ch['id'], gcs_key, 'generated', session_id, segment_index, _u['id'] if _u else None)
                )
                autoclip_db.get_db().commit()
        except Exception as _e:
            app.logger.warning(f'Failed to register thumbnail in library: {_e}')

        # Save on session segment if applicable
        if session_id and segment_index is not None:
            sess = load_session(session_id)
            if sess and int(segment_index) < len(sess.get('segments', [])):
                sess['segments'][int(segment_index)]['thumbnail_gcs_key'] = gcs_key
                sess['segments'][int(segment_index)]['thumbnail_url'] = thumbnail_url
                save_session(session_id, sess)
    else:
        # Legacy fallback: local disk (session has no channel)
        filename = f'thumb_{uuid.uuid4().hex[:8]}.png'
        filepath = THUMBS_DIR / filename
        with open(filepath, 'wb') as f:
            f.write(image_bytes)
        thumbnail_url = f'/static/thumbnails/{filename}'

    return jsonify({'status': 'generated', 'image_url': thumbnail_url, 'thumbnail_url': thumbnail_url})

# ─────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────

def load_session(session_id):
    path = SESSIONS_DIR / f"{session_id}.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None

def save_session(session_id, data):
    path = SESSIONS_DIR / f"{session_id}.json"
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)

def load_ads():
    path = ADS_DIR / 'ads.json'
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return []

def save_ads(ads):
    path = ADS_DIR / 'ads.json'
    with open(path, 'w') as f:
        json.dump(ads, f, indent=2)



# ─────────────────────────────────────────
#  YOUTUBE OAUTH CALLBACK
# ─────────────────────────────────────────

CREDENTIALS_DIR = BASE_DIR / 'credentials'
CLIENT_SECRET    = CREDENTIALS_DIR / 'power2_client_secret.json'
TOKEN_FILE       = CREDENTIALS_DIR / 'power2_token.json'
OAUTH_REDIRECT   = 'https://autoclip.cloud/oauth2callback'

YOUTUBE_SCOPES = [
    'https://www.googleapis.com/auth/youtube.upload',
    'https://www.googleapis.com/auth/youtube.readonly',
    'https://www.googleapis.com/auth/youtube.force-ssl'
]

@app.route('/authorize')
def authorize():
    """Kick off YouTube OAuth to connect a channel. User must be signed in."""
    from google_auth_oauthlib.flow import Flow
    from pathlib import Path

    user = autoclip_auth.get_current_user()
    if not user:
        return redirect(url_for('auth.login'))

    client_secret_file = str(Path.home() / "cfb_clip_studio" / "credentials" / "autoclip_signin_client.json")
    flow = Flow.from_client_secrets_file(
        client_secret_file,
        scopes=[
            'https://www.googleapis.com/auth/youtube.upload',
            'https://www.googleapis.com/auth/youtube.readonly',
            'https://www.googleapis.com/auth/youtube',
        ]
    )
    flow.redirect_uri = 'https://autoclip.cloud/oauth2callback'

    auth_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent select_account',
    )
    flask_session['yt_oauth_state'] = state
    flask_session['yt_oauth_code_verifier'] = getattr(flow, 'code_verifier', None)
    return redirect(auth_url)

@app.route('/oauth2callback')
def oauth2callback():
    """YouTube OAuth callback. Detects channel and saves per-channel token."""
    from google_auth_oauthlib.flow import Flow
    import json
    from pathlib import Path

    user = autoclip_auth.get_current_user()
    if not user:
        return redirect(url_for('auth.login'))

    # Load state saved during /authorize
    state = flask_session.get('yt_oauth_state')
    if not state:
        return "Missing OAuth state. Please retry from /channels.", 400

    client_secret_file = str(Path.home() / "cfb_clip_studio" / "credentials" / "autoclip_signin_client.json")
    flow = Flow.from_client_secrets_file(
        client_secret_file,
        scopes=[
            'https://www.googleapis.com/auth/youtube.upload',
            'https://www.googleapis.com/auth/youtube.readonly',
            'https://www.googleapis.com/auth/youtube',
        ],
        state=state,
    )
    flow.redirect_uri = 'https://autoclip.cloud/oauth2callback'

    saved_verifier = flask_session.pop('yt_oauth_code_verifier', None)
    if saved_verifier:
        flow.code_verifier = saved_verifier

    try:
        flow.fetch_token(authorization_response=request.url)
    except Exception as e:
        return f"OAuth token exchange failed: {e}", 400

    creds = flow.credentials

    # Ask YouTube which channel this token authorizes
    from googleapiclient.discovery import build
    yt = build('youtube', 'v3', credentials=creds)
    resp = yt.channels().list(part='snippet,status', mine=True).execute()
    items = resp.get('items', [])
    if not items:
        return "No YouTube channels returned. Did you pick a channel with content?", 400

    ch = items[0]
    channel_id = ch['id']
    title = ch['snippet']['title']
    long_uploads = ch.get('status', {}).get('longUploadsStatus') == 'allowed'

    handle = title.lower()
    handle = ''.join(c if c.isalnum() or c in '- ' else '' for c in handle)
    handle = '-'.join(handle.split())

    # Save token per channel
    tokens_dir = Path.home() / "cfb_clip_studio" / "credentials" / "tokens"
    tokens_dir.mkdir(parents=True, exist_ok=True)
    token_path_abs = tokens_dir / f"{channel_id}.json"
    with open(token_path_abs, 'w') as f:
        f.write(creds.to_json())

    token_path_rel = f"credentials/tokens/{channel_id}.json"

    # Register or update the channel in DB, assigning current user as owner
    existing = autoclip_db.get_channel_by_youtube_id(channel_id)
    if existing:
        autoclip_db.get_db().execute(
            "UPDATE channels SET title=?, handle=?, token_path=?, long_uploads_enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (title, handle, token_path_rel, 1 if long_uploads else 0, existing['id'])
        )
        autoclip_db.get_db().commit()
        # If the channel is unclaimed, assign to current user
        if existing['owner_user_id'] is None:
            autoclip_db.update_channel_owner(existing['id'], user['id'])
        else:
            # Existing owner - just grant current user access if they aren't owner
            if existing['owner_user_id'] != user['id']:
                autoclip_db.grant_channel_access(user['id'], existing['id'], 'uploader')
    else:
        ch_row = autoclip_db.create_channel(channel_id, title, handle, token_path_rel, owner_user_id=user['id'], long_uploads=long_uploads)
        autoclip_db.grant_channel_access(user['id'], ch_row['id'], 'owner')

    return redirect(url_for('channels_page'))

@app.route('/clip-file/<filename>')
def serve_clip(filename):
    """Redirect to a signed GCS URL for the clip file. Parses filename to find session/segment."""
    # Filename shape: <session_id>_clip_<N>[.<suffix>].mp4
    m = re.match(r'([a-f0-9-]+)_clip_(\d+)(?:\.(ad))?\.mp4', filename)
    if not m:
        return 'Bad filename', 404
    session_id, seg_idx_str, ad_suffix = m.group(1), m.group(2), m.group(3)
    seg_idx = int(seg_idx_str)
    session = load_session(session_id)
    if not session or seg_idx >= len(session.get('segments', [])):
        return 'Session or segment not found', 404
    segment = session['segments'][seg_idx]

    # Access check — user must have channel access, or admin
    user = autoclip_auth.get_current_user()
    if not user:
        return redirect(url_for('auth.login'))
    if user['role'] != 'admin':
        ch_pk = session.get('channel_id')
        if ch_pk and not autoclip_db.user_has_channel_access(user['id'], ch_pk):
            return 'Forbidden', 403

    gcs_key = segment.get('clip_with_ad_gcs_key') if ad_suffix == 'ad' else segment.get('clip_gcs_key')
    if not gcs_key:
        # Legacy fallback: file still local
        legacy = BASE_DIR / 'static' / 'clips' / filename
        if legacy.exists():
            return send_from_directory(str(legacy.parent), legacy.name)
        return 'Clip not found', 404

    try:
        url = gcs_storage.signed_url(gcs_key, expires_seconds=3600)
        return redirect(url)
    except Exception as e:
        return f'Failed to sign URL: {e}', 500

# ============================================================================
# Multi-tenant routes
# ============================================================================

@app.route('/channels')
def channels_page():
    """User's channels list + connect new."""
    user = autoclip_auth.get_current_user()
    if user['role'] == 'admin':
        channels = autoclip_db.list_all_channels()
    else:
        channels = autoclip_db.list_channels_for_user(user['id'])
    return render_template('channels.html', current_user=user, channels=channels)



# __ADMIN_COSTS_V1__
@app.route('/admin/costs')
def admin_costs():
    """Admin-only: per-user cost, revenue and margin for a month."""
    user = autoclip_auth.get_current_user()
    if not user or user['role'] != 'admin':
        return "Forbidden", 403
    db = autoclip_db.get_db()
    month = request.args.get('month') or None
    exclude_founders = request.args.get('founders') != 'include'

    report = costs.margin_report(db, plans, month)

    if exclude_founders:
        keep = [u for u in report['users'] if u['id'] not in ADMIN_USER_IDS]
        rev = sum(u['revenue'] for u in keep)
        cost = sum(u['cost'] for u in keep)
        gross = rev - cost
        net = gross - costs.FIXED_MONTHLY
        paying = [u for u in keep if u['is_paying']]
        report = {
            'month': report['month'],
            'users': keep,
            'summary': {
                'revenue': rev, 'variable_cost': cost,
                'gross_profit': gross,
                'gross_margin_pct': (gross / rev * 100) if rev else None,
                'fixed_cost': costs.FIXED_MONTHLY,
                'net_profit': net,
                'net_margin_pct': (net / rev * 100) if rev else None,
                'paying_users': len(paying),
                'free_users': len(keep) - len(paying),
                'free_user_cost': sum(u['cost'] for u in keep if not u['is_paying']),
                'breakeven_users': None,
            },
        }

    return render_template(
        'admin_costs.html',
        current_user=user,
        report=report,
        months=costs.available_months(db),
        selected_month=month,
        exclude_founders=exclude_founders,
    )


@app.route('/api/admin/costs/<int:target_user_id>')
def admin_user_cost_detail(target_user_id):
    """Admin-only: one user's cost broken down by kind."""
    user = autoclip_auth.get_current_user()
    if not user or user['role'] != 'admin':
        return jsonify({'error': 'forbidden'}), 403
    db = autoclip_db.get_db()
    month = request.args.get('month') or None
    return jsonify(costs.user_costs(db, target_user_id, month))


@app.route('/admin/users')
def admin_users():
    """Admin: manage users."""
    user = autoclip_auth.get_current_user()
    if user['role'] != 'admin':
        return "Forbidden", 403
    users = autoclip_db.list_users()
    return render_template('admin_users.html', current_user=user, users=users)


@app.route('/api/admin/users/<int:user_id>/approve', methods=['POST'])
def admin_approve_user(user_id):
    user = autoclip_auth.get_current_user()
    if user['role'] != 'admin':
        return jsonify({'error': 'admin only'}), 403
    autoclip_db.set_user_approved(user_id, True)
    return jsonify({'status': 'approved'})


# __ADMIN_SET_TIER_V1__
@app.route('/api/admin/users/<int:user_id>/tier', methods=['POST'])
def admin_set_user_tier(user_id):
    user = autoclip_auth.get_current_user()
    if user['role'] != 'admin':
        return jsonify({'error': 'admin only'}), 403
    data = request.get_json() or {}
    video_tier = data.get('video_tier')
    audio_tier = data.get('audio_tier')
    if not video_tier and not audio_tier:
        return jsonify({'error': 'video_tier or audio_tier required'}), 400
    try:
        autoclip_db.set_user_tier(user_id, video_tier=video_tier, audio_tier=audio_tier)
    except AssertionError as _e:
        return jsonify({'error': str(_e)}), 400
    return jsonify({'status': 'ok'})


@app.route('/api/admin/users/<int:user_id>/unapprove', methods=['POST'])
def admin_unapprove_user(user_id):
    user = autoclip_auth.get_current_user()
    if user['role'] != 'admin':
        return jsonify({'error': 'admin only'}), 403
    autoclip_db.set_user_approved(user_id, False)
    return jsonify({'status': 'unapproved'})


@app.route('/api/admin/users/<int:user_id>/role', methods=['POST'])
def admin_set_user_role(user_id):
    user = autoclip_auth.get_current_user()
    if user['role'] != 'admin':
        return jsonify({'error': 'admin only'}), 403
    data = request.get_json() or {}
    role = data.get('role')
    if role not in ('member', 'admin'):
        return jsonify({'error': 'invalid role'}), 400
    autoclip_db.set_user_role(user_id, role)
    return jsonify({'status': 'updated', 'role': role})


@app.route('/admin/channels')
def admin_channels():
    """Admin: manage channels + assign owners."""
    user = autoclip_auth.get_current_user()
    if user['role'] != 'admin':
        return "Forbidden", 403
    channels = autoclip_db.list_all_channels()
    users = autoclip_db.list_users()
    # Enrich channels with owner info + user_channel list
    enriched = []
    for c in channels:
        owner = autoclip_db.get_user_by_id(c['owner_user_id']) if c['owner_user_id'] else None
        members = autoclip_db.list_users_for_channel(c['id'])
        enriched.append({**c, 'owner': owner, 'members': members})
    return render_template('admin_channels.html', current_user=user, channels=enriched, users=users)


@app.route('/api/admin/channels/<int:channel_id>/owner', methods=['POST'])
def admin_set_channel_owner(channel_id):
    user = autoclip_auth.get_current_user()
    if user['role'] != 'admin':
        return jsonify({'error': 'admin only'}), 403
    data = request.get_json() or {}
    new_owner_id = data.get('owner_user_id')
    if new_owner_id is None:
        return jsonify({'error': 'owner_user_id required'}), 400
    autoclip_db.update_channel_owner(channel_id, int(new_owner_id))
    return jsonify({'status': 'updated'})


@app.route('/api/admin/channels/<int:channel_id>/grant', methods=['POST'])
def admin_grant_channel(channel_id):
    user = autoclip_auth.get_current_user()
    if user['role'] != 'admin':
        return jsonify({'error': 'admin only'}), 403
    data = request.get_json() or {}
    target_user_id = data.get('user_id')
    role = data.get('role', 'uploader')
    if not target_user_id:
        return jsonify({'error': 'user_id required'}), 400
    autoclip_db.grant_channel_access(int(target_user_id), channel_id, role)
    return jsonify({'status': 'granted'})


@app.route('/api/admin/channels/<int:channel_id>/revoke', methods=['POST'])
def admin_revoke_channel(channel_id):
    user = autoclip_auth.get_current_user()
    if user['role'] != 'admin':
        return jsonify({'error': 'admin only'}), 403
    data = request.get_json() or {}
    target_user_id = data.get('user_id')
    if not target_user_id:
        return jsonify({'error': 'user_id required'}), 400
    autoclip_db.revoke_channel_access(int(target_user_id), channel_id)
    return jsonify({'status': 'revoked'})


# ============================================================================
# Usage helpers
# ============================================================================

def _current_period():
    """Calendar-month YYYY-MM."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime('%Y-%m')


def _get_usage(user_id, period=None):
    """Return {'clipping_shows', 'clipping_clips', 'audio_episodes'} for a user in a period."""
    period = period or _current_period()
    row = autoclip_db.get_db().execute(
        "SELECT clipping_shows, clipping_clips, audio_episodes FROM usage_monthly WHERE user_id=? AND period=?",
        (user_id, period)
    ).fetchone()
    if not row:
        return {'clipping_shows': 0, 'clipping_clips': 0, 'audio_episodes': 0}
    return dict(row)


def _increment_usage(user_id, field, amount=1):
    """Increment a usage counter. Auto-creates the row if missing."""
    period = _current_period()
    db = autoclip_db.get_db()
    db.execute(
        f"INSERT INTO usage_monthly (user_id, period, {field}) VALUES (?, ?, ?) "
        f"ON CONFLICT(user_id, period) DO UPDATE SET {field} = {field} + ?",
        (user_id, period, amount, amount)
    )
    db.commit()


def _check_clipping_cap(user):
    """Return (ok, message). ok=False if user hit their monthly clipping cap."""
    if not user['has_clipping']:
        return False, "You don't have an active clipping subscription."
    cap = user.get('clipping_monthly_cap')
    if cap is None:
        return True, None  # unlimited
    usage = _get_usage(user['id'])
    if usage['clipping_shows'] >= cap:
        return False, f"You've reached this month's cap of {cap} shows."
    return True, None


# ============================================================================
# Account / usage routes
# ============================================================================

@app.route('/pricing')
def pricing_page():
    """Public pricing page. Shows tiers for video, audio, and bundles."""
    user = autoclip_auth.get_current_user()
    return render_template(
        'pricing.html',
        current_user=user,
        video_tiers=plans.VIDEO_TIERS,
        audio_tiers=plans.AUDIO_TIERS,
        bundle_tiers=plans.BUNDLE_TIERS,
        plan=plans.user_plan_summary(user) if user else {},
    )


@app.route('/account')
def account_page():
    """Show current user their subscription and usage."""
    user = autoclip_auth.get_current_user()
    usage = _get_usage(user['id'])
    audio_config_count = autoclip_db.get_db().execute(
        "SELECT COUNT(*) FROM audio_sync_configs WHERE user_id=?", (user['id'],)
    ).fetchone()[0]
    return render_template(
        'account.html',
        current_user=user,
        usage=usage,
        period=_current_period(),
        audio_config_count=audio_config_count,
        plan=plans.user_plan_summary(user),
    )


# ============================================================================
# Audio sync routes
# ============================================================================

def _audio_gate(user):
    """Return (ok, response) for audio route access."""
    if not user['has_audio']:
        from flask import request as _rq
        if _rq.path.startswith('/api/'):
            return False, (jsonify({'error': 'audio subscription required'}), 403)
        return False, render_template('feature_gated.html', current_user=user, feature='Audio Sync')
    return True, None


@app.route('/audio')
def audio_page():
    """List user's audio sync configs."""
    user = autoclip_auth.get_current_user()
    ok, resp = _audio_gate(user)
    if not ok:
        return resp
    if user['role'] == 'admin':
        configs = autoclip_db.get_db().execute(
            "SELECT c.*, u.email AS owner_email FROM audio_sync_configs c "
            "JOIN users u ON u.id = c.user_id ORDER BY c.created_at DESC"
        ).fetchall()
    else:
        configs = autoclip_db.get_db().execute(
            "SELECT * FROM audio_sync_configs WHERE user_id=? ORDER BY created_at DESC",
            (user['id'],)
        ).fetchall()
    configs = [dict(c) for c in configs]
    channels = autoclip_db.list_channels_for_user(user['id']) if user['role'] != 'admin' else autoclip_db.list_all_channels()
    return render_template('audio.html', current_user=user, configs=configs, channels=channels)


@app.route('/api/audio/configs', methods=['POST'])
def create_audio_config():
    user = autoclip_auth.get_current_user()
    ok, resp = _audio_gate(user)
    if not ok:
        return resp
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    channel_id = data.get('channel_id')
    playlist_id = (data.get('source_playlist_id') or '').strip()
    api_key = (data.get('destination_api_key') or '').strip()
    show_id = (data.get('destination_show_id') or '').strip()
    show_name = (data.get('destination_show_name') or '').strip()
    if not (name and channel_id and playlist_id and api_key and show_id):
        return jsonify({'error': 'name, channel_id, source_playlist_id, destination_api_key, destination_show_id required'}), 400
    ch = autoclip_db.get_channel_by_id(int(channel_id))
    if not ch:
        return jsonify({'error': 'channel not found'}), 400
    if user['role'] != 'admin' and not autoclip_db.user_has_channel_access(user['id'], ch['id']):
        return jsonify({'error': 'no access to that channel'}), 403
    db = autoclip_db.get_db()
    cur = db.execute(
        "INSERT INTO audio_sync_configs "
        "(user_id, name, channel_id, source_youtube_channel_id, source_playlist_id, "
        " destination_type, destination_api_key, destination_show_id, destination_show_name, is_active) "
        "VALUES (?, ?, ?, ?, ?, 'transistor', ?, ?, ?, 1)",
        (user['id'], name, ch['id'], ch['youtube_channel_id'], playlist_id, api_key, show_id, show_name)
    )
    db.commit()
    return jsonify({'id': cur.lastrowid, 'status': 'created'})


@app.route('/api/audio/configs/<int:config_id>', methods=['POST'])
def update_audio_config(config_id):
    user = autoclip_auth.get_current_user()
    ok, resp = _audio_gate(user)
    if not ok:
        return resp
    db = autoclip_db.get_db()
    row = db.execute("SELECT * FROM audio_sync_configs WHERE id=?", (config_id,)).fetchone()
    if not row:
        return jsonify({'error': 'config not found'}), 404
    if row['user_id'] != user['id'] and user['role'] != 'admin':
        return jsonify({'error': 'forbidden'}), 403
    data = request.get_json() or {}
    # Only update fields that were provided
    fields = []
    values = []
    for key in ('name', 'source_playlist_id', 'destination_api_key', 'destination_show_id', 'destination_show_name'):
        if key in data:
            fields.append(f"{key}=?")
            values.append(data[key])
    if 'is_active' in data:
        fields.append("is_active=?")
        values.append(1 if data['is_active'] else 0)
    if not fields:
        return jsonify({'error': 'nothing to update'}), 400
    fields.append("updated_at=CURRENT_TIMESTAMP")
    values.append(config_id)
    db.execute(f"UPDATE audio_sync_configs SET {', '.join(fields)} WHERE id=?", values)
    db.commit()
    return jsonify({'status': 'updated'})


@app.route('/api/audio/configs/<int:config_id>/delete', methods=['POST'])
def delete_audio_config(config_id):
    user = autoclip_auth.get_current_user()
    db = autoclip_db.get_db()
    row = db.execute("SELECT * FROM audio_sync_configs WHERE id=?", (config_id,)).fetchone()
    if not row:
        return jsonify({'error': 'config not found'}), 404
    if row['user_id'] != user['id'] and user['role'] != 'admin':
        return jsonify({'error': 'forbidden'}), 403
    db.execute("DELETE FROM audio_sync_configs WHERE id=?", (config_id,))
    db.commit()
    return jsonify({'status': 'deleted'})


@app.route('/api/audio/transistor/shows', methods=['POST'])
def list_transistor_shows():
    """Given a Transistor API key, return the shows it can access.
    Used during config creation so the user picks from a dropdown."""
    user = autoclip_auth.get_current_user()
    ok, resp = _audio_gate(user)
    if not ok:
        return resp
    import requests as _rq
    data = request.get_json() or {}
    api_key = (data.get('api_key') or '').strip()
    if not api_key:
        return jsonify({'error': 'api_key required'}), 400
    try:
        r = _rq.get('https://api.transistor.fm/v1/shows',
                    headers={'x-api-key': api_key}, timeout=15)
        r.raise_for_status()
        payload = r.json()
        shows = []
        for item in payload.get('data', []):
            shows.append({
                'id': item['id'],
                'title': item.get('attributes', {}).get('title', 'Untitled'),
            })
        return jsonify({'shows': shows})
    except Exception as e:
        return jsonify({'error': f'Transistor API call failed: {e}'}), 400


# ============================================================================
# Admin: manage entitlements per user
# ============================================================================

@app.route('/api/admin/users/<int:user_id>/entitlements', methods=['POST'])
def admin_set_entitlements(user_id):
    user = autoclip_auth.get_current_user()
    if user['role'] != 'admin':
        return jsonify({'error': 'admin only'}), 403
    data = request.get_json() or {}
    fields, values = [], []
    for key in ('has_clipping', 'has_audio'):
        if key in data:
            fields.append(f"{key}=?")
            values.append(1 if data[key] else 0)
    for key in ('clipping_monthly_cap', 'audio_monthly_cap'):
        if key in data:
            fields.append(f"{key}=?")
            values.append(int(data[key]) if data[key] not in (None, '', 'null') else None)
    if not fields:
        return jsonify({'error': 'nothing to update'}), 400
    values.append(user_id)
    autoclip_db.get_db().execute(
        f"UPDATE users SET {', '.join(fields)} WHERE id=?", values
    )
    autoclip_db.get_db().commit()
    return jsonify({'status': 'updated'})



# ============================================================================
# Clip editor helpers + routes
# ============================================================================

def _get_youtube_api_key():
    """Read YouTube Data API v3 key from the credentials directory."""
    p = BASE_DIR / 'credentials' / 'youtube_api_key.txt'
    if p.exists():
        return p.read_text().strip()
    return None


def _fetch_channel_recent_content(youtube_channel_id, max_results=15):
    """Return (titles, descriptions) as two lists of strings. Empty on failure."""
    import requests as _rq
    api_key = _get_youtube_api_key()
    if not api_key or not youtube_channel_id:
        return [], []
    try:
        # First get the uploads playlist id
        r1 = _rq.get('https://www.googleapis.com/youtube/v3/channels',
                     params={'part': 'contentDetails', 'id': youtube_channel_id, 'key': api_key},
                     timeout=15)
        r1.raise_for_status()
        items = r1.json().get('items', [])
        if not items:
            return [], []
        uploads_playlist = items[0]['contentDetails']['relatedPlaylists']['uploads']

        # Then get recent items
        r2 = _rq.get('https://www.googleapis.com/youtube/v3/playlistItems',
                     params={'part': 'snippet', 'playlistId': uploads_playlist,
                             'maxResults': max_results, 'key': api_key},
                     timeout=15)
        r2.raise_for_status()
        items = r2.json().get('items', [])
        titles = [it['snippet'].get('title', '') for it in items if it.get('snippet')]
        descs = [it['snippet'].get('description', '')[:500] for it in items if it.get('snippet')]
        return titles, descs
    except Exception as e:
        app.logger.warning(f"Failed to fetch channel content: {e}")
        return [], []




# __DESCRIPTION_COMPOSER_V1__
DESCRIPTION_ORDERS = {
    'ai_ads_base': ('ai', 'ads', 'base'),
    'base_ai_ads': ('base', 'ai', 'ads'),
    'ai_base_ads': ('ai', 'base', 'ads'),
}


def compose_youtube_description(session, segment, ai_text=None, channel_row=None):
    """
    Assemble the final YouTube description from three parts:
      ai   - AI-generated text about this specific clip
      ads  - description_text of whichever intro/mid/outro ads are selected
      base - the channel's base_description boilerplate

    Order is per-channel (channels.description_order). Missing parts are
    skipped, not left as blank gaps. Result is clamped to YouTube's 5000
    char limit.
    """
    d = autoclip_db.get_db()

    if channel_row is None:
        cid = session.get('channel_id')
        if cid:
            channel_row = d.execute(
                "SELECT base_description, description_order FROM channels WHERE id=?",
                (cid,)
            ).fetchone()
    ch = dict(channel_row) if channel_row else {}

    order_key = (ch.get('description_order') or 'ai_ads_base')
    order = DESCRIPTION_ORDERS.get(order_key, DESCRIPTION_ORDERS['ai_ads_base'])

    # AI part: explicit arg wins, else whatever is already on the segment
    ai = (ai_text if ai_text is not None else segment.get('description')) or ''
    ai = ai.strip()

    # Ads part: the segment's explicitly-chosen description ads.
    # Deliberately NOT intro/mid/outro - video placement and description
    # blurbs are independent choices.
    ad_ids = list(segment.get('description_ad_ids') or [])
    ad_texts, seen = [], set()
    for aid in ad_ids:
        if not aid or aid in seen:
            continue
        seen.add(aid)
        row = d.execute("SELECT description_text FROM ads WHERE id=?", (aid,)).fetchone()
        if row:
            t = (dict(row).get('description_text') or '').strip()
            if t and t not in ad_texts:
                ad_texts.append(t)
    ads = "\n\n".join(ad_texts)

    base = (ch.get('base_description') or '').strip()

    parts = {'ai': ai, 'ads': ads, 'base': base}
    chunks = [parts[k] for k in order if parts.get(k)]
    out = "\n\n".join(chunks).strip()
    return out[:5000]



# __TRACKED_CHAT_V1__
def tracked_chat(_user_id=None, _session_id=None, _segment_index=None, **kwargs):
    """
    client.chat.completions.create with automatic cost recording.
    Cost tracking never breaks the call - a logging failure is swallowed.
    """
    resp = client.chat.completions.create(**kwargs)
    try:
        if _user_id is None:
            # All call sites are inside request handlers, so the session
            # cookie identifies the user without threading ids through.
            try:
                _u = autoclip_auth.get_current_user()
                _user_id = _u['id'] if _u else None
            except Exception:
                _user_id = None
        if _user_id:
            costs.record_gpt_text(
                autoclip_db.get_db(), _user_id, resp,
                model=kwargs.get('model', 'gpt-4o'),
                session_id=_session_id, segment_index=_segment_index)
    except Exception:
        app.logger.exception('gpt cost record failed')
    return resp


# __SEGMENT_TRANSCRIPT_V1__
def segment_transcript_text(session, segment, max_chars=3000):
    """
    Return the transcript text spoken inside a segment's time window.

    Segments do NOT carry their own 'transcript' key - the transcript lives
    at the session root as a list of {start, end, text} lines. Every AI
    prompt used to read segment.get('transcript') and silently get '',
    so titles/descriptions were generated with no knowledge of the clip.
    """
    lines = session.get('transcript') or []
    if not lines:
        return (session.get('full_text') or '')[:max_chars]

    start = float(segment.get('start_time') or segment.get('start') or 0)
    end = float(segment.get('end_time') or segment.get('end') or 0)
    if end <= start:
        return (session.get('full_text') or '')[:max_chars]

    out = []
    for ln in lines:
        try:
            ls = float(ln.get('start', 0))
            le = float(ln.get('end', ls))
        except (TypeError, ValueError):
            continue
        # any overlap with the window
        if le >= start and ls <= end:
            t = (ln.get('text') or '').strip()
            if t:
                out.append(t)
    text = " ".join(out).strip()
    if not text:
        return (session.get('full_text') or '')[:max_chars]
    return text[:max_chars]


@app.route('/api/regenerate_title/<session_id>', methods=['POST'])
def regenerate_title(session_id):
    """Generate 3 alternate title options for a segment, matching channel style."""
    session = load_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404
    data = request.get_json() or {}
    segment_index = int(data.get('segment_index', 0))
    segment = session['segments'][segment_index]

    # Get channel's recent titles for style matching
    channel_yt_id = session.get('channel_youtube_id')
    titles, _ = _fetch_channel_recent_content(channel_yt_id) if channel_yt_id else ([], [])

    # Build the prompt
    transcript_snippet = segment_transcript_text(session, segment, 2000)
    style_examples = "\n".join(f"- {t}" for t in titles[:10]) if titles else "(no examples available)"

    prompt = f"""Generate 3 YouTube video title options for this clip segment.

Match the style of these recent titles from the same channel:
{style_examples}

Segment transcript excerpt:
{transcript_snippet}

Return ONLY a JSON array of 3 title strings. No preamble, no markdown, just the JSON array.
Example format: ["Title one", "Title two", "Title three"]
"""

    # Uses global `client`
    try:
        resp = tracked_chat(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.9,
        )
        text = resp.choices[0].message.content.strip()
        # Strip markdown fences if the model added them
        # Strip markdown fences robustly (handles ```json ... ``` and ``` ... ```)
        text = text.strip()
        if text.startswith('```'):
            # Remove opening fence line (```json or just ```)
            text = text.split('\n', 1)[1] if '\n' in text else text[3:]
        if text.endswith('```'):
            text = text[:-3]
        text = text.strip()
        options = json.loads(text)
        if not isinstance(options, list) or len(options) < 1:
            raise ValueError("Bad response format")
        return jsonify({'options': options[:3]})
    except Exception as e:
        return jsonify({'error': f'Title generation failed: {e}'}), 500


@app.route('/api/regenerate_description/<session_id>', methods=['POST'])
def regenerate_description(session_id):
    """Generate 3 alternate description options for a segment."""
    session = load_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404
    data = request.get_json() or {}
    segment_index = int(data.get('segment_index', 0))
    segment = session['segments'][segment_index]

    channel_yt_id = session.get('channel_youtube_id')
    _, descs = _fetch_channel_recent_content(channel_yt_id) if channel_yt_id else ([], [])

    transcript_snippet = segment_transcript_text(session, segment, 3000)
    style_examples = "\n---\n".join(descs[:5]) if descs else "(no examples available)"

    prompt = f"""Generate 3 YouTube video description options for this clip segment.

Match the style, length, and formatting of these recent descriptions from the same channel:
{style_examples}

Segment transcript:
{transcript_snippet}

Return ONLY a JSON array of 3 description strings. No preamble, no markdown.
"""

    # Uses global `client`
    try:
        resp = tracked_chat(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.85,
        )
        text = resp.choices[0].message.content.strip()
        # Strip markdown fences robustly (handles ```json ... ``` and ``` ... ```)
        text = text.strip()
        if text.startswith('```'):
            # Remove opening fence line (```json or just ```)
            text = text.split('\n', 1)[1] if '\n' in text else text[3:]
        if text.endswith('```'):
            text = text[:-3]
        text = text.strip()
        options = json.loads(text)
        if not isinstance(options, list):
            raise ValueError("Bad response")
        return jsonify({'options': options[:3]})
    except Exception as e:
        return jsonify({'error': f'Description generation failed: {e}'}), 500


@app.route('/api/update_segment/<session_id>', methods=['POST'])
def update_segment(session_id):
    """Update fields on a segment (start, end, title, description)."""
    session = load_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404
    data = request.get_json() or {}
    segment_index = int(data.get('segment_index', 0))
    if segment_index >= len(session['segments']):
        return jsonify({'error': 'segment_index out of range'}), 400

    segment = session['segments'][segment_index]
    for key in ('start', 'end', 'title', 'description'):
        if key in data:
            segment[key] = data[key]
    # Also write to *_time canonical names used elsewhere
    if 'start' in data:
        segment['start_time'] = float(data['start'])
    if 'end' in data:
        segment['end_time'] = float(data['end'])

    save_session(session_id, session)
    return jsonify({'status': 'updated', 'segment': segment})


@app.route('/api/upload_custom_thumbnail/<session_id>', methods=['POST'])
def upload_custom_thumbnail(session_id):
    """Accept a user-uploaded image as the thumbnail for a segment. Saves to GCS."""
    session = load_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'Empty file'}), 400
    segment_index = int(request.form.get('segment_index', 0))
    if segment_index >= len(session['segments']):
        return jsonify({'error': 'segment_index out of range'}), 400

    yt_channel_id = session.get('channel_youtube_id')
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else 'png'
    if ext not in ('png', 'jpg', 'jpeg', 'webp'):
        ext = 'png'

    file_bytes = f.read()
    # Normalize user-uploaded thumbnails to YouTube's 1280x720 (same as AI-generated)
    file_bytes = _resize_thumbnail_to_youtube(file_bytes)
    ext = 'png'  # helper always returns PNG bytes

    if yt_channel_id:
        import hashlib
        digest = hashlib.md5(file_bytes).hexdigest()[:12]
        gcs_key = gcs_storage.thumbnail_key(yt_channel_id, f'{digest}_upload', ext=ext)
        content_type = 'image/png'
        gcs_storage.upload_bytes_to_gcs(file_bytes, gcs_key, content_type=content_type)
        thumbnail_url = f'/api/thumbnail-url?key={gcs_key}'

        # Register in library
        try:
            internal_ch = autoclip_db.get_db().execute(
                'SELECT id FROM channels WHERE youtube_channel_id=?', (yt_channel_id,)
            ).fetchone()
            if internal_ch:
                _u = autoclip_auth.get_current_user()
                autoclip_db.get_db().execute(
                    'INSERT OR IGNORE INTO thumbnails_library (channel_id, gcs_key, source_type, source_session_id, source_segment_index, created_by_user_id) '
                    'VALUES (?, ?, ?, ?, ?, ?)',
                    (internal_ch['id'], gcs_key, 'uploaded', session_id, segment_index, _u['id'] if _u else None)
                )
                autoclip_db.get_db().commit()
        except Exception:
            pass

        session['segments'][segment_index]['thumbnail_gcs_key'] = gcs_key
    else:
        # Legacy local fallback
        fname = f'custom_{session_id}_{segment_index}.{ext}'
        thumb_path = BASE_DIR / 'static' / 'thumbnails' / fname
        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        with open(thumb_path, 'wb') as out:
            out.write(file_bytes)
        thumbnail_url = f'/static/thumbnails/{fname}'

    session['segments'][segment_index]['thumbnail_url'] = thumbnail_url
    save_session(session_id, session)
    return jsonify({'status': 'uploaded', 'thumbnail_url': thumbnail_url})

@app.route('/api/execute_all/<session_id>', methods=['POST'])
def execute_all(session_id):
    """For each segment: cut clip if missing, generate title/description/thumbnail if missing.

    __AUTOCLIP_EXECUTE_ALL_V2__

    Sequential, blocking. Returns a summary of what was done and any errors.
    Requires the session to have channel_youtube_id (post-Phase-1 sessions).
    Legacy sessions without a channel bail with a clear message.
    """
    session = load_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404

    channel_yt_id = session.get('channel_youtube_id')
    summary = {
        'cut': 0, 'titles': 0, 'descriptions': 0, 'thumbnails': 0,
        'skipped': 0, 'errors': []
    }

    if not channel_yt_id:
        return jsonify({
            'status': 'error',
            'summary': summary,
            'error': ('This session has no channel assigned. Execute All requires a '
                      'channel — reupload the source video through /channels or use '
                      'the manual per-segment buttons instead.')
        }), 400

    # Style examples fetched once per session (title + description generation)
    try:
        _titles_examples, _descs_examples = _fetch_channel_recent_content(channel_yt_id)
    except Exception as _e:
        app.logger.warning(f'Style-example fetch failed: {_e}')
        _titles_examples, _descs_examples = [], []
    _title_style = "\n".join(f"- {t}" for t in _titles_examples[:10]) if _titles_examples else "(no examples available)"
    _desc_style = "\n---\n".join(_descs_examples[:5]) if _descs_examples else "(no examples available)"

    import base64
    import hashlib

    for i, segment in enumerate(session.get('segments', [])):
        # Canonical field names, with legacy fallback
        start = segment.get('start_time')
        if start is None:
            start = segment.get('start')
        end = segment.get('end_time')
        if end is None:
            end = segment.get('end')

        if start is None or end is None:
            summary['errors'].append(f'Segment {i+1}: missing start_time/end_time')
            summary['skipped'] += 1
            continue

        # -- 1. Cut clip if missing --
        has_gcs = bool(segment.get('clip_gcs_key'))
        _local = segment.get('clip_path')
        has_local = bool(_local) and os.path.exists(_local) if _local else False

        if not has_gcs and not has_local:
            try:
                source_video = get_local_video_path(session)
                if not source_video or not os.path.exists(source_video):
                    summary['errors'].append(
                        f'Segment {i+1} cut: source video not found on disk')
                    continue

                duration = float(end) - float(start)
                if duration <= 0:
                    summary['errors'].append(
                        f'Segment {i+1} cut: non-positive duration {duration}')
                    continue

                _tmp_dir = tempfile.mkdtemp(prefix='autoclip_execall_')
                _local_clip = os.path.join(_tmp_dir, f'{session_id}_clip_{i}.mp4')

                proc = subprocess.run([
                    'ffmpeg',
                    '-ss', str(start),
                    '-i', source_video,
                    '-t', str(duration),
                    '-c:v', 'copy',
                    '-c:a', 'copy',
                    '-avoid_negative_ts', 'make_zero',
                    '-y', _local_clip
                ], capture_output=True, text=True, timeout=1800)

                if proc.returncode != 0:
                    summary['errors'].append(
                        f'Segment {i+1} cut: ffmpeg exit {proc.returncode}: {(proc.stderr or "")[-200:]}')
                    try:
                        import shutil as _sh
                        _sh.rmtree(_tmp_dir, ignore_errors=True)
                    except Exception:
                        pass
                    continue

                gcs_key = gcs_storage.clip_key(channel_yt_id, session_id, i)
                gcs_storage.upload_file_to_gcs(_local_clip, gcs_key, content_type='video/mp4')

                segment['clip_gcs_key'] = gcs_key
                segment['clip_file'] = f'{session_id}_clip_{i}.mp4'
                segment['clip_path'] = None
                summary['cut'] += 1

                try:
                    import shutil as _sh
                    _sh.rmtree(_tmp_dir, ignore_errors=True)
                except Exception:
                    pass
            except Exception as e:
                summary['errors'].append(f'Segment {i+1} cut: {e}')
                continue

        # -- 2. Title if missing --
        if not segment.get('title'):
            try:
                transcript_snippet = segment_transcript_text(session, segment, 2000)
                prompt = (
                    "Generate a single YouTube video title for this clip segment.\n\n"
                    "Match the style of these recent titles from the same channel:\n"
                    f"{_title_style}\n\n"
                    "Segment transcript excerpt:\n"
                    f"{transcript_snippet}\n\n"
                    "Return ONLY the title text, no quotes, no markdown, no preamble."
                )
                resp = tracked_chat(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.7,
                )
                new_title = resp.choices[0].message.content.strip()
                if len(new_title) >= 2 and new_title[0] in ('"', "'") and new_title[-1] == new_title[0]:
                    new_title = new_title[1:-1]
                segment['title'] = new_title[:100]
                summary['titles'] += 1
            except Exception as e:
                summary['errors'].append(f'Segment {i+1} title: {e}')

        # -- 3. Description if missing --
        if not segment.get('description'):
            try:
                transcript_snippet = segment_transcript_text(session, segment, 3000)
                prompt = (
                    "Generate a YouTube video description (2-3 sentences) for this clip segment.\n\n"
                    "Match the style, length, and formatting of these recent descriptions from the same channel:\n"
                    f"{_desc_style}\n\n"
                    "Segment transcript:\n"
                    f"{transcript_snippet}\n\n"
                    "Return ONLY the description text, no markdown, no preamble."
                )
                resp = tracked_chat(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.7,
                )
                segment['description'] = resp.choices[0].message.content.strip()
                summary['descriptions'] += 1
            except Exception as e:
                summary['errors'].append(f'Segment {i+1} description: {e}')

        # -- 4. Thumbnail if missing --
        has_thumb = bool(segment.get('thumbnail_gcs_key')) or bool(segment.get('thumbnail_url'))
        if not has_thumb:
            try:
                _title = segment.get('title', '') or ''
                _topic = (segment.get('description', '') or '')[:200]
                thumb_prompt = (
                    "Generate a YouTube thumbnail for a college football podcast video.\n"
                    f"Title: {_title}\n"
                    f"Topic: {_topic}\n"
                    "Style: Bold, high energy sports thumbnail. Dark or team-colored background. "
                    "Dramatic lighting. Text overlay space. No people required, focus on energy and drama. "
                    "Make it look like a premium college football YouTube thumbnail."
                )
                _bu = autoclip_auth.get_current_user()
                _bdb = autoclip_db.get_db()
                _bok, _bwhy = plans.can_generate_thumbnail(_bu, _bdb, session_id, i)
                if not _bok:
                    summary['errors'].append(f'Segment {i+1} thumbnail: {_bwhy}')
                    raise RuntimeError(_bwhy)
                img_resp = client.images.generate(
                    model=IMAGE_MODEL,
                    prompt=thumb_prompt,
                    size='1536x1024',
                    quality='high',
                    n=1
                )
                plans.log_thumbnail_generation(_bdb, _bu['id'], session_id, i,
                                               IMAGE_MODEL, 'high')
                image_bytes = base64.b64decode(img_resp.data[0].b64_json)
                image_bytes = _resize_thumbnail_to_youtube(image_bytes)
                digest = hashlib.md5(image_bytes).hexdigest()[:12]
                thumb_key = gcs_storage.thumbnail_key(channel_yt_id, digest, ext='png')
                gcs_storage.upload_bytes_to_gcs(image_bytes, thumb_key, content_type='image/png')

                # Register in library (best-effort)
                try:
                    _ch_row = autoclip_db.get_db().execute(
                        'SELECT id FROM channels WHERE youtube_channel_id=?', (channel_yt_id,)
                    ).fetchone()
                    if _ch_row:
                        _u = autoclip_auth.get_current_user()
                        autoclip_db.get_db().execute(
                            'INSERT OR IGNORE INTO thumbnails_library '
                            '(channel_id, gcs_key, source_type, source_session_id, source_segment_index, created_by_user_id) '
                            'VALUES (?, ?, ?, ?, ?, ?)',
                            (_ch_row['id'], thumb_key, 'generated', session_id, i, _u['id'] if _u else None)
                        )
                        autoclip_db.get_db().commit()
                except Exception as _re:
                    app.logger.warning(f'thumbnail library register failed (seg {i}): {_re}')

                segment['thumbnail_gcs_key'] = thumb_key
                segment['thumbnail_url'] = f'/api/thumbnail-url?key={thumb_key}'
                summary['thumbnails'] += 1
            except Exception as e:
                summary['errors'].append(f'Segment {i+1} thumbnail: {e}')

    # Persist all changes
    try:
        save_session(session_id, session)
    except Exception as e:
        summary['errors'].append(f'Session save: {e}')
        return jsonify({'status': 'error', 'summary': summary}), 500

    return jsonify({'status': 'ok', 'summary': summary})


# ============================================================================
# End clip editor routes
# ============================================================================


# ============================================================================
# GCS-backed URL + library routes
# ============================================================================

@app.route('/api/thumbnail-url')
def thumbnail_signed_redirect():
    """Redirect the caller to a fresh signed URL for a thumbnail GCS key."""
    key = request.args.get('key', '')
    if not key or not key.startswith('thumbnails/'):
        return 'Bad key', 400
    user = autoclip_auth.get_current_user()
    if not user:
        return redirect(url_for('auth.login'))
    # Access check: key format = thumbnails/<yt_channel_id>/thumb_<hash>.png
    parts = key.split('/')
    if len(parts) < 3:
        return 'Bad key', 400
    yt_channel_id = parts[1]
    if user['role'] != 'admin':
        if not autoclip_db.user_has_channel_access_by_yt_id(user['id'], yt_channel_id):
            return 'Forbidden', 403
    try:
        url = gcs_storage.signed_url(key, expires_seconds=3600)
        return redirect(url)
    except Exception as e:
        return f'Failed: {e}', 500


@app.route('/api/thumbnails/library')
def thumbnails_library_list():
    """List thumbnails visible to the current user.

    Query params:
      channel_id       Optional. Filter to a single internal channels.id.
      style_ref_only   Optional. '1' returns only rows tagged is_style_reference.
    Response items include a short-lived signed display_url.
    """
    user = autoclip_auth.get_current_user()
    channel_id = request.args.get('channel_id')
    style_only = request.args.get('style_ref_only') == '1'
    db = autoclip_db.get_db()

    where = []
    params = []
    joins = "JOIN channels c ON c.id = t.channel_id"
    if user['role'] != 'admin':
        joins += " JOIN user_channels uc ON uc.channel_id = c.id"
        where.append("uc.user_id = ?")
        params.append(user['id'])
    if channel_id:
        where.append("t.channel_id = ?")
        try:
            params.append(int(channel_id))
        except (TypeError, ValueError):
            return jsonify({'error': 'channel_id must be int'}), 400
    if style_only:
        where.append("COALESCE(t.is_style_reference, 0) = 1")

    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    sql = (
        "SELECT t.*, c.title AS channel_title FROM thumbnails_library t "
        + joins + " " + where_clause +
        " ORDER BY t.created_at DESC"
    )
    rows = db.execute(sql, params).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        try:
            d['display_url'] = gcs_storage.signed_url(
                d['gcs_key'], expires_seconds=3600
            )
        except Exception as e:
            app.logger.warning(f"signed_url failed for {d.get('gcs_key')}: {e}")
            d['display_url'] = None
        result.append(d)
    return jsonify({'thumbnails': result})


@app.route('/api/thumbnails/<int:thumb_id>/delete', methods=['POST'])
def thumbnails_library_delete(thumb_id):
    user = autoclip_auth.get_current_user()
    db = autoclip_db.get_db()
    row = db.execute("SELECT * FROM thumbnails_library WHERE id=?", (thumb_id,)).fetchone()
    if not row:
        return jsonify({'error': 'not found'}), 404
    # Access check
    if user['role'] != 'admin':
        if not autoclip_db.user_has_channel_access(user['id'], row['channel_id']):
            return jsonify({'error': 'forbidden'}), 403
    # Delete from GCS
    try:
        gcs_storage.delete_from_gcs(row['gcs_key'])
    except Exception as e:
        app.logger.warning(f'GCS delete failed for {row["gcs_key"]}: {e}')
    db.execute("DELETE FROM thumbnails_library WHERE id=?", (thumb_id,))
    db.commit()
    return jsonify({'status': 'deleted'})


@app.route('/api/thumbnails/<int:thumb_id>/download')
def thumbnails_library_download(thumb_id):
    """Redirect to a signed URL flagged as attachment (browser will download)."""
    user = autoclip_auth.get_current_user()
    db = autoclip_db.get_db()
    row = db.execute("SELECT * FROM thumbnails_library WHERE id=?", (thumb_id,)).fetchone()
    if not row:
        return 'not found', 404
    if user['role'] != 'admin':
        if not autoclip_db.user_has_channel_access(user['id'], row['channel_id']):
            return 'forbidden', 403
    # Stream with an attachment header. Redirecting to a signed URL just
    # navigates the browser to the image, which renders it instead of saving.
    try:
        import io as _io
        from flask import send_file as _send_file
        d = dict(row)
        key = d['gcs_key']
        data = gcs_storage.download_bytes(key)
        ext = os.path.splitext(key)[1].lower() or '.png'
        mime = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                '.webp': 'image/webp', '.gif': 'image/gif'}.get(ext, 'application/octet-stream')
        base = (d.get('display_name') or f'thumbnail_{thumb_id}').strip()
        base = re.sub(r'[^A-Za-z0-9._-]+', '_', base) or f'thumbnail_{thumb_id}'
        if not base.lower().endswith(ext):
            base += ext
        return _send_file(_io.BytesIO(data), mimetype=mime,
                          as_attachment=True, download_name=base)
    except Exception as e:
        app.logger.exception('thumbnail download failed')
        return f'Failed: {e}', 500


@app.route('/api/thumbnails/library/upload', methods=['POST'])
def thumbnails_library_upload():
    """Upload a thumbnail image directly to the library (GCS-backed)."""
    user = autoclip_auth.get_current_user()
    channel_id_raw = request.form.get('channel_id')
    if not channel_id_raw:
        return jsonify({'error': 'channel_id required'}), 400
    try:
        channel_id = int(channel_id_raw)
    except (TypeError, ValueError):
        return jsonify({'error': 'channel_id must be int'}), 400

    if user['role'] != 'admin':
        if not autoclip_db.user_has_channel_access(user['id'], channel_id):
            return jsonify({'error': 'forbidden'}), 403

    if 'file' not in request.files:
        return jsonify({'error': 'file required (multipart field "file")'}), 400
    f = request.files['file']
    if not f or not f.filename:
        return jsonify({'error': 'empty file'}), 400

    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else 'png'
    if ext == 'jpg':
        ext = 'jpeg'
    if ext not in ('png', 'jpeg', 'webp'):
        return jsonify({'error': f'unsupported extension {ext}'}), 400
    content_type = f'image/{ext}'

    db = autoclip_db.get_db()
    ch = db.execute(
        "SELECT youtube_channel_id FROM channels WHERE id=?", (channel_id,)
    ).fetchone()
    if not ch:
        return jsonify({'error': 'channel not found'}), 404
    yt_channel_id = ch['youtube_channel_id']

    import hashlib
    data = f.read()
    if not data:
        return jsonify({'error': 'file is empty'}), 400
    if len(data) > 20 * 1024 * 1024:
        return jsonify({'error': 'file too large (max 20MB)'}), 400
    digest = hashlib.md5(data).hexdigest()[:12]
    store_ext = 'png' if ext == 'jpeg' else ext  # normalize ext used in GCS key
    gcs_key = gcs_storage.thumbnail_key(yt_channel_id, digest, ext=store_ext)

    try:
        gcs_storage.upload_bytes_to_gcs(data, gcs_key, content_type=content_type)
    except Exception as e:
        app.logger.exception('GCS upload failed')
        return jsonify({'error': f'gcs upload failed: {e}'}), 500

    try:
        cur = db.execute(
            "INSERT OR IGNORE INTO thumbnails_library "
            "(channel_id, gcs_key, source_type, source_session_id, source_segment_index, "
            "created_by_user_id, is_style_reference) "
            "VALUES (?, ?, 'uploaded', NULL, NULL, ?, 1)",
            (channel_id, gcs_key, user['id'])
        )
        db.commit()
        thumb_id = cur.lastrowid
        if not thumb_id:
            row = db.execute(
                "SELECT id FROM thumbnails_library WHERE gcs_key=?", (gcs_key,)
            ).fetchone()
            thumb_id = row['id'] if row else None
    except Exception as e:
        app.logger.exception('DB insert failed')
        return jsonify({'error': f'db insert failed: {e}'}), 500

    return jsonify({'status': 'uploaded', 'id': thumb_id, 'gcs_key': gcs_key})


@app.route('/api/thumbnails/<int:thumb_id>/toggle_style_ref', methods=['POST'])
def thumbnails_library_toggle_style_ref(thumb_id):
    """Flip the is_style_reference flag on a library thumbnail.

    Body (JSON, optional): {"is_style_reference": true|false}
    If omitted, toggles the current value.
    """
    user = autoclip_auth.get_current_user()
    db = autoclip_db.get_db()
    row = db.execute(
        "SELECT * FROM thumbnails_library WHERE id=?", (thumb_id,)
    ).fetchone()
    if not row:
        return jsonify({'error': 'not found'}), 404
    if user['role'] != 'admin':
        if not autoclip_db.user_has_channel_access(user['id'], row['channel_id']):
            return jsonify({'error': 'forbidden'}), 403
    payload = request.get_json(silent=True) or {}
    if 'is_style_reference' in payload:
        new_val = 1 if payload['is_style_reference'] else 0
    else:
        new_val = 0 if (row['is_style_reference'] or 0) else 1
    db.execute(
        "UPDATE thumbnails_library SET is_style_reference=? WHERE id=?",
        (new_val, thumb_id)
    )
    db.commit()
    return jsonify({'status': 'ok', 'is_style_reference': new_val})


# ============================================================================
# ADS LIBRARY — GCS-backed, channel-scoped (Phase 3a)
# ============================================================================

@app.route('/api/ads/library')
def ads_library_list():
    """List ads for a channel + current intro/outro assignments."""
    user = autoclip_auth.get_current_user()
    channel_id = request.args.get('channel_id')
    if not channel_id:
        return jsonify({'ads': [], 'intro_ad_id': None, 'outro_ad_id': None})
    try:
        channel_id = int(channel_id)
    except (TypeError, ValueError):
        return jsonify({'error': 'channel_id must be int'}), 400

    db = autoclip_db.get_db()
    if user['role'] != 'admin':
        if not autoclip_db.user_has_channel_access(user['id'], channel_id):
            return jsonify({'error': 'forbidden'}), 403

    rows = db.execute(
        "SELECT a.*, c.title AS channel_title FROM ads a "
        "JOIN channels c ON c.id = a.channel_id "
        "WHERE a.channel_id = ? ORDER BY a.created_at DESC",
        (channel_id,)
    ).fetchall()

    cfg = db.execute(
        "SELECT intro_ad_id, outro_ad_id FROM channel_ad_config WHERE channel_id=?",
        (channel_id,)
    ).fetchone()
    intro_id = cfg['intro_ad_id'] if cfg else None
    outro_id = cfg['outro_ad_id'] if cfg else None

    result = []
    for r in rows:
        d = dict(r)
        try:
            d['display_url'] = gcs_storage.signed_url(d['gcs_key'], expires_seconds=3600)
        except Exception as e:
            app.logger.warning(f"signed_url failed for {d.get('gcs_key')}: {e}")
            d['display_url'] = None
        if d['id'] == intro_id:
            d['role'] = 'intro'
        elif d['id'] == outro_id:
            d['role'] = 'outro'
        else:
            d['role'] = 'none'
        result.append(d)

    return jsonify({
        'ads': result,
        'intro_ad_id': intro_id,
        'outro_ad_id': outro_id
    })


@app.route('/api/ads/library/upload', methods=['POST'])
def ads_library_upload():
    """Upload a new ad file to GCS + register in DB with ffprobe duration."""
    user = autoclip_auth.get_current_user()
    channel_id_raw = request.form.get('channel_id')
    if not channel_id_raw:
        return jsonify({'error': 'channel_id required'}), 400
    try:
        channel_id = int(channel_id_raw)
    except (TypeError, ValueError):
        return jsonify({'error': 'channel_id must be int'}), 400

    if user['role'] != 'admin':
        if not autoclip_db.user_has_channel_access(user['id'], channel_id):
            return jsonify({'error': 'forbidden'}), 403

    if 'file' not in request.files:
        return jsonify({'error': 'file required'}), 400
    f = request.files['file']
    if not f or not f.filename:
        return jsonify({'error': 'empty file'}), 400

    display_name = (request.form.get('display_name') or f.filename).strip()
    if not display_name:
        display_name = f.filename

    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else 'mp4'
    if ext not in ('mp3', 'mp4', 'wav', 'm4a', 'mpeg', 'webm', 'aac', 'ogg'):
        return jsonify({'error': f'unsupported extension {ext}'}), 400

    ct_map = {
        'mp3': 'audio/mpeg', 'mpeg': 'audio/mpeg',
        'wav': 'audio/wav',
        'mp4': 'video/mp4', 'm4a': 'audio/mp4',
        'webm': 'video/webm',
        'aac': 'audio/aac',
        'ogg': 'audio/ogg',
    }
    content_type = ct_map.get(ext, 'application/octet-stream')

    db = autoclip_db.get_db()
    ch = db.execute(
        "SELECT youtube_channel_id FROM channels WHERE id=?", (channel_id,)
    ).fetchone()
    if not ch:
        return jsonify({'error': 'channel not found'}), 404
    yt_channel_id = ch['youtube_channel_id']

    import hashlib
    data = f.read()
    if not data:
        return jsonify({'error': 'file is empty'}), 400
    if len(data) > 200 * 1024 * 1024:
        return jsonify({'error': 'file too large (max 200MB)'}), 400
    digest = hashlib.md5(data).hexdigest()[:10]
    slug = ''.join(c if c.isalnum() else '_' for c in display_name)[:40].strip('_') or 'ad'
    gcs_key = f"ads/{yt_channel_id}/{slug}_{digest}.{ext}"

    # Best-effort duration via ffprobe
    duration = None
    import tempfile, subprocess, json as _json
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=f'.{ext}', prefix='autoclip_ad_probe_')
    try:
        with os.fdopen(tmp_fd, 'wb') as tmp_f:
            tmp_f.write(data)
        proc = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', tmp_path],
            capture_output=True, text=True, timeout=15
        )
        if proc.returncode == 0:
            parsed = _json.loads(proc.stdout or '{}')
            dur_str = parsed.get('format', {}).get('duration')
            if dur_str:
                duration = float(dur_str)
    except Exception as e:
        app.logger.warning(f'ffprobe failed for ad: {e}')
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    try:
        gcs_storage.upload_bytes_to_gcs(data, gcs_key, content_type=content_type)
    except Exception as e:
        app.logger.exception('GCS upload failed for ad')
        return jsonify({'error': f'gcs upload failed: {e}'}), 500

    try:
        cur = db.execute(
            "INSERT OR IGNORE INTO ads "
            "(channel_id, gcs_key, display_name, duration_sec, file_size_bytes, "
            "content_type, created_by_user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (channel_id, gcs_key, display_name, duration, len(data), content_type, user['id'])
        )
        db.commit()
        ad_id = cur.lastrowid
        if not ad_id:
            row = db.execute("SELECT id FROM ads WHERE gcs_key=?", (gcs_key,)).fetchone()
            ad_id = row['id'] if row else None
    except Exception as e:
        app.logger.exception('DB insert failed for ad')
        return jsonify({'error': f'db insert failed: {e}'}), 500

    return jsonify({
        'status': 'uploaded',
        'id': ad_id,
        'gcs_key': gcs_key,
        'display_name': display_name,
        'duration_sec': duration,
        'file_size_bytes': len(data),
        'content_type': content_type
    })


@app.route('/api/ads/library/<int:ad_id>/delete', methods=['POST'])
def ads_library_delete(ad_id):
    """Delete an ad from GCS and DB. FK cascade auto-clears intro/outro roles."""
    user = autoclip_auth.get_current_user()
    db = autoclip_db.get_db()
    row = db.execute("SELECT * FROM ads WHERE id=?", (ad_id,)).fetchone()
    if not row:
        return jsonify({'error': 'not found'}), 404
    if user['role'] != 'admin':
        if not autoclip_db.user_has_channel_access(user['id'], row['channel_id']):
            return jsonify({'error': 'forbidden'}), 403
    try:
        gcs_storage.delete_from_gcs(row['gcs_key'])
    except Exception as e:
        app.logger.warning(f'GCS delete failed for {row["gcs_key"]}: {e}')
    db.execute("DELETE FROM ads WHERE id=?", (ad_id,))
    db.commit()
    return jsonify({'status': 'deleted'})



# __DESCRIPTION_TEXT_ENDPOINTS_V1__
@app.route('/api/ads/library/<int:ad_id>/description', methods=['POST'])
def ads_library_set_description(ad_id):
    """Set the description blurb injected into YouTube descriptions when this
    ad is selected on a segment. Body: {"description_text": "..."}"""
    user = autoclip_auth.get_current_user()
    payload = request.get_json(silent=True) or {}
    text = (payload.get('description_text') or '').strip()
    if len(text) > 2000:
        return jsonify({'error': 'description_text max 2000 chars'}), 400
    db = autoclip_db.get_db()
    row = db.execute("SELECT * FROM ads WHERE id=?", (ad_id,)).fetchone()
    if not row:
        return jsonify({'error': 'not found'}), 404
    if user['role'] != 'admin':
        if not autoclip_db.user_has_channel_access(user['id'], row['channel_id']):
            return jsonify({'error': 'forbidden'}), 403
    db.execute("UPDATE ads SET description_text=? WHERE id=?", (text or None, ad_id))
    db.commit()
    return jsonify({'ok': True, 'ad_id': ad_id, 'description_text': text})


@app.route('/api/channels/<int:channel_id>/description_settings', methods=['GET', 'POST'])
def channel_description_settings(channel_id):
    """GET or set a channel's base description and description assembly order."""
    user = autoclip_auth.get_current_user()
    db = autoclip_db.get_db()
    row = db.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
    if not row:
        return jsonify({'error': 'not found'}), 404
    if user['role'] != 'admin':
        if not autoclip_db.user_has_channel_access(user['id'], channel_id):
            return jsonify({'error': 'forbidden'}), 403

    if request.method == 'GET':
        d = dict(row)
        return jsonify({
            'channel_id': channel_id,
            'base_description': d.get('base_description') or '',
            'description_order': d.get('description_order') or 'ai_ads_base',
            'orders': list(DESCRIPTION_ORDERS.keys()),
        })

    payload = request.get_json(silent=True) or {}
    base = (payload.get('base_description') or '').strip()
    order = payload.get('description_order') or 'ai_ads_base'
    if order not in DESCRIPTION_ORDERS:
        return jsonify({'error': f'description_order must be one of {list(DESCRIPTION_ORDERS)}'}), 400
    if len(base) > 4000:
        return jsonify({'error': 'base_description max 4000 chars'}), 400
    db.execute(
        "UPDATE channels SET base_description=?, description_order=?, "
        "updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (base or None, order, channel_id)
    )
    db.commit()
    return jsonify({'ok': True, 'base_description': base, 'description_order': order})


@app.route('/api/ads/library/<int:ad_id>/set_role', methods=['POST'])
def ads_library_set_role(ad_id):
    """Assign this ad as intro or outro for its channel, or clear.

    Body: {"role": "intro" | "outro" | "none"}
    """
    user = autoclip_auth.get_current_user()
    payload = request.get_json(silent=True) or {}
    role = payload.get('role', 'none')
    if role not in ('intro', 'outro', 'none'):
        return jsonify({'error': "role must be 'intro', 'outro', or 'none'"}), 400

    db = autoclip_db.get_db()
    row = db.execute("SELECT * FROM ads WHERE id=?", (ad_id,)).fetchone()
    if not row:
        return jsonify({'error': 'not found'}), 404
    if user['role'] != 'admin':
        if not autoclip_db.user_has_channel_access(user['id'], row['channel_id']):
            return jsonify({'error': 'forbidden'}), 403

    ch_id = row['channel_id']
    db.execute(
        "INSERT OR IGNORE INTO channel_ad_config (channel_id) VALUES (?)", (ch_id,)
    )
    if role == 'intro':
        db.execute(
            "UPDATE channel_ad_config SET intro_ad_id=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE channel_id=?", (ad_id, ch_id)
        )
    elif role == 'outro':
        db.execute(
            "UPDATE channel_ad_config SET outro_ad_id=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE channel_id=?", (ad_id, ch_id)
        )
    else:  # 'none' — clear whichever role this ad currently holds
        db.execute(
            "UPDATE channel_ad_config "
            "SET intro_ad_id = CASE WHEN intro_ad_id=? THEN NULL ELSE intro_ad_id END, "
            "    outro_ad_id = CASE WHEN outro_ad_id=? THEN NULL ELSE outro_ad_id END, "
            "    updated_at = CURRENT_TIMESTAMP "
            "WHERE channel_id=?", (ad_id, ad_id, ch_id)
        )
    db.commit()
    return jsonify({'status': 'ok', 'role': role, 'channel_id': ch_id, 'ad_id': ad_id})


@app.route('/api/ads/library/<int:ad_id>/download')
def ads_library_download(ad_id):
    """Redirect to a signed URL for downloading the ad file."""
    user = autoclip_auth.get_current_user()
    db = autoclip_db.get_db()
    row = db.execute("SELECT * FROM ads WHERE id=?", (ad_id,)).fetchone()
    if not row:
        return 'not found', 404
    if user['role'] != 'admin':
        if not autoclip_db.user_has_channel_access(user['id'], row['channel_id']):
            return 'forbidden', 403
    # Ads can be large video files, so redirect rather than streaming through
    # the VM - but ask GCS for an attachment disposition so the browser saves
    # the file instead of navigating to it.
    try:
        d = dict(row)
        key = d['gcs_key']
        ext = os.path.splitext(key)[1].lower() or '.mp4'
        base = (d.get('display_name') or f'ad_{ad_id}').strip()
        base = re.sub(r'[^A-Za-z0-9._-]+', '_', base) or f'ad_{ad_id}'
        if not base.lower().endswith(ext):
            base += ext
        url = gcs_storage.signed_url(key, expires_seconds=600, download_name=base)
        return redirect(url)
    except Exception as e:
        app.logger.exception('ad download failed')
        return f'Failed: {e}', 500


@app.route('/api/sessions/<session_id>/segments/<int:segment_index>/ads', methods=['POST'])
def save_segment_ads(session_id, segment_index):
    """Save intro/outro/mid ad assignments for a specific segment.

    Body (JSON): any subset of
      {"intro_ad_id": int|null,
       "outro_ad_id": int|null,
       "mid_ad_id": int|null,
       "mid_ad_position_sec": float|null,     (null = AI auto-places)
       "extra_mid_ad_ids": [int, ...],        (max 3 beyond the first)
       "extra_mid_positions": [float|null, ...],
       "description_ad_ids": [int, ...]}      (independent of video placement)

    Only keys present in the body are updated; omitted keys are untouched.
    Each ad_id must belong to the same channel as the session.
    """
    user = autoclip_auth.get_current_user()
    payload = request.get_json(silent=True) or {}

    session = load_session(session_id)
    if not session:
        return jsonify({'error': 'session not found'}), 404
    if segment_index < 0 or segment_index >= len(session.get('segments', [])):
        return jsonify({'error': 'segment not found'}), 404

    channel_id = session.get('channel_id')
    if not channel_id:
        return jsonify({'error': 'session has no channel; cannot assign ads'}), 400
    channel_id = int(channel_id)

    if user['role'] != 'admin':
        if not autoclip_db.user_has_channel_access(user['id'], channel_id):
            return jsonify({'error': 'forbidden'}), 403

    db = autoclip_db.get_db()

    def _validate_ad(ad_id):
        if ad_id is None or ad_id == '':
            return (None, None)
        try:
            ad_id = int(ad_id)
        except (TypeError, ValueError):
            return (None, 'not an integer')
        row = db.execute("SELECT channel_id FROM ads WHERE id=?", (ad_id,)).fetchone()
        if not row:
            return (None, f'ad id {ad_id} not found')
        if row['channel_id'] != channel_id:
            return (None, f'ad id {ad_id} belongs to a different channel')
        return (ad_id, None)

    updates = {}
    for key in ('intro_ad_id', 'outro_ad_id', 'mid_ad_id'):
        if key in payload:
            val, err = _validate_ad(payload[key])
            if err:
                return jsonify({'error': f'{key}: {err}'}), 400
            updates[key] = val

    for pkey in ('mid_ad_position_sec',):
        if pkey in payload:
            raw = payload[pkey]
            if raw is None or (isinstance(raw, str) and raw.strip() == ''):
                updates[pkey] = None
            else:
                try:
                    pos = float(raw)
                    if pos < 0:
                        return jsonify({'error': f'{pkey} must be >= 0'}), 400
                    updates[pkey] = pos
                except (TypeError, ValueError):
                    return jsonify({'error': f'{pkey} must be a number'}), 400

    # Additional mid-roll ads beyond the first. Max 3 extra (4 total mids).
    MAX_EXTRA_MIDS = 3
    if 'extra_mid_ad_ids' in payload:
        raw = payload['extra_mid_ad_ids'] or []
        if not isinstance(raw, list):
            return jsonify({'error': 'extra_mid_ad_ids must be a list'}), 400
        if len(raw) > MAX_EXTRA_MIDS:
            return jsonify({'error': f'at most {MAX_EXTRA_MIDS} extra mid ads'}), 400
        clean = []
        for aid in raw:
            val, err = _validate_ad(aid)
            if err:
                return jsonify({'error': f'extra_mid_ad_ids: {err}'}), 400
            clean.append(val)
        updates['extra_mid_ad_ids'] = clean
    if 'extra_mid_positions' in payload:
        raw = payload['extra_mid_positions'] or []
        if not isinstance(raw, list):
            return jsonify({'error': 'extra_mid_positions must be a list'}), 400
        clean = []
        for p in raw[:MAX_EXTRA_MIDS]:
            if p is None or (isinstance(p, str) and p.strip() == ''):
                clean.append(None)
            else:
                try:
                    fp = float(p)
                    if fp < 0:
                        return jsonify({'error': 'extra_mid_positions must be >= 0'}), 400
                    clean.append(fp)
                except (TypeError, ValueError):
                    return jsonify({'error': 'extra_mid_positions must be numbers'}), 400
        updates['extra_mid_positions'] = clean

    # Description ads are INDEPENDENT of video ad placement. This is the list
    # whose description_text gets appended to the YouTube description.
    if 'description_ad_ids' in payload:
        raw = payload['description_ad_ids'] or []
        if not isinstance(raw, list):
            return jsonify({'error': 'description_ad_ids must be a list'}), 400
        clean = []
        for aid in raw:
            val, err = _validate_ad(aid)
            if err:
                return jsonify({'error': f'description_ad_ids: {err}'}), 400
            if val is not None and val not in clean:
                clean.append(val)
        updates['description_ad_ids'] = clean

    segment = session['segments'][segment_index]
    for k, v in updates.items():
        segment[k] = v
    save_session(session_id, session)

    return jsonify({
        'status': 'ok',
        'session_id': session_id,
        'segment_index': segment_index,
        'updates': updates
    })





# ============================================================================
# Chunked parallel upload routes
# ============================================================================

CHUNK_SIZE_BYTES = 32 * 1024 * 1024  # 32 MB


@app.route('/api/upload/chunks/init', methods=['POST'])
def upload_chunks_init():
    """Start a chunked upload. Returns signed PUT URLs the browser will hit in parallel.

    Request JSON:
        {
          "filename": "myshow.mp4",
          "content_type": "video/mp4",
          "file_size": 2823456789
        }

    Response JSON:
        {
          "session_id": "<uuid>",
          "chunk_size": 33554432,
          "chunk_count": 85,
          "chunks": [
            {"index": 0, "gcs_key": "uploads/chunks/<sid>/part_0.bin", "upload_url": "https://..."},
            ...
          ]
        }
    """
    user = autoclip_auth.get_current_user()
    if not user['has_clipping']:
        return jsonify({'error': 'clipping subscription required'}), 403

    data = request.get_json() or {}
    filename = data.get('filename', 'video.mp4')
    content_type = data.get('content_type', 'video/mp4')
    file_size = int(data.get('file_size', 0))
    if file_size <= 0:
        return jsonify({'error': 'file_size must be positive'}), 400

    session_id = uuid.uuid4().hex[:8]

    # Ceil division for chunk count
    chunk_count = (file_size + CHUNK_SIZE_BYTES - 1) // CHUNK_SIZE_BYTES

    try:
        chunks = gcs_storage.chunk_upload_urls(session_id, chunk_count, expires_seconds=3600)
    except Exception as e:
        return jsonify({'error': f'Failed to prepare chunks: {e}'}), 500

    return jsonify({
        'session_id': session_id,
        'chunk_size': CHUNK_SIZE_BYTES,
        'chunk_count': chunk_count,
        'chunks': chunks,
        'content_type': content_type,
        'filename': filename,
        'file_size': file_size,
    })


@app.route('/api/upload/chunks/complete', methods=['POST'])
def upload_chunks_complete():
    """Compose the uploaded chunks into the final GCS object, then create the AutoClip session.

    Request JSON:
        {
          "session_id": "<same as init>",
          "channel_id": <int, from user's channels>,
          "show_name": "My Show — Aug 8",
          "content_type": "video/mp4",
          "chunk_count": 85
        }
    """
    user = autoclip_auth.get_current_user()
    if not user['has_clipping']:
        return jsonify({'error': 'clipping subscription required'}), 403

    data = request.get_json() or {}
    session_id = data.get('session_id')
    channel_db_id = data.get('channel_id')
    show_name = data.get('show_name', 'Live Show')
    content_type = data.get('content_type', 'video/mp4')
    chunk_count = int(data.get('chunk_count', 0))

    if not session_id or chunk_count <= 0 or not channel_db_id:
        return jsonify({'error': 'session_id, channel_id, chunk_count required'}), 400

    # Access check
    ch = autoclip_db.get_channel_by_id(int(channel_db_id))
    if not ch:
        return jsonify({'error': 'channel not found'}), 400
    if user['role'] != 'admin' and not autoclip_db.user_has_channel_access(user['id'], ch['id']):
        return jsonify({'error': 'no access to selected channel'}), 403

    # Compose chunks into the final object
    final_key = gcs_storage.source_key(ch['youtube_channel_id'], session_id)
    try:
        gcs_storage.compose_chunks(session_id, chunk_count, final_key, content_type=content_type)
    except Exception as e:
        return jsonify({'error': f'compose failed: {e}'}), 500

    # Clean up chunk objects
    try:
        gcs_storage.delete_chunks(session_id)
    except Exception as e:
        app.logger.warning(f'Failed to delete chunks for {session_id}: {e}')
        # Not fatal — GCS lifecycle will clean them up eventually

    # Create the session (same shape as /api/upload/complete)
    session = {
        'id': session_id,
        'show_name': show_name,
        'gcs_key': final_key,
        'gcs_uri': f'gs://{gcs_storage.BUCKET_NAME}/{final_key}',
        'owner_user_id': user['id'],
        'channel_id': int(channel_db_id),
        'channel_youtube_id': ch['youtube_channel_id'],
        'transcript': None,
        'segments': [],
        'clips': []
    }
    save_session(session_id, session)

    # Bump usage: this counts as one "show" processed
    try:
        _period = _current_period()
        _db = autoclip_db.get_db()
        _db.execute(
            "INSERT INTO usage_monthly (user_id, period, clipping_shows) VALUES (?, ?, 1) "
            "ON CONFLICT(user_id, period) DO UPDATE SET clipping_shows = clipping_shows + 1",
            (user['id'], _period)
        )
        _db.commit()
    except Exception:
        pass

    return jsonify({'session_id': session_id, 'status': 'uploaded'})



@app.route('/api/youtube/playlists/<int:channel_pk>')
def list_youtube_playlists(channel_pk):
    """List the playlists on a channel the current user has access to.

    Uses the channel-specific OAuth token to hit the YouTube Data API.
    """
    user = autoclip_auth.get_current_user()
    if not user:
        return jsonify({'error': 'not authenticated'}), 401

    ch = autoclip_db.get_channel_by_id(channel_pk)
    if not ch:
        return jsonify({'error': 'channel not found'}), 404
    if user['role'] != 'admin' and not autoclip_db.user_has_channel_access(user['id'], ch['id']):
        return jsonify({'error': 'no access to this channel'}), 403

    token_path = BASE_DIR / 'credentials' / 'tokens' / f"{ch['youtube_channel_id']}.json"
    if not token_path.exists():
        return jsonify({'error': 'no token for this channel', 'playlists': []}), 400

    try:
        with open(token_path) as f:
            creds_data = json.load(f)
        creds = Credentials(
            token=creds_data['token'],
            refresh_token=creds_data.get('refresh_token'),
            token_uri='https://oauth2.googleapis.com/token',
            client_id=creds_data.get('client_id'),
            client_secret=creds_data.get('client_secret'),
        )
        yt = build('youtube', 'v3', credentials=creds)
        # mine=true means "belonging to the authenticated user"
        resp = yt.playlists().list(part='snippet', mine=True, maxResults=50).execute()
        playlists = [
            {'id': p['id'], 'title': p['snippet']['title']}
            for p in resp.get('items', [])
        ]
        return jsonify({'playlists': playlists})
    except Exception as e:
        return jsonify({'error': f'YouTube API call failed: {e}', 'playlists': []}), 500



@app.route('/api/generate_thumbnail_with_refs', methods=['POST'])
def generate_thumbnail_with_refs():
    """Generate a thumbnail using OpenAI images.edit with reference images.

    multipart/form-data:
      - session_id (required)
      - segment_index (required)
      - guidance (optional text steering the AI)
      - image_0, image_1, ... image_4 (up to 5 reference image files)

    Returns JSON same shape as /api/generate_thumbnail.
    """
    session_id = request.form.get('session_id')
    segment_index = request.form.get('segment_index')
    guidance = (request.form.get('guidance') or '').strip()

    if not session_id or segment_index is None:
        return jsonify({'error': 'session_id and segment_index required'}), 400
    segment_index = int(segment_index)

    session = load_session(session_id)
    if not session or segment_index >= len(session.get('segments', [])):
        return jsonify({'error': 'session or segment not found'}), 404
    segment = session['segments'][segment_index]

    _ru = autoclip_auth.get_current_user()
    _rdb = autoclip_db.get_db()
    _rok, _rwhy = plans.can_generate_thumbnail(_ru, _rdb, session_id, segment_index)
    if not _rok:
        return jsonify({'error': _rwhy, 'upgrade_url': '/pricing'}), 402

    yt_channel_id = session.get('channel_youtube_id')
    if not yt_channel_id:
        return jsonify({'error': 'session has no channel_youtube_id'}), 400

    # Collect uploaded reference image files (up to 5 inline slots)
    ref_files = []
    for i in range(5):
        f = request.files.get(f'image_{i}')
        if f and f.filename:
            ref_files.append(f)

    # Collect library thumbnail IDs (comma-separated) and validate access
    library_ids_raw = (request.form.get('library_thumb_ids') or '').strip()
    library_ids = []
    if library_ids_raw:
        try:
            library_ids = [int(x) for x in library_ids_raw.split(',') if x.strip()]
        except ValueError:
            return jsonify({'error': 'library_thumb_ids must be comma-separated ints'}), 400

    if not ref_files and not library_ids:
        return jsonify({'error': 'at least one reference image or library pick required (use /api/generate_thumbnail for text-only)'}), 400

    MAX_REFS = 16
    total_refs = len(ref_files) + len(library_ids)
    if total_refs > MAX_REFS:
        return jsonify({'error': f'too many references: {total_refs} (max {MAX_REFS})'}), 400

    # Look up + access-check library refs
    library_records = []
    if library_ids:
        _u = autoclip_auth.get_current_user()
        _db = autoclip_db.get_db()
        for tid in library_ids:
            _row = _db.execute(
                "SELECT * FROM thumbnails_library WHERE id=?", (tid,)
            ).fetchone()
            if not _row:
                return jsonify({'error': f'library thumbnail {tid} not found'}), 404
            if _u['role'] != 'admin':
                if not autoclip_db.user_has_channel_access(_u['id'], _row['channel_id']):
                    return jsonify({'error': f'forbidden: no access to library thumbnail {tid}'}), 403
            library_records.append(dict(_row))

    # Save uploaded files to temp so we can pass file handles to OpenAI
    import tempfile
    tmp_dir = tempfile.mkdtemp(prefix='autoclip_thumb_refs_')
    tmp_paths = []
    try:
        for i, f in enumerate(ref_files):
            ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else 'png'
            if ext not in ('png', 'jpg', 'jpeg', 'webp'):
                ext = 'png'
            path = os.path.join(tmp_dir, f'ref_{i}.{ext}')
            f.save(path)
            tmp_paths.append(path)
        # Download library refs from GCS into the same tmp_dir
        for j, _rec in enumerate(library_records):
            _key = _rec['gcs_key']
            _ext = _key.rsplit('.', 1)[-1].lower() if '.' in _key else 'png'
            if _ext not in ('png', 'jpg', 'jpeg', 'webp'):
                _ext = 'png'
            _lib_path = os.path.join(tmp_dir, f'lib_{j}.{_ext}')
            gcs_storage.download_from_gcs(_key, _lib_path)
            tmp_paths.append(_lib_path)

        # Build the prompt
        title = segment.get('title', '')
        topic = segment.get('description', '')[:200]
        base_prompt = (
            "Generate a YouTube thumbnail for a college football podcast video. "
            f"Title: {title}. "
            f"Topic: {topic}. "
            "Bold, high energy sports style with dramatic lighting. "
            "Use the provided reference images as visual references for the people, "
            "logos, or objects that should appear in the thumbnail."
        )
        prompt = (guidance + " " + base_prompt) if guidance else base_prompt

        # Open files as binary for the API call
        file_handles = [open(p, 'rb') for p in tmp_paths]
        try:
            response = client.images.edit(
                model=IMAGE_MODEL,
                image=file_handles if len(file_handles) > 1 else file_handles[0],
                prompt=prompt,
                size="1536x1024",
                quality="high",
                n=1
            )
            plans.log_thumbnail_generation(_rdb, _ru['id'], session_id,
                                           segment_index, IMAGE_MODEL, 'high')
        finally:
            for fh in file_handles:
                try:
                    fh.close()
                except Exception:
                    pass

        # Decode the output
        import base64, hashlib
        image_b64 = response.data[0].b64_json
        image_bytes = base64.b64decode(image_b64)
        image_bytes = _resize_thumbnail_to_youtube(image_bytes)  # 1280x720 for YouTube

        # Save to GCS
        digest = hashlib.md5(image_bytes).hexdigest()[:12]
        gcs_key = gcs_storage.thumbnail_key(yt_channel_id, digest, ext='png')
        gcs_storage.upload_bytes_to_gcs(image_bytes, gcs_key, content_type='image/png')
        thumbnail_url = f'/api/thumbnail-url?key={gcs_key}'

        # Register in library
        try:
            internal_ch = autoclip_db.get_db().execute(
                'SELECT id FROM channels WHERE youtube_channel_id=?', (yt_channel_id,)
            ).fetchone()
            if internal_ch:
                _u = autoclip_auth.get_current_user()
                autoclip_db.get_db().execute(
                    'INSERT OR IGNORE INTO thumbnails_library (channel_id, gcs_key, source_type, source_session_id, source_segment_index, created_by_user_id) '
                    'VALUES (?, ?, ?, ?, ?, ?)',
                    (internal_ch['id'], gcs_key, 'generated_with_refs', session_id, segment_index, _u['id'] if _u else None)
                )
                autoclip_db.get_db().commit()
        except Exception as _e:
            app.logger.warning(f'Failed to register thumbnail: {_e}')

        # Save on segment
        session['segments'][segment_index]['thumbnail_gcs_key'] = gcs_key
        session['segments'][segment_index]['thumbnail_url'] = thumbnail_url
        save_session(session_id, session)

        return jsonify({
            'status': 'generated',
            'image_url': thumbnail_url,
            'thumbnail_url': thumbnail_url,
            'ref_count': len(ref_files)
        })
    except Exception as e:
        app.logger.error(f"Thumbnail with refs failed: {e}", exc_info=True)
        return jsonify({'error': f'Generation failed: {e}'}), 500
    finally:
        # Clean up temp files
        import shutil
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


if __name__ == '__main__':
    ssl_cert = '/etc/letsencrypt/live/autoclip.cloud/fullchain.pem'
    ssl_key  = '/etc/letsencrypt/live/autoclip.cloud/privkey.pem'
    import os
    if os.path.exists(ssl_cert):
        app.run(host='0.0.0.0', port=443, debug=False,
                ssl_context=(ssl_cert, ssl_key))
    else:
        app.run(host='0.0.0.0', port=5000, debug=False)

"""Phase 4.1 — background worker for async publishes.

Runs as a systemd service alongside gunicorn. Polls publish_jobs for
status='pending', picks one, runs the existing compose+upload code,
updates status as it goes.

- One job at a time (single-threaded for now — parallel comes in 4.2)
- Heartbeats regularly so crashed jobs can be detected
- Marks orphaned (running with stale heartbeat) jobs as failed on boot
- Catches all exceptions; jobs never silently die
"""
import os
import sys
import time
import json
import sqlite3
import traceback
import tempfile
import shutil
from datetime import datetime

APP_DIR = os.path.expanduser('~/cfb_clip_studio')
sys.path.insert(0, APP_DIR)
os.chdir(APP_DIR)

DB = 'autoclip.db'
POLL_INTERVAL_SEC = 3
ORPHAN_TIMEOUT_SEC = 300

_app_mod = None


def _load_app():
    global _app_mod
    if _app_mod is not None:
        return _app_mod
    import app as app_module
    _app_mod = app_module
    return _app_mod


def db_conn():
    import db as _db
    if _db.is_postgres():
        return _db.get_conn()
    conn = sqlite3.connect(DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def log(msg, *args):
    ts = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    if args:
        msg = msg % args
    print(f"[{ts}] {msg}", flush=True)


def mark_orphaned_jobs():
    conn = db_conn()
    try:
        n = conn.execute(
            "UPDATE publish_jobs "
            "SET status='failed', "
            "    error='Worker died mid-job (orphaned on startup)', "
            "    finished_at=CURRENT_TIMESTAMP "
            "WHERE status='running' "
            "  AND (heartbeat_at IS NULL "
            f"       OR strftime('%s','now') - strftime('%s',heartbeat_at) > {ORPHAN_TIMEOUT_SEC})"
        ).rowcount
        conn.commit()
        if n:
            log("Marked %d orphaned job(s) as failed on startup", n)
    finally:
        conn.close()


def claim_next_job():
    conn = db_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM publish_jobs "
            "WHERE status='pending' "
            "ORDER BY created_at ASC "
            "LIMIT 1"
        ).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            return None
        conn.execute(
            "UPDATE publish_jobs "
            "SET status='running', started_at=CURRENT_TIMESTAMP, heartbeat_at=CURRENT_TIMESTAMP "
            "WHERE id=?",
            (row['id'],)
        )
        conn.commit()
        return dict(row)
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def update_job(job_id, **fields):
    fields.setdefault('heartbeat_at', 'CURRENT_TIMESTAMP')
    conn = db_conn()
    try:
        pieces = []
        vals = []
        for k, v in fields.items():
            if v == 'CURRENT_TIMESTAMP':
                pieces.append(f"{k}=CURRENT_TIMESTAMP")
            else:
                pieces.append(f"{k}=?")
                vals.append(v)
        vals.append(job_id)
        conn.execute(f"UPDATE publish_jobs SET {', '.join(pieces)} WHERE id=?", vals)
        conn.commit()
    finally:
        conn.close()


def process_job(job):
    # __AUTOCLIP_WORKER_APP_CONTEXT_V41__
    job_id = job['id']
    session_id = job['session_id']
    segment_index = job['segment_index']
    log("Starting job %d: session=%s seg=%d", job_id, session_id, segment_index)

    try:
        payload = json.loads(job['publish_payload'] or '{}')
    except Exception:
        payload = {}

    _tmp_dir = None
    try:
        app = _load_app()
        # Establish a Flask app context so helpers using flask.g work
        # (e.g. autoclip_db.get_db). Everything inside runs as though a
        # request were in flight.
        _ctx = app.app.app_context()
        _ctx.push()

        update_job(job_id, stage='loading_session', progress_pct=5)
        session = app.load_session(session_id)
        if not session:
            raise RuntimeError(f'Session {session_id} not found')
        if segment_index >= len(session.get('segments', [])):
            raise RuntimeError(f'Segment index {segment_index} out of range')
        segment = session['segments'][segment_index]

        update_job(job_id, stage='downloading_clip', progress_pct=10)
        clip_local = segment.get('clip_path')
        clip_gcs_key = segment.get('clip_gcs_key')
        if not clip_local or not os.path.exists(clip_local):
            if not clip_gcs_key:
                raise RuntimeError('No clip available (no local path, no GCS key)')
            _tmp_dir = tempfile.mkdtemp(prefix='autoclip_worker_')
            clip_local = os.path.join(_tmp_dir, os.path.basename(clip_gcs_key))
            app.gcs_storage.download_from_gcs(clip_gcs_key, clip_local)

        video_path = clip_local

        has_ads = any(segment.get(k) for k in ('intro_ad_id', 'outro_ad_id', 'mid_ad_id'))
        if has_ads:
            update_job(job_id, stage='composing_ads', progress_pct=25)
            _work = _tmp_dir or os.path.dirname(video_path)
            composed = app._compose_clip_with_ads(session, segment_index, video_path, _work)
            if composed:
                video_path = composed
                update_job(job_id, stage='compose_complete', progress_pct=70)

        update_job(job_id, stage='uploading_youtube', progress_pct=80)

        import googleapiclient.discovery
        import google.oauth2.credentials
        import google.auth.transport.requests
        from googleapiclient.http import MediaFileUpload

        yt_channel_id = session.get('channel_youtube_id')
        if not yt_channel_id:
            raise RuntimeError('Session has no channel_youtube_id')

        db = app.autoclip_db.get_db()
        ch = db.execute(
            "SELECT * FROM channels WHERE youtube_channel_id=?", (yt_channel_id,)
        ).fetchone()
        if not ch:
            raise RuntimeError(f'Channel {yt_channel_id} not found in DB')
        token_path = os.path.join(APP_DIR, ch['token_path'])
        if not os.path.exists(token_path):
            raise RuntimeError(f'Channel token file missing: {token_path}')

        with open(token_path) as f:
            token_data = json.load(f)
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
            with open(token_path, 'w') as f:
                json.dump(token_data, f)

        yt = googleapiclient.discovery.build('youtube', 'v3', credentials=creds)

        title = payload.get('title') or segment.get('title') or 'Untitled'
        description = payload.get('description') or segment.get('description') or ''
        privacy = payload.get('privacy', job.get('privacy') or 'private')
        tags = payload.get('tags') or []
        category_id = str(payload.get('category_id') or 22)

        body = {
            'snippet': {
                'title': title[:100],
                'description': description,
                'tags': tags,
                'categoryId': category_id,
            },
            'status': {
                'privacyStatus': privacy,
                'selfDeclaredMadeForKids': False,
            }
        }

        media = MediaFileUpload(video_path, mimetype='video/mp4', resumable=True, chunksize=8*1024*1024)
        request = yt.videos().insert(part='snippet,status', body=body, media_body=media)

        response = None
        last_progress_pct = 80
        while response is None:
            status, response = request.next_chunk()
            if status:
                p = int(80 + status.progress() * 15)
                if p != last_progress_pct:
                    update_job(job_id, progress_pct=p)
                    last_progress_pct = p

        video_id = response.get('id')
        if not video_id:
            raise RuntimeError(f'YouTube upload returned no video id: {response}')

        update_job(job_id, stage='saving', progress_pct=98)
        session = app.load_session(session_id)
        session['segments'][segment_index]['youtube_video_id'] = video_id
        app.save_session(session_id, session)

        update_job(
            job_id,
            status='done',
            stage='complete',
            progress_pct=100,
            youtube_video_id=video_id,
            finished_at='CURRENT_TIMESTAMP'
        )
        log("Job %d done: video_id=%s", job_id, video_id)

    except Exception as e:
        tb = traceback.format_exc()
        log("Job %d FAILED: %s\n%s", job_id, e, tb)
        try:
            update_job(
                job_id,
                status='failed',
                error=f'{e}\n\n{tb[-2000:]}',
                finished_at='CURRENT_TIMESTAMP'
            )
        except Exception:
            log("Also failed to update job status: %s", traceback.format_exc())
    finally:
        try:
            _ctx.pop()
        except Exception:
            pass
        if _tmp_dir and os.path.exists(_tmp_dir):
            shutil.rmtree(_tmp_dir, ignore_errors=True)


def main():
    log("Worker starting (poll=%ds, orphan_timeout=%ds)", POLL_INTERVAL_SEC, ORPHAN_TIMEOUT_SEC)
    mark_orphaned_jobs()
    while True:
        try:
            job = claim_next_job()
            if job:
                process_job(job)
            else:
                time.sleep(POLL_INTERVAL_SEC)
        except KeyboardInterrupt:
            log("Interrupted, exiting")
            break
        except Exception:
            log("Unhandled loop error: %s", traceback.format_exc())
            time.sleep(POLL_INTERVAL_SEC)


if __name__ == '__main__':
    main()

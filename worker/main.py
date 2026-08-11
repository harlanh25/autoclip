"""Cloud Run entrypoint. Handles a compose job triggered by Cloud Tasks."""
import os
import sys
import tempfile
import shutil
import traceback
import logging

from flask import Flask, request, jsonify

from gcs_util import download_from_gcs, upload_file_to_gcs
from compose import compose_clip_with_ads
from status_client import update_status

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    stream=sys.stdout,
)
log = logging.getLogger('worker')

OUTPUT_BUCKET = os.environ.get('OUTPUT_GCS_BUCKET', 'autoclip-uploads')

app = Flask(__name__)


@app.route('/', methods=['POST'])
def handle_job():
    payload = request.get_json(silent=True) or {}
    job_id = payload.get('job_id')
    log.info("Job payload received: job_id=%s session=%s seg=%s",
             job_id, payload.get('session_id'), payload.get('segment_index'))

    if not job_id:
        return jsonify({'error': 'job_id missing'}), 400
    required = ('session_id', 'segment_index', 'clip_gcs_key')
    missing = [k for k in required if k not in payload]
    if missing:
        return jsonify({'error': f'missing fields: {missing}'}), 400

    work_dir = tempfile.mkdtemp(prefix='autoclip_worker_')
    try:
        update_status(job_id, status='running', stage='downloading_clip', progress_pct=5)

        clip_local = os.path.join(work_dir, 'clip.mp4')
        download_from_gcs(payload['clip_gcs_key'], clip_local)

        ads = {}
        for role in ('intro', 'mid', 'outro'):
            key = payload.get(f'{role}_ad_gcs_key')
            if key:
                update_status(job_id, stage=f'downloading_{role}_ad', progress_pct=12)
                ext = os.path.splitext(key)[1] or '.mp4'
                p = os.path.join(work_dir, f'ad_{role}{ext}')
                download_from_gcs(key, p)
                ads[role] = p

        # Additional mid-roll ads (list, max enforced upstream)
        extra_mid_paths = []
        for i, key in enumerate(payload.get('extra_mid_ad_gcs_keys') or []):
            if not key:
                continue
            update_status(job_id, stage=f'downloading_mid_ad_{i + 2}', progress_pct=14)
            ext = os.path.splitext(key)[1] or '.mp4'
            p = os.path.join(work_dir, f'ad_mid{i + 2}{ext}')
            download_from_gcs(key, p)
            extra_mid_paths.append(p)

        update_status(job_id, stage='composing', progress_pct=20)
        composed_local = os.path.join(work_dir, 'composed.mp4')
        compose_clip_with_ads(
            clip_path=clip_local,
            intro_ad=ads.get('intro'),
            mid_ad=ads.get('mid'),
            outro_ad=ads.get('outro'),
            mid_position_sec=payload.get('mid_ad_position_sec'),
            extra_mid_ads=extra_mid_paths,
            extra_mid_positions=payload.get('extra_mid_positions') or [],
            output_path=composed_local,
            progress_callback=lambda pct: update_status(
                job_id, progress_pct=20 + int(pct * 0.65)
            ),
        )

        update_status(job_id, stage='uploading_composed', progress_pct=88)
        composed_key = f"composed/{payload['session_id']}/{payload['segment_index']}_{job_id}.mp4"
        upload_file_to_gcs(composed_local, OUTPUT_BUCKET, composed_key, content_type='video/mp4')

        update_status(
            job_id,
            stage='compose_done',
            progress_pct=92,
            composed_gcs_key=composed_key,
            composed_gcs_bucket=OUTPUT_BUCKET,
        )
        log.info("Job %s compose done: gs://%s/%s", job_id, OUTPUT_BUCKET, composed_key)
        return jsonify({'status': 'ok', 'composed_gcs_key': composed_key})

    except Exception as e:
        tb = traceback.format_exc()
        log.exception("Job %s failed", job_id)
        try:
            update_status(job_id, status='failed', error=f'{e}\n{tb[-1500:]}')
        except Exception:
            log.exception("Also failed reporting status back")
        return jsonify({'error': str(e)}), 500
    finally:
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass


@app.route('/healthz')
def healthz():
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    log.info("Worker listening on :%d", port)
    app.run(host='0.0.0.0', port=port)

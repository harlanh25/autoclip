"""HTTP callback to VM for job status updates. Shared-secret auth."""
import os
import logging
import requests

log = logging.getLogger('status')

CALLBACK_BASE = os.environ.get('VM_CALLBACK_URL', 'https://autoclip.cloud').rstrip('/')
SHARED_SECRET = os.environ.get('WORKER_SHARED_SECRET', '')


def update_status(job_id, **fields):
    url = f'{CALLBACK_BASE}/api/publish_jobs/{job_id}/worker_update'
    headers = {
        'Content-Type': 'application/json',
        'X-Worker-Secret': SHARED_SECRET,
    }
    try:
        r = requests.post(url, headers=headers, json=fields, timeout=15)
        if r.status_code >= 400:
            log.warning("Status callback %s returned %s: %s",
                        url, r.status_code, r.text[:200])
    except Exception as e:
        log.warning("Status callback failed: %s", e)

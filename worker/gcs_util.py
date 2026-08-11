"""GCS helpers for the worker container."""
import os
import logging
from google.cloud import storage

log = logging.getLogger('gcs')
_client = None


def _c():
    global _client
    if _client is None:
        _client = storage.Client()
    return _client


def download_from_gcs(gcs_key, local_path, bucket_name=None):
    if gcs_key.startswith('gs://'):
        remainder = gcs_key[5:]
        bucket_name, _, gcs_key = remainder.partition('/')
    else:
        bucket_name = bucket_name or os.environ.get('DEFAULT_GCS_BUCKET', 'autoclip-uploads')
    os.makedirs(os.path.dirname(local_path) or '.', exist_ok=True)
    blob = _c().bucket(bucket_name).blob(gcs_key)
    blob.download_to_filename(local_path)
    log.info("Downloaded gs://%s/%s -> %s (%s bytes)",
             bucket_name, gcs_key, local_path, os.path.getsize(local_path))
    return local_path


def upload_file_to_gcs(local_path, bucket_name, gcs_key, content_type=None):
    blob = _c().bucket(bucket_name).blob(gcs_key)
    blob.upload_from_filename(local_path, content_type=content_type)
    log.info("Uploaded %s -> gs://%s/%s (%s bytes)",
             local_path, bucket_name, gcs_key, os.path.getsize(local_path))
    return f'gs://{bucket_name}/{gcs_key}'

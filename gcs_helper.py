"""GCS helpers for AutoClip. Uses VM's attached service account for auth."""
import datetime
from google.cloud import storage
from google.auth import default
from google.auth.transport.requests import Request
from pathlib import Path

BUCKET_NAME = "autoclip-uploads"
SERVICE_ACCOUNT_EMAIL = "autoclip-storage@youtube-podcast-sync-502121.iam.gserviceaccount.com"

_client = None

def _get_client():
    global _client
    if _client is None:
        _client = storage.Client()
    return _client

def _get_access_token():
    """Get a fresh access token from the VM's attached service account."""
    creds, _ = default()
    creds.refresh(Request())
    return creds.token

def generate_upload_url(gcs_key: str, content_type: str, expires_minutes: int = 30) -> str:
    """Signed URL for a one-time PUT upload from the browser."""
    bucket = _get_client().bucket(BUCKET_NAME)
    blob = bucket.blob(gcs_key)
    return blob.generate_signed_url(
        version="v4",
        expiration=datetime.timedelta(minutes=expires_minutes),
        method="PUT",
        content_type=content_type,
        service_account_email=SERVICE_ACCOUNT_EMAIL,
        access_token=_get_access_token(),
    )

def download_file(gcs_key: str, local_path: str) -> str:
    """Download GCS object to local path for processing."""
    bucket = _get_client().bucket(BUCKET_NAME)
    blob = bucket.blob(gcs_key)
    Path(local_path).parent.mkdir(parents=True, exist_ok=True)
    blob.download_to_filename(local_path)
    return local_path

def delete_object(gcs_key: str):
    """Delete a GCS object. Safe to call on missing keys."""
    bucket = _get_client().bucket(BUCKET_NAME)
    blob = bucket.blob(gcs_key)
    if blob.exists():
        blob.delete()

def gcs_key_from_uri(uri: str) -> str:
    prefix = f"gs://{BUCKET_NAME}/"
    return uri[len(prefix):] if uri.startswith(prefix) else uri

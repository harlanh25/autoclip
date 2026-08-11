"""GCS storage helper for AutoClip.

Central place for all Google Cloud Storage operations. Wraps the client and
provides typed helpers for the common operations the app performs:

  - upload_file_to_gcs(local_path, gcs_key) — used after ffmpeg produces a clip
  - download_from_gcs(gcs_key, local_path) — used before YouTube publish / ad insert
  - signed_url(gcs_key, expires_seconds=3600) — used for browser preview
  - delete_from_gcs(gcs_key) — used when a user deletes something
  - list_prefix(prefix) — used for the thumbnail library gallery
  - object_exists(gcs_key) — sanity checks

Path conventions (multi-tenant SaaS):
    uploads/<channel_id>/<session_id>.mp4       (source, 7-day retention)
    clips/<channel_id>/<session_id>/clip_N.mp4  (cut clips, 30-day retention)
    thumbnails/<channel_id>/thumb_<hash>.png    (library, no expiry)
    ads/<channel_id>/<ad_name>.mp4              (ad library, no expiry)

<channel_id> is the immutable YouTube channel ID (e.g. UCEsOcvBbXtO8AyyY2tZYJpg).
Never use user-editable handles here — IDs are stable, opaque, and unique.
"""
import os
from pathlib import Path
from google.cloud import storage
from google.auth import default as default_auth
from google.auth.transport import requests as google_requests
from datetime import timedelta

BUCKET_NAME = os.environ.get("AUTOCLIP_BUCKET", "autoclip-uploads")

# The GCE service account attached to the VM has cloud-platform scope,
# so the default credentials will work for signing (via IAM sign endpoint).
_client = None


def get_client():
    global _client
    if _client is None:
        _client = storage.Client()
    return _client


def get_bucket():
    return get_client().bucket(BUCKET_NAME)


# ============================================================================
# Path helpers — always use these, never construct paths by hand elsewhere
# ============================================================================

def source_key(channel_id, session_id, ext="mp4"):
    return f"uploads/{channel_id}/{session_id}.{ext}"


def clip_key(channel_id, session_id, segment_index, ext="mp4"):
    return f"clips/{channel_id}/{session_id}/clip_{segment_index}.{ext}"


def thumbnail_key(channel_id, hash_or_name, ext="png"):
    return f"thumbnails/{channel_id}/thumb_{hash_or_name}.{ext}"


def ad_key(channel_id, ad_name, ext="mp4"):
    # Sanitize ad_name to be safe for GCS
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in ad_name)
    return f"ads/{channel_id}/{safe}.{ext}"


# ============================================================================
# Core operations
# ============================================================================

def upload_file_to_gcs(local_path, gcs_key, content_type=None):
    """Upload a local file to GCS. Overwrites if exists."""
    blob = get_bucket().blob(gcs_key)
    if content_type:
        blob.upload_from_filename(local_path, content_type=content_type)
    else:
        blob.upload_from_filename(local_path)
    return gcs_key


def upload_bytes_to_gcs(data_bytes, gcs_key, content_type=None):
    """Upload bytes to GCS (e.g. base64-decoded image data)."""
    blob = get_bucket().blob(gcs_key)
    if content_type:
        blob.upload_from_string(data_bytes, content_type=content_type)
    else:
        blob.upload_from_string(data_bytes)
    return gcs_key


def download_from_gcs(gcs_key, local_path):
    """Download a GCS object to a local path."""
    blob = get_bucket().blob(gcs_key)
    if not blob.exists():
        raise FileNotFoundError(f"GCS object not found: {gcs_key}")
    Path(local_path).parent.mkdir(parents=True, exist_ok=True)
    blob.download_to_filename(local_path)
    return local_path


def download_bytes(gcs_key):
    """Download an object straight into memory. For small files only."""
    return get_bucket().blob(gcs_key).download_as_bytes()


def signed_url(gcs_key, expires_seconds=3600, method="GET", download_name=None):
    """Generate a short-lived signed URL. Uses IAM sign endpoint for GCE service accounts."""
    bucket = get_bucket()
    blob = bucket.blob(gcs_key)

    # For GCE service accounts (no local private key), we need to sign via IAM.
    # The service account attached to this VM must have `iam.serviceAccountTokenCreator`
    # on itself for signing to work.
    from google.auth import compute_engine
    from google.auth.transport import requests as _rq

    _extra = {}
    if download_name:
        _extra["response_disposition"] = f'attachment; filename="{download_name}"'

    try:
        # Fast path: normal signed URL (works if we have a private key)
        return blob.generate_signed_url(
            version="v4",
            expiration=timedelta(seconds=expires_seconds),
            method=method,
            **_extra,
        )
    except Exception:
        # Fallback: sign via IAM (for VM service accounts)
        credentials, _ = default_auth()
        auth_request = _rq.Request()
        credentials.refresh(auth_request)

        return blob.generate_signed_url(
            version="v4",
            expiration=timedelta(seconds=expires_seconds),
            method=method,
            service_account_email=credentials.service_account_email,
            access_token=credentials.token,
            **_extra,
        )


def delete_from_gcs(gcs_key):
    """Delete a GCS object. No-op if it doesn't exist."""
    blob = get_bucket().blob(gcs_key)
    if blob.exists():
        blob.delete()
        return True
    return False


def object_exists(gcs_key):
    return get_bucket().blob(gcs_key).exists()


def list_prefix(prefix):
    """List all object keys under a prefix. Returns [key, size, updated] tuples."""
    results = []
    for blob in get_client().list_blobs(BUCKET_NAME, prefix=prefix):
        results.append({
            'key': blob.name,
            'size': blob.size,
            'updated': blob.updated.isoformat() if blob.updated else None,
        })
    return results


def get_public_or_signed_url(gcs_key, expires_seconds=3600):
    """Wrapper that returns the URL a browser should use to fetch this object."""
    return signed_url(gcs_key, expires_seconds=expires_seconds)


# ============================================================================
# Chunked / parallel upload helpers
# ============================================================================

# Path convention for temporary chunk objects during a chunked upload:
#   uploads/chunks/<session_id>/part_<N>.bin
# These are composed into the final uploads/<channel_id>/<session_id>.mp4
# and then deleted.

def chunks_prefix_for(session_id):
    return f"uploads/chunks/{session_id}"


def chunk_key(session_id, chunk_index):
    return f"{chunks_prefix_for(session_id)}/part_{chunk_index}.bin"


def chunk_upload_urls(session_id, chunk_count, expires_seconds=3600):
    """Generate signed PUT URLs for each chunk. Browser uploads to these directly."""
    urls = []
    for i in range(chunk_count):
        gcs_key = chunk_key(session_id, i)
        try:
            url = signed_url(gcs_key, expires_seconds=expires_seconds, method="PUT")
        except Exception as e:
            raise RuntimeError(f"Failed to sign chunk {i}: {e}")
        urls.append({
            'index': i,
            'gcs_key': gcs_key,
            'upload_url': url,
        })
    return urls


def _compose_batch(source_keys, dest_key, content_type=None):
    """Compose up to 32 source objects into a single destination object."""
    if len(source_keys) > 32:
        raise ValueError("compose supports max 32 sources per call")
    bucket = get_bucket()
    dest_blob = bucket.blob(dest_key)
    if content_type:
        dest_blob.content_type = content_type
    source_blobs = [bucket.blob(k) for k in source_keys]
    dest_blob.compose(source_blobs)
    return dest_key


def compose_chunks(session_id, chunk_count, final_key, content_type="video/mp4"):
    """Compose N chunk objects into the final GCS object. Handles >32 chunks recursively.

    Strategy:
      - If chunk_count <= 32: single compose call
      - Otherwise: compose in batches of 32 into intermediate objects, then compose those
      - Intermediate objects are placed under uploads/chunks/<session_id>/intermediate/level_N/part_M
    """
    prefix = chunks_prefix_for(session_id)
    chunks = [chunk_key(session_id, i) for i in range(chunk_count)]

    level = 0
    current = chunks
    while len(current) > 32:
        level += 1
        next_level = []
        for batch_start in range(0, len(current), 32):
            batch = current[batch_start:batch_start + 32]
            intermediate_key = f"{prefix}/intermediate/level_{level}/part_{batch_start // 32}"
            _compose_batch(batch, intermediate_key)
            next_level.append(intermediate_key)
        current = next_level

    # Final compose into destination
    _compose_batch(current, final_key, content_type=content_type)
    return final_key


def delete_chunks(session_id):
    """Delete all temporary chunk + intermediate objects for a session."""
    prefix = chunks_prefix_for(session_id)
    deleted = 0
    for blob in get_client().list_blobs(BUCKET_NAME, prefix=prefix):
        blob.delete()
        deleted += 1
    return deleted

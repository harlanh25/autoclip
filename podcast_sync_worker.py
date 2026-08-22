"""AutoClip audio sync worker.

Runs on an hourly systemd timer. For each active audio_sync_config:
  1. Queries the source YouTube playlist for new videos
  2. Skips any already in audio_synced_episodes for this config
  3. Downloads audio, extracts, and uploads to Transistor as a new episode
  4. Records the sync in audio_synced_episodes and usage_monthly
  5. Records the run in audio_sync_runs

Design principles:
  - One config's failure does not stop others
  - Cap enforcement checks user's audio_monthly_cap before syncing
  - Everything logged for observability

Run manually:
    python3 ~/cfb_clip_studio/podcast_sync_worker.py

Run via systemd:
    systemctl start autoclip-sync.service
"""
import os
import sys
import json
import logging
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime, timezone
import requests

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "autoclip.db"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# We rely on YOUTUBE_API_KEY from environment. Falls back to a legacy key path.
YOUTUBE_API_KEY = os.environ.get(
    "YOUTUBE_API_KEY",
    ""  # empty = misconfigured, we'll log and skip
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "sync.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# DB helpers
# ----------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def current_period() -> str:
    """Return current calendar-month period key, e.g. '2026-08'."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


# ----------------------------------------------------------------------
# YouTube helpers
# ----------------------------------------------------------------------

def fetch_playlist_videos(playlist_id: str, max_results: int = 20):
    """Return newest videos in a playlist. Each item = {videoId, title, publishedAt}."""
    if not YOUTUBE_API_KEY:
        raise RuntimeError("YOUTUBE_API_KEY not set")

    url = "https://www.googleapis.com/youtube/v3/playlistItems"
    params = {
        "part": "snippet,contentDetails",
        "playlistId": playlist_id,
        "maxResults": max_results,
        "key": YOUTUBE_API_KEY,
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    items = []
    for item in data.get("items", []):
        snippet = item.get("snippet", {})
        content = item.get("contentDetails", {})
        items.append({
            "videoId": content.get("videoId"),
            "title": snippet.get("title"),
            "publishedAt": snippet.get("publishedAt"),
        })
    return items


def download_audio(video_id: str, out_path: str) -> str:
    """Download a video's audio track using yt-dlp. Returns the path to the audio file."""
    # -x = extract audio, --audio-format mp3, quiet output
    cmd = [
        "yt-dlp",
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "-o", out_path,
        "--no-warnings",
        "--quiet",
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp failed: {result.stderr}")
    return out_path


# ----------------------------------------------------------------------
# Transistor helpers
# ----------------------------------------------------------------------

def transistor_authorize_upload(api_key: str, filename: str, mime: str = "audio/mpeg"):
    """Get a pre-signed S3 URL from Transistor for uploading the audio file."""
    resp = requests.get(
        "https://api.transistor.fm/v1/episodes/authorize_upload",
        headers={"x-api-key": api_key},
        params={"filename": filename},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["data"]["attributes"]


def transistor_upload_file(upload_url: str, file_path: str):
    """PUT the audio file to Transistor's S3 URL."""
    with open(file_path, "rb") as f:
        resp = requests.put(
            upload_url,
            data=f,
            headers={"Content-Type": "audio/mpeg"},
            timeout=600,
        )
    resp.raise_for_status()


def transistor_create_episode(api_key: str, show_id: str, title: str, audio_url: str,
                               description: str = "", publish: bool = True):
    """Create an episode on the given Transistor show.

    Transistor rejects status="published" on create - episodes are always
    created as drafts and published via a separate PATCH call. When
    publish=False the episode is left as a draft (used when the destination
    platform has its own ad system that requires the podcaster to approve ads
    before the episode can go live).
    """
    resp = requests.post(
        "https://api.transistor.fm/v1/episodes",
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
        json={
            "episode": {
                "show_id": show_id,
                "title": title,
                "audio_url": audio_url,
                "description": description,
            }
        },
        timeout=60,
    )
    if not resp.ok:
        import logging
        logging.getLogger(__name__).error(
            f"Transistor rejected episode create: {resp.status_code} {resp.text}")
    resp.raise_for_status()
    episode = resp.json()["data"]
    episode_id = episode["id"]

    if not publish:
        return episode  # left as draft

    pub_resp = requests.patch(
        f"https://api.transistor.fm/v1/episodes/{episode_id}/publish",
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
        json={"episode": {"status": "published"}},
        timeout=60,
    )
    if not pub_resp.ok:
        import logging
        logging.getLogger(__name__).error(
            f"Transistor rejected episode publish: {pub_resp.status_code} {pub_resp.text}")
    pub_resp.raise_for_status()
    return pub_resp.json()["data"]


def buzzsprout_create_episode(api_key: str, podcast_id: str, title: str, audio_path: str,
                               description: str = "", publish: bool = True):
    """Upload audio and create an episode on Buzzsprout in a single call.

    Buzzsprout takes the audio file directly (no separate presigned-upload
    step like Transistor) and a private flag controls draft vs. live -
    private=True keeps it unpublished until the podcaster approves ads in
    their own Buzzsprout dashboard.
    """
    with open(audio_path, "rb") as f:
        resp = requests.post(
            f"https://www.buzzsprout.com/api/{podcast_id}/episodes.json",
            headers={"Authorization": f"Token token={api_key}"},
            data={
                "title": title,
                "description": description,
                "private": "false" if publish else "true",
            },
            files={"audio_file": (os.path.basename(audio_path), f, "audio/mpeg")},
            timeout=600,
        )
    if not resp.ok:
        import logging
        logging.getLogger(__name__).error(
            f"Buzzsprout rejected episode create: {resp.status_code} {resp.text}")
    resp.raise_for_status()
    return resp.json()


def publish_episode(destination_type: str, api_key: str, show_id: str, title: str,
                     audio_path: str, description: str, publish: bool):
    """Dispatch episode creation to the right platform. Returns the episode id or None.

    Every destination handler must accept and honor `publish` - when False,
    the episode is created but left unpublished, because the destination
    platform requires the podcaster to manually approve platform ads before
    an episode can go live.
    """
    if destination_type == "transistor":
        upload_attrs = transistor_authorize_upload(api_key, f"{os.path.basename(audio_path)}")
        transistor_upload_file(upload_attrs["upload_url"], audio_path)
        ep = transistor_create_episode(
            api_key=api_key, show_id=show_id, title=title,
            audio_url=upload_attrs["audio_url"], description=description, publish=publish,
        )
        return ep.get("id") if ep else None
    elif destination_type == "buzzsprout":
        ep = buzzsprout_create_episode(
            api_key=api_key, podcast_id=show_id, title=title,
            audio_path=audio_path, description=description, publish=publish,
        )
        return ep.get("id") if ep else None
    else:
        raise ValueError(f"Unsupported destination_type: {destination_type!r}")


# ----------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------

def process_config(conn, config: dict) -> int:
    """Process one sync config. Returns number of episodes synced."""
    config_id = config["id"]
    user_id = config["user_id"]
    name = config["name"]

    log.info(f"[cfg={config_id} {name}] Starting sync")

    # Start run record
    cur = conn.execute(
        "INSERT INTO audio_sync_runs (config_id, status) VALUES (?, 'running')",
        (config_id,),
    )
    run_id = cur.lastrowid
    conn.commit()

    synced_count = 0
    try:
        # Check user's monthly cap
        user_row = conn.execute(
            "SELECT audio_monthly_cap FROM users WHERE id=?", (user_id,)
        ).fetchone()
        cap = user_row["audio_monthly_cap"] if user_row else None
        period = current_period()

        # Get current usage
        usage_row = conn.execute(
            "SELECT audio_episodes FROM usage_monthly WHERE user_id=? AND period=?",
            (user_id, period),
        ).fetchone()
        used = usage_row["audio_episodes"] if usage_row else 0

        if cap is not None and used >= cap:
            log.warning(f"[cfg={config_id}] User {user_id} hit audio cap ({used}/{cap}). Skipping.")
            conn.execute(
                "UPDATE audio_sync_runs SET finished_at=CURRENT_TIMESTAMP, status='success', "
                "episodes_synced=0, error_message='monthly cap reached' WHERE id=?",
                (run_id,),
            )
            conn.commit()
            return 0

        # Fetch playlist items
        videos = fetch_playlist_videos(config["source_playlist_id"])
        log.info(f"[cfg={config_id}] Found {len(videos)} videos in playlist")

        # Filter to those not yet synced for this config
        already = {
            r["youtube_video_id"]
            for r in conn.execute(
                "SELECT youtube_video_id FROM audio_synced_episodes WHERE config_id=?",
                (config_id,),
            )
        }
        new_videos = [v for v in videos if v["videoId"] not in already]
        log.info(f"[cfg={config_id}] {len(new_videos)} new videos to sync")

        # Process oldest-first so podcast order is chronological
        new_videos.reverse()

        for v in new_videos:
            # Re-check cap before each episode
            if cap is not None and (used + synced_count) >= cap:
                log.warning(f"[cfg={config_id}] Cap reached mid-run. Stopping.")
                break

            video_id = v["videoId"]
            title = v["title"] or "Untitled episode"
            log.info(f"[cfg={config_id}]   syncing {video_id} - {title!r}")

            try:
                with tempfile.TemporaryDirectory() as tmpdir:
                    out_template = os.path.join(tmpdir, f"{video_id}.%(ext)s")
                    download_audio(video_id, out_template)
                    # yt-dlp resolves the actual extension; find the mp3
                    mp3_path = os.path.join(tmpdir, f"{video_id}.mp3")
                    if not os.path.exists(mp3_path):
                        # yt-dlp may have written a different extension; grab whatever's in the dir
                        candidates = list(Path(tmpdir).iterdir())
                        if not candidates:
                            raise RuntimeError("yt-dlp produced no output file")
                        mp3_path = str(candidates[0])

                    # has_platform_ads: some destinations (Buzzsprout,
                    # Simplecast) require the podcaster to manually approve
                    # ads in their own dashboard before an episode can go
                    # live - the API cannot do this. When set, episodes are
                    # created but left as drafts.
                    should_publish = not config.get("has_platform_ads")
                    episode_id = publish_episode(
                        destination_type=config.get("destination_type", "transistor"),
                        api_key=config["destination_api_key"],
                        show_id=config["destination_show_id"],
                        title=title,
                        audio_path=mp3_path,
                        description=f"Source: https://www.youtube.com/watch?v={video_id}",
                        publish=should_publish,
                    )
                    if not should_publish:
                        log.info(f"[cfg={config_id}]   created as DRAFT - ads need approval in destination dashboard")

                # Record success
                conn.execute(
                    "INSERT INTO audio_synced_episodes "
                    "(config_id, youtube_video_id, youtube_video_title, transistor_episode_id) "
                    "VALUES (?, ?, ?, ?)",
                    (config_id, video_id, title, episode_id),
                )
                # Bump usage
                conn.execute(
                    "INSERT INTO usage_monthly (user_id, period, audio_episodes) VALUES (?, ?, 1) "
                    "ON CONFLICT(user_id, period) DO UPDATE SET audio_episodes = audio_episodes + 1",
                    (user_id, period),
                )
                conn.commit()
                synced_count += 1

            except Exception as ep_err:
                log.error(f"[cfg={config_id}]   FAILED {video_id}: {ep_err}", exc_info=True)
                # Continue with next episode - don't let one bad video stop the config

        # Mark run success
        conn.execute(
            "UPDATE audio_sync_runs SET finished_at=CURRENT_TIMESTAMP, status='success', episodes_synced=? "
            "WHERE id=?",
            (synced_count, run_id),
        )
        conn.execute(
            "UPDATE audio_sync_configs SET last_synced_at=CURRENT_TIMESTAMP WHERE id=?",
            (config_id,),
        )
        conn.commit()
        log.info(f"[cfg={config_id}] Done. Synced {synced_count} episodes.")

    except Exception as e:
        log.error(f"[cfg={config_id}] Sync failed entirely: {e}", exc_info=True)
        conn.execute(
            "UPDATE audio_sync_runs SET finished_at=CURRENT_TIMESTAMP, status='error', "
            "episodes_synced=?, error_message=? WHERE id=?",
            (synced_count, str(e)[:500], run_id),
        )
        conn.commit()

    return synced_count


def main():
    log.info("=" * 60)
    log.info(f"AutoClip audio sync starting @ {datetime.now().isoformat()}")
    log.info("=" * 60)

    if not YOUTUBE_API_KEY:
        log.error("YOUTUBE_API_KEY environment variable is not set. Aborting.")
        sys.exit(1)

    conn = get_db()

    active = conn.execute(
        "SELECT c.*, u.has_audio FROM audio_sync_configs c "
        "JOIN users u ON u.id = c.user_id "
        "WHERE c.is_active = 1 AND u.has_audio = 1"
    ).fetchall()
    log.info(f"Found {len(active)} active configs (users with audio entitlement).")

    total = 0
    for cfg_row in active:
        cfg = dict(cfg_row)
        try:
            total += process_config(conn, cfg)
        except Exception as e:
            log.error(f"Unhandled error on config {cfg.get('id')}: {e}", exc_info=True)

    log.info(f"Sync complete. Total episodes synced across all configs: {total}")
    conn.close()


if __name__ == "__main__":
    main()

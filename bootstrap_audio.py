"""AutoClip audio + usage bootstrap.

Idempotent. Safe to run multiple times.

Steps:
  1. Applies audio schema (adds columns to users, creates new tables).
  2. Grants unlimited clipping + audio to Harlan (harlanhgharris@gmail.com).
  3. Grants unlimited to TJ (whichever user account he has by then).
  4. Imports TJ's existing hardcoded sync scripts as DB configs, IF a
     TJ user account is found. Playlist ID left blank — TJ must fill in
     via the UI before the sync worker picks them up.

Usage:
    python3 ~/cfb_clip_studio/bootstrap_audio.py
"""
import os
import sqlite3
from pathlib import Path

BASE = Path.home() / "cfb_clip_studio"
DB_PATH = BASE / "autoclip.db"
SCHEMA_PATH = BASE / "schema_audio.sql"

# TJ's existing hardcoded sync configs (from his running scripts).
# We migrate these into DB rows owned by his user account. Playlist ID is
# blank because his old scripts synced the WHOLE channel; we need him to
# create a "Podcast Episodes" playlist on each channel and paste the ID.
TJ_EXISTING_CONFIGS = [
    {
        "name": "ACC Football Addiction",
        "youtube_channel_id": "UCrZfTzLVUeFiYL65Q2ohGGQ",
        "transistor_api_key": "uD8gJhUexenzzZ3hup_guA",
        "transistor_show_id": "80773",
        "transistor_show_name": "ACC Football Addiction",
    },
    # SEC / B1G / B12 configs will need adding once we know their channel/show IDs.
    # TJ said the last three were pending Transistor-side verification.
]

HARLAN_EMAIL = "harlanhgharris@gmail.com"


def apply_schema(conn):
    print(f"[1/4] Applying audio schema from {SCHEMA_PATH.name}...")
    if not SCHEMA_PATH.exists():
        raise RuntimeError(f"Missing {SCHEMA_PATH}. scp it up first.")
    schema_sql = SCHEMA_PATH.read_text()

    # Split and run each statement — SQLite's executescript would fail on
    # duplicate ALTER TABLE calls, so we run individually and swallow
    # "duplicate column" errors that mean "already applied."
    for stmt in schema_sql.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "duplicate column" in msg or "already exists" in msg:
                # Already applied. Fine.
                continue
            raise
    conn.commit()
    print("       Done.")


def grant_unlimited(conn, email: str):
    row = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if not row:
        print(f"       User {email} not found (skipping).")
        return None
    uid = row["id"]
    conn.execute(
        "UPDATE users SET has_clipping=1, has_audio=1, "
        "clipping_monthly_cap=NULL, audio_monthly_cap=NULL WHERE id=?",
        (uid,),
    )
    conn.commit()
    print(f"       Granted unlimited to {email} (user_id={uid}).")
    return uid


def find_tj_user(conn):
    """Find TJ's account heuristically. Any non-Harlan approved user."""
    rows = conn.execute(
        "SELECT id, email FROM users WHERE email != ? AND is_approved=1",
        (HARLAN_EMAIL,),
    ).fetchall()
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]
    # Multiple. Prefer one whose email contains "tj" or "power".
    for r in rows:
        e = r["email"].lower()
        if "tj" in e or "power" in e or "garnet" in e:
            return r
    return rows[0]


def import_tj_configs(conn, tj_user_id: int):
    print(f"[4/4] Importing TJ's legacy configs as DB rows (owned by user {tj_user_id})...")
    imported = 0
    for cfg in TJ_EXISTING_CONFIGS:
        # Look up the channel row (created earlier when TJ authorized channels).
        chan = conn.execute(
            "SELECT id FROM channels WHERE youtube_channel_id=?",
            (cfg["youtube_channel_id"],),
        ).fetchone()
        channel_row_id = chan["id"] if chan else None

        # Idempotent — skip if a row already exists for this show.
        existing = conn.execute(
            "SELECT id FROM audio_sync_configs WHERE user_id=? AND destination_show_id=?",
            (tj_user_id, cfg["transistor_show_id"]),
        ).fetchone()
        if existing:
            print(f"       Config already present for show '{cfg['name']}'. Skipping.")
            continue

        conn.execute(
            "INSERT INTO audio_sync_configs "
            "(user_id, name, channel_id, source_youtube_channel_id, source_playlist_id, "
            " destination_type, destination_api_key, destination_show_id, destination_show_name, is_active) "
            "VALUES (?, ?, ?, ?, ?, 'transistor', ?, ?, ?, 0)",  # is_active=0 until playlist_id is set
            (
                tj_user_id,
                cfg["name"],
                channel_row_id,
                cfg["youtube_channel_id"],
                "",  # PLAYLIST ID EMPTY - TJ must fill in via UI
                cfg["transistor_api_key"],
                cfg["transistor_show_id"],
                cfg["transistor_show_name"],
            ),
        )
        imported += 1
        print(f"       Imported '{cfg['name']}' (inactive - playlist_id must be set).")
    conn.commit()
    print(f"       Imported {imported} configs.")


def main():
    print("=" * 60)
    print("AutoClip audio + usage bootstrap")
    print("=" * 60)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # 1. Schema
    apply_schema(conn)

    # 2. Grant unlimited to Harlan
    print("\n[2/4] Granting unlimited to Harlan...")
    grant_unlimited(conn, HARLAN_EMAIL)

    # 3. Grant unlimited to TJ (if he has an account already)
    print("\n[3/4] Granting unlimited to TJ...")
    tj = find_tj_user(conn)
    if tj:
        grant_unlimited(conn, tj["email"])
    else:
        print("       No non-Harlan users found. TJ can be granted after he signs in.")

    # 4. Import TJ's existing configs
    print("")
    if tj:
        import_tj_configs(conn, tj["id"])
    else:
        print("[4/4] Skipped (TJ not found).")

    conn.close()
    print("\n" + "=" * 60)
    print("Bootstrap complete.")
    print("=" * 60)
    print("\nNotes:")
    print("  - Imported TJ configs are is_active=0 until playlist_id is set.")
    print("  - TJ must go to /audio, edit each config, and paste the")
    print("    'Podcast Episodes' playlist ID from his YouTube channel.")
    print("  - Once a playlist ID is set + is_active=1, the hourly sync")
    print("    worker will pick it up.")
    print("  - Old sync scripts in ~/podcast_sync/ are still running.")
    print("    Disable them AFTER verifying the new system works.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Delete sessions older than the retention window.

Transcripts and full_text live inside sessions.data, so removing the row
removes them. GCS objects (uploads/, clips/, composed/) expire separately
via a bucket lifecycle rule on the same 30-day window.

Usage:
    python3 cleanup_sessions.py --dry-run    # report only
    python3 cleanup_sessions.py              # delete
"""
import sys
import db

RETENTION_DAYS = 30


def _expire_cached_youtube_titles(conn, cur, dry):
    """Clear cached YouTube video titles older than the retention window.

    audio_synced_episodes caches youtube_video_title purely for display.
    YouTube API Services policy III.E.4 requires stored API data to be
    refreshed or deleted at least every 30 days, so the cached title is
    cleared once a row passes that age.

    The row itself stays. youtube_video_id is what the sync worker matches
    on to avoid republishing an episode, so deleting rows would cause the
    entire back catalogue to publish again.
    """
    cur.execute(
        "SELECT COUNT(*) FROM audio_synced_episodes "
        "WHERE youtube_video_title IS NOT NULL "
        "  AND synced_at < NOW() - INTERVAL '%s days'" % RETENTION_DAYS
    )
    n = cur.fetchone()[0]
    if not n:
        print("no cached YouTube titles older than %d days" % RETENTION_DAYS)
        return
    if dry:
        print("dry run: would clear %d cached YouTube title(s)" % n)
        return
    cur.execute(
        "UPDATE audio_synced_episodes SET youtube_video_title = NULL "
        "WHERE youtube_video_title IS NOT NULL "
        "  AND synced_at < NOW() - INTERVAL '%s days'" % RETENTION_DAYS
    )
    conn.commit()
    print("cleared %d cached YouTube title(s)" % n)


def main():
    dry = '--dry-run' in sys.argv
    conn = db.get_conn()
    cur = conn.cursor()
    _expire_cached_youtube_titles(conn, cur, dry)
    cur.execute(
        "SELECT session_id, updated_at, data->>'show_name' "
        "FROM sessions WHERE updated_at < NOW() - INTERVAL '%s days' "
        "ORDER BY updated_at" % RETENTION_DAYS
    )
    rows = cur.fetchall()
    if not rows:
        print("nothing older than %d days" % RETENTION_DAYS)
        conn.close()
        return
    for sid, upd, name in rows:
        print("%s  %s  %s  %s" % (
            "WOULD DELETE" if dry else "DELETING", sid, str(upd)[:16], name or ''))
    if dry:
        print("\ndry run: %d sessions would be deleted" % len(rows))
    else:
        cur.execute(
            "DELETE FROM sessions WHERE updated_at < NOW() - INTERVAL '%s days'"
            % RETENTION_DAYS
        )
        conn.commit()
        print("\ndeleted %d sessions" % len(rows))
    conn.close()


if __name__ == '__main__':
    main()

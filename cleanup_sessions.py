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


def main():
    dry = '--dry-run' in sys.argv
    conn = db.get_conn()
    cur = conn.cursor()
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

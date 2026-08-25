r"""Export live SQLite tables to CSV for loading into Cloud SQL Postgres.

Column order is taken from POSTGRES, not SQLite, so the generated \copy
statements line up even if the two schemas drifted in column order.
Run on the VM. Writes to ./pgexport/ and ./pg_load.sql
"""
import csv
import os
import sqlite3
import sys

TABLES = [
    "users", "channels", "user_channels", "ads", "channel_ad_config",
    "thumbnails_library", "thumbnail_generations", "publish_jobs",
    "audio_sync_configs", "audio_sync_runs", "audio_synced_episodes",
    "usage_events", "usage_monthly",
]

OUT = os.path.abspath("pgexport")
os.makedirs(OUT, exist_ok=True)

os.environ["DB_BACKEND"] = "postgres"
import db as _db
pg = _db.get_conn()

lite = sqlite3.connect("autoclip.db")
lite.row_factory = sqlite3.Row

lite_tables = {r[0] for r in lite.execute(
    "SELECT name FROM sqlite_master WHERE type='table'")}

load_lines = []
problems = []

for t in TABLES:
    if t not in lite_tables:
        problems.append("%s: missing in sqlite, skipped" % t)
        continue

    pg_cols = [r["column_name"] for r in pg.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=? ORDER BY ordinal_position",
        (t,)).fetchall()]
    if not pg_cols:
        problems.append("%s: missing in postgres, skipped" % t)
        continue

    lite_cols = [r[1] for r in lite.execute("PRAGMA table_info(%s)" % t)]
    missing = [c for c in pg_cols if c not in lite_cols]
    extra = [c for c in lite_cols if c not in pg_cols]
    if missing:
        problems.append("%s: in pg but not sqlite: %s" % (t, missing))
    if extra:
        problems.append("%s: in sqlite but not pg: %s" % (t, extra))

    use = [c for c in pg_cols if c in lite_cols]
    path = os.path.join(OUT, t + ".csv")
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(use)
        for row in lite.execute("SELECT %s FROM %s" % (
                ", ".join('"%s"' % c for c in use), t)):
            w.writerow(["" if v is None else v for v in row])
            n += 1

    load_lines.append(
        "\\copy %s (%s) FROM '%s' WITH (FORMAT csv, HEADER true, NULL '')"
        % (t, ", ".join(use), path))
    print("  %-24s %6d rows  %d cols" % (t, n, len(use)))

with open("pg_load.sql", "w", encoding="utf-8") as fh:
    fh.write("BEGIN;\n\n")
    fh.write("TRUNCATE %s RESTART IDENTITY CASCADE;\n\n"
             % ", ".join(t for t in TABLES if t in lite_tables))
    fh.write("\n".join(load_lines))
    fh.write("\n\nCOMMIT;\n")

print()
if problems:
    print("SCHEMA DIFFERENCES:")
    for p in problems:
        print("  !", p)
else:
    print("No schema differences.")
print()
print("Wrote %s/ and pg_load.sql" % OUT)
pg.close(); lite.close()

-- Add thumbnails library table for the GCS migration.
-- Each generated or uploaded thumbnail gets a row so the library UI can list,
-- download, and delete them. Ties every thumbnail to a specific channel so
-- access control is straightforward.

CREATE TABLE IF NOT EXISTS thumbnails_library (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,                       -- FK to channels.id (our internal PK)
    gcs_key TEXT UNIQUE NOT NULL,                      -- thumbnails/<youtube_channel_id>/thumb_<hash>.png
    source_type TEXT NOT NULL,                         -- 'generated' | 'uploaded'
    source_session_id TEXT,                            -- null if uploaded directly to library
    source_segment_index INTEGER,                      -- null if not tied to a segment
    created_by_user_id INTEGER,                        -- who created this
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE,
    FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_thumbnails_channel ON thumbnails_library(channel_id);
CREATE INDEX IF NOT EXISTS idx_thumbnails_created ON thumbnails_library(created_at);

-- AutoClip audio + usage schema addition
-- Applied on top of the existing multi-tenant schema.

-- Extend users with entitlement flags + monthly caps.
-- Uses ALTER TABLE because SQLite allows it (limited but sufficient here).
ALTER TABLE users ADD COLUMN has_clipping INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN has_audio INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN clipping_monthly_cap INTEGER;      -- NULL = unlimited
ALTER TABLE users ADD COLUMN audio_monthly_cap INTEGER;         -- NULL = unlimited

-- Audio sync configurations.
-- One row per (user, source-playlist -> destination-show) mapping.
CREATE TABLE IF NOT EXISTS audio_sync_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,                           -- Display name, e.g. "ACC Football Addiction"
    channel_id INTEGER,                           -- FK to channels table (source YouTube channel)
    source_youtube_channel_id TEXT NOT NULL,      -- YouTube channel ID (e.g. UCrZfTz...)
    source_playlist_id TEXT NOT NULL,             -- YouTube playlist ID user wants to sync
    destination_type TEXT NOT NULL DEFAULT 'transistor',
    destination_api_key TEXT NOT NULL,            -- Encrypted-at-rest would be better; plain for now
    destination_show_id TEXT NOT NULL,            -- Transistor show ID
    destination_show_name TEXT,                   -- Cached display name from Transistor
    is_active INTEGER NOT NULL DEFAULT 1,
    last_synced_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_audio_configs_user ON audio_sync_configs(user_id);
CREATE INDEX IF NOT EXISTS idx_audio_configs_active ON audio_sync_configs(is_active);

-- Per-episode tracking. Prevents double-publishing.
CREATE TABLE IF NOT EXISTS audio_synced_episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    config_id INTEGER NOT NULL,
    youtube_video_id TEXT NOT NULL,
    youtube_video_title TEXT,
    transistor_episode_id TEXT,
    synced_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (config_id) REFERENCES audio_sync_configs(id) ON DELETE CASCADE,
    UNIQUE(config_id, youtube_video_id)
);

CREATE INDEX IF NOT EXISTS idx_synced_episodes_config ON audio_synced_episodes(config_id);

-- Sync run log for observability + debugging.
CREATE TABLE IF NOT EXISTS audio_sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    config_id INTEGER NOT NULL,
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP,
    status TEXT NOT NULL DEFAULT 'running',       -- running | success | error
    episodes_synced INTEGER DEFAULT 0,
    error_message TEXT,
    FOREIGN KEY (config_id) REFERENCES audio_sync_configs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sync_runs_config ON audio_sync_runs(config_id);
CREATE INDEX IF NOT EXISTS idx_sync_runs_started ON audio_sync_runs(started_at);

-- Usage tracking per user per calendar month.
-- Counts increment when: a clip is cut (clipping_shows), an episode syncs to Transistor (audio_episodes).
-- Reset by simply looking up rows for the current YYYY-MM.
CREATE TABLE IF NOT EXISTS usage_monthly (
    user_id INTEGER NOT NULL,
    period TEXT NOT NULL,                         -- YYYY-MM e.g. "2026-08"
    clipping_shows INTEGER NOT NULL DEFAULT 0,    -- unique session_ids processed
    clipping_clips INTEGER NOT NULL DEFAULT 0,    -- total clips cut
    audio_episodes INTEGER NOT NULL DEFAULT 0,    -- episodes synced to Transistor
    PRIMARY KEY (user_id, period),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

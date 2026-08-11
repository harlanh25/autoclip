-- AutoClip multi-tenant schema
-- SQLite database at ~/cfb_clip_studio/autoclip.db

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    google_id TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE NOT NULL,
    name TEXT,
    picture_url TEXT,
    role TEXT NOT NULL DEFAULT 'member',    -- 'member' | 'admin'
    is_approved INTEGER NOT NULL DEFAULT 0, -- 0 = pending, 1 = approved
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);

CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    youtube_channel_id TEXT UNIQUE NOT NULL,  -- e.g. UCEsOcvBbXtO8AyyY2tZYJpg
    title TEXT NOT NULL,                       -- "The Power Two (College Football)"
    handle TEXT,                               -- "power-two" (for GCS paths and display)
    owner_user_id INTEGER,                     -- NULL = unclaimed
    long_uploads_enabled INTEGER DEFAULT 0,
    token_path TEXT NOT NULL,                  -- credentials/tokens/<youtube_channel_id>.json
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (owner_user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_channels_owner ON channels(owner_user_id);

CREATE TABLE IF NOT EXISTS user_channels (
    user_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    role TEXT NOT NULL DEFAULT 'uploader',   -- 'owner' | 'uploader' | 'viewer'
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, channel_id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE
);

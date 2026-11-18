CREATE TABLE IF NOT EXISTS xl_targets (
 id bigserial PRIMARY KEY, label text NOT NULL, spec jsonb NOT NULL,
 enabled boolean NOT NULL DEFAULT false, interval_hours integer NOT NULL DEFAULT 6 CHECK(interval_hours BETWEEN 1 AND 720),
 next_run_at timestamptz, last_success_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS xl_jobs (
 id bigserial PRIMARY KEY, target_id bigint REFERENCES xl_targets(id), spec jsonb NOT NULL,
 trigger text NOT NULL CHECK(trigger IN ('manual','scheduled')),
 status text NOT NULL DEFAULT 'queued', cancel_requested boolean NOT NULL DEFAULT false,
 added_posts integer NOT NULL DEFAULT 0, added_files integer NOT NULL DEFAULT 0,
 message text NOT NULL DEFAULT '', created_at timestamptz NOT NULL DEFAULT now(),
 started_at timestamptz, finished_at timestamptz, heartbeat_at timestamptz
);
CREATE TABLE IF NOT EXISTS xl_slices (
 id bigserial PRIMARY KEY, job_id bigint NOT NULL REFERENCES xl_jobs(id), since_date date, until_date date,
 url text NOT NULL, status text NOT NULL DEFAULT 'queued', seen integer NOT NULL DEFAULT 0,
 warning text NOT NULL DEFAULT '', finished_at timestamptz
);
CREATE INDEX IF NOT EXISTS xl_slices_queue ON xl_slices(job_id,status);
CREATE TABLE IF NOT EXISTS xl_posts (
 post_id text NOT NULL, version text NOT NULL, author text NOT NULL, display_name text NOT NULL,
 content text NOT NULL, published_at timestamptz, captured_at timestamptz NOT NULL DEFAULT now(),
 source_url text NOT NULL, relative_dir text NOT NULL UNIQUE, resource_key text NOT NULL UNIQUE,
 manifest jsonb NOT NULL, local_available boolean NOT NULL DEFAULT true,

 PRIMARY KEY(post_id,version)
);
CREATE INDEX IF NOT EXISTS xl_posts_date ON xl_posts(published_at DESC);
CREATE TABLE IF NOT EXISTS xl_job_posts (
 job_id bigint NOT NULL REFERENCES xl_jobs(id), post_id text NOT NULL, version text NOT NULL,
 PRIMARY KEY(job_id,post_id,version), FOREIGN KEY(post_id,version) REFERENCES xl_posts(post_id,version)
);
CREATE TABLE IF NOT EXISTS xl_state (key text PRIMARY KEY, value jsonb NOT NULL);
ALTER TABLE xl_targets ADD COLUMN IF NOT EXISTS group_name text NOT NULL DEFAULT '';
ALTER TABLE xl_targets ADD COLUMN IF NOT EXISTS archived boolean NOT NULL DEFAULT false;
ALTER TABLE xl_jobs ADD COLUMN IF NOT EXISTS pause_requested boolean NOT NULL DEFAULT false;
ALTER TABLE xl_jobs ADD COLUMN IF NOT EXISTS progress jsonb NOT NULL DEFAULT '{}';
ALTER TABLE xl_jobs ADD COLUMN IF NOT EXISTS elapsed_seconds double precision NOT NULL DEFAULT 0;
ALTER TABLE xl_jobs ADD COLUMN IF NOT EXISTS active_since timestamptz;
CREATE TABLE IF NOT EXISTS xl_events (
 id bigserial PRIMARY KEY, job_id bigint REFERENCES xl_jobs(id),
 created_at timestamptz NOT NULL DEFAULT now(), phase text NOT NULL, message text NOT NULL,
 details jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS xl_events_job ON xl_events(job_id,id DESC);
CREATE TABLE IF NOT EXISTS xl_profiles (
 username text NOT NULL, version text NOT NULL, display_name text NOT NULL,
 relative_dir text NOT NULL UNIQUE, resource_key text NOT NULL UNIQUE, manifest jsonb NOT NULL,
 captured_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(username,version)
);
ALTER TABLE xl_profiles ADD COLUMN IF NOT EXISTS local_available boolean NOT NULL DEFAULT true;

# Twitter/X core

`social-archive-twitter` extracts XLoader's background capture service into a standalone Python package. It includes account timelines/replies, Latest search with bounded date slices and cursor/max-ID pagination, individual posts, text and media, one-hop quote/reply references, avatars, image ALT metadata, durable queues and automatic scheduling. Existing posts are reused only after local manifest verification.

Automatic jobs run ahead of manual jobs. Manual jobs yield between completed slices, and pause/cancel/restart preserve completed work. The twscrape account database retains endpoint cooldowns across process restarts; media responses use bounded retries and a durable CDN cooldown. Failures remain visible in the job queue, and `control ID retry` explicitly resumes failed jobs. Dense single-day searches are marked partial: this package does not claim complete historical coverage.

This package has no HTTP server, browser login, Telegram handler, NAS uploader, NAS cleanup or desktop importer. It writes local archives only. PostgreSQL, a dedicated mounted media filesystem, and an operator-provided X session are required. It is a new service/database and must not be pointed at an existing production queue.

## Install

Use Linux, Python 3.11 or newer and PostgreSQL (integration tested with version 18). Install into a separate virtual environment:

```sh
python3 -m venv .venv-twitter
.venv-twitter/bin/pip install ./packages/twitter
.venv-twitter/bin/social-archive-twitter --help
```

The `xloader` import namespace is retained for compatibility with capture child processes; do not install it over a different XLoader package in the same environment. `twscrape==0.20.1` and the other direct dependencies are pinned to the extracted implementation's versions. Provider changes can require future updates; tests use synthetic responses, not live X credentials.

## Configuration

Create a dedicated PostgreSQL database/role and schema named `xloader` owned by that role. The role only needs access to its own schema. `init` creates application tables, not the database, role or schema. Save a restricted JSON connection file outside the repository with this shape, supplying your own values:

```json
{"database":{"host":"localhost","dbname":"archive_core","user":"archive_core","password":"REPLACE_WITH_YOUR_DATABASE_PASSWORD"}}
```

Do not put credentials in command arguments, source control or logs. Use mode `0600` for the database file and Netscape-format cookie file, and mode `0700` for the control directory. Export only path/configuration values:

```sh
export XLOADER_DB_CONFIG=/secure/archive-core/database.json
export XLOADER_SCHEMA=xloader
export XLOADER_CONTROL=/secure/archive-core/twitter
export XLOADER_MEDIA_MOUNT=/srv/archive-media
export XLOADER_MEDIA_UUID=REPLACE_WITH_YOUR_FILESYSTEM_UUID
export XLOADER_DATA=/srv/archive-media/twitter
export TMPDIR=/srv/archive-media/twitter/tmp
```

Provision and mount the filesystem before creating `XLOADER_DATA` and its `tmp` directory. Place your own Netscape cookie export at `$XLOADER_CONTROL/cookies.txt`; this package never imports host credentials automatically. The parser requires an unexpired X `auth_token` and `ct0`. Runtime account sessions contain those credentials and live in the mode-`0700` `XLOADER_DATA/session` directory with a mode-`0600` SQLite database.

`findmnt` checks the configured mount path, UUID, filesystem root and type. The defaults are filesystem root `/` and type `ext4`; set `XLOADER_MEDIA_FSROOT` or `XLOADER_MEDIA_FSTYPE` for another explicitly configured layout. Media, staging, completed files, previews and temporary files remain in the same mount. Missing/wrong storage or less than 5 GiB available fails closed. A running download rechecks storage at least once a second. There is no local-directory or system-temp fallback.

A service manager must order the worker after its mount and bind its lifecycle to that mount (for systemd, `RequiresMountsFor`, `After` and `BindsTo`), preserve per-unit `TMPDIR`, and deny writes to the unmounted underlying directory. Those deployment settings are deliberately not installed by the package. Do not share this archive, control directory, schema or worker lock namespace between independently running instances in the same database.

## Operate

```sh
social-archive-twitter check
social-archive-twitter init
social-archive-twitter target example --enable --interval-hours 6
social-archive-twitter enqueue 1 --mode incremental
social-archive-twitter worker
```

`target` prints the new target ID; substitute that ID for `1`. Initial incremental account jobs cover the latest seven calendar days, then use the last successful capture with a two-day overlap. `--mode full` uses the target's historical date range. To define a bounded historical target or a search, use `--since`, exclusive `--until`, and optionally `--kind search`:

```sh
social-archive-twitter target example --since 2020-01-01 --until 2020-02-01
social-archive-twitter enqueue 2 --mode full
social-archive-twitter target 'from:example filter:media' --kind search
social-archive-twitter target https://x.com/example/status/123456789 --kind post
social-archive-twitter jobs
social-archive-twitter control 12 pause
social-archive-twitter control 12 resume
social-archive-twitter control 12 retry
social-archive-twitter control 12 cancel
social-archive-twitter automation 1 disable
```

Single-post targets are manual only. Cancellation is permanent for that job and never deletes saved files. `automation ... disable` prevents future scheduled jobs; it does not cancel an already queued/running job. Use `control` for those jobs. To refill metadata for images already saved for an account, queue `python -m xloader.alt_backfill --schedule example`; it preserves image originals and processes descriptions through the normal worker.

Run exactly one worker per database. The worker takes a PostgreSQL advisory lock. Stop it with SIGTERM/SIGINT; child process groups are stopped and unfinished slices return to the queue. Paused/cancelled jobs stay paused/cancelled. Failed jobs are not silently discarded. Completed originals and incomplete files are never automatically cleaned up.

## Offline verification

Set `XLOADER_TEST_TMP` to an existing directory reserved for synthetic test media. On hosts with an HDD media policy, this must be on the verified HDD. Set `TMPDIR` to the same location, then:

```sh
PYTHONPATH=packages/twitter/src python -m unittest discover -s packages/twitter/tests -v
```

The suite checks query planning, parsing, preserved rate-limit locks, mocked media HTTP, complete-file publication, safe errors, CLI handling and storage failure cases. PostgreSQL tests skip unless the disposable runner enables them. To run the database/media integration as a non-root user:

```sh
export PG_BIN=/path/to/postgresql/bin
export XLOADER_TEST_CONTROL_TMP=/short/project/path/pg-tests
python packages/twitter/tests/run_postgres.py
```

The runner creates a private Unix-socket-only disposable cluster, initializes `xloader_test`, exercises scheduling, deduplication, pause/resume/cancel, retry and mocked provider-to-media publication, then stops and removes that cluster. It never connects to the normal PostgreSQL socket. The control path must be short enough for Unix socket limits. Integration tests replace mount checks because their purpose is capture/DB behavior; mount failure cases have separate tests.

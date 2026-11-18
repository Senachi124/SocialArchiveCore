# Instagram archive core

A standalone, headless extraction of the Instagram fetch and automatic archive
pipeline in `SocialMedia_autoArchive`. It retains the current gallery-dl Web
GraphQL discovery, Posts/Reels/Stories/Highlights/avatar handling, full-history
checks, automatic-first queue priority, persistent account cooldowns, automatic
retry chains, cancellation, and restart recovery.

The original web API, viewer, browser automation, retired Instaloader capture
engine, PostgreSQL/NAS helper, deployment units, and production configuration are
not included. Instaloader remains a dependency only for its authenticated session
object used by the Web GraphQL adapter. This package does not send data to NAS or
delete completed/partial media. A local SQLite catalog replaces the host archive
adapter; it verifies completed files before recording media and preserves
per-target post membership for incremental boundaries.

## Install

Python 3.11+, Linux `findmnt`, FFmpeg (`ffmpeg` and `ffprobe`) are required.

```sh
python3 -m venv .venv
.venv/bin/pip install ./packages/instagram
```

Use a dedicated mounted media filesystem. Create a private directory there before
running the CLI. Set its actual filesystem UUID and filesystem root (`findmnt`
reports `/` for the filesystem itself; a bind mount reports its source subpath).
No credentials or media directory is created in the code checkout or default temp.

```sh
export IG_CORE_MEDIA_ROOT=/mnt/archive/instagram
export IG_CORE_MOUNT_UUID=YOUR_MEDIA_FILESYSTEM_UUID
export IG_CORE_FSROOT=/
.venv/bin/instagram-archive init
.venv/bin/instagram-archive account --label primary --cookie-file /secure/instagram-cookies.txt
.venv/bin/instagram-archive target example_account --account 1 --includes posts,reels,stories,avatar --interval-hours 24
.venv/bin/instagram-archive run
```

The cookie file must already exist in Netscape format, be an absolute regular
file, and have mode 0600. Supply a complete Instagram cookie snapshot including
`sessionid` and `csrftoken`; the CLI records only its path and does not copy,
print, or update its contents. Keep this file in the installation’s protected credential area, following its
credential policy and granting access only to the intended service identity. Never place credentials in this repository.

`fetch USER --account ID --includes posts,reels` durably queues a manual profile
fetch. `--full-check` traverses history past known posts (request rate capped at
60/minute), while the default stops at a complete previously known post.
`target ... --disabled` disables that target's automatic schedule.
`run --once` schedules due targets, runs at most one job, and exits.
`status` reads recent queue and target state. `cancel ID` stops a queued/running
job without deleting files. A process lock permits only one active worker per
root; separate CLI commands can safely enqueue work while it runs.

Schedules use persisted UTC instants and default to 24-hour intervals; diagnostic
time formatting uses `Asia/Hong_Kong`. Account restrictions pause capture; HTTP
429 waits at least 15 minutes, with bounded exponential automatic retry delay.
No authenticated/anonymous or browser-engine fallback is attempted.

## Storage and recovery

Everything owned by this core is beneath `IG_CORE_MEDIA_ROOT`: `library/` holds
completed and incomplete downloads, `runtime/` holds sanitized logs and per-job
configuration, `tmp/` is the subprocess temporary directory, `archive.sqlite3`
stores accounts/targets/jobs, and `media.sqlite3` stores verified media and target
membership. Cache and rate state also remain under this root. Staging and final
media share a filesystem. The external cookie file is operator-managed.

The filesystem UUID/FSROOT and at least 5 GiB available space are checked before
work, during downloads (five-second monitor), and before catalog commits. Missing
mounts or unsafe child paths stop work; there is no alternate-disk fallback.
Only the worker's startup recovers interrupted jobs; read/status/enqueue commands
do not requeue an active process. Re-running a stopped worker resumes through the
persistent queue and existing media catalog. No NAS availability is claimed.

The SQLite adapter is intentionally single-worker. It does not reproduce the
original multi-host PostgreSQL lease system. A complete automatic interval means
the upstream extractor finished its selected scope, not proof that Instagram
exposed every historical/private/deleted post. Platform changes may require
adapter changes; gallery-dl is pinned to the checked interface.

## Offline tests

```sh
PYTHONPATH=packages/instagram/src python -m unittest discover -s packages/instagram/tests -v
```

Tests use synthetic bytes and mocked upstream responses. On hosts with an HDD policy, point `TMPDIR`
at a private directory on the verified HDD. No real account login, production
queue, network capture, or NAS transfer is used by the tests.

`PROVENANCE.json` identifies the original code paths and their hashes. Modules
were extracted from the current backend; `core.py` was reduced to its headless
functions and adapted for explicit storage. `catalog.py`, `storage.py`, `cli.py`
and redaction are standalone adaptations, not copies of host infrastructure.

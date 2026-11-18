# Operating the standalone cores

Install each package in its own virtual environment and follow its README for
configuration, state initialization and CLI commands. Run one worker per package
state directory. These packages do not start listening HTTP services.

## Storage and credentials

Provision the media mount before starting workers. Keep staging, completed media
and media-processing temporary files on the same filesystem. Configure the
expected mount identity and preserve at least the package's documented reserve.
Bind mounts must resolve to the intended filesystem root. Missing or mismatched
storage is an error, not permission to write beneath an unmounted mountpoint.

Use an external, permission-restricted account/session file. Do not place real
cookies or database passwords in source, command-line arguments, examples or
logs. Use a separate database/schema, operating-system account and state directory
for each installation; never reuse another service's production queue.

## Supervision and logs

Use an operating-system supervisor for unattended operation. Configure mount and
database dependencies, a dedicated execution account and temporary directory,
and a bounded restart delay. Verify the package's configuration before enabling
the worker at boot. Service units, public ports and host routes are not installed
by this repository. Send worker output to the supervisor's log facility and
restrict access to logs and state.

Check job status and recent successful captures in addition to process health.
An alive worker is not evidence that a platform session remains valid. Preserve
incomplete files and queued work when addressing authentication, rate-limit or
storage failures. Follow package-specific retry behavior.

## Backup, update and rollback

Stop the standalone worker cleanly before taking a consistent backup of its state,
metadata and account session databases. PostgreSQL requires a native database
backup. Back up completed and incomplete media separately, retaining their layout
and permissions. Verify a restore into isolated paths before relying on a backup.
Credentials need a separate protected backup; never place them in Git.

For an update, record the currently installed commit, stop the worker, back up
state, install and test the new package, then restart against the same verified
storage. A code rollback does not undo jobs that have already run or database
migrations. Restore a database only after accounting for all later writes; never
overwrite new media or queues with an older snapshot.

## NAS boundary

Archiving here means acquisition into persistent local storage. The original
host's NAS uploader, remote reader, backup catalog and destructive cleanup are
not bundled. Local completion does not mean a remote copy exists. A separately
implemented NAS adapter must verify complete content before declaring success,
preserve unique originals, and preserve scheduled transfer windows and resumable
state. The originating services use Asia/Hong_Kong 03:00–09:00 for automatic NAS
work; the unrelated resource-report exception does not apply to these cores.

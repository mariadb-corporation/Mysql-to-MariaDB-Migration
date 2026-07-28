# App user migration

How application users are carried from the source to the target, how to enable it,
and what to watch for. Companion to `run-files-config-and-cleanup.md`.

App user migration is optional and runs for all four modes (Serial Streaming Copy,
Parallel Restartable Streaming Copy, Offline Copy, Replication). It copies
application accounts — user, host, authentication, and grants — to the target so
apps can connect after cutover without being recreated by hand.

## Enabling it

Interactive:

```
Migrate application users? (y/n) [y]:
Default password for app users:
```

Non-interactive (set in `config/migration.yaml` or as environment flags):

- `MIGRATE_APP_USERS=1` — turn the phase on (`0` skips it).
- `APP_USER_DEFAULT_PASSWORD` — password assigned to accounts whose original
  password can't be carried over. Leave unset to be prompted.
- `APP_USER_PWD_EXPIRE` — whether default-password accounts must change their
  password at first login. See "Default-password expiry" below.

## Where it runs

The phase runs immediately after preflight and before the assessment/precheck,
so a user-discovery problem (source unreachable, permissions) surfaces early
rather than mid-migration. Discovery is read-only during assess; accounts are
actually written to the target during the run. A discovery failure is logged
but does not abort the run — user migration is optional and the rest of the
migration can still proceed.

## Passwords and authentication

Each migrated account keeps its original password wherever the tool can carry
the credential across intact.

`caching_sha2_password` accounts cannot be transported at present — this may
change in a future release. Those accounts are created on the target with the
shared default password instead, so no account is dropped for a password that
can't be moved.

## Default-password expiry

`APP_USER_PWD_EXPIRE` controls only the accounts that were assigned the shared
default password — accounts whose original password was carried over are never
affected either way.

- **Expire on (`APP_USER_PWD_EXPIRE=1`)** — every account given the default
  password must set a new one at first login before it can do anything else.
  Use this when the default is a temporary handoff value and you want each
  owner to replace it. Automated/service accounts that log in unattended will
  be blocked until someone changes the password, so exclude those or plan to
  set their passwords directly.
- **Expire off (default, `APP_USER_PWD_EXPIRE=0` or unset)** — accounts keep the
  default password until you change it yourself. Use this when the default is a
  known value the application is already configured with, or when unattended
  accounts need to connect immediately after cutover.

Either way, treat the default password as a value to rotate off soon after
migration — it is shared across every account that couldn't be ported.

## What gets migrated — and what to exclude

Discovery currently returns application accounts *and* infrastructure/replication
accounts. Until the exclusion deny-list ships, review the discovered set and
drop infrastructure accounts by hand. Typical names to exclude:

```
repl_user  replicator  dbpwf*  pmm_*  xtrabackup*  aws_*  mysqld_exporter
```

These belong to the source platform (managed-service internals, monitoring,
backup, replication) and should not land on the target.

## Grants

Grants are reapplied after the users are created. Some fail on MariaDB for
distinct reasons — MySQL-only dynamic privileges, role-grant ordering, or
syntax that has no MariaDB equivalent. A grant that drops is logged; it does
not stop the run. Review grant failures in the log and reapply anything the
application actually needs.

## Reading the results

Everything lands in the run log alongside the rest of the migration:

```bash
# app-user phase progress and any dropped grants
grep -niE 'app.user|grant|denied' artifacts/<run>/run.log

# full live view
tail -f artifacts/<run>/run.log
```

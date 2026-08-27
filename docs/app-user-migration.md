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

Two further options are read from the environment only, not from
`config/migration.yaml`:

- `PORT_SHA2_PASSWORDS` — whether to carry over passwords for
  `caching_sha2_password` accounts. On by default; set to `0` to give every such
  account the default password instead.
- `PORT_SHA2_INSTALL_PLUGIN` — whether the tool may load the target
  authentication plugin itself when it isn't already loaded. Off by default; the
  tool reports what to run instead.

## Where it runs

The phase runs immediately after preflight and before the assessment/precheck,
so a user-discovery problem (source unreachable, permissions) surfaces early
rather than mid-migration. Discovery is read-only during assess; accounts are
actually written to the target during the run. A discovery failure is logged
but does not abort the run — user migration is optional and the rest of the
migration can still proceed.

## Passwords and authentication

Each migrated account keeps its original password wherever the tool can carry
the credential across intact. Accounts it cannot carry are created on the target
with the shared default password instead, so no account is dropped for a
password that can't be moved.

Accounts using `caching_sha2_password` keep their original password when the
target supports it. Two things have to be true:

- **The target has the `caching_sha2_password` plugin loaded.** It is available
  in MariaDB 11.4.9, 11.8.4, and later releases, but is not loaded by default.
  If it isn't loaded, the tool says so at the start of the phase and gives you
  the statement to run on the target:

  ```sql
  INSTALL SONAME 'auth_mysql_sha2';
  ```

  Re-run the phase afterwards and those accounts keep their original passwords.
  Alternatively set `PORT_SHA2_INSTALL_PLUGIN=1` to let the tool load it as part
  of the run.

- **The stored password is in the expected format.** The tool checks each one
  before carrying it over and falls back to the default password for anything
  unexpected, recording the reason, rather than creating an account nobody can
  log into.

Accounts using `sha256_password` always get the default password.

Transport caveat for `caching_sha2_password` accounts: this authentication
plugin requires TLS or RSA key exchange to log in. If the connection has
neither, the login fails with a generic **access denied**, even when the
password was carried over correctly. If a migrated user can't connect on the
target, check the connection's TLS/RSA setup before suspecting the credential.

## What isn't carried over

Accounts are recreated on the target with their authentication and grants.
Password policy and connection requirements attached to the account on the
source are not reproduced:

- Password history, reuse interval, and "require current password" settings
- Password expiry policy (as distinct from the one-off expiry
  `APP_USER_PWD_EXPIRE` applies to default-password accounts)
- Failed-login lockout settings
- `REQUIRE SSL`, `REQUIRE X509`, and cipher/issuer/subject requirements

The last of these loosens the account: a user that could only connect over TLS
on the source can connect without it on the target. Re-apply anything your
security posture depends on after the users phase completes.

## Re-running the phase

Running the users phase again does not drop or recreate accounts — it updates
them in place. Each account's password is reset to what the source holds, so if
someone changed a default password on the target between runs, a re-run replaces
it. Grants are reapplied additively; a privilege revoked on the target by hand
comes back on the next run, because the source is treated as the source of
truth.

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
- **Expire off (`APP_USER_PWD_EXPIRE=0`)** — accounts keep the
  default password until you change it yourself. Use this when the default is a
  known value the application is already configured with, or when unattended
  accounts need to connect immediately after cutover.

Either way, treat the default password as a value to rotate off soon after
migration — it is shared across every account that couldn't be ported.

The migration report names which accounts kept their original password and which
got the default one, with the reason for each fallback.

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
grep -niE 'app.user|grant|denied|caching_sha2' artifacts/<run>/run.log

# full live view
tail -f artifacts/<run>/run.log
```

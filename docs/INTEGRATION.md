# Integration surface

For consumers that drive the migrator programmatically rather than through
the launcher: the MariaDB Shell MCP plugin and the Migrator AI skills.
These bind to the documents below and nothing else; anything not listed is
internal and may change without notice.

| Surface | Document |
|---|---|
| Entry points, flags, phases | `docs/INVOCATION_REFERENCE.md` |
| Environment variables | `docs/environment-variables.md` |
| Finding IDs, classes, severities | `docs/check-classes.md` |
| Run files and artifact layout | `docs/run-files-config-and-cleanup.md` |

## Non-interactive callers

A session without a terminal does not prompt: it logs the report and
continues, unless `MIGRATOR_BLOCK_ON_LOAD_FAILURES=1`. Honoured by the
launcher, `orchestrator/migrationctl.py` and `scripts/26_staged_load.sh`.

That default suits a scripted caller that has already assessed. MCP and
AI-skill consumers should set the variable on every invocation, present the
findings as a decision, and re-invoke without it once accepted.

`run` re-gates on precheck even where a step is marked done, so a `resume`
can stop where an earlier invocation proceeded. Report that as a precheck
result, not a tool error.

## Changing this surface

A change to any document above needs a line in `CHANGELOG.md` naming the
surface.

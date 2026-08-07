# `pv` / progress-signal fixtures (TDD unit 2, `pvparse.py`)

| File | Provenance |
|---|---|
| `fixture_staged_dump_1.6.txt` | **Real capture.** `pv -pet -f -i 10 -N sakila -s <size>` against pv 1.6.6 (EC2 host, EPEL package — matches the plan's "CentOS 7 / RHEL 7" 1.6.x target). Confirms `\r`-delimited updates, no byte/rate field (since `-b`/`-r` are never passed), trailing padding spaces on the final line. |
| `fixture_one_step.txt` | **Real capture.** Same host, `pv -pet -f -i 10` with no `-s`/`-N` — confirms no percent, no ETA when there's no total. |
| `fixture_staged_load.txt` | **Real capture.** Same host, `-f` omitted with stderr redirected to a file (not a tty) — confirms pv suppresses all output entirely, i.e. this fixture is intentionally empty. Encodes the `26_staged_load.sh` "-f missing" finding (§10.1 item 2 of the design doc). |
| `fixture_staged_dump_1.8.txt` | **Hand-authored**, not captured — no pv 1.8.x host was available (only 1.6.6 via EPIC on the accessible EC2 host). Built as the same field grammar as the real 1.6.x capture (structure is stable across pv's `-pet` implementation); revisit with a real capture if an Ubuntu 24.04-class host becomes available. |
| `fixture_file_size_probe.txt` | Not from `pv` — reproduces `scripts/25_staged_dump.sh`'s `file_size_probe()` output format exactly (`fmt_size`/`rate_kbs`/`pct` per `:242-268`), which is a deterministic bash `echo`, not a third-party tool quirk. |
| `fixture_done_lines.txt` | Same rationale — reproduces the `[done]` line format from `25_staged_dump.sh:338` exactly. |
| `fixture_negative.txt` | Hand-built negative cases: a `mariadb-dump` warning, an `ERROR:` line, a `CREATE TABLE` line containing a literal `100%` inside a string default, and a line containing a bare timestamp (`12:34:56`). All must parse to `None` — a naive regex would false-match several of these. |

`fixture_staged_dump_1.8.txt` is the one gap worth closing for real if a suitable host turns up — version skew is the specific risk the plan flags (§8.2), not size skew, and it's the one file here that isn't a genuine capture.

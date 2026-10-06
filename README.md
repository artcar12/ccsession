# ccsession

Reads this machine's Claude Code sessions and turns them into plain session rows:

- **live** sessions from `~/.claude/sessions/<pid>.json` (status, what each one is waiting for), with a
  liveness check on the pid and its `procStart`
- the **index** of every transcript under `~/.claude/projects/*/*.jsonl`, scanned incrementally by mtime
- the **desktop index** of each Claude desktop data dir (`~/Library/Application Support/Claude*`):
  titles, status summaries, PRs, branches and the archived flag, for Code and Cowork sessions
- the **project** for each session: the repo under `~/Projects`, with `.claude/worktrees/<x>` folded
  back into its repo, or the repo its tool calls touched most for sessions started at the root

`ccsessiond` serves the same rows to local programs over HTTP on `127.0.0.1` or over stdin/stdout, and
keeps them current as sessions change.

It reads, and sends nothing anywhere. The one thing it writes is the `isArchived` flag in a desktop
record, when a local program asks it to archive or unarchive a session.

Standard library only, Python 3.11 or newer, macOS and Linux (the desktop index is macOS only).

## Install from source

`ccsessiond` is meant to live at a known path, `~/.local/bin/ccsessiond`, in its own environment.

With [uv](https://docs.astral.sh/uv/) (recommended):

```bash
uv tool install git+https://github.com/artcar12/ccsession   # @vX.Y.Z pins a release; or: uv tool install ./ccsession
ccsession                         # counts of what was read
ccsession --json                  # live sessions and the index as JSON
ccsessiond --once                 # the live sessions as one Session v1 snapshot
```

`uv tool install --force …` upgrades it in place. Without uv (Python 3.11 or newer):

```bash
python3 -m venv ~/.local/share/ccsession-venv
~/.local/share/ccsession-venv/bin/pip install git+https://github.com/artcar12/ccsession
mkdir -p ~/.local/bin
ln -sf ~/.local/share/ccsession-venv/bin/ccsession ~/.local/share/ccsession-venv/bin/ccsessiond ~/.local/bin/
```

Or straight from a checkout, with nothing installed: `python3 -m ccsession`, `python3 -m ccsession.daemon`.

## Run as a service

```bash
ccsessiond service install      # launchd agent (macOS) or systemd user unit (Linux), started now and at login
ccsessiond service status
ccsessiond service uninstall
ccsessiond doctor               # self-check; --json for programs
```

`service install` writes `~/Library/LaunchAgents/com.github.artcar12.ccsessiond.plist` (restarted if it
crashes) or `~/.config/systemd/user/ccsessiond.service` (`Restart=on-failure`) for the `ccsessiond` you ran
it from, and starts it. Run it from `~/.local/bin/ccsessiond` so an upgrade keeps the service working.
Errors go to `~/.local/state/ccsession/ccsessiond.log`. `--dry-run` prints the file instead.

`ccsessiond doctor` checks the config file, the Claude dirs it reads, the token file's mode, the service
(installed, running, pointing at an existing program), the API (answers with this token and version),
`claude-open` (macOS), and files that fail to parse. It exits 1 when a check fails.

## Config

`~/.config/ccsession/config.json` (or `$XDG_CONFIG_HOME/ccsession/config.json`). Every key is optional.

| Key | Default | What |
|---|---|---|
| `claude_dir` | `$CLAUDE_CONFIG_DIR` or `~/.claude` | where `sessions/` and `projects/` live |
| `projects_root` | `~/Projects` | the folder whose first-level dirs are projects |
| `app_dirs` | unset | desktop data dirs to read; unset reads every `Application Support/Claude*` dir that has `claude-code-sessions` |
| `min_path_mentions` | `3` | tool-call mentions needed to attribute a root-level session to a project |
| `port` | `8788` | `ccsessiond`'s port on `127.0.0.1` |
| `machine` | short host name | the `machine` field in snapshots |
| `token_file` | `~/.local/state/ccsession/token` | `ccsessiond`'s API token (created 0600 on first run) |
| `claude_open` | unset | path to `claude-open` (from Session Tiles), which delivers opens on macOS; `--claude-open` overrides it |

## Reader rules

- A live file with no `procStart` is kept if its pid is alive. One whose `procStart` doesn't match the
  process start time (within 2 s) is dropped, because the pid was reused.
- `instance` is the desktop data dir's name (`Claude`, `Claude-personal`); `account` is the account-id
  folder under `claude-code-sessions`. The same account id can appear under two instances.
- Desktop records are matched to live sessions on `hostSessionId` and to transcripts on `cliSessionId`.
- Cowork sessions are included with `kind: "cowork"`; sessions with no desktop record are `kind: "cli"`.
- A session with a desktop record and no live process is `dormant`. `scan_index` rows assume nothing is
  live; `read_all` marks the live ones (`overlay_live`). A desktop transcript resumed from a terminal
  (no matching `hostSessionId`) is a separate `cli` live session, and its desktop record stays dormant.
- **Outcome:** `error` when the last assistant entry is an API error (`isApiErrorMessage`), keeping
  `error_kind` (its `error`, e.g. `rate_limit`), `api_status` (e.g. `429`) and `resets_at`
  (`quotaLimits.resetsAt`); a later successful assistant turn clears it. `interrupted` when the last
  user entry is a user interrupt (`[Request interrupted by user…]`), which doesn't count as a prompt or
  a turn. Otherwise `ok`.
- **Error status:** a live session whose newest entry is that API error has `status: "error"` from the
  error's time, until a newer entry arrives.
- **Owner:** a desktop session's `owner` is `{data_dir, pid}`: the data dir holding its record, and that
  instance's main process. For a live session, the ppid chain from its pid (`claude` → `disclaimer` →
  `Claude`) gives the pid when it reaches the instance holding the record; otherwise the running
  `Claude` main process started with that `--user-data-dir` (none means `Application Support/Claude`);
  `null` when that instance isn't running.

Each rule has a case in `schema/fixtures/reader/` that the tests run through the reader.

## Session v1

`schema/session.v1.json` (JSON Schema 2020-12) is the session shape: one object per session, live or
indexed, with times in epoch milliseconds. It covers status (`waiting`, `idle`, `busy`, `unknown`,
`error`, or `null` when not live), `status_since`, `dormant`, `archived`, `instance`, `account`, `kind`,
`owner` (`{data_dir, pid}` for desktop sessions), `outcome` (`ok`, `error`, `interrupted`) and the error
fields (`error_kind`, `api_status`, `resets_at`). `$defs/live` is the live snapshot
(`{machine, now, sessions}`).

`read_all` and `ccsessiond` emit rows that validate against it; the tests check both.

`schema/fixtures/sessions/` holds made-up Session v1 examples (desktop, CLI, Cowork, waiting, error, and a
live snapshot) for programs that consume sessions to test against.

## ccsessiond

```bash
ccsessiond              # local API on 127.0.0.1:8788
ccsessiond --stdio      # events on stdout, actions on stdin (for a program that starts it as a child)
ccsessiond --once       # one /v1/live snapshot, then exit
ccsessiond doctor       # self-check
```

It reads `~/.claude/sessions` every 0.5 s (the files are rewritten in place) and the transcripts and
desktop records every 2 s, re-reading only what changed and only the new part of a transcript. The first
snapshot comes from the live sessions' transcripts and the desktop records; the rest of the index fills
in behind it.

**HTTP.** Every request needs `Authorization: Bearer <token>` (the token file above) and a `Host` of
`127.0.0.1:<port>` or `localhost:<port>`; anything else gets a 401 or a 403. There are no CORS headers,
so browsers can't read it.

| Method & path | Returns / does |
|---|---|
| `GET /v1/health` | `ccsession` version, `schema`, `machine`, instances (data dir, running pid, accounts), accounts, reader config, `warnings` (files that failed to parse twice in a row), counts |
| `GET /v1/live` | `{machine, now, sessions}`: the live sessions (`$defs/live`) |
| `GET /v1/events` | Server-sent events, below |
| `GET /v1/sessions` | `{sessions, removed, cursor, reset}`: the index, newest first. Filters: `kind`, `instance`, `account`, `project`, `archived=true\|false`, `since` (ms, on `last`). Pass the returned `cursor` back to get only rows that changed since, and the ids `removed` since; `reset: true` means the cursor was from an earlier run and every row is returned |
| `GET /v1/sessions/{id}` | one session |
| `GET /v1/sessions/{id}/transcript?prose=1` | `{id, entries: [{role, text, at}]}`: user prompts and assistant prose, without tool calls, tool results, thinking or API error notices |
| `POST /v1/sessions/{id}/archive` | `{"archived": true\|false}` sets `isArchived` in the desktop record: the one flag is swapped in place and a complete copy renamed over the file. 409 `not_single_flag` unless the record holds exactly one `isArchived`; 409 `not_desktop` for a `cli` session |
| `POST /v1/sessions/{id}/open` | 501 for now |
| `POST /v1/sessions/{id}/message` | 501 (planned) |

**Events.** `/v1/events` and `--stdio` send the same events. The stream covers every live session and
every desktop (Code and Cowork) session, so dormant and archived ones are included; `cli` sessions that
aren't live are only in `/v1/sessions`.

| Event | Data |
|---|---|
| `snapshot` | `{machine, now, sessions}`, first |
| `health` | the `/v1/health` body, after the snapshot and whenever it changes |
| `upsert` | a whole session, new or changed |
| `status` | `{id, status, status_since, waiting_for}` when only those changed |
| `remove` | `{id}` |

Over HTTP each is `event: <name>` plus `data: <json>`, with a comment line every 15 s on a quiet
stream. Over `--stdio` each is one line, `{"event": "<name>", "data": …}`.

**Actions over `--stdio`.** One JSON object per line on stdin, answered by a `result` event:

```
{"action": "archive", "id": "<session id>", "archived": true, "req": "a1"}
{"event": "result", "data": {"req": "a1", "action": "archive", "id": "<session id>", "status": 200, "result": {"id": "<session id>", "archived": true}}}
```

`status` and `result` are what the HTTP route would return. `ccsessiond --stdio` exits when stdin
closes.

## As a library

```python
import ccsession

cfg = ccsession.load_config()
live, sessions, stats = ccsession.read_all(cfg)                # Session v1 rows

rows, files, stats = ccsession.scan_index(cfg)               # first pass
rows, files, stats = ccsession.scan_index(cfg, files=files)  # only what changed since
```

## Tests

```bash
uv run python -m unittest discover -s tests   # with jsonschema (dev dependency) for the schema tests
python3 -m unittest discover -s tests          # stdlib only; skips schema validation
```

## License

MIT

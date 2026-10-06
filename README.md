# ccsession

Reads this machine's Claude Code sessions and turns them into plain session rows:

- **live** sessions from `~/.claude/sessions/<pid>.json` (status, what each one is waiting for), with a
  liveness check on the pid and its `procStart`
- the **index** of every transcript under `~/.claude/projects/*/*.jsonl`, scanned incrementally by mtime
- the **desktop index** of each Claude desktop data dir (`~/Library/Application Support/Claude*`):
  titles, status summaries, PRs, branches and the archived flag, for Code and Cowork sessions
- the **project** for each session: the repo under `~/Projects`, with `.claude/worktrees/<x>` folded
  back into its repo, or the repo its tool calls touched most for sessions started at the root

It only reads. It never writes Claude's files and sends nothing anywhere.

Standard library only, Python 3.11 or newer, macOS and Linux (the desktop index is macOS only).

## Run from source

With [uv](https://docs.astral.sh/uv/) (recommended):

```bash
git clone https://github.com/artcar12/ccsession.git
uv tool install ./ccsession       # puts `ccsession` on your PATH
ccsession                         # counts of what was read
ccsession --json                  # live sessions and the index as JSON
```

Without uv:

```bash
git clone https://github.com/artcar12/ccsession.git
python3 -m venv ~/.local/share/ccsession-venv
~/.local/share/ccsession-venv/bin/pip install ./ccsession
~/.local/share/ccsession-venv/bin/ccsession
```

Or straight from the checkout, with nothing installed: `python3 -m ccsession`.

## Config

`~/.config/ccsession/config.json` (or `$XDG_CONFIG_HOME/ccsession/config.json`). Every key is optional.

| Key | Default | What |
|---|---|---|
| `claude_dir` | `$CLAUDE_CONFIG_DIR` or `~/.claude` | where `sessions/` and `projects/` live |
| `projects_root` | `~/Projects` | the folder whose first-level dirs are projects |
| `app_dirs` | unset | desktop data dirs to read; unset reads every `Application Support/Claude*` dir that has `claude-code-sessions` |
| `min_path_mentions` | `3` | tool-call mentions needed to attribute a root-level session to a project |

## Reader rules

- A live file with no `procStart` is kept if its pid is alive. One whose `procStart` doesn't match the
  process start time (within 2 s) is dropped, because the pid was reused.
- `instance` is the desktop data dir's name (`Claude`, `Claude-personal`); `account` is the account-id
  folder under `claude-code-sessions`. The same account id can appear under two instances.
- Desktop records are matched to live sessions on `hostSessionId` and to transcripts on `cliSessionId`.
- Cowork sessions are included with `kind: "cowork"`; sessions with no desktop record are `kind: "cli"`.
- A session with a desktop record and no live process is `dormant`. `scan_index` rows assume nothing is
  live; `read_all` marks the live ones (`overlay_live`).

Each rule has a case in `schema/fixtures/reader/` that the tests run through the reader.

## Session v1

`schema/session.v1.json` (JSON Schema 2020-12) is the session shape: one object per session, live or
indexed, with times in epoch milliseconds. It covers status (`waiting`, `idle`, `busy`, `unknown`,
`error`, or `null` when not live), `status_since`, `dormant`, `archived`, `instance`, `account`, `kind`,
`owner` (`{data_dir, pid}` for desktop sessions), `outcome` (`ok`, `error`, `interrupted`) and the error
fields (`error_kind`, `api_status`, `resets_at`). `$defs/live` is the live snapshot
(`{machine, now, sessions}`).

The reader emits everything except `owner`, `outcome` and the error fields, which need the session
daemon (owner resolution and transcript error detection).

`schema/fixtures/sessions/` holds made-up Session v1 examples (desktop, CLI, Cowork, waiting, error, and a
live snapshot) for programs that consume sessions to test against.

## As a library

```python
import ccsession

cfg = ccsession.load_config()
live, sessions, stats = ccsession.read_all(cfg)

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

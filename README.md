# duet

**An adversarial, cross-vendor code loop.** One model writes the code, a
**different vendor's** model reviews it, and they loop until the reviewer says
*"looks good"* — or until the loop detects a stall. **duet never commits or
pushes.** The diff is left uncommitted for a human to inspect and approve.

```
┌──────────┐   writes / edits files    ┌───────────┐
│ builder  │ ────────────────────────► │  working  │
│ (Claude) │                           │   tree    │
└──────────┘                           └─────┬─────┘
     ▲                                       │ uncommitted diff
     │ apply the fixes                       ▼
     │                                 ┌───────────┐
     └──────── reviewer feedback ───── │ reviewer  │
                                       │  (Codex)  │
                                       └───────────┘
     loop until the reviewer says LGTM · human commits · never automatic
```

## Why cross-vendor?

A model reviewing its own output shares its own blind spots — the same
reasoning that produced a bug tends to wave it through on review. A reviewer
from a **different vendor** catches classes of mistakes the author's model
systematically misses. duet makes that the default: the builder and the
reviewer are two different CLI tools, from two different labs.

The other opinionated choice: **the loop can never write git history.** A shim
on the builder's `PATH` blocks `commit`/`push`/`merge`/`rebase`/`reset --hard`
for the duration of a session. When the loop converges you get a diff, not a
commit — you stay the only one who merges.

## How it compares

Most parallel-agent tools (Claude Squad, Conductor, …) run N agents side by
side, each in its own worktree, and let *you* review the results. duet is a
different shape: a **single task**, driven by a **two-model adversarial loop**
that decides for itself when the work is actually done (reviewer LGTM), and
that refuses to touch git history. It composes fine with the worktree tools —
point duet at one worktree.

## Install

duet is a single Python file, standard library only (Python 3.8+).

```bash
curl -O https://raw.githubusercontent.com/Onyx-ia/duet/main/duet.py
chmod +x duet.py
# optionally: sudo mv duet.py /usr/local/bin/duet
```

You need the two CLIs on your `PATH`:

- **builder** — [Claude Code](https://claude.com/claude-code) (`claude`)
- **reviewer** — [Codex CLI](https://developers.openai.com/codex/cli) (`codex`), logged in (`codex login`)

Both are swappable — see *Configuration*.

## Usage

```bash
# short form: duet <repo> "<objective>"
duet ~/code/myapp "Add rate limiting to the /login endpoint, 5 attempts/min"

# on a protected branch? work on a fresh session branch instead
duet ~/code/myapp "Refactor the auth module" --branch

# check the plumbing without running the loop
duet doctor ~/code/myapp

# from another terminal, while a loop runs:
duet status ~/code/myapp
duet stop   ~/code/myapp     # graceful stop after the current exchange
duet resume ~/code/myapp     # continue a paused session (rate limit, 10-exchange pause, …)
```

The working tree must be **clean** before a run — duet needs the diff it
produces to be entirely its own.

### What you get

Everything lands in `.duet/` inside the repo:

- `session.json` — live state (status, exchange count, files, per-exchange excerpts)
- `session.log` — human-readable log
- `NNN_builder.txt` / `NNN_reviewer.txt` — full output of each turn
- `final.diff` — the complete diff at the end, ready for `git apply` review

The loop ends in one of: **converged** (reviewer LGTM), **already_done**,
**stalled** (repeating or no changes), or **paused** (rate limit / max
exchanges — resume when ready).

## Configuration

Everything is env vars or flags — no config file.

| Env var | Flag | Default | Meaning |
|---|---|---|---|
| `DUET_BUILDER_BIN` | | `claude` | builder CLI command |
| `DUET_REVIEWER_BIN` | | `codex` | reviewer CLI command |
| `DUET_BUILDER_MODEL` | `--builder-model` | tool default | model id for the builder |
| `DUET_REVIEWER_MODEL` | `--reviewer-model` | tool default | model id for the reviewer |
| `DUET_MAX_EXCHANGES` | `--max-exchanges` | `10` | exchanges before pausing for a human |
| `DUET_WORKSPACE` | | `.` | base dir for bare repo names |
| `DUET_WEBHOOK_URL` | | (none) | POST `{task, event, text}` on every human-relevant event |
| `DUET_TIMEOUT_BUILDER` | | `1200` | builder timeout (seconds) |
| `DUET_TIMEOUT_REVIEWER` | | `600` | reviewer timeout (seconds) |

**Playbooks.** Give each side a persona / checklist:

```bash
duet ~/code/myapp "Harden the file upload path" \
  --builder-prompt  ./examples/builder.md \
  --reviewer-prompt ./examples/reviewer.md
```

The reviewer playbook is handed to Codex as `AGENTS.md` for the duration of each
review (and restored afterwards).

## Safety model

- **No automatic git history.** A `PATH` shim blocks history-mutating git during
  the session. You review `final.diff` and commit yourself.
- **Clean-tree precondition.** Refuses to start on a dirty tree.
- **Protected branches.** Refuses `main`/`master` unless you pass `--branch`.
- **Secret scanning.** Flags secret-like patterns the builder emits.

duet still runs a coding agent that edits your files. Run it on a repo you can
throw away changes to (that's why the diff stays uncommitted), and read the
diff before you commit.

## Status

Extracted and generalized from a private multi-agent setup that ran this loop
in production against a real codebase. The convergence logic is the
battle-tested part; the packaging is new. Issues and PRs welcome.

## License

MIT — see [LICENSE](LICENSE).

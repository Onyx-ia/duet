#!/usr/bin/env python3
"""
duet.py — An adversarial, cross-vendor code loop.

One model writes the code, a *different vendor's* model reviews it, and they
loop until the reviewer says "looks good" (LGTM) — or until the loop detects a
stall/oscillation. duet never commits or pushes: the diff is left uncommitted
for a human to inspect and approve.

Default pairing (both are real CLI tools you already have installed):
    builder  = Claude Code   (`claude -p ...`)   -> writes/edits files
    reviewer = Codex CLI      (`codex review`)    -> critiques the uncommitted diff

Why cross-vendor? A model reviewing its own output shares its own blind spots.
A reviewer from a different vendor catches classes of mistakes the author's
model tends to miss.

Usage:
    duet <repo-or-worktree> "<objective>" [--branch] [options]
    duet status  <repo>
    duet stop    <repo>
    duet resume  <repo>
    duet doctor  <repo>

<repo> is a path to a git repository or worktree. If you pass a bare name it is
resolved against $DUET_WORKSPACE (default: current directory).

Options:
    --branch                 Work on a fresh session/<name>-<ts> branch instead
                             of the current one (required if current branch is
                             protected: main/master).
    --builder-model M        Model id for the builder    (env DUET_BUILDER_MODEL)
    --reviewer-model M       Model id for the reviewer    (env DUET_REVIEWER_MODEL)
    --builder-prompt FILE    Extra system prompt for the builder (a "playbook").
    --reviewer-prompt FILE   Extra system prompt for the reviewer (written to
                             AGENTS.md for the duration of each review).
    --max-exchanges N        Exchanges before pausing for a human (default 10).

Notifications (all optional, best-effort):
    Desktop:  uses `notify-send` if present.
    Webhook:  set DUET_WEBHOOK_URL to POST {text, event, task} as JSON on every
              human-relevant event (start, converge, stall, secret, end).

No third-party dependencies — Python 3.8+ standard library only.
MIT licensed.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

HOME = Path.home()
WORKSPACE = Path(os.environ.get("DUET_WORKSPACE", ".")).expanduser()

# --- Builder / reviewer commands -----------------------------------------
BUILDER_BIN  = os.environ.get("DUET_BUILDER_BIN", "claude")
REVIEWER_BIN = os.environ.get("DUET_REVIEWER_BIN", "codex")
BUILDER_MODEL  = os.environ.get("DUET_BUILDER_MODEL", "")   # "" => tool default
REVIEWER_MODEL = os.environ.get("DUET_REVIEWER_MODEL", "")

# --- Loop tuning ----------------------------------------------------------
MAX_EXCHANGES   = int(os.environ.get("DUET_MAX_EXCHANGES", "10"))
STALL_WINDOW    = 2
TIMEOUT_BUILDER = int(os.environ.get("DUET_TIMEOUT_BUILDER", "1200"))
TIMEOUT_REVIEW  = int(os.environ.get("DUET_TIMEOUT_REVIEWER", "600"))
PROTECTED       = {"main", "master"}

WEBHOOK_URL = os.environ.get("DUET_WEBHOOK_URL", "")

BUILDER_ALREADY_DONE_PATTERNS = [
    "already implemented", "already in place", "already done", "already exists",
    "no changes needed", "nothing to change", "no change required",
]
BUILDER_LIMIT_PATTERNS = [
    "you've hit your session limit", "session limit", "rate limit", "resets ",
    "usage limit",
]
SECRET_PATTERNS = [
    r"sk-[a-zA-Z0-9]{20,}",
    r"(?i)(?:api|secret|access)[_-]?(?:key|token)\s*[:=]\s*['\"][^'\"]{8,}",
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    r"AKIA[0-9A-Z]{16}",
]
REVIEWER_ERROR_PATTERNS = [
    "rate limit", "quota exceeded", "insufficient_quota", "too many requests",
    "context length", "api error", "authentication failed", "unauthorized",
    " 503 ", " 502 ", "connection error", "not logged in", "please run codex login",
]
NO_CHANGES_PATTERNS = [
    "no staged", "no unstaged", "no changes were found", "no patch",
    "working tree clean", "no uncommitted", "nothing to review", "no code changes",
]

# The git guard: a shim placed first on PATH for the builder subprocess. It
# lets reads through and blocks history-mutating writes, so the loop can never
# commit/push/reset --hard behind your back. The diff is what you review.
GIT_GUARD_SCRIPT = """\
#!/bin/bash
# duet git guard — blocks history-mutating writes, allows everything else.
SUBCMD="${1:-}"
BLOCK_MSG="DUET GUARD: git $* — commit/push/merge/rebase/reset --hard are blocked during a session. The diff stays uncommitted for human review."
case "$SUBCMD" in
    commit|push|merge|rebase)
        echo "$BLOCK_MSG" >&2; exit 1;;
    reset)
        for arg in "$@"; do [[ "$arg" == "--hard" ]] && { echo "$BLOCK_MSG" >&2; exit 1; }; done;;
esac
exec REAL_GIT "$@"
"""


# --- Resolution -----------------------------------------------------------
def resolve_repo(task: str) -> Path:
    p = Path(task).expanduser()
    if p.is_dir():
        return p.resolve()
    cand = (WORKSPACE / task).expanduser()
    if cand.is_dir():
        return cand.resolve()
    sys.exit(f"Repository not found for '{task}' (pass a path or a name under "
             f"$DUET_WORKSPACE={WORKSPACE}).")


def task_name(repo: Path) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", repo.name) or "duet"


def find_bin(name: str) -> str:
    b = shutil.which(name)
    if b:
        return b
    sys.exit(f"Command '{name}' not found on PATH. Install it or set the "
             f"matching DUET_*_BIN environment variable.")


def real_git() -> str:
    return shutil.which("git") or "/usr/bin/git"


# --- Notifications --------------------------------------------------------
class Notifier:
    def __init__(self, task: str, log_file: Path):
        self.task = task
        self.log_file = log_file

    def log(self, msg: str):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [{self.task}] {msg}\n"
        try:
            self.log_file.open("a").write(line)
        except Exception:
            pass
        print(line, end="")

    def _webhook(self, text: str, event: str):
        if not WEBHOOK_URL:
            return
        try:
            data = json.dumps({"task": self.task, "event": event, "text": text}).encode()
            req = urllib.request.Request(
                WEBHOOK_URL, data=data,
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as e:
            self.log(f"WEBHOOK_ERROR {e}")

    def _desktop(self, title: str, msg: str):
        if not shutil.which("notify-send"):
            return
        try:
            subprocess.run(["notify-send", "-a", "duet", str(title), str(msg)[:200]],
                           capture_output=True, timeout=20)
        except Exception:
            pass

    # A human-relevant event: log, desktop toast, webhook.
    def alert(self, text: str, title: str = None, event: str = "alert"):
        self.log(text.replace("\n", " ")[:300])
        if title is not None:
            self._desktop(title, re.sub(r"[*_`#]", "", text))
        self._webhook(text, event)

    # Quiet progress: webhook only (keeps the terminal clean).
    def progress(self, text: str):
        self._webhook(text, "progress")


# --- Convergence logic (battle-tested; kept verbatim) ---------------------
def _extract_verdict(raw: str) -> str:
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    if any(l.lower() in ("lgtm", "lgtm.", "lgtm!") for l in lines):
        return "LGTM"
    action_prefixes = ("p0 ", "p1 ", "p2 ", "p3 ", "blocker:", "fix:", "- fix",
                       "must ", "should ", "- p")
    action_lines = [l for l in lines if any(l.lower().startswith(p) for p in action_prefixes)]
    if action_lines:
        return "\n".join(action_lines[:6])
    paragraphs = [p.strip() for p in raw.split("\n\n") if len(p.strip()) > 20]
    return paragraphs[-1][:400] if paragraphs else raw[:400]


def reviewer_is_error(raw: str) -> bool:
    m = raw.lower()
    return any(p in m for p in REVIEWER_ERROR_PATTERNS)


def converged(review_msg: str) -> bool:
    m = review_msg.lower().strip()
    if not m:
        return False
    action_items = ["blocker:", "fix:", "should ", "must ", "needs to", "need to",
                    "have to", "fix ", "add this", " add a ", "remove ",
                    "missing ", "incorrect", "vulnerability", "- p0", "- p1",
                    "- p2", "- p3", "review comment", "suggestion:", "issue:"]
    if any(k in m for k in action_items):
        return False
    explicit_ok = ["lgtm", "looks good", "all good", "approved", "nothing to add",
                   "no further changes", "no changes needed"]
    if any(k in m for k in explicit_ok):
        return True
    implicit_ok = ["no evident", "no discrete", "did not find", "found no",
                   "not find any", "no problem", "no concern", "no evidence of",
                   "not evident"]
    if any(k in m for k in implicit_ok):
        return True
    if re.search(r"\bno\b.{0,30}\b(bug|issue|regression|error|vulnerability|problem|concern|risk)\b", m):
        return True
    return False


def reviewer_no_changes(msg: str) -> bool:
    m = msg.lower()
    return any(k in m for k in NO_CHANGES_PATTERNS)


def stalled(history: list) -> bool:
    if len(history) < STALL_WINDOW:
        return False
    tail = history[-STALL_WINDOW:]
    if len({hashlib.md5(r[:300].lower().encode()).hexdigest() for r in tail}) == 1:
        return True
    return all(reviewer_no_changes(r) for r in tail)


def oscillating(history: list, window: int = 4) -> bool:
    if len(history) < window:
        return False
    tail = history[-window:]
    no_chg = sum(1 for r in tail if reviewer_no_changes(r))
    return no_chg >= 2 and (window - no_chg) >= 2


def check_secrets(text: str) -> list:
    return [f"Secret-like pattern detected: `{pat}`"
            for pat in SECRET_PATTERNS if re.search(pat, text)]


# --- Session -------------------------------------------------------------
class Session:
    def __init__(self, task: str, repo: Path, new_branch: bool,
                 builder_prompt: str, reviewer_prompt: str):
        self.task = task
        self.wt = repo
        self.new_branch = new_branch
        self.builder_prompt = builder_prompt
        self.reviewer_prompt = reviewer_prompt
        self.bus = repo / ".duet"
        self.bus.mkdir(parents=True, exist_ok=True)
        self.session_file = self.bus / "session.json"
        self.log_file = self.bus / "session.log"
        self.guard_dir = self.bus / "bin"
        self.pid_file    = Path(f"/tmp/duet_{task}.pid")
        self.stop_file   = Path(f"/tmp/duet_{task}.stop")
        self.resume_file = Path(f"/tmp/duet_{task}.resume")
        self.builder  = find_bin(BUILDER_BIN)
        self.reviewer = find_bin(REVIEWER_BIN)
        self.notif = Notifier(task, self.log_file)
        self.state = {}

    def git(self, args):
        return subprocess.run([real_git()] + args, capture_output=True, text=True, cwd=self.wt)

    def install_git_guard(self) -> str:
        self.guard_dir.mkdir(parents=True, exist_ok=True)
        gp = self.guard_dir / "git"
        gp.write_text(GIT_GUARD_SCRIPT.replace("REAL_GIT", real_git()))
        gp.chmod(0o755)
        return f"{self.guard_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"

    def remove_git_guard(self):
        (self.guard_dir / "git").unlink(missing_ok=True)

    def save(self):
        self.state["updated_at"] = datetime.now().isoformat()
        self.session_file.write_text(json.dumps(self.state, indent=2, ensure_ascii=False))

    def changed_files(self):
        r = self.git(["diff", "--name-only", "HEAD"])
        files = [f.strip() for f in r.stdout.splitlines() if f.strip()]
        if not files:
            r2 = self.git(["status", "--porcelain"])
            files = [l[3:].strip() for l in r2.stdout.splitlines() if l.strip()]
        return files

    def run_builder(self, prompt: str, n: int, guarded_path: str) -> str:
        self.notif.log(f"E{n}: builder writes code")
        env = os.environ.copy()
        env["PATH"] = guarded_path
        cmd = [self.builder, "-p", "--permission-mode", "acceptEdits"]
        if BUILDER_MODEL:
            cmd += ["--model", BUILDER_MODEL]
        if self.builder_prompt:
            cmd += ["--append-system-prompt", self.builder_prompt]
        cmd.append(prompt)
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=TIMEOUT_BUILDER, cwd=self.wt, env=env)
        out = result.stdout.strip()
        if result.returncode != 0 and result.stderr.strip():
            out += f"\n[stderr: {result.stderr.strip()[:300]}]"
        (self.bus / f"{n:03d}_builder.txt").write_text(out)
        return out

    def run_reviewer(self, n: int) -> str:
        self.notif.log(f"E{n}: reviewer reviews the uncommitted diff")
        # Codex reads AGENTS.md as its instructions; inject the reviewer playbook
        # for the duration of the review, then restore the original file.
        agents_md = self.wt / "AGENTS.md"
        existed = agents_md.exists()
        original = agents_md.read_text() if existed else None
        if self.reviewer_prompt:
            agents_md.write_text(self.reviewer_prompt)
        try:
            cmd = [self.reviewer, "review", "--uncommitted"]
            if REVIEWER_MODEL:
                cmd = [self.reviewer, "review", "--model", REVIEWER_MODEL, "--uncommitted"]
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=TIMEOUT_REVIEW, cwd=self.wt)
            raw = (result.stdout or result.stderr or "").strip()
            (self.bus / f"{n:03d}_reviewer.txt").write_text(raw)
            if not raw:
                return "[reviewer: empty response]"
            if result.returncode != 0 and reviewer_is_error(raw):
                return f"[reviewer error: {raw[:200]}]"
            return _extract_verdict(raw)
        finally:
            if self.reviewer_prompt:
                if original is not None:
                    agents_md.write_text(original)
                else:
                    agents_md.unlink(missing_ok=True)

    def wait_for_resume(self) -> bool:
        self.resume_file.unlink(missing_ok=True)
        while True:
            if self.stop_file.exists():
                return False
            if self.resume_file.exists():
                self.resume_file.unlink(missing_ok=True)
                return True
            time.sleep(3)


# --- Preflight ------------------------------------------------------------
def git_preflight(s: Session) -> str:
    if not (s.wt / ".git").exists() and not s.git(["rev-parse", "--git-dir"]).returncode == 0:
        sys.exit(f"{s.wt} is not a git repository.")
    cur = s.git(["branch", "--show-current"]).stdout.strip()
    dirty = s.git(["status", "--porcelain"]).stdout.strip()
    if dirty:
        files = "\n".join(f"  {l}" for l in dirty.splitlines()[:10])
        msg = (f"Session refused ({s.task}): the working tree is dirty.\n{files}\n"
               f"Commit / stash / checkout first — duet needs a clean start so the "
               f"diff it produces is entirely its own.")
        s.notif.alert(msg, title=f"duet {s.task}: dirty tree")
        sys.exit(msg)
    if cur in PROTECTED and not s.new_branch:
        msg = (f"Refused: '{cur}' is a protected branch. Pass --branch to work on a "
               f"fresh session branch instead.")
        s.notif.alert(msg, title=f"duet {s.task}: protected branch")
        sys.exit(msg)
    if s.new_branch:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        br = f"session/{s.task}-{ts}"
        r = s.git(["checkout", "-b", br])
        if r.returncode != 0:
            sys.exit(f"Could not create branch: {r.stderr.strip()[:200]}")
        return br
    return cur


# --- Simple subcommands ---------------------------------------------------
def cmd_doctor(task: str):
    repo = resolve_repo(task)
    name = task_name(repo)
    print(f"=== doctor: {name} -> {repo} ===")
    builder = find_bin(BUILDER_BIN); reviewer = find_bin(REVIEWER_BIN)
    print(f"builder  ({BUILDER_BIN}) : {builder}")
    print(f"reviewer ({REVIEWER_BIN}) : {reviewer}")
    cur = subprocess.run([real_git(), "branch", "--show-current"],
                         capture_output=True, text=True, cwd=repo).stdout.strip()
    dirty = subprocess.run([real_git(), "status", "--porcelain"],
                           capture_output=True, text=True, cwd=repo).stdout.strip()
    print(f"branch : {cur or '(detached / none)'}")
    print(f"tree   : {'clean' if not dirty else 'DIRTY (' + str(len(dirty.splitlines())) + ' files) — clean it before a run'}")
    print(f"webhook: {'configured' if WEBHOOK_URL else 'not set (DUET_WEBHOOK_URL) — local notifications only'}")
    print(f"desktop: {'notify-send present' if shutil.which('notify-send') else 'no notify-send (silent)'}")


def cmd_signal(task: str, kind: str):
    repo = resolve_repo(task)
    name = task_name(repo)
    Path(f"/tmp/duet_{name}.{kind}").touch()
    print(f"Signal '{kind}' sent for {name}.")


def cmd_status(task: str):
    repo = resolve_repo(task)
    sf = repo / ".duet" / "session.json"
    if not sf.exists():
        print(f"No session for {task_name(repo)}.")
        return
    d = json.loads(sf.read_text())
    print(f"Task     : {d.get('task','-')}")
    print(f"Objective: {d.get('objective','-')[:70]}")
    print(f"Status   : {d.get('status','-')}")
    print(f"Exchange : {d.get('exchange_count',0)}")
    print(f"Converged: {d.get('converged')}")
    print(f"Files    : {', '.join(d.get('files_modified', [])[:5])}")


# --- Main loop ------------------------------------------------------------
def cmd_run(task_arg: str, objective: str, new_branch: bool,
            builder_prompt: str, reviewer_prompt: str):
    repo = resolve_repo(task_arg)
    task = task_name(repo)
    s = Session(task, repo, new_branch, builder_prompt, reviewer_prompt)
    nf = s.notif

    s.pid_file.write_text(str(os.getpid()))
    s.stop_file.unlink(missing_ok=True)
    s.resume_file.unlink(missing_ok=True)

    branch = git_preflight(s)
    guarded_path = s.install_git_guard()
    nf.log("git guard installed")

    s.state = {
        "task": task, "objective": objective, "branch": branch,
        "started_at": datetime.now().isoformat(), "status": "running",
        "exchange_count": 0, "exchanges": [], "warnings": [],
        "converged": False, "files_modified": [],
    }
    s.save()

    nf.alert(f"Session started — `{task}`\nObjective: `{objective[:200]}`\nBranch: `{branch}`",
             title=f"duet {task}: started", event="start")

    history, prompt, round_offset, no_changes_streak = [], \
        f"{objective}\n\nBe concise: max 300 words.", 0, 0

    while True:
        done = False
        for i in range(1, MAX_EXCHANGES + 1):
            n = round_offset + i
            s.state["exchange_count"] = n
            s.save()
            if s.stop_file.exists():
                done = True; break

            nf.progress(f"E{n} — builder…")
            try:
                builder_out = s.run_builder(prompt, n, guarded_path)
            except subprocess.TimeoutExpired:
                nf.alert(f"Builder timeout E{n} ({task}) — stopping", title=f"duet {task}: timeout", event="timeout"); done = True; break

            if "duet guard:" in builder_out.lower():
                nf.log(f"git guard triggered E{n}")

            if not s.changed_files() and any(p in builder_out.lower() for p in BUILDER_ALREADY_DONE_PATTERNS):
                nf.alert(f"Already implemented — {task} E{n}\n{builder_out[:500]}", title=f"duet {task}: already done", event="converge")
                s.state.update(converged=True, status="already_done"); s.save(); done = True; break

            if any(p in builder_out.lower() for p in BUILDER_LIMIT_PATTERNS):
                nf.alert(f"Builder rate limit — {task} E{n}\nPaused. `duet resume {task}` when the quota returns.",
                         title=f"duet {task}: rate limit", event="paused")
                s.state["status"] = "paused"; s.save()
                if not s.wait_for_resume():
                    done = True
                break

            for h in check_secrets(builder_out):
                s.state["warnings"].append(h)
                nf.alert(f"Secret alert (builder) {task}\n{h}", title=f"duet {task}: secret?", event="secret")

            nf.progress(f"E{n} — reviewer…")
            try:
                review_out = s.run_reviewer(n)
            except subprocess.TimeoutExpired:
                nf.alert(f"Reviewer timeout E{n} ({task}) — stopping", title=f"duet {task}: timeout", event="timeout"); done = True; break

            if review_out.startswith("[reviewer error:") or review_out == "[reviewer: empty response]":
                nf.alert(f"Reviewer error — {task} E{n}\n`{review_out}`\nPaused. `duet resume {task}` after fixing.",
                         title=f"duet {task}: reviewer error", event="paused")
                s.state["status"] = "paused"; s.save()
                if not s.wait_for_resume():
                    done = True
                break

            history.append(review_out)
            s.state["files_modified"] = s.changed_files()
            s.state["exchanges"].append({"n": n, "builder": builder_out[:200], "reviewer": review_out[:200]})
            s.save()

            if reviewer_no_changes(review_out):
                no_changes_streak += 1
                if no_changes_streak >= 2:
                    nf.alert(f"Stuck — {task} E{n}\nThe builder is not modifying any files ({no_changes_streak}x).",
                             title=f"duet {task}: no changes", event="stall")
                    s.state["status"] = "stalled"; s.save(); done = True; break
                prompt = (f"Objective: {objective}\n\nIMPORTANT: the reviewer sees no git changes. "
                          f"Edit real files in the working tree. Max 200 words.")
                continue
            no_changes_streak = 0

            if converged(review_out):
                nf.alert(f"Converged — {task} E{n} ✅\nReviewer:\n{review_out[:500]}\n\nUncommitted diff, ready for you to validate.",
                         title=f"duet {task}: LGTM ✅", event="converge")
                s.state.update(converged=True, status="converged"); s.save(); done = True; break

            if stalled(history):
                nf.alert(f"Stalled (repeating) — {task} E{n}\n{review_out[:300]}\n`duet resume/stop {task}`",
                         title=f"duet {task}: stalled", event="stall")
                s.state["status"] = "stalled"; s.save(); done = True; break

            if oscillating(history):
                nf.alert(f"Oscillating — {task} E{n}\nThe objective may be contradictory.\n{review_out[:300]}",
                         title=f"duet {task}: oscillation", event="stall")
                s.state["status"] = "stalled"; s.save(); done = True; break

            nf.progress(f"E{n} — reviewer: {review_out[:250]}")
            prompt = (f"Objective: {objective}\n\nReviewer feedback E{n}:\n{review_out}\n\n"
                      f"Apply the fixes. Max 300 words.")

        if done or s.stop_file.exists():
            break

        last = history[-1] if history else "N/A"
        nf.alert(f"Paused — {MAX_EXCHANGES} exchanges ({task})\nNot converged yet.\n{last[:300]}\n"
                 f"`duet resume {task}` / `duet stop {task}`",
                 title=f"duet {task}: paused after {MAX_EXCHANGES}", event="paused")
        s.state["status"] = "paused"; s.save()
        if not s.wait_for_resume():
            break
        round_offset += MAX_EXCHANGES
        s.state["status"] = "running"; s.save()

    if s.state.get("status") not in ("converged", "stalled", "already_done"):
        s.state["status"] = "ended"
    s.state["ended_at"] = datetime.now().isoformat()
    s.save()

    files = s.changed_files()
    diff = s.git(["diff", "HEAD"]).stdout
    (s.bus / "final.diff").write_text(diff)
    summary = (f"Session ended — {task}\nStatus: {s.state['status']}\n"
               f"Branch: `{branch}`\nFiles: {', '.join(files[:6]) or 'none'}\n"
               f"Diff: `.duet/final.diff` ({len(diff.splitlines())} lines)\n"
               f"Commits: 0 (diff left uncommitted for your review)")
    nf.alert(summary, title=f"duet {task}: ended ({s.state['status']})", event="end")
    nf.log("END")

    s.remove_git_guard()
    s.pid_file.unlink(missing_ok=True)
    s.stop_file.unlink(missing_ok=True)


def _read_prompt(path: str) -> str:
    if not path:
        return ""
    p = Path(path).expanduser()
    if not p.is_file():
        sys.exit(f"Prompt file not found: {p}")
    return p.read_text()


def main():
    global BUILDER_MODEL, REVIEWER_MODEL, MAX_EXCHANGES
    p = argparse.ArgumentParser(prog="duet", add_help=True,
                                description="Adversarial cross-vendor code loop (builder writes, reviewer critiques, loop until LGTM, never commits).")
    sub = p.add_subparsers(dest="cmd")
    for c in ("doctor", "stop", "resume", "status"):
        sp = sub.add_parser(c); sp.add_argument("repo")
    pr = sub.add_parser("run")
    pr.add_argument("repo")
    pr.add_argument("objective", nargs="+")
    pr.add_argument("--branch", action="store_true", help="work on a fresh session branch")
    pr.add_argument("--builder-model", default="")
    pr.add_argument("--reviewer-model", default="")
    pr.add_argument("--builder-prompt", default="")
    pr.add_argument("--reviewer-prompt", default="")
    pr.add_argument("--max-exchanges", type=int, default=0)

    argv = sys.argv[1:]
    known = {"doctor", "stop", "resume", "status", "run"}
    if argv and argv[0] not in known and not argv[0].startswith("-"):
        argv = ["run"] + argv   # short form: duet <repo> "objective"
    args = p.parse_args(argv)

    if args.cmd == "doctor":   return cmd_doctor(args.repo)
    if args.cmd == "status":   return cmd_status(args.repo)
    if args.cmd == "stop":     return cmd_signal(args.repo, "stop")
    if args.cmd == "resume":   return cmd_signal(args.repo, "resume")
    if args.cmd == "run":
        if args.builder_model:  BUILDER_MODEL = args.builder_model
        if args.reviewer_model: REVIEWER_MODEL = args.reviewer_model
        if args.max_exchanges:  MAX_EXCHANGES = args.max_exchanges
        return cmd_run(args.repo, " ".join(args.objective), args.branch,
                       _read_prompt(args.builder_prompt), _read_prompt(args.reviewer_prompt))
    p.print_help()


if __name__ == "__main__":
    main()

#!/usr/bin/env bash
#
# play.sh — an illustrative reconstruction of a duet session.
#
# This is NOT a live capture. It reproduces duet's real terminal output format
# on a neutral toy example (a fake web app), so the flow can be shown without
# recording against a private codebase. The messages, the loop, and the diff
# match what duet actually prints; only the target repo is invented.
#
# Record it with:   asciinema rec duet-demo.cast -c ./demo/play.sh
# Turn into a GIF:  agg duet-demo.cast duet-demo.gif
#
set -u

# --- colors ---
DIM=$'\033[2m'; B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; C=$'\033[36m'; R=$'\033[0m'
GREEN=$'\033[32m'; RED=$'\033[31m'

pause() { sleep "${1:-0.6}"; }

# type a shell command out, char by char, behind a green prompt
type_cmd() {
  printf '%s$ %s' "$G" "$B"
  local s="$1"; local i
  for (( i=0; i<${#s}; i++ )); do printf '%s' "${s:$i:1}"; sleep 0.018; done
  printf '%s\n' "$R"; pause 0.4
}

# a timestamped duet log line (dim timestamp + tag, like the real Notifier)
logline() {
  local ts="$1"; shift
  printf '%s[%s] [webapp]%s %s\n' "$DIM" "$ts" "$R" "$*"
  pause "${LOG_PAUSE:-0.9}"
}

pause 0.3

type_cmd 'duet ~/code/webapp "Add rate limiting to the /login endpoint: 5 attempts/min per IP" --branch'

logline "2026-07-04 14:22:01" "git guard installed"
logline "2026-07-04 14:22:01" "Session started: ${C}webapp${R} Objective: ${DIM}Add rate limiting to the /login endpoint: 5 attempts/min per IP${R} Branch: ${C}session/webapp-20260704-142201${R}"

LOG_PAUSE=1.4 logline "2026-07-04 14:22:41" "${Y}E1${R}: builder writes code"
LOG_PAUSE=1.6 logline "2026-07-04 14:23:58" "${Y}E1${R}: reviewer reviews the uncommitted diff"
printf '%s        ↳ reviewer returned a P0 blocker; builder will address it%s\n' "$DIM" "$R"; pause 1.0
LOG_PAUSE=1.4 logline "2026-07-04 14:24:30" "${Y}E2${R}: builder writes code"
LOG_PAUSE=1.6 logline "2026-07-04 14:25:22" "${Y}E2${R}: reviewer reviews the uncommitted diff"

printf '%s[%s] [webapp]%s %sConverged: webapp E2 ✅%s Reviewer: %sLGTM. The sliding window bounds attempts to the last 60s; lockout now recovers.%s\n' \
  "$DIM" "2026-07-04 14:25:41" "$R" "$GREEN$B" "$R" "$DIM" "$R"; pause 1.2
logline "2026-07-04 14:25:41" "Session ended: webapp Status: ${GREEN}converged${R} Branch: ${C}session/webapp-20260704-142201${R} Files: app/auth.py Diff: ${C}.duet/final.diff${R} (34 lines) ${B}Commits: 0${R} (diff left uncommitted for your review)"

pause 1.0
printf '\n%sWhat did the reviewer actually catch? It is stored, not committed:%s\n\n' "$DIM" "$R"; pause 0.8

type_cmd 'cat .duet/001_reviewer.txt'
cat <<EOF
${B}P0 blocker: rate limiting is not per-window.${R}

\`login_attempts[ip]\` is an integer that only ever increments; it is never
reset or aged out. This counts failures across the whole process lifetime,
not per minute. After 5 lifetime failures an IP is locked out forever, and a
restart is the only way to recover. The objective ("5 attempts/min") is not met.

Fix: store a list of timestamps per IP and, before counting, drop the ones
older than 60s (a sliding window). Count what remains.
EOF
pause 2.2

printf '\n%sThe fix the builder applied, ready for a human to review and commit:%s\n\n' "$DIM" "$R"; pause 0.8
type_cmd 'cat .duet/final.diff'
cat <<EOF
${C}--- a/app/auth.py${R}
${C}+++ b/app/auth.py${R}
${DIM}@@ -1,14 +1,20 @@${R}
${GREEN}+import time${R}
${GREEN}+${R}
${GREEN}+WINDOW = 60${R}
${GREEN}+MAX_ATTEMPTS = 5${R}
 login_attempts = {}  ${DIM}# ip -> list[timestamp]${R}

 @app.post("/login")
 def login():
     ip = request.remote_addr
${GREEN}+    now = time.time()${R}
${GREEN}+    recent = [t for t in login_attempts.get(ip, []) if now - t < WINDOW]${R}
${RED}-    if login_attempts.get(ip, 0) >= 5:${R}
${GREEN}+    if len(recent) >= MAX_ATTEMPTS:${R}
         abort(429, "Too many attempts")
     user = authenticate(request.form["email"], request.form["password"])
     if user is None:
${RED}-        login_attempts[ip] = login_attempts.get(ip, 0) + 1${R}
${GREEN}+        recent.append(now)${R}
         abort(401)
${GREEN}+    login_attempts[ip] = recent${R}
     return start_session(user)
EOF
pause 1.6
printf '\n%s%sTwo models, two vendors. One wrote it, the other refused to let a real bug ship. You commit.%s\n\n' "$B" "$G" "$R"
pause 1.2

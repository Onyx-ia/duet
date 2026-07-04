# Demo

`play.sh` is an **illustrative reconstruction** of a duet session, not a live
capture. It reproduces duet's real terminal output format on a neutral toy
example (a fake `webapp`), so the flow can be shown without recording against a
private codebase. The messages, the loop, the convergence, and the diff match
what duet actually prints; only the target repository is invented.

## The scenario

Objective: *"Add rate limiting to the /login endpoint: 5 attempts/min per IP."*

1. **E1** — the builder writes plausible code, but with a real bug: the attempt
   counter is a plain integer that only ever increments, never aged out. It
   counts failures for the whole process lifetime, so after 5 lifetime failures
   an IP is locked out forever. The objective ("per minute") is not met.
2. **E1 reviewer** (a different vendor's model) catches it and returns a **P0
   blocker** describing the fix: use a sliding 60s window.
3. **E2** — the builder rewrites it with a timestamp window.
4. **E2 reviewer** — **LGTM**. Converged.
5. duet stops and leaves the diff uncommitted for a human to review. **0
   commits.**

The point of the example: the bug is exactly the kind a model's *own* review
waves through (the code looks right), and exactly the kind a reviewer from a
*different* lab flags.

## Watch it

```bash
./demo/play.sh
```

## Record it (for the README GIF)

```bash
# record to an asciinema cast
asciinema rec duet-demo.cast -c ./demo/play.sh

# optional: turn the cast into an animated GIF for GitHub
#   agg  -> https://github.com/asciinema/agg
agg duet-demo.cast duet-demo.gif
```

Then embed the GIF at the top of the main README, or link the cast.

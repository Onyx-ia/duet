---
title: "Why I let a rival model review my AI's code"
description: "A model reviewing its own output shares its own blind spots. So I put a reviewer from a different vendor in the loop, and never let either of them commit."
tags: [ai, llm, code-review, agents, claude, codex]
canonical_url: ""
---

# Why I let a rival model review my AI's code

I build an AI product. Most of the code that ships in it is now written by a
coding agent, and for a while my review process was embarrassingly simple: I'd
ask the *same* agent, "are you sure this is right?" It would think for a moment
and reassure me. It was almost always confident. It was often wrong.

That's the thing nobody tells you about self-review: it's theater. The model
that just wrote a subtly broken diff is the least qualified thing in the world
to catch that the diff is broken, because the exact reasoning that produced the
bug is the reasoning it uses to review it. Ask it to check its work and it
re-derives the same wrong answer, then congratulates itself. Shared author,
shared blind spots.

Human teams figured this out decades ago. You don't merge your own PR. The
reviewer isn't valuable because they're smarter. They're valuable because
they're *someone else*, with a different mental model, who didn't fall in love
with the approach while writing it. The disagreement is the point.

So I asked an obvious question: what if the reviewer wasn't just a different
*chat*, but a model from a different *vendor entirely*?

## Different lab, different blind spots

Models from the same family fail in correlated ways. They were trained on
overlapping data, tuned by overlapping teams, and they inherit the same habits:
the same favorite libraries, the same off-by-one tendencies, the same too-clever
refactors, the same confident hand-waving over the tricky edge case. A model
reviewing a sibling's output nods along, because the sibling made exactly the
kinds of choices it would have made.

Cross the vendor line and the correlation breaks. Point one lab's model at
another lab's diff and it stops being polite. It flags the assumption the author
never questioned. It notices the error handling that isn't there. It argues.

That adversarial friction, one model that wants to ship and another that wants
to poke holes, is the whole mechanism. It's not that either model is better.
It's that they're *different*, and the difference does the work.

## How I wired it up

I built a tiny tool called **duet**. It's one Python file. The loop is dumb on
purpose:

1. **The builder** (one vendor's coding agent) writes and edits real files to
   satisfy an objective.
2. **The reviewer** (a *different* vendor's model) reviews the uncommitted diff
   and comes back with concrete, prioritized feedback, or says nothing's wrong.
3. The builder applies the fixes. Back to step 2.
4. This repeats until the reviewer genuinely has no blocking issues left, until
   it stops finding things.

Then it stops and hands me a diff.

Two design choices turned out to matter more than I expected.

**Knowing when to stop is the hard part.** It's easy to make two models talk
forever. The value is in detecting the three ways a loop actually ends:
convergence (the reviewer runs out of real objections), stall (they're repeating
themselves), and oscillation (the builder ping-pongs between two states because
the objective is contradictory). Getting that classification right is most of
what makes the thing usable instead of a token bonfire. A reviewer that invents
busywork to look diligent, like "consider renaming this variable" on a correct
diff, is a bug, not a feature, and the loop has to be able to tell "this is a
real blocker" from "this is polish."

**Neither model is allowed to touch git.** This was non-negotiable for me. For
the duration of a session, a shim on the builder's `PATH` blocks
`commit`, `push`, `merge`, `rebase`, and `reset --hard`. When the loop converges,
I don't get a commit. I get a diff. The agents can argue all they want; the one
who merges is me. I read `final.diff`, and I decide. No agent writes my git
history. Ever.

That last rule is the one that lets me actually *use* this on code I care about.
The loop can be aggressive and autonomous precisely because its output is
inert until a human blesses it.

## What I'd tell you before you try it

I want to be honest, because this space is full of overclaiming.

- **It's not magic.** A cross-vendor reviewer catches a *class* of bugs the
  author misses. It does not catch everything, and it will occasionally wave
  through something ugly. You still read the diff.
- **It costs two models.** Every exchange is an author turn *and* a reviewer
  turn. For a throwaway script that's silly. For code that ships to users, the
  ratio is fine.
- **The space is crowded.** There are excellent tools for running many agents in
  parallel, each in its own worktree, and letting you review the pile. duet is a
  different shape on purpose: one task, a two-model *adversarial loop* that
  decides for itself when the work is done, and that refuses to write git
  history. Use it *inside* one of those worktrees if you like.

The reason I ship this instead of keeping it private is simple: the idea is more
valuable than the code. You don't need my tool. You need to stop asking a model
to grade its own homework. Wire up *any* second model from a different vendor as
a reviewer, make the disagreement structural, and keep the commit button in
human hands.

If you want the tool anyway, it's one file, zero dependencies, MIT:
**https://github.com/Onyx-ia/duet**

The best review I get on my AI's code now comes from a model that has every
incentive to prove the first one wrong. Turns out that's exactly what a reviewer
is for.

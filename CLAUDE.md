# gc-harness

GobbleCube Core Intelligence — a plan-then-execute intelligence harness.
See `docs/intelligence-harness/README.md` for the architecture.

## AI-DLC (opt-in)

The AWS AI-DLC staged delivery workflow is available via `/aidlc`. It is
**opt-in by invocation** — nothing runs unless you type it.

Use it for non-trivial changes to the harness (a new lane or stage, a capability
pack, the approval flow, the plan cache, anything that moves eval outcomes). Do
**not** use it for one-line fixes, read-only questions, scratch experiments, a
single eval case, or dependency bumps.

Full gate, default scope, and precedence: @aidlc/ACTIVATION.md
Setup (the engine is regenerated locally, not vendored): `aidlc/ONBOARDING.md`

Where AI-DLC's generic guidance conflicts with this repo's standards or the eval
contracts in `evals/`, **the repo standards win**.

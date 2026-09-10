# When AI-DLC applies in gc-harness

AI-DLC is **opt-in by invocation** — it only runs when you type `/aidlc`. This
file records when reaching for it is the right call, and when it is overkill.

## Use AI-DLC when

The work is a **non-trivial change to the plan-then-execute harness** and would
benefit from staged design and an approval trail:

- a new lane or stage in `core/plan`, `core/execute`, `core/classify`, or
  `core/gateway`
- a new capability pack under `packs/`, or reshaping how packs are resolved
- changes to the approval flow, the plan cache, or the Plan IR contract
- anything that moves eval outcomes — the `evals/` golden asks and recordings
  are the regression surface
- reverse-engineering an area of the harness that is no longer understood

## Do NOT use AI-DLC for

- one-line fixes, typos, comment and docstring edits
- read-only questions ("what does this do?", "where is X defined?")
- scratch experiments and throwaway probes
- adding a single eval case or fixture
- dependency bumps and `uv.lock` refreshes
- anything a single obvious edit resolves

## Default scope

`AWS_AIDLC_DEFAULT_SCOPE` is **`express`** (minimal: reverse-engineering →
requirements → code generation → build & test → deploy). This is only the
fallback — scope is normally auto-detected from what you describe, and an
explicit `/aidlc --scope <name>` always wins.

| Scope | Use for |
| --- | --- |
| `express` (default) | Most harness work |
| `bugfix` / `refactor` | Targeted fixes and restructuring |
| `feature` | A genuinely new capability needing design up front |
| `infra` | Dockerfile, deploy, and orchestration changes |

## Repo standards win

Where AI-DLC's generic guidance conflicts with this repo's own standards —
`.claude/CLAUDE.md`, `docs/intelligence-harness/`, the eval contracts in
`evals/` — **the repo standards win**. AI-DLC structures the work; it does not
redefine how this harness is built. In particular it does not get to relax the
eval suite: a change that moves golden asks ships with that movement explained.

## Artifacts

Records live under `aidlc/spaces/default/intents/<record>/` and are committed,
so the reasoning behind a change stays with the repo.

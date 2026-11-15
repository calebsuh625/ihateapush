# Phase 4 progress (overnight run)

Branch `phase-4`, cut from `main` at `15aceb7`. Nothing is pushed. One commit per finished item.

## Status

| # | Item | Status |
|---|---|---|
| 1 | Build-your-own rotation | not started |
| 2 | Transition path | not started |
| 3 | Whole-farm planner | not started |
| 4 | Twin view | not started |
| 5 | Map layers + time-lapse | not started |
| 6 | Water reality check | not started |
| 7 | Farm advisor (chat) | not started |
| 8 | Performance + reliability | not started |

## Decisions made without you

- **Your prompt was cut off** in item 8 after "Fast plan". I read item 8 as: smooth zoom with all fields,
  fast plan responses, and clear loading / error states everywhere. If there were items after 8, they
  were not received and are not done.
- **The advisor runs on Gemini, not Claude**, because you gave a Gemini key. The key is in the
  git-ignored file `.env` (`GEMINI_API_KEY=...`) and is never committed, logged or shown. The panel is
  called "Farm advisor". Without a key it shows an "add API key" message and everything else works.
- **`make e2e` runs every browser suite**, not only the first one, so each item is checked against all
  existing features.
- No Earth Engine jobs, no pushes, no deploys, no accounts.

## Done

(nothing yet)

## What is left

Everything in the table above.

## For you in the morning

(filled in as items finish)

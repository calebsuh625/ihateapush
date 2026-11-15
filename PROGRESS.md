# Phase 4 progress (overnight run)

Branch `phase-4`, cut from `main` at `15aceb7`. Nothing is pushed. One commit per finished item.

## Status

| # | Item | Status |
|---|---|---|
| 1 | Build-your-own rotation | done (checked, committed) |
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

**1. Build-your-own rotation.** In the field panel under the plan cards, "Try your own rotation": pick a
crop per year (2 to 6 years), drag or use the earlier / later buttons, type "corn, milo, wheat, fallow",
"Start from what this field grows now", or "Edit this plan" on a card. It is scored by the Phase 3 engine
with the same settings as the cards and shown as Yours | Our 1 | 2 | 3, then plain sentences from the API.
- Try: http://localhost:5173/#KS-014376 (irrigated) or #KS-013847 (dryland); API
  `GET /field/{id}/score?rotation=corn,milo,wheat,fallow` (+ the /plans parameters). Notes: schema.md, README "Phase 4".
- Decisions: the rotation is read as a repeating cycle; "too close to call" means the six climate models
  do not all put the same side ahead (a winner is named only when all six agree); a rotation that breaks
  our agronomy rules is still scored, with the rule shown; all text is parsed and worded by the API.
- Checker fixes: year slots widened (crop names were breaking mid-word); a rotation that is fallow every
  year shows "no crop" instead of "100% of water need met / 0% fail", and those cells are not marked.
- Limits: six crops only; 5- and 6-year cycles are never "one of ours"; drag is mouse only (buttons for
  touch and keyboard); "Start from what this field grows now" is hidden when the last four years include
  a crop we have no numbers for (for example KS-013847: grass/pasture in 2024).

## What is left

Items 2 to 8 in the table above.

## For you in the morning

- Item 1, look at: rule-breaking rotations can outrank our picks because the engine has no yield penalty
  for them. On dryland, fallow every year would rank first with default priorities (full water and risk
  marks, nothing to fail); irrigated continuous corn would rank number 2 of 106. The page says so and
  shows the rule broken, but you may want the engine to penalise these.
- Item 1: "our pick number 2" can rank number 3 of the candidates (the three cards are not always
  ranks 1 to 3, as in Phase 3); the sentence states both numbers.

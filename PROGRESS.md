# Phase 4 progress (overnight run)

Branch `phase-4`, cut from `main` at `15aceb7`. Nothing is pushed. One commit per finished item.

## Status

| # | Item | Status |
|---|---|---|
| 1 | Build-your-own rotation | done (checked, committed) |
| 2 | Transition path | done (checked, committed) |
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

**2. Transition path.** "How to get there" on each plan card and under the build-your-own result opens a
year-by-year path from the field's real last years to that rotation: the last three real years greyed,
bridge years marked, each year's return an acre with its low-high across the six climate models; two
sentences (short-term cost, long-term gain); a running-total chart and table against carrying on as now
with the payback year; folds for water, bare days and failure chance, and for what is left out.
- Try: http://localhost:5173/#KS-004139 (dryland, card 2 pays back in year 4), #KS-012807 (irrigated, one
  bridge year of alfalfa), #KS-000005 (card 1: already there), #KS-013847 (no path: last year is grass);
  API `GET /field/{id}/transition?plan=2` or `?target=corn,milo,wheat,fallow` (`&years=4..12` + the /plans parameters).
- Decisions: the per-cycle agronomy rules are read one year at a time (dryland: no corn within two years
  of corn; irrigated: no fallow within three years of fallow; our reading, stated in README); among valid
  ways in, fewest bridge years, then the highest return over the span; a rule the target rotation itself
  breaks every round is listed, not held against the path; "paid back" = the running total (average of
  the models) stays at or above zero to the end of the span; a long-run winner is named only when all
  six models agree; "carry on as now" repeats the last four years as they are when they do not repeat.
- Also here (carry-over from item 1): a rotation that is fallow every year is no longer ranked or
  compared; it gets one sentence from its own figures.
- Checker fixes: "1 fewer day" grammar; the rounding note said "a dollar or two" where 12-year totals
  drift up to $7 ("a few dollars" now); "Grass/Pasture" no longer breaks mid-word in the greyed years.
- Limits: every year gets the climate window's average weather (calendar years are labels, not
  forecasts); no weaker first alfalfa year, equipment, contracts or price changes (listed in the page);
  about 1 field in 8 ends in a crop we have no numbers for and gets one sentence, no path; with fewer
  than four years in a row of the six crops there is a path but no comparison; year-by-year bare days
  carry no Phase 2B shift (the long-run figure does).

## What is left

Items 3 to 8 in the table above.

## For you in the morning

- Item 1, look at: rule-breaking rotations can outrank our picks because the engine has no yield penalty
  for them. On dryland, fallow every year would rank first with default priorities (full water and risk
  marks, nothing to fail); irrigated continuous corn would rank number 2 of 106. The page says so and
  shows the rule broken, but you may want the engine to penalise these.
- Item 1: "our pick number 2" can rank number 3 of the candidates (the three cards are not always
  ranks 1 to 3, as in Phase 3); the sentence states both numbers.
- Item 1 follow-up, done in item 2: fallow every year is now "not ranked" instead of "would come first".
  Irrigated continuous corn still ranks high; that part is still yours to decide.
- Item 2, look at: on irrigated corn fields (for example KS-014376) every path to our top plans shows as
  behind carrying on with continuous corn in dollars in all six models, because the engine has no yield
  penalty for corn after corn. The page says so honestly (and shows fewer bare days), but it reads as
  "our plan does not pay". Same root cause as the item 1 note.
- Item 2: dryland paths quote dollar differences (the brief asked for them) with a note that the sign
  depends on price years and the fallow cost; the note sits in the "What this leaves out" fold. Say if
  you want it more prominent.

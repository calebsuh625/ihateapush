import { useEffect, useId, useRef, useState } from "react";
import type { DragEvent, FormEvent } from "react";
import { api, isAbort } from "../api";
import { cropColor, cropLabel, textColorOn } from "../crops";
import { useLatest } from "../hooks/useLatest";
import {
  OWN_CROPS,
  addSlot,
  canAdd,
  canRemove,
  columnLabel,
  emptySlots,
  moveSlot,
  rangeText,
  removeSlot,
  sameSlots,
  scoreQuery,
  setSlot,
  slotsFrom,
  verdictLabel,
} from "../lib/custom";
import type { Slot } from "../lib/custom";
import { formatScore } from "../lib/format";
import { RANKING_SCORE_LABEL, RANKING_SCORE_TITLE, formatDollars, formatInches, formatShare, wellQualifier } from "../lib/planner";
import type { FieldScore, Plan } from "../types";
import { HowToGetThere } from "./TransitionPath";
import { WeatherReplay } from "./WeatherReplay";
import "./OwnRotation.css";

interface OwnRotationProps {
  fieldId: string;
  /** The settings the plan cards are asked with (lib/planner.ts plansQuery), so both follow the same controls. */
  plansQuery: string;
  /** One entry per year; owned by the panel so "Edit this plan" on a card can fill them. */
  slots: Slot[];
  onSlots: (next: Slot[]) => void;
  /** What this field grows now, as a cycle of the six crops; null when it cannot be scored. */
  currentCycle: readonly Slot[] | null;
  /** Changes when a plan card sent its rotation here: bring the section into view. */
  jump: number;
}

type TextState = { status: "idle" } | { status: "busy" } | { status: "error"; message: string };

/**
 * "Try your own rotation": year slots the farmer fills by picking, dragging or typing, scored by
 * GET /field/{id}/score next to our top picks, with the API's own sentences on why ours is ahead
 * or is not. Hidden when the server can only run the basic planner (the endpoint answers 503).
 */
export function OwnRotation({ fieldId, plansQuery, slots, onSlots, currentCycle, jump }: OwnRotationProps) {
  const id = useId();
  const rootRef = useRef<HTMLElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const query = scoreQuery(plansQuery, slots);
  const latest = useLatest<FieldScore>(fieldId, query, api.score);

  // ---- typing a rotation: the API reads the text, so there is one list of crop names -------------
  const [text, setText] = useState("");
  const [textState, setTextState] = useState<TextState>({ status: "idle" });
  const textRequest = useRef<AbortController | null>(null);
  useEffect(() => () => textRequest.current?.abort(), []);
  const submitText = (event: FormEvent) => {
    event.preventDefault();
    textRequest.current?.abort();
    const controller = new AbortController();
    textRequest.current = controller;
    setTextState({ status: "busy" });
    api.score(fieldId, controller.signal, `rotation=${encodeURIComponent(text)}${plansQuery ? `&${plansQuery}` : ""}`).then(
      (body) => {
        if (controller.signal.aborted) return;
        const understood = slotsFrom(body.understood);
        if (understood) onSlots(understood);
        setTextState({ status: "idle" });
      },
      (err: unknown) => {
        if (controller.signal.aborted || isAbort(err)) return;
        setTextState({ status: "error", message: err instanceof Error ? err.message : String(err) });
      },
    );
  };

  // ---- moving years: drag the handle, or use the earlier / later buttons --------------------------
  const [drag, setDrag] = useState<{ from: number; over: number | null } | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const focusAfter = useRef<string | null>(null);
  const move = (from: number, to: number, focus?: "earlier" | "later") => {
    if (to < 0 || to >= slots.length || to === from) return;
    const crop = slots[from];
    onSlots(moveSlot(slots, from, to));
    setAnnouncement(`${crop ? cropLabel(crop) : "Empty year"} moved to year ${to + 1} of ${slots.length}.`);
    if (focus) {
      // keep the keyboard on the year that moved; at either end the button used is disabled, so take the other
      const edge = focus === "earlier" ? to === 0 : to === slots.length - 1;
      focusAfter.current = `${edge ? (focus === "earlier" ? "later" : "earlier") : focus}-${to}`;
    }
  };
  useEffect(() => {
    if (focusAfter.current === null) return;
    rootRef.current?.querySelector<HTMLButtonElement>(`[data-own-tool="${focusAfter.current}"]`)?.focus();
    focusAfter.current = null;
  }, [slots]);

  const firstJump = useRef(jump);
  useEffect(() => {
    if (jump === firstJump.current) return;
    firstJump.current = jump;
    rootRef.current?.scrollIntoView({ block: "start" });
    headingRef.current?.focus({ preventScroll: true });
  }, [jump]);

  if (latest.errorStatus === 503) return null; // the server has no future-weather planner: no such feature

  const onDragStart = (index: number) => (event: DragEvent<HTMLElement>) => {
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", String(index));
    const slot = event.currentTarget.closest("li");
    if (slot) event.dataTransfer.setDragImage(slot, 12, 12);
    setDrag({ from: index, over: null });
  };
  const onDragOver = (index: number) => (event: DragEvent<HTMLElement>) => {
    if (!drag) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    if (drag.over !== index) setDrag({ from: drag.from, over: index });
  };
  const onDrop = (index: number) => (event: DragEvent<HTMLElement>) => {
    if (!drag) return;
    event.preventDefault();
    move(drag.from, index);
    setDrag(null);
  };

  const untouched = sameSlots(slots, emptySlots());
  const result = latest.resource;
  const state = query === null ? "empty" : result.status;

  return (
    <section className="section own" aria-labelledby="section-own" data-testid="own-rotation" ref={rootRef}>
      <div className="section-head">
        <h3 id="section-own" tabIndex={-1} ref={headingRef}>
          Try your own rotation
        </h3>
        <p className="section-meta">Scored like our plans</p>
      </div>

      <p className="own-intro">
        Pick a crop for each year, or type a rotation, and see it scored next to our plans with the same settings.
      </p>

      <ol className="own-slots" data-testid="own-slots" aria-label="Your rotation, one crop a year">
        {slots.map((slot, index) => {
          const color = slot ? cropColor(slot) : null;
          const year = index + 1;
          const className = ["own-slot", drag?.from === index ? "is-dragged" : "", drag && drag.over === index && drag.from !== index ? "is-target" : ""].filter(Boolean).join(" ");
          return (
            <li key={index} className={className} data-testid="own-slot" data-crop={slot ?? ""} onDragOver={onDragOver(index)} onDrop={onDrop(index)}>
              <div
                className="own-slot-head"
                draggable
                onDragStart={onDragStart(index)}
                onDragEnd={() => setDrag(null)}
                title="Drag to move this year"
                data-testid={`own-drag-${index}`}
              >
                <span className="own-grip" aria-hidden="true" />
                <span className="rotation-year">Year {year}</span>
              </div>
              {/* A real <select> (keyboard, touch, screen readers) laid invisibly over a chip that can
                  wrap "Winter Wheat" onto two lines, as the plan cards do. */}
              <span className="own-pick">
                <select
                  className="own-select"
                  value={slot ?? ""}
                  aria-label={`Crop for year ${year}`}
                  data-testid={`own-crop-${index}`}
                  onChange={(event) => onSlots(setSlot(slots, index, event.target.value))}
                >
                  {slot === null && (
                    <option value="" disabled>
                      Pick a crop
                    </option>
                  )}
                  {OWN_CROPS.map((crop) => (
                    <option key={crop} value={crop}>
                      {cropLabel(crop)}
                    </option>
                  ))}
                </select>
                <span
                  className={slot ? "chip own-chip" : "chip own-chip own-chip--empty"}
                  style={color ? { backgroundColor: color, color: textColorOn(color) } : undefined}
                  aria-hidden="true"
                  data-testid={`own-chip-${index}`}
                >
                  <span className="own-chip-text">{slot ? cropLabel(slot) : "Pick a crop"}</span>
                  <svg className="own-caret" viewBox="0 0 10 6" width="9" height="6">
                    <path d="M1 1l4 4 4-4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" fill="none" />
                  </svg>
                </span>
              </span>
              <div className="own-tools">
                <button
                  type="button"
                  className="own-tool"
                  aria-label={`Move year ${year} earlier`}
                  title="Move earlier"
                  disabled={index === 0}
                  data-own-tool={`earlier-${index}`}
                  data-testid={`own-earlier-${index}`}
                  onClick={() => move(index, index - 1, "earlier")}
                >
                  <Arrow direction="left" />
                </button>
                <button
                  type="button"
                  className="own-tool"
                  aria-label={`Move year ${year} later`}
                  title="Move later"
                  disabled={index === slots.length - 1}
                  data-own-tool={`later-${index}`}
                  data-testid={`own-later-${index}`}
                  onClick={() => move(index, index + 1, "later")}
                >
                  <Arrow direction="right" />
                </button>
                <button
                  type="button"
                  className="own-tool"
                  aria-label={`Remove year ${year}`}
                  title="Remove this year"
                  disabled={!canRemove(slots)}
                  data-testid={`own-remove-${index}`}
                  onClick={() => {
                    onSlots(removeSlot(slots, index));
                    setAnnouncement(`Year ${year} removed. ${slots.length - 1} years left.`);
                  }}
                >
                  <svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true">
                    <path d="M2.5 2.5l7 7m0-7l-7 7" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" fill="none" />
                  </svg>
                </button>
              </div>
            </li>
          );
        })}
      </ol>
      <p className="sr-only" role="status" data-testid="own-announcement">
        {announcement}
      </p>

      <div className="own-actions">
        <button
          type="button"
          className="link-button"
          disabled={!canAdd(slots)}
          data-testid="own-add"
          onClick={() => {
            onSlots(addSlot(slots));
            setAnnouncement(`Year ${slots.length + 1} added.`);
          }}
        >
          Add a year
        </button>
        {currentCycle && (
          <button type="button" className="link-button" data-testid="own-start-current" onClick={() => onSlots([...currentCycle])}>
            Start from what this field grows now
          </button>
        )}
        <button type="button" className="link-button" disabled={untouched} data-testid="own-clear" onClick={() => onSlots(emptySlots())}>
          Clear
        </button>
      </div>

      <form className="own-text" onSubmit={submitText} noValidate>
        <label htmlFor={`${id}-text`}>Or type it</label>
        <div className="own-text-row">
          <input
            id={`${id}-text`}
            type="text"
            value={text}
            placeholder="corn, sorghum, wheat, fallow"
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            aria-describedby={textState.status === "error" ? `${id}-text-error` : undefined}
            aria-invalid={textState.status === "error"}
            data-testid="own-text"
            onChange={(event) => {
              setText(event.target.value);
              if (textState.status === "error") setTextState({ status: "idle" });
            }}
          />
          <button type="submit" className="button own-text-submit" disabled={textState.status === "busy"} data-testid="own-text-submit">
            Score it
          </button>
        </div>
        {textState.status === "error" && (
          <p className="own-text-error" id={`${id}-text-error`} role="alert" data-testid="own-text-error">
            {textState.message}
          </p>
        )}
      </form>

      <div
        className={latest.updating ? "own-result is-updating" : "own-result"}
        aria-live="polite"
        aria-busy={latest.updating || state === "loading"}
        data-testid="own-result"
        data-state={state}
        data-updating={latest.updating ? "true" : "false"}
        data-query={query !== null ? (latest.shownQuery ?? undefined) : undefined}
      >
        {state === "empty" && (
          <p className="own-hint muted" data-testid="own-hint">
            Pick a crop for every year to see how it scores next to our plans.
          </p>
        )}
        {state === "loading" && (
          <>
            <div className="skeleton-block" aria-hidden="true">
              <div className="skeleton" style={{ height: 300 }} />
            </div>
            <p className="sr-only" role="status">
              Scoring your rotation…
            </p>
          </>
        )}
        {state === "error" && result.status === "error" && (
          <p className="plans-update-error" role="alert" data-testid="own-error">
            <span>Could not score this rotation. {result.message}</span>
            <button type="button" className="link-button" onClick={latest.retry}>
              Try again
            </button>
          </p>
        )}
        {state === "ready" && result.status === "ready" && (
          <>
            {latest.updateError !== null && (
              <p className="plans-update-error" role="alert" data-testid="own-update-error">
                <span>Could not update the scores, so these are the last ones we worked out. {latest.updateError}</span>
                <button type="button" className="link-button" onClick={latest.retry}>
                  Try again
                </button>
              </p>
            )}
            <ScoreResult score={result.data} fieldId={fieldId} plansQuery={plansQuery} />
          </>
        )}
      </div>
    </section>
  );
}

function Arrow({ direction }: { direction: "left" | "right" }) {
  return (
    <svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true">
      <path d={direction === "left" ? "M7.5 2L3.5 6l4 4" : "M4.5 2l4 4-4 4"} stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" fill="none" />
    </svg>
  );
}

// ---------------------------------------------------------------------------------------------
// The answer: side-by-side table, then why
// ---------------------------------------------------------------------------------------------

interface Cell {
  text: string;
  /** a second, smaller line: the low-high range across the climate models */
  sub?: string | null;
}

interface Row {
  /** also the key of `marks` in the API answer */
  key: string;
  label: string;
  title?: string;
  cells: Cell[];
}

const NO_FIGURE = "no figure";
/** A rotation that is fallow every year has no crop to water or to lose, so those cells say so. */
const NO_CROP = "no crop";
/** ...and it is not ranked: with no crop it is not a cropping plan (the API sends `yours.rankable: false`). */
const NOT_RANKED = "not ranked";
const plantsNothing = (plan: Plan) => (plan.cycle ?? plan.rotation).every((crop) => crop === "fallow");

/** The table rows for yours and our picks, each figure formatted the way the plan cards format it. */
function tableRows(score: FieldScore): { scores: Row[]; figures: Row[] } {
  const columns: Plan[] = [score.yours, ...score.plans.slice(0, 3)];
  const irrigated = score.settings.irrigated;
  // Yours plants nothing: no ranking score, and no water or risk score (they would be full marks only
  // because there is no crop to run short of water or to fail).
  const unranked = score.yours.rankable === false;
  const each = (cell: (plan: Plan, column: number) => Cell | string): Cell[] =>
    columns.map((plan, column) => {
      const made = cell(plan, column);
      return typeof made === "string" ? { text: made } : made;
    });
  const scoreRow = (key: "soil" | "water" | "money" | "risk", label: string): Row => ({
    key,
    label,
    cells: each((plan, column) =>
      unranked && column === 0 && (key === "water" || key === "risk")
        ? NO_CROP
        : typeof plan.scores[key] === "number"
          ? formatScore(plan.scores[key] as number)
          : NO_FIGURE,
    ),
  });
  const scores: Row[] = [
    {
      key: "robust",
      label: RANKING_SCORE_LABEL,
      title: RANKING_SCORE_TITLE,
      cells: each((plan, column) =>
        unranked && column === 0 ? NOT_RANKED : formatScore(typeof plan.scores.robust === "number" ? plan.scores.robust : plan.scores.overall),
      ),
    },
    scoreRow("soil", "Soil protection"),
    scoreRow("water", "Water"),
    scoreRow("money", "Money"),
    scoreRow("risk", "Risk"),
  ];
  const figures: Row[] = [
    {
      key: "holds",
      label: "Futures it holds up in",
      title: irrigated ? `Counted ${wellQualifier(score.settings.irrigation_limit_in)}.` : undefined,
      cells: each((plan) => (plan.futures ? `${plan.futures.holds} of ${plan.futures.total}` : NO_FIGURE)),
    },
    { key: "bare_days", label: "Bare days a year", cells: each((plan) => (plan.numbers ? String(Math.round(plan.numbers.bare_days)) : NO_FIGURE)) },
    irrigated
      ? { key: "water_use", label: "Water pumped a year", cells: each((plan) => (plan.numbers ? formatInches(plan.numbers.irrigation_in, plan.numbers.irrigation_mm) : NO_FIGURE)) }
      : { key: "water_use", label: "Crop water need met by rain", cells: each((plan) => (plantsNothing(plan) ? NO_CROP : plan.numbers ? formatShare(plan.numbers.water_met_share) : NO_FIGURE)) },
    {
      key: "profit",
      label: "Return per acre a year, over direct costs",
      title: "The second line is the lowest and the highest climate model.",
      cells: each((plan) => {
        if (!plan.numbers) return NO_FIGURE;
        const spread = plan.spread?.profit_per_acre;
        return { text: formatDollars(plan.numbers.profit_per_acre), sub: spread ? rangeText(formatDollars(spread.low), formatDollars(spread.high)) : null };
      }),
    },
    { key: "failed", label: "Planted crops that fail", cells: each((plan) => (plantsNothing(plan) ? NO_CROP : plan.numbers ? formatShare(plan.numbers.failed_share) : NO_FIGURE)) },
  ];
  return { scores, figures };
}

function ScoreResult({ score, fieldId, plansQuery }: { score: FieldScore; fieldId: string; plansQuery: string }) {
  const { yours, comparison } = score;
  const picks = score.plans.slice(0, 3);
  const columns = [yours, ...picks];
  const { scores, figures } = tableRows(score);
  const isOurFirst = yours.same_as_pick === 1;
  const unranked = yours.rankable === false;
  const renderRows = (rows: Row[]) =>
    rows.map((row) => {
      const marked = score.marks[row.key] ?? [];
      return (
        <tr key={row.key} data-testid={`own-row-${row.key}`}>
          <th scope="row" title={row.title}>
            {row.label}
          </th>
          {row.cells.map((cell, column) => {
            const best = marked.includes(column);
            return (
              <td key={column} className={best ? "is-best" : undefined} data-best={best ? "true" : "false"} data-testid={`own-cell-${row.key}-${column}`}>
                <span className="own-cell-value">{cell.text}</span>
                {best && <span className="sr-only"> (marked: not behind any other column)</span>}
                {cell.sub && <span className="own-cell-sub">{cell.sub}</span>}
              </td>
            );
          })}
        </tr>
      );
    });

  return (
    <>
      <ol className="own-key" data-testid="own-key">
        {columns.map((plan, column) => (
          <li key={column}>
            <span className={column === 0 ? "own-key-label own-key-label--yours" : "own-key-label"}>{columnLabel(column)}</span>
            <span className="own-key-name" data-testid={`own-key-name-${column}`}>
              {plan.name}
            </span>
          </li>
        ))}
      </ol>

      <div className="own-table-wrap">
        <table className="own-table tabular" data-testid="own-table">
          <caption className="sr-only">Your rotation next to our top plans for this field, with the same settings</caption>
          <thead>
            <tr>
              <td />
              {columns.map((plan, column) => (
                <th key={column} scope="col" title={plan.name} className={column === 0 ? "own-col-yours" : undefined}>
                  {columnLabel(column)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            <tr className="own-group">
              <th colSpan={columns.length + 1} scope="colgroup">
                Scores, 0–1 · higher is better
              </th>
            </tr>
            {renderRows(scores)}
          </tbody>
          <tbody>
            <tr className="own-group">
              <th colSpan={columns.length + 1} scope="colgroup">
                The figures behind them
              </th>
            </tr>
            {renderRows(figures)}
          </tbody>
        </table>
      </div>
      <p className="own-marks-note" data-testid="own-marks-note">
        <span className="own-swatch" aria-hidden="true" />
        {score.marks_note}
      </p>
      {score.settings.irrigated && (
        <p className="own-holds muted" data-testid="own-holds">
          Futures are counted {wellQualifier(score.settings.irrigation_limit_in)}.
        </p>
      )}

      <div className="own-why" data-testid="own-why">
        <h4>{unranked ? "Why yours is not ranked" : isOurFirst ? "How yours compares" : "How yours compares with our first pick"}</h4>
        <div className="own-headline" data-testid="own-headline">
          {comparison.headline.map((sentence, index) => (
            <p key={index}>{sentence}</p>
          ))}
        </div>
        {!isOurFirst && comparison.rows.length > 0 && (
          <ul className="own-compare" data-testid="own-compare">
            {comparison.rows.map((row) => (
              <li key={row.key} data-verdict={row.verdict} data-testid={`own-compare-${row.key}`}>
                <p className="own-compare-head">
                  <span className="own-compare-label">{row.label}</span>
                  <span className={`own-verdict own-verdict--${row.verdict}`}>{verdictLabel(row.verdict)}</span>
                </p>
                <p className="own-compare-sentence">{row.sentence}</p>
              </li>
            ))}
          </ul>
        )}
        {comparison.priority_sentences.length > 0 && (
          <>
            <h4>When would yours come first?</h4>
            <div className="own-priorities" data-testid="own-priorities">
              {comparison.priority_sentences.map((sentence, index) => (
                <p key={index}>{sentence}</p>
              ))}
            </div>
          </>
        )}
        {yours.rules_broken.length > 0 && (
          <>
            <h4>What our plans avoid</h4>
            <ul className="own-broken" data-testid="own-broken">
              {yours.rules_broken.map((sentence, index) => (
                <li key={index}>{sentence}</li>
              ))}
            </ul>
          </>
        )}
        {comparison.notes.length > 0 && (
          <ul className="own-notes muted" data-testid="own-notes">
            {comparison.notes.map((sentence, index) => (
              <li key={index}>{sentence}</li>
            ))}
          </ul>
        )}
      </div>
      {/* The way from what the field grows now to this rotation. Not for one that plants nothing. */}
      {!unranked && <WeatherReplay fieldId={fieldId} plansQuery={plansQuery} target={{ rotation: score.understood }} name={yours.name} />}
      {!unranked && <HowToGetThere fieldId={fieldId} plansQuery={plansQuery} target={{ rotation: score.understood }} name={yours.name} />}
    </>
  );
}

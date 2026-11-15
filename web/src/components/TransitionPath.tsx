import { Fragment, useId, useState } from "react";
import type { ReactNode } from "react";
import { api } from "../api";
import { cropBackground, cropColor, cropLabel, textColorOn } from "../crops";
import { useLatest } from "../hooks/useLatest";
import { rangeText } from "../lib/custom";
import { formatDollars, formatInches, formatShare } from "../lib/planner";
import { DEFAULT_SPAN, SPANS, columnGeometry, shortYear, signedDollars, signedRange, totalDomain, transitionQuery, zeroAt } from "../lib/transition";
import type { TransitionTarget } from "../lib/transition";
import type { FieldTransition, TransitionYear } from "../types";
import "./TransitionPath.css";

interface HowToGetThereProps {
  fieldId: string;
  /** The settings the plan cards are asked with (lib/planner.ts plansQuery): the path follows the same controls. */
  plansQuery: string;
  /** One of our picks (its card number) or the farmer's own rotation. */
  target: TransitionTarget;
  /** The rotation's name, for the button's accessible name. */
  name: string;
  /** Other actions shown on the same line as the button ("Edit this plan" on a card). */
  actions?: ReactNode;
}

/**
 * "How to get there": a button that opens the year-by-year path from what the field grows now to
 * a rotation (GET /field/{id}/transition). Nothing is asked until it is opened; once open it
 * follows the planner controls like the cards do. Every figure and sentence is the API's.
 */
export function HowToGetThere({ fieldId, plansQuery, target, name, actions }: HowToGetThereProps) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [span, setSpan] = useState(DEFAULT_SPAN);
  const query = open ? transitionQuery(plansQuery, target, span) : null;
  const latest = useLatest<FieldTransition>(fieldId, query, api.transition);
  const result = latest.resource;
  return (
    <div className="how" data-testid="how">
      <div className="how-actions">
        <button
          type="button"
          className="link-button how-toggle"
          aria-expanded={open}
          aria-controls={`${id}-region`}
          aria-label={`How to get there: ${name}`}
          data-testid="how-toggle"
          onClick={() => setOpen((was) => !was)}
        >
          <svg className="how-caret" viewBox="0 0 10 6" width="9" height="6" aria-hidden="true">
            <path d="M1 1l4 4 4-4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" fill="none" />
          </svg>
          How to get there
        </button>
        {actions}
      </div>
      {open && query !== null && (
        <div
          id={`${id}-region`}
          className={latest.updating ? "how-region is-updating" : "how-region"}
          role="region"
          aria-label={`How to get there: ${name}`}
          aria-live="polite"
          aria-busy={latest.updating || result.status === "loading"}
          data-testid="how-region"
          data-state={result.status}
          data-updating={latest.updating ? "true" : "false"}
          data-query={latest.shownQuery ?? undefined}
          data-status={result.status === "ready" ? result.data.status : undefined}
        >
          {result.status === "loading" && (
            <>
              <div className="skeleton-block" aria-hidden="true">
                <div className="skeleton" style={{ height: 220 }} />
              </div>
              <p className="sr-only" role="status">
                Working out the path…
              </p>
            </>
          )}
          {result.status === "error" && (
            <p className="plans-update-error" role="alert" data-testid="how-error">
              <span>Could not work out this path. {result.message}</span>
              <button type="button" className="link-button" onClick={latest.retry}>
                Try again
              </button>
            </p>
          )}
          {result.status === "ready" && (
            <>
              {latest.updateError !== null && (
                <p className="plans-update-error" role="alert" data-testid="how-update-error">
                  <span>Could not update the path, so this is the last one we worked out. {latest.updateError}</span>
                  <button type="button" className="link-button" onClick={latest.retry}>
                    Try again
                  </button>
                </p>
              )}
              <PathView data={result.data} span={span} onSpan={setSpan} selectId={`${id}-span`} />
            </>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// The answer
// ---------------------------------------------------------------------------------------------

const NO_CROP = "no crop";

/** 190.6 bu/acre → "191 bu/acre", 4.8 tons/acre → "4.8 tons/acre": no more digits than the unit carries. */
function yieldText(row: TransitionYear): string | null {
  if (row.yield === null || !row.yield_unit) return null;
  return `${row.yield >= 20 ? Math.round(row.yield) : row.yield.toFixed(1)} ${row.yield_unit}`;
}

function PathView({ data, span, onSpan, selectId }: { data: FieldTransition; span: number; onSpan: (years: number) => void; selectId: string }) {
  const { path, carry_on: carryOn, difference, sentences } = data;
  const irrigated = data.settings.irrigated;
  return (
    <>
      <p className="how-summary" data-testid="how-summary">
        {data.summary}
      </p>
      {data.blocked.length > 0 && (
        <div className="how-blocked">
          <p>{path ? "In the way of starting the rotation straight away:" : "In the way:"}</p>
          <ul data-testid="how-blocked">
            {data.blocked.map((sentence, index) => (
              <li key={index}>{sentence}</li>
            ))}
          </ul>
        </div>
      )}

      {(data.history.length > 0 || path) && (
        <>
          <ol className="how-strip tabular" data-testid="how-strip" aria-label="The field's last years, then the path year by year">
            {data.history.map((past) => (
              <li key={past.year} className="how-year how-year--history" data-testid="how-year" data-kind="history" data-year={past.year} data-crop={past.crop}>
                <span className="rotation-year">{past.year}</span>
                <span className="chip how-chip" style={{ background: cropBackground(past.crop), color: textColorOn(cropColor(past.crop)) }}>
                  {/* a break is offered after each slash, so "Grass/Pasture" wraps there and not mid-word */}
                  {cropLabel(past.crop).split("/").map((part, i) => (
                    <Fragment key={i}>
                      {i > 0 && <>/<wbr /></>}
                      {part}
                    </Fragment>
                  ))}
                </span>
                <span className="how-year-note">grown</span>
              </li>
            ))}
            {path?.years.map((row) => {
              const color = cropColor(row.crop);
              const range = rangeText(formatDollars(row.return.low), formatDollars(row.return.high));
              return (
                <li
                  key={row.year}
                  className={row.role === "bridge" ? "how-year how-year--bridge" : "how-year"}
                  data-testid="how-year"
                  data-kind={row.role}
                  data-year={row.year}
                  data-crop={row.crop}
                >
                  <span className="rotation-year">
                    {row.year}
                    {row.role === "bridge" && <span className="how-tag"> bridge</span>}
                  </span>
                  <span className="chip how-chip" style={{ backgroundColor: color, color: textColorOn(color) }}>
                    {cropLabel(row.crop)}
                  </span>
                  <span className="how-return" data-testid="how-return">
                    {formatDollars(row.return.mid)}
                  </span>
                  {range && (
                    <span className="how-range" data-testid="how-range">
                      <Unbroken text={range} />
                    </span>
                  )}
                </li>
              );
            })}
          </ol>
          {path && (
            <p className="how-key muted">
              Grey years are what the field grew. Dollars are the return an acre over direct costs that year, with the lowest and highest
              climate model under it.
            </p>
          )}
        </>
      )}

      {sentences && (
        <div className="how-sentences">
          <p data-testid="how-short">{sentences.short_term}</p>
          <p data-testid="how-long">{sentences.long_term}</p>
        </div>
      )}

      {path && carryOn && difference && (
        <div className="how-versus">
          <h5>Against carrying on as now</h5>
          <p className="how-carry muted" data-testid="how-carry">
            Carrying on as now: {carryOn.name}.{data.carry_on_note ? ` ${data.carry_on_note}` : ""}
          </p>
          <RunningTotalChart difference={difference} />
          <p className="how-total" data-testid="how-total">
            {difference.total_sentence}
          </p>
          <div className="how-table-wrap">
            <table className="how-table tabular" data-testid="how-table">
              <caption className="sr-only">Return an acre over direct costs, year by year: this path, carrying on as now, the difference and its running total</caption>
              <thead>
                <tr>
                  <th scope="col">Year</th>
                  <th scope="col">This path</th>
                  <th scope="col">Carry on</th>
                  <th scope="col">Difference</th>
                  <th scope="col">Running total</th>
                </tr>
              </thead>
              <tbody>
                {difference.years.map((row, index) => {
                  const mine = path.years[index];
                  const theirs = carryOn.years[index];
                  if (!mine || !theirs) return null;
                  const paidBack = difference.payback.status === "paid_back" && difference.payback.n === row.n;
                  return (
                    <tr key={row.n} data-testid={`how-row-${row.n}`} className={paidBack ? "is-payback" : undefined}>
                      <th scope="row">
                        {row.year}
                        {paidBack && <span className="how-cell-sub">paid back</span>}
                      </th>
                      <Cell testId={`how-cell-path-${row.n}`} value={formatDollars(mine.return.mid)} sub={cropLabel(mine.crop)} />
                      <Cell testId={`how-cell-carry-${row.n}`} value={formatDollars(theirs.return.mid)} sub={cropLabel(theirs.crop)} />
                      <Cell testId={`how-cell-diff-${row.n}`} value={signedDollars(row.return.mid)} sub={signedRange(row.return.low, row.return.high)} />
                      <Cell testId={`how-cell-total-${row.n}`} value={signedDollars(row.cumulative.mid)} sub={signedRange(row.cumulative.low, row.cumulative.high)} />
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
      {path && !carryOn && data.carry_on_note && (
        <p className="how-carry muted" data-testid="how-carry">
          {data.carry_on_note}
        </p>
      )}

      {path && (
        <details className="how-more" data-testid="how-details">
          <summary>Water, bare days and crop failure, year by year</summary>
          <div className="how-table-wrap">
            <table className="how-table how-table--detail tabular" data-testid="how-detail-table">
              <caption className="sr-only">Each year of the path: crop and yield, water, bare days and the chance the crop fails</caption>
              <thead>
                <tr>
                  <th scope="col">Year</th>
                  <th scope="col">Crop</th>
                  <th scope="col">{irrigated ? "Water pumped" : "Water need met by rain"}</th>
                  <th scope="col">Bare days</th>
                  <th scope="col">Chance it fails</th>
                </tr>
              </thead>
              <tbody>
                {path.years.map((row) => (
                  <tr key={row.n} data-testid={`how-detail-${row.n}`}>
                    <th scope="row">{row.year}</th>
                    <Cell testId={`how-detail-crop-${row.n}`} value={cropLabel(row.crop)} sub={yieldText(row)} />
                    <Cell
                      testId={`how-detail-water-${row.n}`}
                      value={irrigated ? formatInches(row.irrigation_in, row.irrigation_mm) : row.water_met_share === null ? NO_CROP : formatShare(row.water_met_share)}
                    />
                    <Cell testId={`how-detail-bare-${row.n}`} value={String(Math.round(row.bare_days))} />
                    <Cell testId={`how-detail-fail-${row.n}`} value={row.fail_chance === null ? NO_CROP : formatShare(row.fail_chance)} />
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="muted">A crop fails when its yield is under half of normal. Bare days are county averages for that crop after the crop before it.</p>
        </details>
      )}

      {data.target.rules_broken.length > 0 && path && (
        <div className="how-blocked">
          <p>The rotation itself, every round, does what our plans avoid. The path does not change that:</p>
          <ul data-testid="how-target-broken">
            {data.target.rules_broken.map((sentence, index) => (
              <li key={index}>{sentence}</li>
            ))}
          </ul>
        </div>
      )}

      {path && (
        <details className="how-more" data-testid="how-limits">
          <summary>What this leaves out</summary>
          <ul data-testid="how-not-modelled">
            {data.not_modelled.map((sentence, index) => (
              <li key={index}>{sentence}</li>
            ))}
          </ul>
          <ul className="muted" data-testid="how-notes">
            {data.notes.map((sentence, index) => (
              <li key={index}>{sentence}</li>
            ))}
          </ul>
        </details>
      )}

      {path && (
        <p className="how-span">
          <label htmlFor={selectId}>Years ahead</label>
          <select id={selectId} value={span} data-testid="how-span" onChange={(event) => onSpan(Number(event.target.value))}>
            {SPANS.map((years) => (
              <option key={years} value={years}>
                {years}
              </option>
            ))}
          </select>
        </p>
      )}
    </>
  );
}

function Cell({ testId, value, sub }: { testId: string; value: string; sub?: string | null }) {
  return (
    <td data-testid={testId}>
      <span className="how-cell-value">{value}</span>
      {sub && (
        <span className="how-cell-sub">
          <Unbroken text={sub} />
        </span>
      )}
    </td>
  );
}

/** "−$136 to −$61" with each figure kept whole: a line may break at the spaces, never after a minus sign. */
function Unbroken({ text }: { text: string }) {
  const parts = text.split(" to ");
  if (parts.length !== 2) return <>{text}</>;
  return (
    <>
      <span className="how-nowrap">{parts[0]}</span> to <span className="how-nowrap">{parts[1]}</span>
    </>
  );
}

/**
 * The running total of the difference, one column a year: a bar from the lowest to the highest
 * climate model, a dot at their average, a line at zero. The same marks as the return strip on the
 * plan cards, turned upright. The table under it carries every number.
 */
function RunningTotalChart({ difference }: { difference: NonNullable<FieldTransition["difference"]> }) {
  const totals = difference.years.map((row) => row.cumulative);
  const domain = totalDomain(totals);
  const zero = zeroAt(domain);
  const [low, high] = domain;
  const words = difference.years
    .map((row) => {
      const range = signedRange(row.cumulative.low, row.cumulative.high);
      return `${row.year}: ${signedDollars(row.cumulative.mid)}${range ? ` (${range})` : ""}`;
    })
    .join("; ");
  const payback = difference.payback.status === "paid_back" ? difference.payback.n : null;
  return (
    <div className="how-chart" data-testid="how-chart">
      <p className="how-chart-title">Running total of the difference, $ an acre</p>
      <div className="how-chart-body tabular" role="img" aria-label={`Running total of this path minus carrying on as now, dollars an acre. ${words}`}>
        <div className="how-chart-axis" aria-hidden="true">
          {high > 0 && <span style={{ bottom: "100%" }}>{signedDollars(high)}</span>}
          <span style={{ bottom: `${zero}%` }}>$0</span>
          {low < 0 && <span style={{ bottom: "0%" }}>{signedDollars(low)}</span>}
        </div>
        <div className="how-chart-plot" style={{ gridTemplateColumns: `repeat(${difference.years.length}, minmax(0, 1fr))` }}>
          <span className="how-chart-zero" style={{ bottom: `${zero}%` }} />
          {difference.years.map((row) => {
            const g = columnGeometry(row.cumulative, domain);
            const range = signedRange(row.cumulative.low, row.cumulative.high);
            return (
              <div
                key={row.n}
                className={payback === row.n ? "how-chart-col is-payback" : "how-chart-col"}
                title={`${row.year}: ${signedDollars(row.cumulative.mid)} an acre${range ? ` (${range} across the climate models)` : ""}`}
                data-testid="how-chart-col"
                data-year={row.year}
                data-mid={row.cumulative.mid}
              >
                <span className="how-chart-range" style={{ bottom: `${g.bottom}%`, height: `${g.height}%` }} />
                <span className="how-chart-mid" style={{ bottom: `${g.mid}%` }} />
              </div>
            );
          })}
        </div>
        <div className="how-chart-years" aria-hidden="true" style={{ gridTemplateColumns: `repeat(${difference.years.length}, minmax(0, 1fr))` }}>
          {difference.years.map((row) => (
            <span key={row.n} className={payback === row.n ? "is-payback" : undefined}>
              {shortYear(row.year)}
            </span>
          ))}
        </div>
      </div>
      <p className="how-chart-key muted">
        Above the line this path is ahead of carrying on as now. Bar: lowest to highest climate model. Dot: their average.
        {payback !== null && " Shaded: the year the switch has paid back."}
      </p>
    </div>
  );
}

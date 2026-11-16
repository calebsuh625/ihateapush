import { useId, useState } from "react";
import { api } from "../api";
import { cropLabel } from "../crops";
import { useLatest } from "../hooks/useLatest";
import { formatDollars, formatShare } from "../lib/planner";
import { CHART, barGeometry, labelledYears, replayQuery, returnDomain, shortYear, signedDollars, ticks, yOf, yearTitle } from "../lib/replay";
import type { FieldReplay, ReplaySeries, ReplayTarget } from "../lib/replay";
import "./WeatherReplay.css";

interface WeatherReplayProps {
  fieldId: string;
  /** The settings the plan cards are asked with (lib/planner.ts plansQuery): the replay follows the same controls. */
  plansQuery: string;
  /** One of our picks (its card number) or the farmer's own rotation. */
  target: ReplayTarget;
  /** The rotation's name, for the button's accessible name. */
  name: string;
}

/**
 * "How it would have done in real weather": a button that opens the plan run through the observed
 * years (GET /field/{id}/replay). Nothing is asked until it is opened; once open it follows the
 * planner controls like the cards do. Every figure, mark and sentence is the API's.
 */
export function WeatherReplay({ fieldId, plansQuery, target, name }: WeatherReplayProps) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const query = open ? replayQuery(plansQuery, target) : null;
  const latest = useLatest<FieldReplay>(fieldId, query, api.replay);
  const result = latest.resource;
  return (
    <div className="replay" data-testid="replay">
      <div className="how-actions">
        <button
          type="button"
          className="link-button how-toggle"
          aria-expanded={open}
          aria-controls={`${id}-region`}
          aria-label={`How it would have done in real weather: ${name}`}
          data-testid="replay-toggle"
          onClick={() => setOpen((was) => !was)}
        >
          <svg className="how-caret" viewBox="0 0 10 6" width="9" height="6" aria-hidden="true">
            <path d="M1 1l4 4 4-4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" fill="none" />
          </svg>
          How it would have done in real weather
        </button>
      </div>
      {open && query !== null && (
        <div
          id={`${id}-region`}
          className={latest.updating ? "replay-region is-updating" : "replay-region"}
          role="region"
          aria-label={`How it would have done in real weather: ${name}`}
          aria-live="polite"
          aria-busy={latest.updating || result.status === "loading"}
          data-testid="replay-region"
          data-state={result.status}
          data-updating={latest.updating ? "true" : "false"}
          data-query={latest.shownQuery ?? undefined}
        >
          {result.status === "loading" && (
            <>
              <div className="skeleton-block" aria-hidden="true">
                <div className="skeleton" style={{ height: 240 }} />
              </div>
              <p className="sr-only" role="status">
                Running the plan through the real weather…
              </p>
            </>
          )}
          {result.status === "error" && (
            <p className="plans-update-error" role="alert" data-testid="replay-error">
              <span>Could not replay this plan. {result.message}</span>
              <button type="button" className="link-button" onClick={latest.retry}>
                Try again
              </button>
            </p>
          )}
          {result.status === "ready" && (
            <>
              {latest.updateError !== null && (
                <p className="plans-update-error" role="alert" data-testid="replay-update-error">
                  <span>Could not update the replay, so this is the last one we worked out. {latest.updateError}</span>
                  <button type="button" className="link-button" onClick={latest.retry}>
                    Try again
                  </button>
                </p>
              )}
              <ReplayView data={result.data} />
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

function ReplayView({ data }: { data: FieldReplay }) {
  const { plan, carry_on: carryOn, comparison, weather } = data;
  const s = plan.summary;
  const record = `${weather.first_year}-${weather.last_year}`;
  const total = plan.basis === "this_start" ? s.years : s.crop_years;
  const worstWhat = s.worst_year.crop ? `${s.worst_year.year}, ${cropLabel(s.worst_year.crop).toLowerCase()}` : `${s.worst_year.year}`;
  return (
    <>
      <p className="replay-lead" data-testid="replay-lead">
        {plan.sentences[0]}
      </p>
      <dl className="replay-figures tabular" data-testid="replay-figures">
        <div data-testid="replay-worst" data-year={s.worst_year.year} data-value={s.worst_year.return}>
          <dd>{formatDollars(s.worst_year.return)}</dd>
          <dt>worst year ({worstWhat})</dt>
        </div>
        <div data-testid="replay-median" data-value={s.median_return}>
          <dd>{formatDollars(s.median_return)}</dd>
          <dt>middle year</dt>
        </div>
        <div data-testid="replay-under" data-count={s.below_cost_years} data-total={total}>
          <dd>
            {s.below_cost_years} of {total}
          </dd>
          <dt>
            {plan.basis === "this_start" ? "years" : "crop-years"} under direct costs ({formatShare(s.below_cost_share)})
          </dt>
        </div>
      </dl>
      {plan.effects_range && (
        <p className="replay-range tabular" data-testid="replay-range">
          Allowing for how sure we are of the crop-after-crop yield effects: worst year {formatDollars(plan.effects_range.worst_return[0])} to{" "}
          {formatDollars(plan.effects_range.worst_return[1])}, middle year {formatDollars(plan.effects_range.median_return[0])} to {formatDollars(plan.effects_range.median_return[1])}.
        </p>
      )}

      <ReturnChart plan={plan} carry={comparison ? comparison.carry_return : null} carryName={carryOn?.name ?? null} weather={weather} record={record} />

      <ul className="replay-sentences" data-testid="replay-sentences">
        {plan.sentences.slice(1).map((sentence, i) => (
          <li key={i}>{sentence}</li>
        ))}
      </ul>

      {comparison && (
        <div className="replay-versus" data-testid="replay-versus">
          <h5>Against carrying on as now</h5>
          <ul className="replay-sentences">
            {comparison.sentences.map((sentence, i) => (
              <li key={i}>{sentence}</li>
            ))}
          </ul>
          <dl className="replay-figures replay-figures--small tabular" data-testid="replay-versus-figures">
            <div data-testid="replay-ahead" data-count={comparison.years_ahead} data-total={comparison.total}>
              <dd>
                {comparison.years_ahead} of {comparison.total}
              </dd>
              <dt>years ahead of carrying on</dt>
            </div>
            <div data-testid="replay-mean-difference" data-value={comparison.mean_difference}>
              <dd>{signedDollars(comparison.mean_difference)}</dd>
              <dt>average difference, an acre a year</dt>
            </div>
            <div data-testid="replay-worst-versus" data-plan={comparison.worst_year.plan} data-carry={comparison.worst_year.carry_on}>
              <dd>
                {formatDollars(comparison.worst_year.plan)} / {formatDollars(comparison.worst_year.carry_on)}
              </dd>
              <dt>worst year, plan / carrying on</dt>
            </div>
          </dl>
        </div>
      )}

      <YearTable plan={plan} carry={comparison ? comparison.carry_return : null} />

      <p className="replay-error-note" data-testid="replay-model-error">
        {modelErrorText(plan)}
      </p>
      <ul className="replay-notes" data-testid="replay-notes">
        {data.notes.map((note, i) => (
          <li key={i}>{note}</li>
        ))}
      </ul>
      <details className="replay-more" data-testid="replay-not-modelled">
        <summary>What the replay leaves out</summary>
        <ul>
          {data.not_modelled.map((note, i) => (
            <li key={i}>{note}</li>
          ))}
        </ul>
      </details>
    </>
  );
}

/** How far the crop model typically misses one year's yield, so a single bar is not read as exact. */
function modelErrorText(plan: ReplaySeries): string {
  const known = plan.model_error.filter((e) => e.typical_miss_pct !== null);
  if (known.length === 0) return "Single years are rough: the crop model is not fitted for these crops.";
  const list = known.map((e) => `${cropLabel(e.crop).toLowerCase()} ${e.typical_miss_pct}%`).join(", ");
  return `Single years are rough. How far our crop model typically missed a year's yield in the years it was fitted, as a share of a normal yield: ${list}. Read the pattern across years more than any one bar.`;
}

// ---------------------------------------------------------------------------------------------
// The chart: one bar a year, inline SVG
// ---------------------------------------------------------------------------------------------

function ReturnChart({
  plan,
  carry,
  carryName,
  weather,
  record,
}: {
  plan: ReplaySeries;
  carry: number[] | null;
  carryName: string | null;
  weather: FieldReplay["weather"];
  record: string;
}) {
  const rows = plan.years;
  const domain = returnDomain(rows, carry ?? []);
  const zero = yOf(0, domain);
  const plotRight = CHART.width - CHART.right;
  const plotBottom = CHART.height - CHART.bottom;
  const known = plan.phase.known;
  const dryList = weather.dry_years.join(", ");
  const label = `Return an acre over direct costs in each year ${record}. ${plan.summary.below_cost_years} bars are under zero. Dry growing seasons: ${dryList}. The table below has every year.`;
  return (
    <figure className="replay-chart" data-testid="replay-chart">
      <figcaption className="replay-chart-title">
        Return an acre over direct costs, each year {record}
        {known ? "" : " (average of every starting year)"}
      </figcaption>
      <svg viewBox={`0 0 ${CHART.width} ${CHART.height}`} role="img" aria-label={label} className="replay-svg">
        {ticks(domain).map((value) => {
          const y = yOf(value, domain);
          return (
            <g key={value}>
              <line x1={CHART.left} x2={plotRight} y1={y} y2={y} className={value === 0 ? "replay-zero" : "replay-grid"} />
              <text x={CHART.left - 4} y={y + 3} textAnchor="end" className="replay-axis">
                {formatDollars(value)}
              </text>
            </g>
          );
        })}
        {rows.map((row, i) => {
          if (!row.dry) return null;
          const g = barGeometry(i, rows.length, row.return, domain);
          return (
            <g key={`dry-${row.year}`} data-testid="replay-dry-mark" data-year={row.year}>
              <rect x={g.x} y={CHART.top} width={g.band} height={plotBottom - CHART.top} className="replay-dry-band" />
              <rect x={g.x + 1} y={plotBottom + 1} width={g.band - 2} height={3} className="replay-dry-tick" />
            </g>
          );
        })}
        {rows.map((row, i) => {
          const g = barGeometry(i, rows.length, row.return, domain);
          const low = yOf(row.low, domain);
          const high = yOf(row.high, domain);
          const carryValue = carry?.[i] ?? null;
          const rank = row.worst_rank;
          const ringY = row.return >= 0 ? g.y + g.height + 8 : g.y - 8;
          return (
            <g
              key={row.year}
              data-testid="replay-bar"
              data-year={row.year}
              data-return={row.return}
              data-dry={row.dry ? "true" : "false"}
              data-under={row.below_cost ? "true" : "false"}
              data-rank={rank ?? undefined}
            >
              <title>{yearTitle(row, formatDollars, cropLabel)}</title>
              <rect x={g.barX} y={g.y} width={g.barWidth} height={g.height} className={row.below_cost ? "replay-bar replay-bar--under" : "replay-bar"} />
              {row.low !== row.high && <line x1={g.centre} x2={g.centre} y1={high} y2={low} className="replay-whisker" />}
              {carryValue !== null && <line x1={g.barX - 1} x2={g.barX + g.barWidth + 1} y1={yOf(carryValue, domain)} y2={yOf(carryValue, domain)} className="replay-carry" />}
              {rank !== null && (
                <g className="replay-rank" data-testid="replay-rank">
                  <circle cx={g.centre} cy={Math.min(plotBottom - 6, Math.max(CHART.top + 6, ringY))} r="5.5" />
                  <text x={g.centre} y={Math.min(plotBottom - 6, Math.max(CHART.top + 6, ringY)) + 3} textAnchor="middle">
                    {rank}
                  </text>
                </g>
              )}
            </g>
          );
        })}
        {labelledYears(rows.length).map((i) => (
          <text key={rows[i]?.year ?? i} x={barGeometry(i, rows.length, 0, domain).centre} y={CHART.height - 8} textAnchor="middle" className="replay-axis">
            {shortYear(rows[i]?.year ?? 0)}
          </text>
        ))}
        <line x1={CHART.left} x2={plotRight} y1={zero} y2={zero} className="replay-zero" />
      </svg>
      <ul className="replay-key" data-testid="replay-key">
        <li>
          <span className="replay-swatch replay-swatch--bar" aria-hidden="true" /> return over direct costs
        </li>
        <li>
          <span className="replay-swatch replay-swatch--under" aria-hidden="true" /> under direct costs
        </li>
        <li>
          <span className="replay-swatch replay-swatch--dry" aria-hidden="true" /> dry growing season (driest fifth of the years on record)
        </li>
        <li>
          <span className="replay-swatch replay-swatch--rank" aria-hidden="true" /> the three worst years
        </li>
        {plan.phases > 1 && (
          <li>
            <span className="replay-swatch replay-swatch--whisker" aria-hidden="true" /> best and worst of {plan.phases} starting years
          </li>
        )}
        {carry && carryName && (
          <li>
            <span className="replay-swatch replay-swatch--carry" aria-hidden="true" /> carrying on as now ({carryName})
          </li>
        )}
      </ul>
    </figure>
  );
}

function YearTable({ plan, carry }: { plan: ReplaySeries; carry: number[] | null }) {
  const irrigated = plan.years.some((y) => y.water_in !== null);
  return (
    <details className="replay-more replay-table-fold" data-testid="replay-table-fold">
      <summary>Year by year (table)</summary>
      <div className="replay-table-wrap">
        <table className="replay-table tabular" data-testid="replay-table">
          <thead>
            <tr>
              <th scope="col">Year</th>
              <th scope="col">Crop</th>
              <th scope="col">Return</th>
              {carry && <th scope="col">Carrying on</th>}
              <th scope="col">Yield vs normal</th>
              <th scope="col">Failed</th>
              <th scope="col">{irrigated ? "Water pumped" : "Water need met"}</th>
              <th scope="col">Dry season</th>
            </tr>
          </thead>
          <tbody>
            {plan.years.map((y, i) => (
              <tr key={y.year} data-year={y.year} className={y.worst_rank ? "is-worst" : undefined}>
                <th scope="row">{y.year}</th>
                <td>{y.crop ? cropLabel(y.crop) : "average"}</td>
                <td>{formatDollars(y.return)}</td>
                {carry && <td>{formatDollars(carry[i] ?? 0)}</td>}
                <td>{y.yield_pct === null ? "–" : `${Math.round(y.yield_pct)}%`}</td>
                <td>{y.failed === null ? (y.failed_phases > 0 ? `${y.failed_phases} of ${plan.phases} starts` : "no") : y.failed ? "yes" : y.crop === "fallow" ? "–" : "no"}</td>
                <td>{irrigated ? (y.water_in === null ? "–" : `${y.water_in.toFixed(1)} in`) : y.water_met === null ? "–" : formatShare(y.water_met)}</td>
                <td>{y.dry ? "dry" : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

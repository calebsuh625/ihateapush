// "Compare with its twin": a field and its best-matched look-alike neighbour from Phase 2B, side by
// side in a wide sheet over the bottom of the screen (the map stays visible above it, with both
// fields outlined). Data: GET /field/{id}/twin (api/twin.py). Everything stated about the data (every
// number, every sentence about what it means) comes from that answer; this file only draws it.
// What is shown is a difference between look-alike neighbours in days the soil sits bare. Nothing
// here says a rotation caused or improved anything.
import type { CSSProperties } from "react";
import { useEffect, useRef } from "react";
import { cropBackground, cropLabel, cropPalette, textColorOn } from "../crops";
import type { Resource } from "../hooks/useResource";
import { rotationHint, rotationLabel, yearSpan } from "../lib/neighbors";
import { agreementShares, differenceAxis, lineChart, readingIn, signed, yearRange } from "../lib/twin";
import type { BarePoint, TwinAnswer, TwinSide } from "../lib/twin";
import { ErrorNote } from "./FieldPanel";
import "./Twin.css";

interface TwinViewProps {
  fieldId: string;
  res: Resource<TwinAnswer>;
  onRetry: () => void;
  onClose: () => void;
  /** Open a field (the twin, or an example) in the field panel. */
  onOpenField: (id: string) => void;
  /** Height of the sheet in pixels (the map above is fitted to what is left). */
  height: number;
}

export function TwinView({ fieldId, res, onRetry, onClose, onOpenField, height }: TwinViewProps) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    headingRef.current?.focus({ preventScroll: true });
  }, [fieldId]);

  const ready = res.status === "ready" ? res.data : null;
  return (
    <section
      className="tw-sheet"
      style={{ "--tw-height": `${height}px` } as CSSProperties}
      aria-labelledby="twin-title"
      data-testid="twin-view"
      data-state={res.status}
      data-field-id={fieldId}
      data-twin-id={ready?.twin?.field_id ?? ""}
    >
      <header className="tw-head">
        <div>
          <h2 id="twin-title" tabIndex={-1} ref={headingRef}>
            {ready?.twin ? (
              <>
                <span className="tw-name tw-name--this tabular">{ready.this.field_id}</span>
                <span className="tw-vs"> and its twin </span>
                <span className="tw-name tw-name--twin tabular">{ready.twin.field_id}</span>
              </>
            ) : (
              <>
                <span className="tw-name tw-name--this tabular">{fieldId}</span> and its twin
              </>
            )}
          </h2>
          {ready?.twin && ready.design && (
            <p className="tw-sub">
              Two look-alike neighbours that grew different crops in {yearSpan(ready.design.rotation_years)}. This compares the days
              each one sat bare. It shows what was measured, not that any rotation changed anything.
              {ready.is_fake && (
                <span className="badge badge--demo" data-testid="twin-fake">
                  Fake soil data · not findings
                </span>
              )}
            </p>
          )}
        </div>
        <button type="button" className="button tw-close" onClick={onClose} data-testid="twin-close" aria-label="Close the comparison">
          Close
        </button>
      </header>

      <div className="tw-body">
        {res.status === "loading" && (
          <>
            <div className="skeleton" style={{ height: 260 }} aria-hidden="true" />
            <p className="sr-only" role="status">
              Loading the comparison…
            </p>
          </>
        )}
        {res.status === "error" && (
          <ErrorNote
            title="Could not load the comparison"
            message={res.message}
            onRetry={res.message.includes("make causal") ? undefined : onRetry}
          />
        )}
        {ready && !ready.has_twin && <NoTwin data={ready} onOpenField={onOpenField} />}
        {ready && ready.has_twin && ready.twin && <TwinBody data={ready} twin={ready.twin} onOpenField={onOpenField} />}
      </div>
    </section>
  );
}

function NoTwin({ data, onOpenField }: { data: TwinAnswer; onOpenField: (id: string) => void }) {
  return (
    <div className="tw-none" data-testid="twin-none">
      <p>This field has no twin. {data.reason}</p>
      {data.examples.length > 0 && (
        <p className="muted">
          Fields with a close look-alike:{" "}
          {data.examples.map((id, i) => (
            <span key={id}>
              {i > 0 && ", "}
              <button type="button" className="link-button tabular" onClick={() => onOpenField(id)}>
                {id}
              </button>
            </span>
          ))}
        </p>
      )}
    </div>
  );
}

function TwinBody({ data, twin, onOpenField }: { data: TwinAnswer; twin: TwinSide; onOpenField: (id: string) => void }) {
  const years = yearRange(data.this.crop_history, twin.crop_history);
  const palette = cropPalette([...Object.values(data.this.crop_history), ...Object.values(twin.crop_history)]);
  const soilYears = new Set(data.soil_years);
  return (
    <div className="tw-grid">
      <Column side="this" label="This field" data={data} who={data.this} years={years} palette={palette} soilYears={soilYears} />
      <Column
        side="twin"
        label="Its twin"
        data={data}
        who={twin}
        years={years}
        palette={palette}
        soilYears={soilYears}
        onOpen={() => onOpenField(twin.field_id)}
      />
      <div className="tw-analysis">
        {data.match && <Match data={data} />}
        <BareChart data={data} twin={twin} />
        {data.measured && data.comparison && <Difference data={data} />}
        {data.comparison && <Agree data={data} />}
        {data.comparison && <Trust data={data} />}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// The two columns: one row per year, so a year lines up across them
// ---------------------------------------------------------------------------------------------

function Column({
  side,
  label,
  data,
  who,
  years,
  palette,
  soilYears,
  onOpen,
}: {
  side: "this" | "twin";
  label: string;
  data: TwinAnswer;
  who: TwinSide;
  years: number[];
  palette: ReadonlyMap<string, string>;
  soilYears: Set<number>;
  onOpen?: () => void;
}) {
  const kind = who.rotation_post;
  return (
    <div className={`tw-col tw-col--${side}`} data-testid={`twin-col-${side}`} data-field-id={who.field_id}>
      <p className="tw-col-label">
        <span className="tw-key" aria-hidden="true" />
        {label}
      </p>
      <p className="tw-col-id">
        {onOpen ? (
          <button type="button" className="link-button tabular" onClick={onOpen} data-testid="twin-open" title="Open this field">
            {who.field_id}
          </button>
        ) : (
          <span className="tabular">{who.field_id}</span>
        )}
        {kind && (
          <span className="tag" title={rotationHint(kind) ?? undefined}>
            {rotationLabel(kind)}
          </span>
        )}
      </p>
      <p className="tw-col-meta muted">
        {[
          who.irrigation,
          kind ? `${yearSpan(data.design.rotation_years)}: ${rotationLabel(kind).toLowerCase()}` : null,
          who.rotation_pre ? `${yearSpan(data.design.history_years)}: ${rotationLabel(who.rotation_pre).toLowerCase()}` : null,
        ]
          .filter(Boolean)
          .join(" · ")}
      </p>
      <ol className="tw-years" aria-label={`${label}: crops and bare days by year`}>
        {years.map((y) => {
          const crop = who.crop_history[String(y)];
          const bare = readingIn(who.bare_days, y);
          const inSoil = soilYears.has(y);
          const bg = crop ? cropBackground(crop, palette) : undefined;
          const firstBg = crop ? (palette.get(crop.trim().toLowerCase()) ?? "#ffffff") : "#ffffff";
          return (
            <li key={y} className={`tw-year${y === data.switch_year ? " is-switch" : ""}`} data-year={y}>
              <span className="tw-y tabular">{y}</span>
              <span
                className={`tw-crop${crop ? "" : " is-empty"}`}
                style={crop ? { background: bg, color: /^#/.test(firstBg) ? textColorOn(firstBg) : undefined } : undefined}
              >
                {crop ? cropLabel(crop) : "no data"}
              </span>
              <span className="tw-bare tabular" data-testid={inSoil ? "twin-bare" : undefined}>
                {inSoil ? (bare === null ? <span className="muted">no reading</span> : `${bare.toFixed(1)} d`) : ""}
              </span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// How they were matched
// ---------------------------------------------------------------------------------------------

function Match({ data }: { data: TwinAnswer }) {
  const m = data.match;
  if (!m) return null;
  return (
    <section className="tw-block" data-testid="twin-match">
      <h3>How alike they are</h3>
      <p>{m.sentence}</p>
      <ul className="tw-matched">
        {m.matched_on.map((item) => (
          <li key={item.key} data-key={item.key}>
            {item.text}
          </li>
        ))}
      </ul>
      {m.pairs_of_this_field > 1 && (
        <p className="muted">
          This field is in {m.pairs_of_this_field} matched pairs; this is its closest match.
        </p>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------------------------
// One chart: both fields' bare days by year
// ---------------------------------------------------------------------------------------------

function BareChart({ data, twin }: { data: TwinAnswer; twin: TwinSide }) {
  const outcome = data.outcome;
  const c = lineChart([data.this.bare_days, twin.bare_days], data.soil_years, data.switch_year);
  const gaps = [data.this.bare_days, twin.bare_days].reduce((n, s) => n + s.filter((p: BarePoint) => p.value === null).length, 0);
  const label = outcome?.label ?? "Days the soil sits bare each year";
  return (
    <section className="tw-block" data-testid="twin-chart-block">
      <h3>{label}</h3>
      <p className="tw-legend" aria-hidden="true">
        <span>
          <svg width="22" height="10" viewBox="0 0 22 10">
            <line x1="0" x2="22" y1="5" y2="5" className="tw-l tw-l--this" />
            <circle cx="11" cy="5" r="3.5" className="tw-d tw-d--this" />
          </svg>{" "}
          This field
        </span>
        <span>
          <svg width="22" height="10" viewBox="0 0 22 10">
            <line x1="0" x2="22" y1="5" y2="5" className="tw-l tw-l--twin" />
            <rect x="7.5" y="1.5" width="7" height="7" className="tw-d tw-d--twin" />
          </svg>{" "}
          Its twin
        </span>
        <span className="muted">fewer bare days = more of the year covered</span>
      </p>
      <svg
        className="tw-chart"
        viewBox={`0 0 ${c.width} ${c.height}`}
        role="img"
        aria-label={`${label} by year, this field and its twin. The year ${data.switch_year} cut-off is marked.`}
        data-testid="twin-chart"
      >
        {c.afterX && <rect x={c.afterX[0]} y={c.plot.top} width={c.afterX[1] - c.afterX[0]} height={c.plot.bottom - c.plot.top} className="tw-after" />}
        {c.yTicks.map((t) => (
          <g key={t}>
            <line x1={c.plot.left} x2={c.plot.right} y1={c.y(t)} y2={c.y(t)} className="tw-grid-line" />
            <text x={c.plot.left - 5} y={c.y(t) + 4} textAnchor="end" className="tw-tick">
              {t}
            </text>
          </g>
        ))}
        {c.years.map((y) => (
          <text key={y} x={c.x(y)} y={c.plot.bottom + 16} textAnchor="middle" className="tw-tick">
            {y}
          </text>
        ))}
        {c.cutX !== null && (
          <g data-testid="twin-cutoff">
            <line x1={c.cutX} x2={c.cutX} y1={c.plot.top} y2={c.plot.bottom} className="tw-cut" />
            <text x={c.cutX + 4} y={c.plot.top + 10} className="tw-cut-label">
              {data.switch_year} cut-off
            </text>
          </g>
        )}
        <path d={c.paths[1]} className="tw-l tw-l--twin" />
        <path d={c.paths[0]} className="tw-l tw-l--this" />
        {c.dots[1]?.map((p) => (
          <rect key={`t${p.year}`} x={p.x - 3.5} y={p.y - 3.5} width="7" height="7" className="tw-d tw-d--twin" data-testid="twin-dot" data-who="twin" data-year={p.year} data-value={p.value} />
        ))}
        {c.dots[0]?.map((p) => (
          <circle key={`s${p.year}`} cx={p.x} cy={p.y} r="3.5" className="tw-d tw-d--this" data-testid="twin-dot" data-who="this" data-year={p.year} data-value={p.value} />
        ))}
      </svg>
      <p className="muted tw-cap">
        Days a year the soil sat bare, {yearSpan([data.soil_years[0] ?? 0, data.soil_years[data.soil_years.length - 1] ?? 0])}. The shaded part is from the
        {" "}
        {data.switch_year} cut-off on (not a year in which anyone changed: many fields carried on).
        {gaps > 0 ? " A break in a line is a year with no satellite reading." : ""}
      </p>
    </section>
  );
}

// ---------------------------------------------------------------------------------------------
// The measured difference, with its interval
// ---------------------------------------------------------------------------------------------

function Difference({ data }: { data: TwinAnswer }) {
  const m = data.measured;
  const comp = data.comparison;
  if (!m || !comp) return null;
  const est = comp.estimate;
  const spread = comp.agreement.spread_of_other_pairs;
  const diff = m.difference_in_change;
  const axis = differenceAxis([diff, est?.effect, est?.ci_low, est?.ci_high, spread?.p25, spread?.p75]);
  const rowH = 44;
  const top = 6;
  const H = top + 3 * rowH + 38;
  const axisY = top + 3 * rowH + 4;
  const rows: { y: number; label: string; value: string }[] = [
    { y: top, label: "This pair", value: signed(diff) },
    {
      y: top + rowH,
      label: spread ? `Middle half of the ${spread.n} other pairs` : "Other pairs",
      value: spread ? `${signed(spread.p25)} to ${signed(spread.p75)}` : "none",
    },
    {
      y: top + 2 * rowH,
      label: `All ${comp.n_pairs} pairs: average, 95% interval`,
      value: est ? `${signed(est.effect)} (${signed(est.ci_low)} to ${signed(est.ci_high)})` : "no average",
    },
  ];
  const bar = (a: number, b: number) => ({ x: Math.min(axis.x(a), axis.x(b)), width: Math.max(1.5, Math.abs(axis.x(a) - axis.x(b))) });
  const pre = yearSpan(m.pre_years);
  const post = yearSpan(m.post_years);
  const cell = (n: number | null) => (n === null ? "?" : n.toFixed(1));
  return (
    <section className="tw-block" data-testid="twin-difference">
      <h3>How much their bare days moved apart</h3>
      <p data-testid="twin-measured">{m.sentence}</p>
      <table className="tw-table tabular">
        <caption className="sr-only">Mean days sat bare, before and after, this field and its twin</caption>
        <thead>
          <tr>
            <th scope="col" />
            <th scope="col">{pre}</th>
            <th scope="col">{post}</th>
            <th scope="col">Change</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <th scope="row">This field</th>
            <td>{cell(m.this.before)}</td>
            <td>{cell(m.this.after)}</td>
            <td>{signed(m.this.change)}</td>
          </tr>
          <tr>
            <th scope="row">Its twin</th>
            <td>{cell(m.twin.before)}</td>
            <td>{cell(m.twin.after)}</td>
            <td>{signed(m.twin.change)}</td>
          </tr>
          <tr className="tw-table-diff">
            <th scope="row">This minus twin</th>
            <td>{signed(m.this.before !== null && m.twin.before !== null ? m.this.before - m.twin.before : null)}</td>
            <td>{signed(m.level_gap_after)}</td>
            <td>{signed(diff)}</td>
          </tr>
        </tbody>
      </table>
      <svg
        className="tw-bars"
        viewBox={`0 0 ${axis.width} ${H}`}
        role="img"
        aria-label={`Change in bare days, this field minus its twin: ${signed(diff)} days a year for this pair${est ? `; ${signed(est.effect)} on average across ${comp.n_pairs} pairs, 95% interval ${signed(est.ci_low)} to ${signed(est.ci_high)}` : ""}`}
        data-testid="twin-bar"
        data-diff={diff ?? ""}
      >
        {axis.ticks.map((t) => (
          <g key={t}>
            <line x1={axis.x(t)} x2={axis.x(t)} y1={top} y2={axisY} className="tw-grid-line" />
            <text x={axis.x(t)} y={axisY + 14} textAnchor="middle" className="tw-tick">
              {t === 0 ? "0" : signed(t, 0)}
            </text>
          </g>
        ))}
        <line x1={axis.zeroX} x2={axis.zeroX} y1={top} y2={axisY} className="tw-zero" />
        {rows.map((r) => (
          <g key={r.label}>
            <text x={axis.left} y={r.y + 12} className="tw-row-label">
              {r.label}
            </text>
            <text x={axis.right} y={r.y + 12} textAnchor="end" className="tw-row-value">
              {r.value}
            </text>
          </g>
        ))}
        {diff !== null && <rect {...bar(0, diff)} y={top + 20} height="14" className="tw-bar-pair" data-testid="twin-bar-pair" />}
        {spread && spread.p25 !== null && spread.p75 !== null && (
          <>
            <rect {...bar(spread.p25, spread.p75)} y={top + rowH + 20} height="14" className="tw-bar-spread" />
            {spread.median !== null && <line x1={axis.x(spread.median)} x2={axis.x(spread.median)} y1={top + rowH + 17} y2={top + rowH + 37} className="tw-median" />}
          </>
        )}
        {est && est.effect !== null && est.ci_low !== null && est.ci_high !== null && (
          <g data-testid="twin-bar-interval">
            <line x1={axis.x(est.ci_low)} x2={axis.x(est.ci_high)} y1={top + 2 * rowH + 27} y2={top + 2 * rowH + 27} className="tw-ci" />
            <line x1={axis.x(est.ci_low)} x2={axis.x(est.ci_low)} y1={top + 2 * rowH + 21} y2={top + 2 * rowH + 33} className="tw-ci" />
            <line x1={axis.x(est.ci_high)} x2={axis.x(est.ci_high)} y1={top + 2 * rowH + 21} y2={top + 2 * rowH + 33} className="tw-ci" />
            <circle cx={axis.x(est.effect)} cy={top + 2 * rowH + 27} r="4.5" className="tw-ci-dot" />
          </g>
        )}
        <text x={axis.left} y={H - 2} className="tw-tick">
          ← this field fewer bare days
        </text>
        <text x={axis.right} y={H - 2} textAnchor="end" className="tw-tick">
          this field more bare days →
        </text>
      </svg>
      <p className="muted tw-cap">
        Change in days a year (this field minus its twin), from {pre} to {post}. One pair alone is noisy: the grey band shows how far the
        other pairs of this comparison spread. The dot and line are the average of all pairs and its 95% interval.
      </p>
      <p data-testid="twin-estimate">{comp.sentence}</p>
    </section>
  );
}

// ---------------------------------------------------------------------------------------------
// How many other pairs agree
// ---------------------------------------------------------------------------------------------

function Agree({ data }: { data: TwinAnswer }) {
  const comp = data.comparison;
  if (!comp) return null;
  const a = comp.agreement;
  const s = agreementShares(a);
  const pct = (v: number) => `${(v * 100).toFixed(2)}%`;
  return (
    <section className="tw-block" data-testid="twin-agree-block">
      <h3>Do the other pairs agree?</h3>
      <p className="tw-agree" data-testid="twin-agree">
        {a.sentence}
      </p>
      {a.other_pairs > 0 && a.this_pair_direction !== null && (
        <>
          <div
            className="tw-share"
            role="img"
            aria-label={`${a.same_way} of ${a.other_pairs} other pairs point the same way, ${a.opposite_way} the other way, ${a.no_change} show no change`}
          >
            <span className="tw-share-same" style={{ width: pct(s.same) }} />
            <span className="tw-share-opposite" style={{ width: pct(s.opposite) }} />
            <span className="tw-share-none" style={{ width: pct(s.none) }} />
          </div>
          <p className="tw-share-key muted">
            <span>
              <i className="tw-sw tw-sw--same" aria-hidden="true" />
              same way {a.same_way}
            </span>
            <span>
              <i className="tw-sw tw-sw--opposite" aria-hidden="true" />
              other way {a.opposite_way}
            </span>
            {a.no_change > 0 && (
              <span>
                <i className="tw-sw tw-sw--none" aria-hidden="true" />
                no change {a.no_change}
              </span>
            )}
          </p>
        </>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------------------------
// How far to trust it: the placebo
// ---------------------------------------------------------------------------------------------

function Trust({ data }: { data: TwinAnswer }) {
  const p = data.comparison?.placebo;
  if (!p) return null;
  return (
    <section className="tw-block tw-trust" data-testid="twin-placebo">
      <h3>How far to trust it</h3>
      <p>{p.sentence}</p>
      {p.shuffled_labels && p.shuffled_labels.p_value !== null && (
        <p className="muted">
          Shuffling which neighbour grew which rotation gave an average this large in {(p.shuffled_labels.p_value * 100).toFixed(1)}% of {p.shuffled_labels.n_shuffles} shuffles.
        </p>
      )}
    </section>
  );
}

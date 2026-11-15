// "Your neighbors": look-alike fields nearby that grew different crops from the switch year on,
// side by side with this field (crop strips + one line per field for the outcome, by default the
// days a year the soil sits bare). Data: GET /field/{id}/neighbors (api/causal.py).
// Every year and every "better / worse" comes from the response: the years from `design`, the
// direction from `outcome.higher_is_better` (bare days: fewer is better).
// Each pair gets two separate sentences, because they can point opposite ways: where the two fields
// stood in the "after" years (a level; the "better protected / more exposed" tag belongs to this
// one only) and how much each changed since the "before" years (a change; no tag).
// Self-contained so the field panel only needs one line to show it.
import type { CSSProperties } from "react";
import { getJson } from "../api";
import { cropBackground, cropLabel, cropPalette } from "../crops";
import { useResource } from "../hooks/useResource";
import {
  afterGap,
  changeText,
  chartGeometry,
  effectDecimals,
  effectReading,
  formatSigned,
  levelText,
  rotationHint,
  rotationLabel,
  sharedHistoryPct,
  unitsText,
  valueDecimals,
  verdictLabel,
  verdictOf,
  yearSpan,
} from "../lib/neighbors";
import type { FieldNeighbors, Neighbor, Outcome, SoilPoint } from "../lib/neighbors";
import { ErrorNote } from "./FieldPanel";
import "./Neighbors.css";

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null;

function loadNeighbors(fieldId: string, signal: AbortSignal) {
  return getJson<FieldNeighbors>(
    `/field/${encodeURIComponent(fieldId)}/neighbors`,
    (b) => isObject(b) && b.field_id === fieldId && Array.isArray(b.neighbors),
    signal,
  );
}

export function Neighbors({ fieldId }: { fieldId: string }) {
  const [res, retry] = useResource(fieldId, loadNeighbors);
  const isFake = res.status === "ready" && res.data.is_fake;

  return (
    <section className="section" aria-labelledby="section-neighbors" data-testid="neighbors" data-state={res.status}>
      <div className="section-head">
        <h3 id="section-neighbors">Your neighbors</h3>
        <p className="section-meta">
          {isFake ? (
            <span
              className="badge badge--demo"
              data-testid="neighbors-fake"
              title="The soil numbers are made up because the satellite soil table is missing"
            >
              Fake soil data · not findings
            </span>
          ) : res.status === "ready" ? (
            `Look-alike fields nearby that grew different crops ${grewWhen(res.data)}`
          ) : (
            "Look-alike fields nearby"
          )}
        </p>
      </div>
      {res.status === "loading" && (
        <>
          <div className="skeleton" style={{ height: 220 }} aria-hidden="true" />
          <p className="sr-only" role="status">
            Loading neighbors…
          </p>
        </>
      )}
      {res.status === "error" &&
        (res.notFound ? null : (
          <ErrorNote
            title="Could not load neighbors"
            message={res.message}
            onRetry={res.message.includes("make causal") || res.message.includes("not measured in") ? undefined : retry}
          />
        ))}
      {res.status === "ready" && <NeighborsBody data={res.data} />}
    </section>
  );
}

/** "in 2020–2024", from the run's rotation years (older responses: "from 2020"). */
function grewWhen(data: FieldNeighbors): string {
  return data.design ? `in ${yearSpan(data.design.rotation_years)}` : `from ${data.switch_year}`;
}

function NeighborsBody({ data }: { data: FieldNeighbors }) {
  if (data.neighbors.length === 0) {
    return (
      <div className="nb-empty" data-testid="neighbors-empty">
        <p className="muted">{data.reason ?? "No look-alike neighbors for this field."}</p>
        {data.examples.length > 0 && (
          <p className="nb-examples">
            Fields with a close look-alike:{" "}
            {data.examples.map((id, i) => (
              <span key={id}>
                {i > 0 && ", "}
                <a href={`#${encodeURIComponent(id)}`} className="tabular">
                  {id}
                </a>
              </span>
            ))}
          </p>
        )}
      </div>
    );
  }
  const d = data.design;
  return (
    <>
      <p className="nb-intro">
        Nearby fields that matched this one in mapped soil, slope{d ? `, ${yearSpan(d.history_years)} crops` : ", past crops"}
        {d ? ` and where they stood in ${yearSpan(d.baseline_years)}` : " and starting level"}, and grew different crops{" "}
        {grewWhen(data)}.
        {d
          ? ` They were close in ${yearSpan(d.baseline_years)} only: by ${yearSpan(d.pre_years)} some pairs were already far apart, and the numbers under each chart show it.`
          : ""}
      </p>
      {data.outcome?.note && (
        <p className="nb-note muted" data-testid="neighbors-note">
          {data.outcome.note}
        </p>
      )}
      <ol className="nb-list">
        {data.neighbors.map((n) => (
          <NeighborCard key={n.pair_id} data={data} n={n} />
        ))}
      </ol>
    </>
  );
}

function NeighborCard({ data, n }: { data: FieldNeighbors; n: Neighbor }) {
  const outcome = data.outcome;
  const shared = sharedHistoryPct(n.similarity.history_distance);
  const palette = cropPalette([...Object.values(data.crop_history), ...Object.values(n.crop_history)]);
  return (
    <li className="nb-card" data-testid="neighbor" data-neighbor-id={n.field_id}>
      <div className="nb-head">
        <a href={`#${encodeURIComponent(n.field_id)}`} className="nb-id tabular">
          {n.field_id}
        </a>
        <span className="muted tabular">{n.distance_km !== null && `${n.distance_km.toFixed(1)} km away`}</span>
      </div>
      <p className="nb-alike muted">
        {[
          n.similarity.slope_diff !== null && `slope within ${n.similarity.slope_diff.toFixed(1)} pts`,
          n.similarity.ssurgo_om_diff !== null && `mapped soil OM within ${n.similarity.ssurgo_om_diff.toFixed(2)} pts`,
          shared !== null && `${shared}% same crops ${data.design ? yearSpan(data.design.history_years) : "before"}`,
        ]
          .filter(Boolean)
          .join(" · ")}
      </p>

      <div className="nb-rows">
        <Row label="This field" rotation={n.this_rotation} history={data.crop_history} palette={palette} switchYear={data.switch_year} tone="self" />
        <Row label="Neighbor" rotation={n.rotation} history={n.crop_history} palette={palette} switchYear={data.switch_year} tone="other" />
      </div>

      {outcome && (
        <SoilChart self={data.soil} other={n.soil} switchYear={data.switch_year} outcome={outcome} />
      )}

      {outcome && n.divergence !== null && <Takeaway data={data} n={n} outcome={outcome} />}
    </li>
  );
}

function Row({
  label,
  rotation,
  history,
  palette,
  switchYear,
  tone,
}: {
  label: string;
  rotation: string;
  history: Record<string, string>;
  palette: ReadonlyMap<string, string>;
  switchYear: number;
  tone: "self" | "other";
}) {
  const years = Object.keys(history).map(Number).filter(Number.isInteger);
  const first = Math.min(...years);
  const last = Math.max(...years);
  const cells: number[] = [];
  for (let y = first; y <= last; y++) cells.push(y);
  return (
    <div className={`nb-row nb-row--${tone}`}>
      <p className="nb-row-label">
        <span className="nb-key" aria-hidden="true" />
        {label}{" "}
        <span className="tag" title={rotationHint(rotation) ?? undefined}>
          {rotationLabel(rotation)}
        </span>
      </p>
      <ol className="nb-strip" style={{ "--cells": cells.length } as CSSProperties} aria-label={`${label}: crops ${first} to ${last}`}>
        {cells.map((y) => {
          const crop = history[String(y)];
          return (
            <li
              key={y}
              className={`nb-cell${y === switchYear ? " is-switch" : ""}${crop ? "" : " is-empty"}`}
              style={crop ? { background: cropBackground(crop, palette) } : undefined}
              title={`${y}: ${crop ? cropLabel(crop) : "no data"}`}
            />
          );
        })}
      </ol>
    </div>
  );
}

const W = 360;
const H = 96;

function SoilChart({ self, other, switchYear, outcome }: { self: SoilPoint[]; other: SoilPoint[]; switchYear: number; outcome: Outcome }) {
  if (self.length === 0 && other.length === 0) return null;
  const g = chartGeometry([self, other], W, H, 6);
  const sx = g.x(switchYear) - (g.x(switchYear) - g.x(switchYear - 1)) / 2;
  const decimals = valueDecimals(outcome);
  return (
    <figure className="nb-chart">
      <figcaption className="nb-chart-cap">
        <span>
          {outcome.label}
          <span className="muted"> · {outcome.higher_is_better ? "higher" : "lower"} is better</span>
        </span>
        <span className="muted tabular">
          {g.min.toFixed(decimals)}–{g.max.toFixed(decimals)}
        </span>
      </figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${outcome.label} by year, this field and its neighbor`} preserveAspectRatio="none">
        <line x1={sx} x2={sx} y1={0} y2={H} className="nb-switch" />
        <path d={g.paths[1]} className="nb-line nb-line--other" />
        <path d={g.paths[0]} className="nb-line nb-line--self" />
      </svg>
      <div className="nb-axis tabular muted">
        <span>{g.firstYear}</span>
        <span>compared from {switchYear}</span>
        <span>{g.lastYear}</span>
      </div>
    </figure>
  );
}

function Takeaway({ data, n, outcome }: { data: FieldNeighbors; n: Neighbor; outcome: Outcome }) {
  const d = n.divergence ?? 0;
  // the tag is about the "after" level only; the change (d) can point the other way
  const gap = afterGap(n);
  const verdict = verdictOf(gap, outcome);
  const e = n.comparison_effect;
  const design = data.design;
  const vd = valueDecimals(outcome);
  const fmt = (v: number | null | undefined) => (v === null || v === undefined ? "?" : v.toFixed(vd));
  const change = (before: number | null, after: number | null) =>
    before === null || after === null ? null : formatSigned(after - before, vd);
  const changeThis = n.before && n.after ? change(n.before.this, n.after.this) : null;
  const changeOther = n.before && n.after ? change(n.before.neighbor, n.after.neighbor) : null;
  const ed = effectDecimals(outcome);
  return (
    <div className="nb-takeaway">
      {n.before && n.after && (
        <p className="tabular" data-testid="neighbor-levels">
          {design ? `${yearSpan(design.pre_years)} → ${yearSpan(design.post_years)}` : "Before → after"}: this field{" "}
          {fmt(n.before.this)} → {fmt(n.after.this)}, look-alike {fmt(n.before.neighbor)} → {fmt(n.after.neighbor)}{" "}
          {unitsText(outcome)}
        </p>
      )}
      {gap !== null && (
        <p data-testid="neighbor-level">
          {design ? `In ${yearSpan(design.post_years)}` : "Afterwards"} this field{" "}
          <strong className="tabular">{levelText(gap, outcome)}</strong> its look-alike
          {verdict && (
            <>
              {" "}
              <span className={`tag nb-verdict nb-verdict--${verdict}`}>{verdictLabel(verdict, outcome)}</span>
            </>
          )}
        </p>
      )}
      <p data-testid="neighbor-divergence">
        Change since {design ? yearSpan(design.pre_years) : "before"}:
        {changeThis !== null && changeOther !== null && (
          <span className="tabular">
            {" "}
            this field {changeThis}, look-alike {changeOther} {unitsText(outcome)}.
          </span>
        )}{" "}
        This field's change was <strong className="tabular">{changeText(d, outcome)}</strong>
        {changeText(d, outcome) === "the same" ? " as" : " than"} its look-alike's.
      </p>
      {e && e.effect !== null && e.ci_low !== null && e.ci_high !== null && (
        <p className="muted" data-testid="neighbor-effect">
          Across {e.n_pairs} look-alike pairs, {rotationLabel(e.a).toLowerCase()} vs {rotationLabel(e.b).toLowerCase()},
          the same difference in change averages{" "}
          <span className="tabular">
            {formatSigned(e.effect, ed)} {unitsText(outcome)} (95% CI {formatSigned(e.ci_low, ed)} to{" "}
            {formatSigned(e.ci_high, ed)})
          </span>
          {" — "}
          {effectReading(e, outcome)}
          {e.ci_excludes_zero && e.from_crops && e.from_crops.effect !== null
            ? `; the crops grown would predict ${formatSigned(e.from_crops.effect, ed)} on their own`
            : ""}
          {data.is_fake ? " · fake soil data, not a finding" : ""}
        </p>
      )}
    </div>
  );
}

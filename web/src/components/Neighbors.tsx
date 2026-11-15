// "Your neighbors": look-alike fields nearby that were farmed differently after 2016, side by side
// with this field (crop strips + soil score lines). Data: GET /field/{id}/neighbors (api/causal.py).
// Self-contained so the field panel only needs one line to show it.
import type { CSSProperties } from "react";
import { getJson } from "../api";
import { cropBackground, cropLabel, cropPalette } from "../crops";
import { useResource } from "../hooks/useResource";
import { chartGeometry, formatSigned, rotationLabel, sharedHistoryPct } from "../lib/neighbors";
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
    <section className="section" aria-labelledby="section-neighbors" data-testid="neighbors">
      <div className="section-head">
        <h3 id="section-neighbors">Your neighbors</h3>
        <p className="section-meta">
          {isFake ? (
            <span className="badge badge--demo" title="The soil scores are made up until the satellite soil data is in">
              Fake soil data · not findings
            </span>
          ) : (
            "Look-alike fields nearby, farmed differently since 2016"
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
            onRetry={res.message.includes("make causal") ? undefined : retry}
          />
        ))}
      {res.status === "ready" && <NeighborsBody data={res.data} />}
    </section>
  );
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
  return (
    <>
      <p className="nb-intro">
        Nearby fields that matched this one in soil, slope, starting soil score and 2008–2015 crops, then
        went a different way.
      </p>
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
          shared !== null && `${shared}% same crops 2008–15`,
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

      {outcome && n.divergence !== null && <Takeaway n={n} outcome={outcome} isFake={data.is_fake} />}
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
        {label} <span className="tag">{rotationLabel(rotation)}</span>
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
  return (
    <figure className="nb-chart">
      <figcaption className="nb-chart-cap">
        <span>{outcome.label}</span>
        <span className="muted tabular">
          {g.min.toFixed(2)}–{g.max.toFixed(2)}
        </span>
      </figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${outcome.label} by year, this field and its neighbor`} preserveAspectRatio="none">
        <line x1={sx} x2={sx} y1={0} y2={H} className="nb-switch" />
        <path d={g.paths[1]} className="nb-line nb-line--other" />
        <path d={g.paths[0]} className="nb-line nb-line--self" />
      </svg>
      <div className="nb-axis tabular muted">
        <span>{g.firstYear}</span>
        <span>rotations split {switchYear}</span>
        <span>{g.lastYear}</span>
      </div>
    </figure>
  );
}

function Takeaway({ n, outcome, isFake }: { n: Neighbor; outcome: Outcome; isFake: boolean }) {
  const d = n.divergence ?? 0;
  const better = outcome.higher_is_better ? d > 0 : d < 0;
  const e = n.comparison_effect;
  return (
    <div className="nb-takeaway">
      <p>
        Since the split, this field's score rose <strong className="tabular">{Math.abs(d).toFixed(2)}</strong>{" "}
        {outcome.units} {d >= 0 ? "more" : "less"} than its look-alike's{" "}
        <span className={`tag nb-verdict nb-verdict--${better ? "better" : "worse"}`}>{better ? "better" : "worse"}</span>
      </p>
      {e && e.effect !== null && e.ci_low !== null && e.ci_high !== null && (
        <p className="muted">
          Across {e.n_pairs} look-alike pairs, {rotationLabel(e.a).toLowerCase()} vs {rotationLabel(e.b).toLowerCase()}:{" "}
          <span className="tabular">
            {formatSigned(e.effect)} (95% CI {formatSigned(e.ci_low)} to {formatSigned(e.ci_high)})
          </span>
          {e.ci_excludes_zero ? "" : " — not distinguishable from zero"}
          {isFake ? " · fake soil data, not a finding" : ""}
        </p>
      )}
    </div>
  );
}

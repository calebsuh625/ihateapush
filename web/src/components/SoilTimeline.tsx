import { useMemo, useState } from "react";
import type { CSSProperties, KeyboardEvent } from "react";
import { cropBackground, cropPalette } from "../crops";
import { formatOneDecimal, plural } from "../lib/format";
import type { FieldSoil, SoilYear } from "../types";

/** Fixed scale for the organic-matter row, in %. Values above it sit on the top edge. */
export const OM_SCALE_MAX = 5;
/** Fixed scale for the bare-days row: a whole year. */
const BARE_SCALE_MAX = 366;
const DOT = 10;

type Signal = "om_pred" | "bare_days";

interface Column {
  year: number;
  crop: string | null;
  /** null = the soil file has no row for this year */
  soil: SoilYear | null;
}

/** One column per year over the SAME range as the crop-history strip, so the two line up. */
function buildColumns(history: Record<string, string>, soil: FieldSoil): Column[] {
  const years = Object.keys(history)
    .map(Number)
    .filter((y) => Number.isInteger(y))
    .sort((a, b) => a - b);
  const first = years[0];
  const last = years[years.length - 1];
  if (first === undefined || last === undefined) return [];
  const byYear = new Map(soil.years.map((row) => [row.year, row]));
  const columns: Column[] = [];
  for (let year = first; year <= last; year++) {
    columns.push({ year, crop: history[String(year)] ?? null, soil: byYear.get(year) ?? null });
  }
  return columns;
}

const labelStep = (cellCount: number) => Math.max(1, Math.ceil(cellCount / 9));
const clamp01 = (n: number) => Math.max(0, Math.min(1, n));

export function confidenceWord(confidence: number): "low" | "medium" | "high" {
  if (confidence < 0.34) return "low";
  if (confidence < 0.67) return "medium";
  return "high";
}

const views = (n: number) => {
  const rounded = Math.round(n);
  return `${rounded} ${plural(rounded, "bare view")}`;
};
const days = (n: number) => {
  const rounded = Math.round(n);
  return `${rounded} ${plural(rounded, "day")}`;
};

/** "2019 · 2.4% · medium confidence · 3 bare views" */
export function describeOm(column: Column): string {
  const parts: string[] = [String(column.year)];
  const row = column.soil;
  if (!row) return [...parts, "no soil data"].join(" · ");
  if (row.om_pred === null) {
    parts.push("no estimate (no clear bare-soil view)", views(row.n_obs));
    return parts.join(" · ");
  }
  parts.push(`${formatOneDecimal(row.om_pred)}%`, `${confidenceWord(row.confidence)} confidence`, views(row.n_obs));
  return parts.join(" · ");
}

/** "2019 · corn · 142 days bare" */
export function describeBare(column: Column): string {
  const parts: string[] = [String(column.year), column.crop ?? "no crop on record"];
  const row = column.soil;
  if (!row) return [...parts, "no soil data"].join(" · ");
  parts.push(row.bare_days === null ? "no estimate (too few clear views)" : `${days(row.bare_days)} bare`);
  return parts.join(" · ");
}

/** "steady (+2 days per decade)", "rising (+14 days per decade)". Steady = under 5 days per decade. */
export function bareTrendText(trend: number): string {
  const number = `${trend >= 0 ? "+" : "−"}${Math.round(Math.abs(trend))} days per decade`;
  if (Math.abs(trend) < 5) return `steady (${number})`;
  return `${trend > 0 ? "rising" : "falling"} (${number})`;
}

interface Props {
  history: Record<string, string>;
  soil: FieldSoil;
}

export function SoilTimelineChart({ history, soil }: Props) {
  const columns = useMemo(() => buildColumns(history, soil), [history, soil]);
  const palette = useMemo(() => cropPalette(Object.values(history)), [history]);
  const [active, setActive] = useState<{ row: Signal; index: number } | null>(null);

  if (columns.length === 0) return <p className="muted">No crop history to line the soil years up with.</p>;

  const order: Signal[] = soil.recommended === "bare_days" ? ["bare_days", "om_pred"] : ["om_pred", "bare_days"];
  const tagFor = (signal: Signal): "good" | "warn" | null =>
    soil.recommended === null ? null : soil.recommended === signal ? "good" : "warn";

  const step = labelStep(columns.length);
  const first = columns[0]!;
  const last = columns[columns.length - 1]!;
  const gridStyle = { "--cells": columns.length } as CSSProperties;
  const activeColumn = active === null ? undefined : columns[active.index];
  const tipPosition = active === null ? 0 : ((active.index + 0.5) / columns.length) * 100;

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const end = columns.length - 1;
    const from = active?.index ?? end;
    let to: number | null = null;
    if (event.key === "ArrowRight") to = Math.min(end, from + 1);
    else if (event.key === "ArrowLeft") to = Math.max(0, from - 1);
    else if (event.key === "Home") to = 0;
    else if (event.key === "End") to = end;
    if (to === null) return;
    event.preventDefault();
    setActive({ row: active?.row ?? order[0]!, index: to });
  };

  const hover = (row: Signal, index: number) => ({
    onPointerEnter: () => setActive({ row, index }),
    onClick: () => setActive({ row, index }),
  });
  const colClass = (row: Signal, index: number, extra: string) =>
    active !== null && active.row === row && active.index === index ? `sat-col ${extra} is-active` : `sat-col ${extra}`;

  const { summary } = soil;
  const noise = soil.om_noise_sd;
  const mean = summary.om_pred_mean;
  const ssurgo = soil.ssurgo_om_pct;
  const showBand = noise !== null && noise > 0 && mean !== null;
  const bandLow = showBand ? clamp01((mean - noise) / OM_SCALE_MAX) : 0;
  const bandHigh = showBand ? clamp01((mean + noise) / OM_SCALE_MAX) : 0;
  const trend = summary.bare_days_trend_per_decade;

  const tag = (signal: Signal) => {
    const kind = tagFor(signal);
    if (kind === null) return null;
    return kind === "good" ? (
      <span className="soil-tag soil-tag-good" data-testid="soil-tag" data-signal={signal}>
        more reliable
      </span>
    ) : (
      <span className="soil-tag soil-tag-warn" data-testid="soil-tag" data-signal={signal}>
        noisy - use with caution
      </span>
    );
  };

  const omBlock = (
    <div key="om_pred" className="soil-block" data-testid="soil-om-block" data-signal="om_pred">
      <div className="soil-head">
        <p className="sat-label">
          Estimated organic matter <span className="muted">· % of topsoil, 0 to {OM_SCALE_MAX}</span>
        </p>
        {tag("om_pred")}
      </div>
      <div className="sat-row soil-row-om" style={gridStyle} aria-hidden="true" data-testid="soil-om">
        {showBand && (
          <div
            className="soil-band"
            data-testid="soil-band"
            style={{ bottom: `${bandLow * 100}%`, height: `${(bandHigh - bandLow) * 100}%` }}
          />
        )}
        {ssurgo !== null && (
          <div
            className="soil-ref"
            data-testid="soil-ref"
            data-value={ssurgo}
            style={{ bottom: `${clamp01(ssurgo / OM_SCALE_MAX) * 100}%` }}
          />
        )}
        {columns.map((column, index) => {
          const value = column.soil?.om_pred;
          return (
            <div
              key={column.year}
              className={colClass("om_pred", index, "soil-om-col")}
              data-testid="soil-om-col"
              data-year={column.year}
              data-state={column.soil === null ? "gap" : value == null ? "null" : "value"}
              {...hover("om_pred", index)}
            >
              {value == null || !column.soil ? null : (
                <span
                  className="soil-dot"
                  data-testid="soil-dot"
                  data-value={value}
                  style={{
                    bottom: `calc(${clamp01(value / OM_SCALE_MAX) * 100}% - ${DOT / 2}px)`,
                    opacity: 0.25 + 0.75 * clamp01(column.soil.confidence),
                  }}
                />
              )}
            </div>
          );
        })}
      </div>
      <p className="soil-legend" data-testid="soil-legend">
        {ssurgo !== null && (
          <span className="soil-key">
            <span className="soil-swatch soil-swatch-ref" aria-hidden="true" />
            <span>
              soil survey <strong className="tabular">{formatOneDecimal(ssurgo)}%</strong>
            </span>
          </span>
        )}
        {showBand && (
          <span className="soil-key">
            <span className="soil-swatch soil-swatch-band" aria-hidden="true" />
            <span>
              typical year-to-year noise, ±<strong className="tabular">{noise.toFixed(2)}</strong> around this field&rsquo;s
              average
            </span>
          </span>
        )}
        <span className="soil-key">
          <span className="soil-swatch soil-swatch-dot" aria-hidden="true" />
          fainter = less confident
        </span>
      </p>
    </div>
  );

  const bareBlock = (
    <div key="bare_days" className="soil-block" data-testid="soil-bare-block" data-signal="bare_days">
      <div className="soil-head">
        <p className="sat-label">
          Bare soil days <span className="muted">· days a year, 0 to {BARE_SCALE_MAX}</span>
        </p>
        {tag("bare_days")}
      </div>
      <div className="sat-row soil-row-bare" style={gridStyle} aria-hidden="true" data-testid="soil-bare">
        {columns.map((column, index) => {
          const value = column.soil?.bare_days;
          return (
            <div
              key={column.year}
              className={colClass("bare_days", index, "soil-bare-col")}
              data-testid="soil-bare-col"
              data-year={column.year}
              data-state={column.soil === null ? "gap" : value == null ? "null" : "value"}
              {...hover("bare_days", index)}
            >
              {column.soil === null ? null : value == null ? (
                <span className="sat-null" />
              ) : (
                <span
                  className="sat-bar"
                  style={{
                    height: `${clamp01(value / BARE_SCALE_MAX) * 100}%`,
                    background: column.crop === null ? "var(--line-strong)" : cropBackground(column.crop, palette),
                  }}
                />
              )}
            </div>
          );
        })}
      </div>
    </div>
  );

  return (
    <div className="satellite soil">
      <div
        className="sat-chart"
        role="group"
        tabIndex={0}
        aria-label={`Soil condition each year, ${first.year} to ${last.year}. Use the left and right arrow keys to step through the years.`}
        data-testid="soil-chart"
        onKeyDown={onKeyDown}
        onFocus={() => setActive((current) => current ?? { row: order[0]!, index: columns.length - 1 })}
        onBlur={() => setActive(null)}
        onPointerLeave={(event) => {
          if (event.pointerType === "mouse") setActive(null);
        }}
      >
        {order.map((signal) => (signal === "om_pred" ? omBlock : bareBlock))}

        <div className="strip-years sat-years" style={gridStyle} aria-hidden="true">
          {columns.map((column, index) =>
            index % step === 0 ? (
              <span key={column.year} style={{ gridColumn: index + 1 }}>
                {column.year}
              </span>
            ) : null,
          )}
        </div>

        {activeColumn && active && (
          <div
            className="strip-tip sat-tip"
            role="tooltip"
            style={{ left: `${tipPosition}%`, transform: `translateX(-${tipPosition}%)` }}
            data-testid="soil-tip"
          >
            {active.row === "om_pred" ? describeOm(activeColumn) : describeBare(activeColumn)}
          </div>
        )}

        <div className="sr-only">
          <table data-testid="soil-table">
            <caption>Soil numbers for each year</caption>
            <thead>
              <tr>
                <th scope="col">Year</th>
                <th scope="col">Crop</th>
                <th scope="col">Estimated organic matter</th>
                <th scope="col">Confidence</th>
                <th scope="col">Bare views</th>
                <th scope="col">Bare soil days</th>
              </tr>
            </thead>
            <tbody>
              {columns.map((column) => (
                <tr key={column.year}>
                  <th scope="row">{column.year}</th>
                  <td>{column.crop ?? "no crop on record"}</td>
                  <td>{column.soil?.om_pred == null ? "no data" : `${formatOneDecimal(column.soil.om_pred)}%`}</td>
                  <td>{column.soil?.om_pred == null ? "no data" : confidenceWord(column.soil.confidence)}</td>
                  <td>{column.soil ? views(column.soil.n_obs) : "no data"}</td>
                  <td>{column.soil?.bare_days == null ? "no data" : days(column.soil.bare_days)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <p className="sat-summary" data-testid="soil-summary">
        {mean === null && summary.bare_days_mean === null ? (
          <span className="muted">No clear satellite views of bare soil in these years.</span>
        ) : (
          <>
            {mean !== null && (
              <>
                Average estimated organic matter <strong className="tabular">{formatOneDecimal(mean)}%</strong>
                {summary.om_pred_years !== null && (
                  <span className="muted">
                    {" "}
                    ({summary.om_pred_years} {plural(summary.om_pred_years, "year")})
                  </span>
                )}
              </>
            )}
            {mean !== null && summary.bare_days_mean !== null && " · "}
            {summary.bare_days_mean !== null && (
              <>
                average <strong className="tabular">{Math.round(summary.bare_days_mean)}</strong> bare days a year
              </>
            )}
            {trend !== null && (
              <>
                {" · "}bare-days trend <strong>{bareTrendText(trend)}</strong>
              </>
            )}
          </>
        )}
      </p>
      {soil.verdict && (
        <p className="sat-note muted" data-testid="soil-verdict">
          {soil.verdict}
        </p>
      )}
    </div>
  );
}

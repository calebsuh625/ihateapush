import { useMemo, useState } from "react";
import type { CSSProperties, KeyboardEvent } from "react";
import { cropBackground, cropPalette } from "../crops";
import { plural } from "../lib/format";
import type { FieldSatellite, SatelliteYear } from "../types";

interface Column {
  year: number;
  /** that year's crop from the crop history, null when not on record */
  crop: string | null;
  /** null = the satellite file has no row for this year */
  sat: SatelliteYear | null;
}

/** One column per year over the SAME range as the crop-history strip, so the two line up. */
function buildColumns(history: Record<string, string>, satellite: FieldSatellite): Column[] {
  const years = Object.keys(history)
    .map(Number)
    .filter((y) => Number.isInteger(y))
    .sort((a, b) => a - b);
  const first = years[0];
  const last = years[years.length - 1];
  if (first === undefined || last === undefined) return [];
  const byYear = new Map(satellite.years.map((row) => [row.year, row]));
  const columns: Column[] = [];
  for (let year = first; year <= last; year++) {
    columns.push({ year, crop: history[String(year)] ?? null, sat: byYear.get(year) ?? null });
  }
  return columns;
}

const labelStep = (cellCount: number) => Math.max(1, Math.ceil(cellCount / 9));

const pct = (fraction: number) => `${Math.round(fraction * 100)}%`;
const greenness = (n: number) => n.toFixed(2);
const views = (n: number) => {
  const rounded = Math.round(n);
  return `${rounded} ${plural(rounded, "view")}`;
};

/** "2014 · winter wheat · peak greenness 0.62 · green 41% of clear views · 13 views" */
export function describeYear(column: Column): string {
  const parts: string[] = [String(column.year), column.crop ?? "no crop on record"];
  const row = column.sat;
  if (!row) {
    parts.push("no satellite data");
    return parts.join(" · ");
  }
  parts.push(row.ndvi_peak === null ? "peak greenness: no data" : `peak greenness ${greenness(row.ndvi_peak)}`);
  parts.push(row.cover_frac === null ? "green cover: no data" : `green ${pct(row.cover_frac)} of clear views`);
  parts.push(views(row.n_obs));
  return parts.join(" · ");
}

/** "rising (+0.03 per decade)", or "steady" when the change is under 0.02 per decade. */
export function trendText(trend: number): string {
  const number = `${trend >= 0 ? "+" : "−"}${Math.abs(trend).toFixed(2)} per decade`;
  if (Math.abs(trend) < 0.02) return `steady (${number})`;
  return `${trend > 0 ? "rising" : "falling"} (${number})`;
}

interface SatelliteChartProps {
  history: Record<string, string>;
  satellite: FieldSatellite;
}

export function SatelliteChart({ history, satellite }: SatelliteChartProps) {
  const columns = useMemo(() => buildColumns(history, satellite), [history, satellite]);
  const palette = useMemo(() => cropPalette(Object.values(history)), [history]);
  const [active, setActive] = useState<number | null>(null);

  if (columns.length === 0) return <p className="muted">No crop history to line the satellite years up with.</p>;

  const step = labelStep(columns.length);
  const first = columns[0]!;
  const last = columns[columns.length - 1]!;
  const activeColumn = active === null ? undefined : columns[active];
  const tipPosition = active === null ? 0 : ((active + 0.5) / columns.length) * 100;
  const gridStyle = { "--cells": columns.length } as CSSProperties;

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const end = columns.length - 1;
    const from = active ?? end;
    let to: number | null = null;
    if (event.key === "ArrowRight") to = Math.min(end, from + 1);
    else if (event.key === "ArrowLeft") to = Math.max(0, from - 1);
    else if (event.key === "Home") to = 0;
    else if (event.key === "End") to = end;
    if (to === null) return;
    event.preventDefault();
    setActive(to);
  };

  const hover = (index: number) => ({
    onPointerEnter: () => setActive(index),
    onClick: () => setActive(index),
  });
  const rowClass = (index: number, extra: string) => (index === active ? `sat-col ${extra} is-active` : `sat-col ${extra}`);

  const { summary } = satellite;
  const trend = summary.ndvi_peak_trend_per_decade;

  return (
    <div className="satellite">
      <div
        className="sat-chart"
        role="group"
        tabIndex={0}
        aria-label={`Satellite greenness each year, ${first.year} to ${last.year}. Use the left and right arrow keys to step through the years.`}
        data-testid="sat-chart"
        onKeyDown={onKeyDown}
        onFocus={() => setActive((current) => current ?? columns.length - 1)}
        onBlur={() => setActive(null)}
        onPointerLeave={(event) => {
          if (event.pointerType === "mouse") setActive(null);
        }}
      >
        <p className="sat-label">
          Peak greenness <span className="muted">· 0 to 1</span>
        </p>
        <div className="sat-row sat-row-peak" style={gridStyle} aria-hidden="true" data-testid="sat-peak">
          {columns.map((column, index) => {
            const peak = column.sat?.ndvi_peak;
            return (
              <div
                key={column.year}
                className={rowClass(index, "sat-peak-col")}
                data-testid="sat-peak-col"
                data-year={column.year}
                data-state={column.sat === null ? "gap" : peak == null ? "null" : "value"}
                {...hover(index)}
              >
                {column.sat === null ? null : peak == null ? (
                  <span className="sat-null" />
                ) : (
                  <span
                    className="sat-bar"
                    style={{
                      height: `${Math.max(0, Math.min(1, peak)) * 100}%`,
                      background: column.crop === null ? "var(--line-strong)" : cropBackground(column.crop, palette),
                    }}
                  />
                )}
              </div>
            );
          })}
        </div>

        <p className="sat-label">
          Green cover <span className="muted">· share of clear views that were green</span>
        </p>
        <div className="sat-row sat-row-cover" style={gridStyle} aria-hidden="true" data-testid="sat-cover">
          {columns.map((column, index) => {
            const cover = column.sat?.cover_frac;
            return (
              <div
                key={column.year}
                className={rowClass(index, "sat-cover-col")}
                data-testid="sat-cover-col"
                data-year={column.year}
                data-state={column.sat === null ? "gap" : cover == null ? "null" : "value"}
                {...hover(index)}
              >
                {column.sat === null ? null : cover == null ? (
                  <span className="sat-null" />
                ) : (
                  <span className="sat-bar sat-bar-cover" style={{ height: `${Math.max(0, Math.min(1, cover)) * 100}%` }} />
                )}
              </div>
            );
          })}
        </div>

        <div className="strip-years sat-years" style={gridStyle} aria-hidden="true">
          {columns.map((column, index) =>
            index % step === 0 ? (
              <span key={column.year} style={{ gridColumn: index + 1 }}>
                {column.year}
              </span>
            ) : null,
          )}
        </div>

        {activeColumn && (
          <div
            className="strip-tip sat-tip"
            role="tooltip"
            style={{ left: `${tipPosition}%`, transform: `translateX(-${tipPosition}%)` }}
            data-testid="sat-tip"
          >
            {describeYear(activeColumn)}
          </div>
        )}

        <div className="sr-only">
        <table data-testid="sat-table">
          <caption>Satellite numbers for each year</caption>
          <thead>
            <tr>
              <th scope="col">Year</th>
              <th scope="col">Crop</th>
              <th scope="col">Peak greenness</th>
              <th scope="col">Green cover</th>
              <th scope="col">Clear views</th>
            </tr>
          </thead>
          <tbody>
            {columns.map((column) => (
              <tr key={column.year}>
                <th scope="row">{column.year}</th>
                <td>{column.crop ?? "no crop on record"}</td>
                <td>{column.sat?.ndvi_peak == null ? "no data" : greenness(column.sat.ndvi_peak)}</td>
                <td>{column.sat?.cover_frac == null ? "no data" : pct(column.sat.cover_frac)}</td>
                <td>{column.sat ? views(column.sat.n_obs) : "no data"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      </div>

      <p className="sat-summary" data-testid="sat-summary">
        {summary.ndvi_peak_mean === null && summary.cover_frac_mean === null ? (
          <span className="muted">No clear satellite views in these years.</span>
        ) : (
          <>
            {summary.ndvi_peak_mean !== null && (
              <>
                Average peak greenness <strong className="tabular">{greenness(summary.ndvi_peak_mean)}</strong>
              </>
            )}
            {summary.ndvi_peak_mean !== null && summary.cover_frac_mean !== null && " · "}
            {summary.cover_frac_mean !== null && (
              <>
                average green cover <strong className="tabular">{pct(summary.cover_frac_mean)}</strong>
              </>
            )}
            {trend !== null && (
              <>
                {" · "}trend <strong>{trendText(trend)}</strong>
              </>
            )}
          </>
        )}
      </p>
      <p className="sat-note muted">
        From Landsat satellite images, 30 m. Greenness is NDVI: 0 is bare soil, 0.8+ is a dense crop.
      </p>
    </div>
  );
}

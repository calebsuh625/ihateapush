import { useMemo, useState } from "react";
import type { CSSProperties, KeyboardEvent } from "react";
import { cropBackground, cropLabel, cropPalette } from "../crops";

interface Cell {
  year: number;
  /** null = no observation that year */
  crop: string | null;
}

interface LegendEntry {
  crop: string | null;
  count: number;
}

/** One cell per year from the first to the last year present; gaps become "no data" cells. */
function buildCells(history: Record<string, string>): Cell[] {
  const years = Object.keys(history)
    .map(Number)
    .filter((y) => Number.isInteger(y))
    .sort((a, b) => a - b);
  const first = years[0];
  const last = years[years.length - 1];
  if (first === undefined || last === undefined) return [];
  const cells: Cell[] = [];
  for (let year = first; year <= last; year++) cells.push({ year, crop: history[String(year)] ?? null });
  return cells;
}

/** Crops in this field, most frequent first (then by name); "no data" last. */
function buildLegend(cells: Cell[]): LegendEntry[] {
  const counts = new Map<string, number>();
  let missing = 0;
  for (const { crop } of cells) {
    if (crop === null) missing++;
    else counts.set(crop, (counts.get(crop) ?? 0) + 1);
  }
  const entries: LegendEntry[] = [...counts]
    .map(([crop, count]) => ({ crop, count }))
    .sort((a, b) => b.count - a.count || a.crop.localeCompare(b.crop));
  if (missing > 0) entries.push({ crop: null, count: missing });
  return entries;
}

const describe = (cell: Cell) => `${cell.year}: ${cell.crop ?? "no data"}`;

/** Label every year when there is room, otherwise every 2nd, 4th, … so labels never collide. */
const labelStep = (cellCount: number) => Math.max(1, Math.ceil(cellCount / 9));

export function Timeline({ history }: { history: Record<string, string> }) {
  const cells = useMemo(() => buildCells(history), [history]);
  const legend = useMemo(() => buildLegend(cells), [cells]);
  // One palette for the strip and its legend, so crops shown together never share a colour.
  const palette = useMemo(() => cropPalette(Object.values(history)), [history]);
  /** Index of the cell whose tooltip is showing. */
  const [active, setActive] = useState<number | null>(null);
  // Picking a legend entry keeps that crop's years and fades the rest — a second way to tell the
  // crops apart that does not depend on colour. Hover previews it, click (or Enter) pins it.
  // The crop is a name, or null for "no data"; `undefined` means nothing is picked.
  const [pinned, setPinned] = useState<string | null | undefined>(undefined);
  const [previewed, setPreviewed] = useState<string | null | undefined>(undefined);
  const spotlight = previewed !== undefined ? previewed : pinned;

  const first = cells[0];
  const last = cells[cells.length - 1];
  if (!first || !last) return <p className="muted">No crop history on record for this field.</p>;

  const step = labelStep(cells.length);
  const activeCell = active === null ? undefined : cells[active];
  // Slide the tooltip along with the cell: left-aligned at the first year, right-aligned at the last.
  const tipPosition = active === null ? 0 : ((active + 0.5) / cells.length) * 100;

  const onKeyDown = (event: KeyboardEvent<HTMLOListElement>) => {
    const end = cells.length - 1;
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

  return (
    <div className="timeline">
      <div className="strip-wrap">
        <ol
          className="strip"
          style={{ "--cells": cells.length } as CSSProperties}
          tabIndex={0}
          aria-label={`Crop grown each year, ${first.year} to ${last.year}. Use the left and right arrow keys to step through the years.`}
          data-testid="timeline"
          onKeyDown={onKeyDown}
          onFocus={() => setActive((current) => current ?? cells.length - 1)}
          onBlur={() => setActive(null)}
          onPointerLeave={(event) => {
            if (event.pointerType === "mouse") setActive(null);
          }}
        >
          {cells.map((cell, index) => {
            const classes = ["strip-cell"];
            if (cell.crop === null) classes.push("is-empty");
            if (index === active) classes.push("is-active");
            if (spotlight !== undefined) classes.push(cell.crop === spotlight ? "is-lit" : "is-dim");
            return (
              <li
                key={cell.year}
                className={classes.join(" ")}
                style={cell.crop === null ? undefined : { background: cropBackground(cell.crop, palette) }}
                data-testid="timeline-cell"
                data-year={cell.year}
                data-crop={cell.crop ?? ""}
                onPointerEnter={() => setActive(index)}
                onClick={() => setActive(index)}
              >
                <span className="sr-only">{describe(cell)}</span>
              </li>
            );
          })}
        </ol>

        <div className="strip-years" style={{ "--cells": cells.length } as CSSProperties} aria-hidden="true">
          {cells.map((cell, index) =>
            index % step === 0 ? (
              <span key={cell.year} style={{ gridColumn: index + 1 }}>
                {cell.year}
              </span>
            ) : null,
          )}
        </div>

        {activeCell && (
          <div
            className="strip-tip"
            role="tooltip"
            style={{ left: `${tipPosition}%`, transform: `translateX(-${tipPosition}%)` }}
            data-testid="timeline-tip"
          >
            <span className="tabular">{activeCell.year}</span>: {activeCell.crop ?? "no data"}
          </div>
        )}
        <span className="sr-only" aria-live="polite">
          {activeCell ? describe(activeCell) : ""}
        </span>
      </div>

      <ul className="legend" aria-label="Crops in this field and how many years each was grown" data-testid="legend">
        {legend.map(({ crop, count }) => (
          <li key={crop ?? "(no data)"}>
            <button
              type="button"
              className="legend-item"
              title="Highlight these years"
              aria-pressed={pinned === crop}
              onClick={() => setPinned((current) => (current === crop ? undefined : crop))}
              onPointerEnter={(event) => {
                if (event.pointerType === "mouse") setPreviewed(crop);
              }}
              onPointerLeave={() => setPreviewed(undefined)}
            >
              <span
                className={crop === null ? "swatch is-empty" : "swatch"}
                style={crop === null ? undefined : { background: cropBackground(crop, palette) }}
                aria-hidden="true"
              />
              <span>{crop === null ? "No data" : cropLabel(crop)}</span>
              <span className="legend-count tabular">
                {count}
                <span className="sr-only"> {count === 1 ? "year" : "years"}</span>
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

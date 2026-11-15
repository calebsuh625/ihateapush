import { useId, useRef, useState } from "react";
import type { PointerEvent } from "react";
import {
  chartScales,
  depthIn,
  feetBand,
  inchesWords,
  limitChoice,
  limitRange,
  limitValue,
  linePath,
  milesText,
  oneDecimal,
  rateWithInterval,
  resetWords,
  spreadLabels,
  winterAt,
} from "../lib/water";
import type { ChartBox } from "../lib/water";
import type { WaterOutlook, WaterWell, WaterWhatIf } from "../types";
import "./Water.css";

const UNAVAILABLE_TEXT: Record<string, string> = {
  not_built: "Groundwater levels have not been built on this server (make groundwater), so there are no wells to show.",
  no_wells_nearby: "Too few wells with a long enough record lie near this field to show a water outlook for it.",
};

interface WaterOutlookBlockProps {
  outlook: WaterOutlook;
  /** The farmer's own pumping limit, inches; null = the planner's. May be ahead of the answer on screen. */
  limitIn: number | null;
  onLimitIn: (value: number | null) => void;
}

/**
 * The water reality check for an irrigated field: what the wells near it have measured, the same
 * rate carried forward (said to be an extrapolation right where it is shown), and the what-if that
 * scores the plans under less water. Every number comes from the plans answer's `water_outlook`.
 */
export function WaterOutlookBlock({ outlook, limitIn, onLimitIn }: WaterOutlookBlockProps) {
  return (
    <div className="water" data-testid="water-outlook" data-available={String(outlook.available)}>
      {outlook.available && outlook.wells ? (
        <>
          <p className="water-sentence" data-testid="water-sentence">
            {outlook.sentence}
          </p>
          <WellChart wells={outlook.wells} />
          <WellTable wells={outlook.wells} />
          {outlook.extrapolation && (
            <div className="water-later" data-testid="water-later">
              <h4>If the measured rate simply continued</h4>
              <p data-testid="water-extrapolation">{outlook.extrapolation}</p>
              {outlook.column && outlook.column.years.length > 0 && (
                <table className="water-table tabular" data-testid="water-column-table">
                  <caption className="sr-only">Water column in the wells with a depth on record, today and if the measured rate continued</caption>
                  <thead>
                    <tr>
                      <th scope="col">Year</th>
                      <th scope="col">Water column, middle well (lowest to highest)</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <th scope="row">Today</th>
                      <td data-testid="water-column-now">{feetBand(outlook.column.now_ft)}</td>
                    </tr>
                    {outlook.column.years.map((entry) => (
                      <tr key={entry.year} data-testid="water-column-year" data-year={entry.year}>
                        <th scope="row">{entry.year}</th>
                        <td>{feetBand(entry.left_ft)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              <p className="water-caveat" data-testid="water-caveat">
                {outlook.caveat}
              </p>
            </div>
          )}
        </>
      ) : (
        <p className="muted" data-testid="water-unavailable">
          {outlook.note ?? UNAVAILABLE_TEXT[outlook.reason ?? "no_wells_nearby"] ?? UNAVAILABLE_TEXT.no_wells_nearby}
        </p>
      )}

      <WhatIf whatIf={outlook.what_if} limitIn={limitIn} onLimitIn={onLimitIn} />

      {outlook.available && (
        <details className="water-about" data-testid="water-about">
          <summary>How this is measured</summary>
          {outlook.rule && <p>{outlook.rule}</p>}
          {outlook.column_note && <p>{outlook.column_note}</p>}
          {outlook.threshold_note && <p data-testid="water-threshold-note">{outlook.threshold_note}</p>}
          {outlook.source && <p className="muted">Source: {outlook.source}.</p>}
        </details>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// "What if I can only pump N inches a year?"
// ---------------------------------------------------------------------------------------------

function WhatIf({ whatIf, limitIn, onLimitIn }: { whatIf: WaterWhatIf; limitIn: number | null; onLimitIn: (value: number | null) => void }) {
  const id = useId();
  const { min, max } = limitRange(whatIf);
  const value = limitValue(limitIn, whatIf);
  const on = limitIn !== null;
  return (
    <div className="water-whatif" role="group" aria-labelledby={`${id}-label`} data-testid="water-whatif" data-active={String(on)}>
      <div className="water-whatif-head">
        <label id={`${id}-label`} htmlFor={`${id}-limit`}>
          What if I can only pump{" "}
          <output className="tabular" htmlFor={`${id}-limit`} data-testid="water-limit-value">
            {inchesWords(value)}
          </output>{" "}
          a year?
        </label>
        {on && (
          <button type="button" className="link-button" data-testid="water-limit-reset" onClick={() => onLimitIn(null)}>
            {resetWords(whatIf)}
          </button>
        )}
      </div>
      <input
        id={`${id}-limit`}
        type="range"
        min={min}
        max={max}
        step={1}
        value={value}
        data-testid="water-limit"
        aria-valuetext={`${inchesWords(value)} an acre a year`}
        onChange={(event) => onLimitIn(limitChoice(Number(event.target.value), whatIf))}
      />
      <div className="water-whatif-ends tabular" aria-hidden="true">
        <span>{min} in</span>
        <span>{max} in</span>
      </div>
      <p className="water-hint" data-testid="water-whatif-note">
        Per acre, on every irrigated plan. The plans above, your own rotation and the paths to them are scored again under
        it. {whatIf.note}
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// The chart: depth to water by winter, one line a well
// ---------------------------------------------------------------------------------------------

const BOX: ChartBox = { width: 384, height: 196, left: 40, right: 50, top: 10, bottom: 22 };
const LABEL_GAP = 12;

function WellChart({ wells }: { wells: WaterWell[] }) {
  const [winter, setWinter] = useState<number | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const scales = chartScales(wells, BOX);
  if (scales === null) return null;
  const plotBottom = BOX.height - BOX.bottom;
  const plotRight = BOX.width - BOX.right;
  const ends = wells.map((well) => well.points[well.points.length - 1] ?? well.fit[1]);
  const labelY = spreadLabels(
    ends.map((end) => scales.y(end[1])),
    LABEL_GAP,
    BOX.top + 4,
    plotBottom - 2,
  );
  const words = wells
    .map((well) => `${milesText(well.distance_mi)} away: ${well.points[0]?.[1] ?? "?"} ft in ${well.first_winter}, ${well.depth_to_water_ft} ft in ${well.last_winter}`)
    .join("; ");

  const onMove = (event: PointerEvent<SVGSVGElement>) => {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect || rect.width === 0) return;
    const x = ((event.clientX - rect.left) / rect.width) * BOX.width;
    setWinter(winterAt(x, BOX, scales.winters));
  };
  const hovered = winter === null ? [] : wells.map((well) => ({ well, depth: depthIn(well, winter) })).filter((row) => row.depth !== null);
  const tipLeft = winter === null ? 0 : (scales.x(winter) / BOX.width) * 100;

  return (
    <figure className="water-chart" data-testid="water-chart">
      <figcaption>Depth to water in the nearest wells, each winter (feet below the surface)</figcaption>
      <div className="water-chart-wrap">
        <svg
          ref={svgRef}
          viewBox={`0 0 ${BOX.width} ${BOX.height}`}
          role="img"
          aria-label={`Depth to water by winter in the ${wells.length} nearest wells; lower on the chart is deeper. ${words}. The table below has each well's rate.`}
          onPointerMove={onMove}
          onPointerDown={onMove}
          onPointerLeave={() => setWinter(null)}
        >
          {scales.depthTicks.map((depth) => (
            <g key={depth}>
              <line className="water-grid" x1={BOX.left} x2={plotRight} y1={scales.y(depth)} y2={scales.y(depth)} />
              <text className="water-tick" x={BOX.left - 6} y={scales.y(depth)} textAnchor="end" dominantBaseline="middle">
                {depth} ft
              </text>
            </g>
          ))}
          {scales.winterTicks.map((year) => (
            <text key={year} className="water-tick" x={scales.x(year)} y={plotBottom + 14} textAnchor="middle">
              {year}
            </text>
          ))}
          {wells.map((well) => (
            <line
              key={`fit-${well.well_id}`}
              className="water-fit"
              x1={scales.x(well.fit[0][0])}
              y1={scales.y(well.fit[0][1])}
              x2={scales.x(well.fit[1][0])}
              y2={scales.y(well.fit[1][1])}
            />
          ))}
          {wells.map((well) => (
            <path key={`line-${well.well_id}`} className="water-line" d={linePath(well.points, scales)} data-testid="water-well-line" data-well={well.well_id} />
          ))}
          {wells.map((well, i) => {
            const end = ends[i];
            if (!end) return null;
            return (
              <text key={`label-${well.well_id}`} className="water-end" x={scales.x(end[0]) + 5} y={labelY[i]} dominantBaseline="middle">
                {milesText(well.distance_mi)}
              </text>
            );
          })}
          {winter !== null && (
            <g aria-hidden="true">
              <line className="water-cross" x1={scales.x(winter)} x2={scales.x(winter)} y1={BOX.top} y2={plotBottom} />
              {hovered.map(({ well, depth }) => (
                <circle key={well.well_id} className="water-dot" cx={scales.x(winter)} cy={scales.y(depth as number)} r={3.5} />
              ))}
            </g>
          )}
        </svg>
        {winter !== null && (
          <div
            className={tipLeft > 55 ? "water-tip water-tip--left tabular" : "water-tip tabular"}
            style={tipLeft > 55 ? { right: `${100 - tipLeft}%` } : { left: `${tipLeft}%` }}
            data-testid="water-tip"
            aria-hidden="true"
          >
            <p className="water-tip-title">Winter {winter}</p>
            {hovered.length === 0 ? (
              <p className="muted">No measurement</p>
            ) : (
              hovered.map(({ well, depth }) => (
                <p key={well.well_id}>
                  <span className="muted">{milesText(well.distance_mi)} away</span> {oneDecimal(depth as number)} ft
                </p>
              ))
            )}
          </div>
        )}
      </div>
      <p className="water-key">
        <span className="water-key-line" aria-hidden="true" /> measured each winter, labelled with the well's distance from the field
        <span className="water-key-fit" aria-hidden="true" /> its straight-line trend. Lower is deeper.
      </p>
    </figure>
  );
}

/** Every well's numbers: the chart's table view. */
function WellTable({ wells }: { wells: WaterWell[] }) {
  return (
    <table className="water-table tabular" data-testid="water-wells">
      <caption className="sr-only">The wells used, nearest first</caption>
      <thead>
        <tr>
          <th scope="col">Well</th>
          <th scope="col" title="Change in depth to water, feet a year, with its 95% interval. Above zero means the level is falling.">
            Dropping, ft a year
          </th>
          <th scope="col">Depth to water</th>
          <th scope="col" title="The well's depth minus the depth to water">
            Water column
          </th>
        </tr>
      </thead>
      <tbody>
        {wells.map((well) => (
          <tr key={well.well_id} data-testid="water-well-row" data-well={well.well_id}>
            <th scope="row" title={`${well.well_id}: ${well.winters} winters, ${well.first_winter} to ${well.last_winter}`}>
              {milesText(well.distance_mi)} away
            </th>
            <td>{rateWithInterval(well.decline_ft_per_year, well.interval)}</td>
            <td>{well.depth_to_water_ft} ft</td>
            <td>{well.column_ft === null ? <span className="muted">depth not on record</span> : `${well.column_ft} ft`}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

import { useState } from "react";
import type { MapLayerState } from "../hooks/useMapLayers";
import { formatCount } from "../lib/format";
import { FIELDS_LAYER, NO_VALUE_COLOUR, classCell, detailText, formatAcreCount, formatValue } from "../lib/layers";
import type { LayerInfo, ViewStats } from "../lib/layers";
import "./LayerControl.css";

/** The names in the layer list. The API's own titles are longer ("Days the soil sat bare"). */
const SHORT_TITLES: Record<string, string> = { bare_days: "Bare days" };

const optionLabel = (layer: LayerInfo) => SHORT_TITLES[layer.name] ?? layer.title;

const narrow = () => typeof window !== "undefined" && window.matchMedia("(max-width: 760px)").matches;

/** "6 to 139" */
const spread = (stats: ViewStats | null, scale: number) =>
  stats && stats.p25 !== null && stats.p75 !== null ? `${formatValue(stats.p25, scale)} to ${formatValue(stats.p75, scale)}` : "–";
const median = (stats: ViewStats | null, scale: number) => (stats && stats.median !== null ? formatValue(stats.median, scale) : "–");

function Legend({ state }: { state: MapLayerState }) {
  const { layer, view, county, year, colours } = state;
  if (!layer || !layer.classes || !county) return null;
  const scale = layer.value_scale;
  const number = layer.kind === "number";
  const heading = year !== null ? `${layer.title}, ${year}` : layer.title;
  const hasNoValueClass = layer.classes.some((c) => c.no_value);
  const wind = layer.name === "erosion_risk" && year !== null ? layer.inputs?.erosion_climate_by_year?.[String(year)] : undefined;
  return (
    <div className="lyr-legend" data-testid="layer-legend">
      <table className="lyr-table" data-testid="layer-table">
        <caption>
          <strong data-testid="layer-heading">{heading}</strong>
          {layer.unit && (
            <span className="lyr-unit" data-testid="layer-unit">
              {" "}
              · {layer.unit}
              {number && layer.higher_is_worse ? ", darker is higher" : ""}
            </span>
          )}
        </caption>
        <thead>
          <tr>
            <th scope="col">
              <span className="sr-only">Class</span>
            </th>
            <th scope="col" data-testid="layer-view-head">
              In view
            </th>
            <th scope="col">County</th>
          </tr>
        </thead>
        <tbody>
          <tr data-testid="layer-row-fields">
            <th scope="row">Fields</th>
            <td className="tabular">{view ? formatCount(view.fields) : "–"}</td>
            <td className="tabular">{formatCount(county.fields)}</td>
          </tr>
          <tr data-testid="layer-row-acres">
            <th scope="row">Acres{number ? " with a value" : ""}</th>
            <td className="tabular">{view ? formatAcreCount(view.acres) : "–"}</td>
            <td className="tabular">{formatAcreCount(county.acres)}</td>
          </tr>
          {number && (
            <>
              <tr data-testid="layer-row-median">
                <th scope="row">Median</th>
                <td className="tabular">{median(view, scale)}</td>
                <td className="tabular">{median(county, scale)}</td>
              </tr>
              <tr data-testid="layer-row-spread">
                <th scope="row">Middle half of fields</th>
                <td className="tabular">{spread(view, scale)}</td>
                <td className="tabular">{spread(county, scale)}</td>
              </tr>
            </>
          )}
          {layer.classes.map((c) => (
            <tr key={c.index} className="lyr-class" data-testid="layer-class" data-index={c.index} data-key={c.key ?? ""}>
              <th scope="row">
                <span className="lyr-swatch" style={{ background: colours[c.index] ?? NO_VALUE_COLOUR }} aria-hidden="true" />
                <span className="lyr-text">
                  <span className="lyr-label">{c.label}</span>
                  {/* the gap of a class that is "the same plan" is 0 by definition: not worth a line */}
                  {c.key !== "same" && detailText(layer, county.classes[c.index]) !== null && (
                    <span className="lyr-detail" data-testid="layer-class-detail">
                      {detailText(layer, county.classes[c.index])}
                    </span>
                  )}
                </span>
              </th>
              <td className="tabular" data-testid="layer-class-view">
                {classCell(view?.classes[c.index])}
              </td>
              <td className="tabular" data-testid="layer-class-county">
                {classCell(county.classes[c.index])}
              </td>
            </tr>
          ))}
          {number && !hasNoValueClass && (
            <tr className="lyr-class" data-testid="layer-novalue">
              <th scope="row">
                <span className="lyr-swatch" style={{ background: NO_VALUE_COLOUR }} aria-hidden="true" />
                <span className="lyr-label">No value (too few clear satellite views)</span>
              </th>
              <td className="tabular">{view ? formatCount(view.noValue) : "–"}</td>
              <td className="tabular">{formatCount(county.noValue)}</td>
            </tr>
          )}
        </tbody>
      </table>
      <p className="lyr-note">
        {number ? "Shares are of the fields with a value. " : "Shares are of all fields. "}
        “In view” counts every field with any part on the map.
      </p>
      {!number && layer.detail && (
        <p className="lyr-note" data-testid="layer-detail-note">
          {layer.detail.key === "score_gap" ? "Gap" : layer.detail.key === "irrigated_prob" ? "Chance" : "The second figure"}: {layer.detail.label.charAt(0).toLowerCase() + layer.detail.label.slice(1)}
          {layer.detail.unit && layer.detail.unit !== "%" ? `, in ${layer.detail.unit}` : ""}; the middle value of the class across the county.
        </p>
      )}
      {wind !== undefined && (
        <p className="lyr-note" data-testid="layer-wind">
          Wind-erosion weather in {year}: {formatCount(wind)} (100 is the 2000–2024 average).
        </p>
      )}
      <p className="lyr-caveat" data-testid="layer-caveat">
        {layer.caveat}
      </p>
    </div>
  );
}

/** The layer list, the year slider with play, and the legend with the numbers for the fields in view. */
export function LayerControl({ state, disabled }: { state: MapLayerState; disabled?: boolean }) {
  const [open, setOpen] = useState(() => !narrow());
  const { catalog, name, years, year, playing } = state;
  const on = name !== FIELDS_LAYER;
  const hasYears = years !== null && years.length > 1;
  const first = years?.[0];
  const last = years?.[(years?.length ?? 1) - 1];

  return (
    <section className="lyr" data-testid="layer-control" data-layer={name} data-state={state.state} data-year={year ?? undefined} aria-label="Map layers">
      <div className="lyr-row">
        <label htmlFor="layer-select" className="lyr-name">
          Colour fields by
        </label>
        <select
          id="layer-select"
          className="lyr-select"
          data-testid="layer-select"
          value={name}
          disabled={disabled}
          onChange={(event) => state.setName(event.target.value)}
        >
          <option value={FIELDS_LAYER}>Fields</option>
          {(catalog?.layers ?? []).map((layer) => (
            <option key={layer.name} value={layer.name}>
              {optionLabel(layer)}
            </option>
          ))}
        </select>
      </div>
      {state.catalogError !== null && (
        <p className="lyr-note" role="status" data-testid="layer-catalog-error">
          The map layers could not be loaded.
        </p>
      )}

      {on && (
        <>
          <div className="lyr-row lyr-year" data-testid="layer-year-row">
            <button
              type="button"
              className="lyr-play"
              data-testid="layer-play"
              aria-label={playing ? "Pause the time-lapse" : "Play the time-lapse"}
              aria-pressed={playing}
              disabled={!hasYears || state.state !== "ready"}
              onClick={playing ? state.pause : state.play}
            >
              {playing ? "❚❚" : "▶"}
            </button>
            <input
              type="range"
              className="lyr-slider"
              data-testid="layer-year"
              aria-label="Year"
              aria-valuetext={year !== null ? String(year) : "no years"}
              min={first ?? 0}
              max={last ?? 1}
              step={1}
              value={year ?? last ?? 0}
              disabled={!hasYears}
              onPointerDown={state.pause}
              onChange={(event) => state.setYear(Number(event.target.value))}
            />
            <output className="lyr-year-text tabular" data-testid="layer-year-text">
              {year ?? "–"}
            </output>
          </div>
          {!hasYears && (
            <p className="lyr-note" data-testid="layer-year-reason">
              One value per field, so there are no years to step through.
            </p>
          )}

          {state.state === "loading" && (
            <p className="lyr-note" role="status" data-testid="layer-status">
              <span className="spinner" aria-hidden="true" />
              {state.progress && state.progress.total > 0
                ? `Preparing this layer: ${formatCount(state.progress.done)} of ${formatCount(state.progress.total)} fields…`
                : "Loading this layer…"}
            </p>
          )}
          {(state.state === "unavailable" || state.state === "mismatch") && (
            <p className="lyr-note" role="status" data-testid="layer-status">
              {state.message ?? "This layer is not available."}
            </p>
          )}
          {state.state === "error" && (
            <p className="lyr-note" role="alert" data-testid="layer-status">
              Could not load this layer.{" "}
              <button type="button" className="link-button" onClick={state.retry}>
                Try again
              </button>
            </p>
          )}

          {state.state === "ready" && (
            <>
              <p className="lyr-summary" data-testid="layer-summary">
                {state.view ? `${formatCount(state.view.fields)} of ${formatCount(state.county?.fields ?? 0)} fields in view` : "Counting the fields in view…"}
              </p>
              <button
                type="button"
                className="link-button lyr-toggle"
                data-testid="layer-details-toggle"
                aria-expanded={open}
                onClick={() => setOpen((was) => !was)}
              >
                {open ? "Hide legend and numbers" : "Show legend and numbers"}
              </button>
              {open && <Legend state={state} />}
            </>
          )}
        </>
      )}
    </section>
  );
}

import { useId } from "react";
import {
  DEFAULT_WEIGHTS,
  HORIZONS,
  SCENARIOS,
  WEIGHT_KEYS,
  WEIGHT_LABELS,
  WEIGHT_MAX,
  WEIGHT_MIN,
  climateLine,
  climateRange,
  horizonAt,
  horizonIndex,
  horizonLabel,
  irrigationReason,
  isDefaultPriceCost,
  isDefaultWeights,
  PRICE_COST_MAX,
  PRICE_COST_MIN,
  breakEvenLine,
  signedPercent,
  yieldChangeLine,
  yieldChangeRange,
} from "../lib/planner";
import type { PlannerChoices, Sensitivity } from "../lib/planner";
import type { ClimateSummary, PlannerSettings } from "../types";
import "./Planner.css";

interface PlannerControlsProps {
  /** What the sliders show: the user's choices, which may be ahead of the answer on screen. */
  choices: PlannerChoices;
  onChange: (next: PlannerChoices) => void;
  /**
   * From the answer on screen: what the planner actually used. The farm panel has no single field,
   * so it leaves `settings` out (no yield line, no irrigation switch) and passes `horizon` instead.
   */
  settings?: PlannerSettings;
  climate?: ClimateSummary;
  /** The future the climate line describes, when there are no `settings` to read it from. */
  horizon?: number;
  /** null = our guess; true/false = the user's own call for this field. */
  irrigationOverride?: boolean | null;
  onIrrigationOverride?: (value: boolean | null) => void;
  /** From the plans answer: the break-even line under the price and cost sliders (a single field only). */
  sensitivity?: Sensitivity | null;
}

/** The planner's inputs: what matters, which future, irrigated or not. Sits above the plan cards. */
export function PlannerControls({ choices, onChange, settings, climate, horizon, irrigationOverride = null, onIrrigationOverride, sensitivity }: PlannerControlsProps) {
  const id = useId();
  const irrigated = irrigationOverride ?? settings?.irrigated ?? false;
  const horizonPosition = horizonIndex(choices.horizon);
  const shownHorizon = settings?.horizon ?? horizon ?? choices.horizon;
  const range = climate ? climateRange(climate, shownHorizon) : null;
  const yields = climate && settings ? yieldChangeLine(climate.yield_change, settings.irrigated, settings.horizon) : null;
  const yieldTitle = climate && settings ? [yieldChangeRange(climate.yield_change), settings.yield_change_note].filter(Boolean).join(" ") : "";

  return (
    <>
    <div className="planner" data-testid="planner-controls">
      <div className="planner-group" role="group" aria-labelledby={`${id}-weights`}>
        <div className="planner-head">
          <h4 id={`${id}-weights`}>What matters most to you?</h4>
          <button
            type="button"
            className="link-button"
            data-testid="weights-reset"
            disabled={isDefaultWeights(choices.weights)}
            onClick={() => onChange({ ...choices, weights: DEFAULT_WEIGHTS })}
          >
            Reset
          </button>
        </div>
        {WEIGHT_KEYS.map((key) => (
          <div className="planner-row" key={key}>
            <label htmlFor={`${id}-${key}`}>{WEIGHT_LABELS[key]}</label>
            <input
              id={`${id}-${key}`}
              type="range"
              min={WEIGHT_MIN}
              max={WEIGHT_MAX}
              step={1}
              value={choices.weights[key]}
              data-testid={`weight-${key}`}
              aria-valuetext={`${choices.weights[key]} out of ${WEIGHT_MAX}`}
              onChange={(event) => onChange({ ...choices, weights: { ...choices.weights, [key]: Number(event.target.value) } })}
            />
            <output className="planner-value tabular" htmlFor={`${id}-${key}`} data-testid={`weight-${key}-value`}>
              {choices.weights[key]}
            </output>
          </div>
        ))}
        <p className="planner-hint">
          0 means it does not matter, 10 means it matters most. Soil protection means fewer bare days a year and lower
          wind-erosion risk.
        </p>
      </div>

      <div className="planner-group" role="group" aria-labelledby={`${id}-future`}>
        <div className="planner-head">
          <h4 id={`${id}-future`}>Which future?</h4>
          <output className="planner-year tabular" htmlFor={`${id}-horizon`} data-testid="horizon-value">
            {horizonLabel(choices.horizon)}
          </output>
        </div>
        <div className="planner-future">
          <label htmlFor={`${id}-horizon`} className="sr-only">
            Year to plan for
          </label>
          <input
            id={`${id}-horizon`}
            type="range"
            min={0}
            max={HORIZONS.length - 1}
            step={1}
            value={horizonPosition}
            data-testid="horizon"
            aria-valuetext={horizonLabel(choices.horizon)}
            onChange={(event) => onChange({ ...choices, horizon: horizonAt(Number(event.target.value)) })}
          />
          <div className="planner-ticks tabular" aria-hidden="true">
            {HORIZONS.map((year, index) => (
              <span key={year} className={index === horizonPosition ? "is-on" : undefined}>
                {horizonLabel(year)}
              </span>
            ))}
          </div>
        </div>
        <div className="segmented" role="radiogroup" aria-label="Emissions path" data-testid="scenario">
          {SCENARIOS.map((scenario) => (
            <label key={scenario.id} className="segmented-option">
              <input
                type="radio"
                name={`${id}-scenario`}
                value={scenario.id}
                checked={choices.scenario === scenario.id}
                data-testid={`scenario-${scenario.id}`}
                onChange={() => onChange({ ...choices, scenario: scenario.id })}
              />
              <span>{scenario.label}</span>
            </label>
          ))}
        </div>
        {climate && (
          <p className="planner-climate" data-testid="climate-line">
            {climateLine(climate, shownHorizon)}
          </p>
        )}
        {range && (
          <p className="planner-climate-range tabular" data-testid="climate-range">
            {range}
          </p>
        )}
        {yields && (
          <p className="planner-yields tabular" data-testid="yield-line" title={yieldTitle || undefined}>
            {yields}
          </p>
        )}
      </div>

      {settings && onIrrigationOverride && (
        <div className="planner-irrigation">
          <p data-testid="irrigation-line">
            Treated as <strong>{settings.irrigated ? "irrigated" : "dryland"}</strong>
            <span className="muted"> · {irrigationReason(settings.irrigation_source, settings.irrigation_basis)}</span>
          </p>
          <div className="planner-irrigation-actions">
            <label className="switch">
              <input
                type="checkbox"
                role="switch"
                checked={irrigated}
                data-testid="irrigation-switch"
                onChange={(event) => onIrrigationOverride(event.target.checked)}
              />
              <span className="switch-track" aria-hidden="true" />
              <span>Irrigated</span>
            </label>
            {irrigationOverride !== null && (
              <button type="button" className="link-button" data-testid="irrigation-reset" onClick={() => onIrrigationOverride(null)}>
                Use our guess
              </button>
            )}
          </div>
        </div>
      )}
    </div>
    <div className="planner planner--price-cost">
      <div className="planner-group" role="group" aria-labelledby={`${id}-money`} data-testid="price-cost">
        <div className="planner-head">
          <h4 id={`${id}-money`}>What if prices or costs change?</h4>
          <button
            type="button"
            className="link-button"
            data-testid="price-cost-reset"
            disabled={isDefaultPriceCost(choices)}
            onClick={() => onChange({ ...choices, pricePct: 0, costPct: 0 })}
          >
            Reset
          </button>
        </div>
        {(
          [
            ["price", "Crop prices", choices.pricePct ?? 0],
            ["cost", "Direct costs", choices.costPct ?? 0],
          ] as const
        ).map(([key, label, value]) => (
          <div className="planner-row planner-row--pct" key={key}>
            <label htmlFor={`${id}-${key}-pct`}>{label}</label>
            <input
              id={`${id}-${key}-pct`}
              type="range"
              min={PRICE_COST_MIN}
              max={PRICE_COST_MAX}
              step={5}
              value={value}
              data-testid={`${key}-pct`}
              aria-valuetext={`${signedPercent(value)} against today`}
              onChange={(event) => onChange({ ...choices, [key === "price" ? "pricePct" : "costPct"]: Number(event.target.value) })}
            />
            <output className="planner-value tabular" htmlFor={`${id}-${key}-pct`} data-testid={`${key}-pct-value`}>
              {signedPercent(value)}
            </output>
          </div>
        ))}
        <p className="planner-hint">
          We do not predict prices or costs: you pick the change, every crop at once, and the plans are scored again.
          Dollars are returns over direct costs.
        </p>
        {breakEvenLine(sensitivity) && (
          <p className="planner-breakeven tabular" data-testid="break-even-line">
            {breakEvenLine(sensitivity)}
          </p>
        )}
      </div>

    </div>
    </>
  );
}

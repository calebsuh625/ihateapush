import { cropColor, cropLabel, textColorOn } from "../crops";
import { clamp01, formatScore } from "../lib/format";
import type { Plan, PlanScores } from "../types";

const SCORE_ROWS: readonly { key: keyof PlanScores; label: string }[] = [
  { key: "soil", label: "Soil" },
  { key: "water", label: "Water" },
  { key: "money", label: "Money" },
];

function ScoreBar({ label, value }: { label: string; value: number }) {
  const shown = clamp01(value);
  return (
    <div className="score">
      <span className="score-label">{label}</span>
      <span
        className="score-track"
        role="meter"
        aria-label={`${label} score`}
        aria-valuemin={0}
        aria-valuemax={1}
        aria-valuenow={shown}
        aria-valuetext={`${formatScore(value)} out of 1`}
      >
        <span className="score-fill" style={{ width: `${shown * 100}%` }} />
      </span>
      <span className="score-value tabular" aria-hidden="true">
        {formatScore(value)}
      </span>
    </div>
  );
}

/** One ranked rotation plan. `rank` is 1-based; rank 1 is presented as the recommendation. */
export function PlanCard({ plan, rank }: { plan: Plan; rank: number }) {
  const top = rank === 1;
  const headingId = `plan-${rank}-name`;
  return (
    <li className={top ? "plan plan--top" : "plan"} aria-labelledby={headingId} data-testid="plan-card">
      <div className="plan-head">
        <span className="plan-rank" aria-hidden="true">
          {rank}
        </span>
        <span className={top ? "plan-tag plan-tag--top" : "plan-tag"}>{top ? "Recommended" : "Alternative"}</span>
        <span className="plan-overall">
          <span className="plan-overall-label">Overall</span>
          <span className="plan-overall-value tabular" data-testid="plan-overall">
            {formatScore(plan.scores.overall)}
          </span>
        </span>
      </div>

      {/* The plan's name spells out the same crops as the chips below ("Corn – Soybeans – …"),
          so it is the card's heading for screen readers and is not repeated on screen. */}
      <h4 className="sr-only" id={headingId} data-testid="plan-name">
        {plan.name}
      </h4>

      <ol className="rotation" aria-label="Crop for each year of the rotation">
        {plan.rotation.map((crop, index) => {
          const color = cropColor(crop);
          return (
            <li key={index} className="rotation-step">
              <span className="rotation-year">Year {index + 1}</span>
              <span
                className="chip"
                style={{ backgroundColor: color, color: textColorOn(color) }}
                data-testid="plan-chip"
                data-crop={crop}
              >
                {cropLabel(crop)}
              </span>
            </li>
          );
        })}
      </ol>

      <p className="plan-reason" data-testid="plan-reason">
        {plan.reason}
      </p>

      <div className="scores">
        {SCORE_ROWS.map(({ key, label }) => (
          <ScoreBar key={key} label={label} value={plan.scores[key]} />
        ))}
      </div>
    </li>
  );
}

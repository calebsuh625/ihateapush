import { cropColor, cropLabel, textColorOn } from "../crops";
import { clamp01, formatScore } from "../lib/format";
import { formatDollars, formatShare, holdsText, holdsTone, spreadGeometry } from "../lib/planner";
import type { Plan, PlanNumbers, PlanScores, Spread } from "../types";
import "./Planner.css";

type ScoreKey = "soil" | "water" | "money" | "risk";

const SCORE_LABELS: Record<ScoreKey, string> = { soil: "Soil", water: "Water", money: "Money", risk: "Risk" };

/** The score rows a plan has: soil, water, money, and risk when the smart planner answered. */
export function scoreRows(scores: PlanScores): { key: ScoreKey; label: string; value: number }[] {
  const keys: ScoreKey[] = typeof scores.risk === "number" ? ["soil", "water", "money", "risk"] : ["soil", "water", "money"];
  return keys.map((key) => ({ key, label: SCORE_LABELS[key], value: scores[key] as number }));
}

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

/** "Holds up in 4 of 6 futures", coloured by the share (the words carry the count too). */
export function HoldsBadge({ futures }: { futures: { holds: number; total: number } }) {
  const tone = holdsTone(futures.holds, futures.total);
  return (
    <span className={`holds holds--${tone}`} data-testid="plan-holds" data-tone={tone}>
      <span className="holds-dot" aria-hidden="true" />
      {holdsText(futures.holds, futures.total)}
    </span>
  );
}

/** Profit per acre across the futures: a bar from the lowest to the highest, a mark at the middle. */
function SpreadStrip({ spread, domain }: { spread: Spread; domain: readonly [number, number] }) {
  const g = spreadGeometry(spread, domain);
  const words = `Profit per acre a year across the futures: low ${formatDollars(spread.low)}, middle ${formatDollars(spread.mid)}, high ${formatDollars(spread.high)}`;
  return (
    <div className="spread" data-testid="plan-spread">
      <p className="spread-title">Profit per acre a year, across the futures</p>
      <div className="spread-strip" role="img" aria-label={words}>
        {g.zero !== null && <span className="spread-zero" style={{ left: `${g.zero}%` }} />}
        <span className="spread-range" style={{ left: `${g.left}%`, width: `${g.width}%` }} />
        <span className="spread-mid" style={{ left: `${g.mid}%` }} />
      </div>
      <p className="spread-values tabular" aria-hidden="true">
        <span>Low {formatDollars(spread.low)}</span>
        <span className="spread-values-mid">Middle {formatDollars(spread.mid)}</span>
        <span>High {formatDollars(spread.high)}</span>
      </p>
    </div>
  );
}

/** Bare days, water, profit and failed crops, as four small figures. */
function NumbersLine({ numbers, irrigated }: { numbers: PlanNumbers; irrigated: boolean }) {
  return (
    <dl className="plan-numbers tabular" data-testid="plan-numbers">
      <div>
        <dd>{Math.round(numbers.bare_days)}</dd>
        <dt>bare days a year</dt>
      </div>
      {irrigated ? (
        <div>
          <dd>{Math.round(numbers.irrigation_mm)} mm</dd>
          <dt>water pumped a year</dt>
        </div>
      ) : (
        <div>
          <dd>{formatShare(numbers.water_met_share)}</dd>
          <dt>of crop water need met by rain</dt>
        </div>
      )}
      <div>
        <dd>{formatDollars(numbers.profit_per_acre)}</dd>
        <dt>profit per acre a year</dt>
      </div>
      <div>
        <dd>{formatShare(numbers.failed_share)}</dd>
        <dt>of planted crops fail</dt>
      </div>
    </dl>
  );
}

interface PlanCardProps {
  plan: Plan;
  /** 1-based; rank 1 is presented as the recommendation. */
  rank: number;
  /** Smart planner only: is the field treated as irrigated (picks the water figure shown). */
  irrigated?: boolean;
  /** Smart planner only: the shared scale for the profit strips, so cards compare by eye. */
  profitDomain?: readonly [number, number];
}

/** One ranked rotation plan. The future-weather parts appear only when the answer carries them. */
export function PlanCard({ plan, rank, irrigated = false, profitDomain }: PlanCardProps) {
  const top = rank === 1;
  const headingId = `plan-${rank}-name`;
  const profit = plan.spread?.profit_per_acre;
  // The smart planner's cards show their name (a 3-year cycle says so only there).
  const smart = plan.futures !== undefined;
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

      {/* Without the smart planner the name only repeats the chips below, so it is kept for
          screen readers and not shown twice. */}
      <h4 className={smart ? "plan-name" : "sr-only"} id={headingId} data-testid="plan-name">
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

      {plan.futures && (
        <div className="plan-futures">
          <HoldsBadge futures={plan.futures} />
          {profit && profitDomain && <SpreadStrip spread={profit} domain={profitDomain} />}
        </div>
      )}

      <div className="scores">
        {scoreRows(plan.scores).map(({ key, label, value }) => (
          <ScoreBar key={key} label={label} value={value} />
        ))}
      </div>

      {plan.numbers && <NumbersLine numbers={plan.numbers} irrigated={irrigated} />}
    </li>
  );
}

import { cropColor, cropLabel, textColorOn } from "../crops";
import { clamp01, formatScore } from "../lib/format";
import {
  DRYLAND_WATER_TITLE,
  RANKING_SCORE_HINT,
  RANKING_SCORE_LABEL,
  RANKING_SCORE_TITLE,
  formatDollars,
  formatInches,
  formatMm,
  formatShare,
  holdsText,
  holdsTone,
  placeText,
  spreadGeometry,
  wellQualifier,
} from "../lib/planner";
import type { Plan, PlanNumbers, PlanScores, Spread } from "../types";
import { HowToGetThere } from "./TransitionPath";
import "./Planner.css";

type ScoreKey = "soil" | "water" | "money" | "risk";

const SCORE_LABELS: Record<ScoreKey, string> = { soil: "Soil", water: "Water", money: "Money", risk: "Risk" };

/** The score rows a plan has: soil, water, money, and risk when the smart planner answered. */
export function scoreRows(scores: PlanScores): { key: ScoreKey; label: string; value: number }[] {
  const keys: ScoreKey[] = typeof scores.risk === "number" ? ["soil", "water", "money", "risk"] : ["soil", "water", "money"];
  return keys.map((key) => ({ key, label: SCORE_LABELS[key], value: scores[key] as number }));
}

function ScoreBar({ label, value, title }: { label: string; value: number; title?: string }) {
  const shown = clamp01(value);
  return (
    <div className="score" title={title}>
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
export function HoldsBadge({ futures, qualifier }: { futures: { holds: number; total: number }; qualifier?: string | null }) {
  const tone = holdsTone(futures.holds, futures.total);
  const badge = (
    <span className={`holds holds--${tone}`} data-testid="plan-holds" data-tone={tone}>
      <span className="holds-dot" aria-hidden="true" />
      {holdsText(futures.holds, futures.total)}
    </span>
  );
  if (!qualifier) return badge;
  // Irrigated plans hold up only for as long as the water is there, so the badge says so.
  return (
    <span className="holds-wrap">
      {badge}
      <span className="holds-note" data-testid="plan-holds-note">
        {qualifier}
      </span>
    </span>
  );
}

/** Profit per acre across the futures: a bar from the lowest to the highest, a mark at the middle. */
function SpreadStrip({ spread, domain }: { spread: Spread; domain: readonly [number, number] }) {
  const g = spreadGeometry(spread, domain);
  const words = `Return per acre a year over direct costs, across the futures: low ${formatDollars(spread.low)}, middle ${formatDollars(spread.mid)}, high ${formatDollars(spread.high)}`;
  return (
    <div className="spread" data-testid="plan-spread">
      <p className="spread-title">Return per acre a year over direct costs, across the futures</p>
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
        <div title={`${formatMm(numbers.irrigation_mm)} a year`} data-testid="plan-water">
          <dd>{formatInches(numbers.irrigation_in, numbers.irrigation_mm)}</dd>
          <dt>
            water pumped a year
            {numbers.water_met_share < 0.995 && ` (${formatShare(numbers.water_met_share)} of crop need)`}
          </dt>
        </div>
      ) : (
        <div>
          <dd>{formatShare(numbers.water_met_share)}</dd>
          <dt>of crop water need met by rain</dt>
        </div>
      )}
      <div>
        <dd>{formatDollars(numbers.profit_per_acre)}</dd>
        <dt>return per acre a year, over direct costs</dt>
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
  /** Smart planner only: how many patterns were compared (for "number 7 of 115"). */
  candidates?: number;
  /** Smart planner only: the most a well is assumed to deliver, inches a year; null = no limit. */
  irrigationLimitIn?: number | null;
  /** When given, the card offers "Edit this plan" (it opens in "Try your own rotation"). */
  onEdit?: () => void;
  /** Smart planner only: the field and the settings the cards were asked with, for "How to get there". */
  howTo?: { fieldId: string; plansQuery: string };
  /** Irrigated fields with measured wells nearby: the plan's pumping next to the measured decline. */
  waterLine?: string | null;
}

/** One ranked rotation plan. The future-weather parts appear only when the answer carries them. */
export function PlanCard({ plan, rank, irrigated = false, profitDomain, candidates, irrigationLimitIn, onEdit, howTo, waterLine }: PlanCardProps) {
  const top = rank === 1;
  const headingId = `plan-${rank}-name`;
  const profit = plan.spread?.profit_per_acre;
  // The smart planner's cards show their name (a 3-year cycle says so only there).
  const smart = plan.futures !== undefined;
  // The smart planner sorts on scores.robust, so that is the number the card leads with.
  const robust = typeof plan.scores.robust === "number" ? plan.scores.robust : null;
  const place = smart ? placeText(plan.rank, candidates, rank) : null;
  const editButton = onEdit && (
    <button type="button" className="link-button" data-testid="plan-edit" aria-label={`Edit this plan: ${plan.name}`} onClick={onEdit}>
      Edit this plan
    </button>
  );
  return (
    <li className={top ? "plan plan--top" : "plan"} aria-labelledby={headingId} data-testid="plan-card">
      <div className="plan-head">
        <span className="plan-rank" aria-hidden="true">
          {rank}
        </span>
        <span className={top ? "plan-tag plan-tag--top" : "plan-tag"}>{top ? "Recommended" : "Alternative"}</span>
        {robust !== null ? (
          <span className="plan-overall plan-overall--ranking" title={RANKING_SCORE_TITLE}>
            <span className="plan-overall-label" data-testid="plan-overall-label">
              {RANKING_SCORE_LABEL}
              <span className="plan-overall-hint">{RANKING_SCORE_HINT}</span>
            </span>
            <span className="plan-overall-value tabular" data-testid="plan-overall">
              {formatScore(robust)}
            </span>
          </span>
        ) : (
          <span className="plan-overall">
            <span className="plan-overall-label">Overall</span>
            <span className="plan-overall-value tabular" data-testid="plan-overall">
              {formatScore(plan.scores.overall)}
            </span>
          </span>
        )}
      </div>

      {place && (
        <p className="plan-place tabular" data-testid="plan-place">
          {place}
        </p>
      )}

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
          <HoldsBadge futures={plan.futures} qualifier={irrigated ? wellQualifier(irrigationLimitIn) : null} />
          {profit && profitDomain && <SpreadStrip spread={profit} domain={profitDomain} />}
        </div>
      )}

      <div className="scores">
        {scoreRows(plan.scores).map(({ key, label, value }) => (
          <ScoreBar key={key} label={label} value={value} title={smart && !irrigated && key === "water" ? DRYLAND_WATER_TITLE : undefined} />
        ))}
      </div>

      {plan.numbers && <NumbersLine numbers={plan.numbers} irrigated={irrigated} />}

      {waterLine && (
        <p className="plan-water-line" data-testid="plan-water-line">
          {waterLine}
        </p>
      )}

      {howTo ? (
        // keyed by field: an opened path belongs to the field it was opened for
        <HowToGetThere key={howTo.fieldId} fieldId={howTo.fieldId} plansQuery={howTo.plansQuery} target={{ plan: rank }} name={plan.name} actions={editButton} />
      ) : (
        editButton && <p className="plan-edit">{editButton}</p>
      )}
    </li>
  );
}

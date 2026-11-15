import { formatScore } from "../lib/format";
import { RANKING_SCORE_LABEL, RANKING_SCORE_TITLE, wellQualifier, windowText } from "../lib/planner";
import type { CurrentPattern, PlannerSettings } from "../types";
import { HoldsBadge, scoreRows } from "./PlanCard";
import "./Planner.css";

/** The field's own recent pattern, scored like the plans, as one compact row. */
export function CurrentPatternRow({
  current,
  note,
  candidates,
  settings,
}: {
  current: CurrentPattern | null | undefined;
  note: string | null | undefined;
  candidates: number | undefined;
  settings?: PlannerSettings;
}) {
  if (!current) {
    return note ? (
      <p className="current-note muted" data-testid="current-note">
        What this field grows now is not scored. {note}
      </p>
    ) : null;
  }
  const broken = current.rules_broken ?? [];
  return (
    <div className="current" data-testid="current-pattern">
      <div className="current-head">
        <p className="current-title">
          What this field grows now
          {current.years && <span className="muted tabular"> · {windowText(current.years)}</span>}
        </p>
        {typeof current.rank === "number" && typeof candidates === "number" && (
          <p className="current-rank muted tabular">
            Ranks {current.rank} of {candidates}
          </p>
        )}
      </div>
      <p className="current-name" data-testid="current-name">
        {current.name}
      </p>
      <dl className="current-scores tabular">
        {scoreRows(current.scores).map(({ key, label, value }) => (
          <div key={key}>
            <dt>{label}</dt>
            <dd>{formatScore(value)}</dd>
          </div>
        ))}
        {typeof current.scores.robust === "number" ? (
          <div className="current-overall" title={RANKING_SCORE_TITLE}>
            <dt>{RANKING_SCORE_LABEL}</dt>
            <dd>{formatScore(current.scores.robust)}</dd>
          </div>
        ) : (
          <div className="current-overall">
            <dt>Overall</dt>
            <dd>{formatScore(current.scores.overall)}</dd>
          </div>
        )}
      </dl>
      {current.futures && (
        <HoldsBadge futures={current.futures} qualifier={settings?.irrigated ? wellQualifier(settings.irrigation_limit_in) : null} />
      )}
      {broken.length > 0 && (
        <ul className="current-broken" data-testid="current-broken">
          {broken.map((sentence, index) => (
            <li key={index}>{sentence}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Collapsed by default: the rules behind the scores, in the planner's own words. */
export function ScoringNote({ settings }: { settings: PlannerSettings }) {
  const models = settings.models ?? [];
  const limits = settings.limits ?? [];
  return (
    <details className="scoring" data-testid="scoring-note">
      <summary>How this is scored</summary>
      <div className="scoring-body">
        {settings.holds_up_rule && <p>{settings.holds_up_rule}</p>}
        {settings.ranking_rule && <p>{settings.ranking_rule}</p>}
        {settings.profit_basis && <p>{settings.profit_basis}</p>}
        {settings.soil_note && (
          <p>The soil score is soil protection: fewer bare days a year and lower wind-erosion risk. {settings.soil_note}</p>
        )}
        {settings.yield_change_note && <p>{settings.yield_change_note}</p>}
        {limits.length > 0 && (
          <>
            <p className="scoring-limits-title">What the scores leave out</p>
            <ul className="scoring-limits" data-testid="scoring-limits">
              {limits.map((sentence, index) => (
                <li key={index}>{sentence}</li>
              ))}
            </ul>
          </>
        )}
        {models.length > 0 && (
          <p>
            Each future is one climate model: <span className="scoring-models">{models.join(", ")}</span>.
          </p>
        )}
      </div>
    </details>
  );
}

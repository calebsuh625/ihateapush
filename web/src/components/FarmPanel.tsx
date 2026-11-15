import { useId } from "react";
import type { ReactNode } from "react";
import { api } from "../api";
import { cropBackground, cropLabel } from "../crops";
import { useLatest } from "../hooks/useLatest";
import {
  FIELD_TAGS,
  LIMIT_TAGS,
  farmRequest,
  fieldChange,
  formatAcres,
  formatFarmDollars,
  formatFarmScore,
  formatWhole,
  limitTone,
  metWords,
  parseAmount,
  rangeWords,
  shortDollars,
  yearScale,
} from "../lib/farm";
import { hectaresToAcres, plural } from "../lib/format";
import { plansQuery } from "../lib/planner";
import type { PlannerChoices } from "../lib/planner";
import type { FarmBand, FarmField, FarmLimit, FarmPlan, FarmYear } from "../types";
import { ErrorNote } from "./FieldPanel";
import { inchesWords } from "../lib/water";
import { PlannerControls } from "./PlannerControls";
import "./Farm.css";

/** A solve can take seconds, so wait a little longer after the last change than the field view does. */
const FARM_DEBOUNCE_MS = 500;
/** One farm at a time: the hook keeps the last answer for this key while a newer one is on its way. */
const FARM_KEY = "farm";

const loadFarm = (_key: string, signal: AbortSignal, body: string) => api.farmPlan(body, signal);

interface FarmPanelProps {
  /** The farm's fields, in the order they were picked. */
  ids: readonly string[];
  /** Hectares from the map layer, shown at once (the plan brings the acres the planner used). */
  areaById: ReadonlyMap<string, number>;
  /** The same sliders, future and scenario as the field view. */
  choices: PlannerChoices;
  onChoices: (next: PlannerChoices) => void;
  /** The two limit boxes, as typed ("" = no limit). */
  water: string;
  minimum: string;
  onLimits: (water: string, minimum: string) => void;
  onRemove: (id: string) => void;
  onClear: () => void;
  onOpenField: (id: string) => void;
  onClose: () => void;
}

export function FarmPanel({ ids, areaById, choices, onChoices, water, minimum, onLimits, onRemove, onClear, onOpenField, onClose }: FarmPanelProps) {
  const id = useId();
  const request = farmRequest(ids, choices, water, minimum);
  const body = request === null ? null : JSON.stringify(request);
  const planner = useLatest(FARM_KEY, body, loadFarm, FARM_DEBOUNCE_MS);
  // The climate line under "Which future?" is the county's, the same for every field: ask for it
  // with the farm's first field.
  const firstId = ids[0] ?? "";
  const climateSource = useLatest(firstId, firstId ? plansQuery(choices) : null, api.plans);
  const climateAnswer = climateSource.resource.status === "ready" && climateSource.resource.data.engine === "smart" ? climateSource.resource.data : null;

  const answer = planner.resource.status === "ready" && ids.length > 0 ? planner.resource.data : null;
  // A refusal (too many fields, say) is the API's plain sentence, not a failure.
  const refusal =
    planner.errorStatus === 400 ? (planner.resource.status === "error" ? planner.resource.message : planner.updateError) : null;
  const failure = refusal === null && planner.resource.status === "error" ? planner.resource.message : null;
  const waiting = body !== null && (planner.resource.status === "loading" || planner.updating);
  // The answer on screen is for exactly the fields now picked (in any settings).
  const sameFields = answer !== null && answer.fields.length === ids.length && answer.fields.every((field, index) => field.field_id === ids[index]);
  const byId = new Map<string, FarmField>((answer?.fields ?? []).map((field) => [field.field_id, field]));

  const ownAcres = ids.reduce((sum, fieldId) => sum + hectaresToAcres(areaById.get(fieldId) ?? 0), 0);
  const totalAcres = answer && sameFields ? answer.farm.area_acres : ownAcres;
  const waterAmount = parseAmount(water);
  const minimumAmount = parseAmount(minimum, true);
  const guide = answer && refusal === null ? answer : null;

  return (
    <aside className="panel" aria-labelledby="farm-title" data-testid="farm-panel" data-fields={ids.length}>
      <header className="panel-head">
        <div className="panel-heading">
          <p className="eyebrow">Farm plan</p>
          <h2 id="farm-title" className="tabular" data-testid="farm-title">
            {ids.length === 0 ? "No fields yet" : `${formatWhole(ids.length)} ${plural(ids.length, "field")}`}
          </h2>
          <p className="panel-area tabular" data-testid="farm-acres">
            {ids.length > 0 && <>{formatWhole(totalAcres)} acres in total</>}
          </p>
        </div>
        <button type="button" className="icon-button" onClick={onClose} aria-label="Close the farm plan" title="Close the farm plan">
          <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
            <path d="M3.5 3.5l9 9m0-9l-9 9" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" fill="none" />
          </svg>
        </button>
      </header>

      <div className="panel-body">
        <FarmSection
          title="Fields in the farm"
          id={`${id}-fields`}
          meta={
            ids.length > 0 && (
              <button type="button" className="link-button" data-testid="farm-panel-clear" onClick={onClear}>
                Clear
              </button>
            )
          }
        >
          {ids.length === 0 ? (
            <p className="muted" data-testid="farm-empty">
              Click fields on the map to add them, or switch to Lasso or Box in the corner of the map and draw around
              several. The fields are then planned together.
            </p>
          ) : (
            <ul className="farm-chips" data-testid="farm-chips">
              {ids.map((fieldId) => {
                const known = byId.get(fieldId);
                const area = areaById.get(fieldId);
                const acres = known ? known.area_acres : area === undefined ? null : hectaresToAcres(area);
                return (
                  <li key={fieldId} className="farm-chip" data-testid="farm-chip" data-field-id={fieldId}>
                    <span className="farm-chip-text tabular">
                      <strong>{fieldId}</strong>
                      <span className="muted">
                        {acres === null ? "" : ` · ${formatAcres(acres)} acres`}
                        {known ? ` · ${known.irrigated ? "irrigated" : "dryland"}` : ""}
                      </span>
                    </span>
                    <button type="button" className="farm-chip-remove" aria-label={`Take ${fieldId} out of the farm`} title="Take out of the farm" onClick={() => onRemove(fieldId)}>
                      <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
                        <path d="M3.5 3.5l9 9m0-9l-9 9" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none" />
                      </svg>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
          {answer && sameFields && (
            <p className="farm-small muted" data-testid="farm-practice-note">
              Irrigated or dryland is our guess from each field's crop history and water use; open a field to see why.
            </p>
          )}
        </FarmSection>

        {ids.length > 0 && (
          <FarmSection title="What the plan should do" id={`${id}-inputs`}>
            <PlannerControls choices={choices} onChange={onChoices} climate={climateAnswer?.climate} horizon={climateAnswer?.settings?.horizon} />
            {typeof choices.limitIn === "number" && (
              <p className="farm-small" data-testid="farm-whatif">
                What-if set on a field: no field may pump more than {inchesWords(choices.limitIn)} on an acre in a year.{" "}
                <button type="button" className="link-button" data-testid="farm-whatif-reset" onClick={() => onChoices({ ...choices, limitIn: null })}>
                  Remove
                </button>
              </p>
            )}
            <div className="farm-limits" data-testid="farm-limits">
              <LimitInput
                id={`${id}-water`}
                testId="farm-water"
                label="Water budget"
                unit="acre-inches a year"
                value={water}
                bad={!waterAmount.ok}
                badText="Type a number of acre-inches, zero or more, or leave it empty for no budget."
                onChange={(next) => onLimits(next, minimum)}
                guide={
                  guide &&
                  (guide.farm.irrigated_fields === 0
                    ? "None of these fields is treated as irrigated, so the farm pumps no water."
                    : `With no budget, each field planned alone, this farm pumps up to ${formatWhole(guide.alone.peak_water.value)} acre-inches in its heaviest year and ${formatWhole(guide.alone.average.water_acre_in)} in an average year.`)
                }
                stale={planner.updating && !sameFields}
              />
              <LimitInput
                id={`${id}-minimum`}
                testId="farm-minimum"
                label="Least return in any year"
                unit="dollars a year, over direct costs"
                value={minimum}
                bad={!minimumAmount.ok}
                badText="Type a number of dollars, or leave it empty for no minimum."
                onChange={(next) => onLimits(water, next)}
                guide={
                  guide &&
                  `With no minimum, each field planned alone, the leanest year returns ${formatFarmDollars(guide.alone.worst_return.value)} over direct costs and an average year ${formatFarmDollars(guide.alone.average.return)}.`
                }
                stale={planner.updating && !sameFields}
              />
              <p className="farm-small muted">
                Each of the 12 years has to meet the limits by itself, on the average of the climate models. Leave a box
                empty for no limit.
              </p>
            </div>
          </FarmSection>
        )}

        {ids.length > 0 && (
          <FarmSection title="The farm plan" id={`${id}-plan`} meta="12 years · every field at once">
            {waiting && (
              <p className="farm-waiting" role="status" data-testid="farm-waiting">
                <span className="spinner" aria-hidden="true" />
                <span>
                  Working out the farm plan…{" "}
                  {answer
                    ? `With tight limits this can take up to about ${formatWhole(answer.solver.time_limit_s)} seconds.`
                    : "This can take a few seconds."}
                </span>
              </p>
            )}
            {body === null && (
              <p className="farm-small muted" data-testid="farm-blocked">
                {answer ? "The plan below is from before the last change: " : "No plan yet: "}one of the limit boxes above does not
                hold a number. Correct it or empty it and the farm is planned again.
              </p>
            )}
            {refusal !== null && (
              <p className="farm-callout farm-callout--short" data-testid="farm-refusal">
                {refusal}
              </p>
            )}
            {failure !== null && <ErrorNote title="Could not work out the farm plan" message={failure} onRetry={planner.retry} />}
            {refusal === null && planner.updateError !== null && (
              <p className="plans-update-error" role="alert" data-testid="farm-update-error">
                <span>Could not update the farm plan, so this is the last one we worked out. {planner.updateError}</span>
                <button type="button" className="link-button" onClick={planner.retry}>
                  Try again
                </button>
              </p>
            )}
            {answer && refusal === null && (
              <div
                className={planner.updating ? "plans-live is-updating" : "plans-live"}
                aria-live="polite"
                aria-busy={planner.updating}
                data-testid="farm-result"
                data-updating={planner.updating ? "true" : "false"}
                data-status={answer.status}
                data-query={planner.shownQuery ?? undefined}
              >
                <FarmResult answer={answer} onOpenField={onOpenField} />
              </div>
            )}
          </FarmSection>
        )}
      </div>
    </aside>
  );
}

// ---------------------------------------------------------------------------------------------
// Inputs
// ---------------------------------------------------------------------------------------------

interface LimitInputProps {
  id: string;
  testId: string;
  label: string;
  unit: string;
  value: string;
  bad: boolean;
  badText: string;
  onChange: (next: string) => void;
  /** What the farm does with no limit, from the plan on screen. */
  guide: string | null | false;
  /** The guide is for another set of fields while a new plan is on its way. */
  stale: boolean;
}

function LimitInput({ id, testId, label, unit, value, bad, badText, onChange, guide, stale }: LimitInputProps) {
  return (
    <div className="farm-limit">
      <label htmlFor={id}>
        {label} <span className="muted">({unit})</span>
      </label>
      <input
        id={id}
        type="text"
        inputMode="decimal"
        autoComplete="off"
        placeholder="No limit"
        value={value}
        aria-invalid={bad}
        aria-describedby={`${id}-guide`}
        data-testid={testId}
        onChange={(event) => onChange(event.target.value)}
      />
      <p id={`${id}-guide`} className={bad ? "farm-guide is-bad" : stale ? "farm-guide is-stale" : "farm-guide"} data-testid={`${testId}-guide`}>
        {bad ? badText : guide || ""}
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// The result
// ---------------------------------------------------------------------------------------------

const STATUS_WORDS: Record<FarmPlan["status"], string> = {
  no_limits: "No limits set",
  limits_met: "Limits met",
  limits_not_met: "Limits cannot all be met",
};

function FarmResult({ answer, onOpenField }: { answer: FarmPlan; onOpenField: (id: string) => void }) {
  const { plan, alone, limits, settings } = answer;
  const years = plan.years.length;
  const limitSentences = [limits.water.sentence, limits.return.sentence].filter((sentence): sentence is string => !!sentence);
  // The limits get their own lines below, so they are not said twice.
  const summary = answer.summary.filter((sentence) => !limitSentences.includes(sentence));
  const models = settings.models.length;
  const pumps = plan.peak_water.value > 0 || alone.peak_water.value > 0;
  const crops = [...new Set(answer.fields.flatMap((field) => field.plan.years))];

  return (
    <>
      <div className={answer.status === "limits_not_met" ? "farm-callout farm-callout--short" : "farm-callout"} data-testid="farm-summary">
        <p className="farm-status" data-testid="farm-status">
          {STATUS_WORDS[answer.status]}
        </p>
        {summary.map((sentence, index) => (
          <p key={index}>{sentence}</p>
        ))}
      </div>

      {(limits.water.set || limits.return.set) && (
        <div className="farm-block" data-testid="farm-limit-lines">
          <h4>The limits</h4>
          {limits.water.set && <LimitLine name="water" limit={limits.water} />}
          {limits.return.set && <LimitLine name="return" limit={limits.return} />}
        </div>
      )}

      <div className="farm-block">
        <h4>The farm planned together, against each field planned alone</h4>
        <table className="farm-compare tabular" data-testid="farm-compare">
          <thead>
            <tr>
              <th scope="col">
                <span className="sr-only">Measure</span>
              </th>
              <th scope="col">Farm plan</th>
              <th scope="col">Each field alone</th>
            </tr>
          </thead>
          <tbody>
            <CompareRow name="score" label="Ranking score" hint="by acres; higher is better" plan={formatFarmScore(plan.score)} alone={formatFarmScore(alone.score)} />
            <CompareRow
              name="peak-water"
              label="Most water pumped in a year"
              hint="acre-inches"
              plan={`${formatWhole(plan.peak_water.value)} (year ${plan.peak_water.n})`}
              alone={`${formatWhole(alone.peak_water.value)} (year ${alone.peak_water.n})`}
            />
            <CompareRow name="average-water" label="Water in an average year" hint="acre-inches" plan={formatWhole(plan.average.water_acre_in)} alone={formatWhole(alone.average.water_acre_in)} />
            <CompareRow
              name="worst-return"
              label="Return in the leanest year"
              hint="over direct costs"
              plan={`${formatFarmDollars(plan.worst_return.value)} (year ${plan.worst_return.n})`}
              alone={`${formatFarmDollars(alone.worst_return.value)} (year ${alone.worst_return.n})`}
            />
            <CompareRow name="average-return" label="Return in an average year" hint="over direct costs" plan={formatFarmDollars(plan.average.return)} alone={formatFarmDollars(alone.average.return)} />
            <CompareRow
              name="water-ok"
              label="Within the water budget"
              plan={metWords(plan.water_ok, plan.water_years_over, years, "No budget set")}
              alone={metWords(alone.water_ok, alone.water_years_over, years, "No budget set")}
              tones={[plan.water_ok, alone.water_ok]}
            />
            <CompareRow
              name="return-ok"
              label="Meets the least return"
              plan={metWords(plan.return_ok, plan.return_years_under, years, "No minimum set")}
              alone={metWords(alone.return_ok, alone.return_years_under, years, "No minimum set")}
              tones={[plan.return_ok, alone.return_ok]}
            />
          </tbody>
        </table>
      </div>

      <div className="farm-block" data-testid="farm-years">
        <h4>Year by year</h4>
        <YearChart
          testId="farm-chart-return"
          title="Farm return over direct costs, dollars a year"
          years={plan.years}
          aloneYears={alone.years}
          pick={(year) => year.return}
          ok={(year) => year.return_ok}
          limit={limits.return.set ? limits.return.limit : null}
          limitWord="least return"
          format={formatFarmDollars}
          axis={shortDollars}
        />
        {plan.return_years_under.length > 0 && (
          <p className="farm-small farm-short-note" data-testid="farm-return-short">
            Hollow dots: under the least return in {plan.return_years_under.length} of {years} years.
          </p>
        )}
        {pumps ? (
          <YearChart
            testId="farm-chart-water"
            title="Farm water pumped, acre-inches a year"
            years={plan.years}
            aloneYears={alone.years}
            pick={(year) => year.water_acre_in}
            ok={(year) => year.water_ok}
            limit={limits.water.set ? limits.water.limit : null}
            limitWord="budget"
            format={formatWhole}
            axis={formatWhole}
          />
        ) : (
          <p className="farm-small muted" data-testid="farm-no-water">
            This farm pumps no water in any year, so there is no water chart.
          </p>
        )}
        {plan.water_years_over.length > 0 && (
          <p className="farm-small farm-short-note" data-testid="farm-water-over">
            Hollow dots: over the water budget in {plan.water_years_over.length} of {years} years.
          </p>
        )}
        <ul className="farm-legend" aria-label="How to read the charts">
          <li>
            <span className="farm-key farm-key--dot" aria-hidden="true" />
            Farm plan, average of the {models} climate models
          </li>
          <li>
            <span className="farm-key farm-key--range" aria-hidden="true" />
            Lowest to highest model
          </li>
          <li>
            <span className="farm-key farm-key--alone" aria-hidden="true" />
            Each field planned alone
          </li>
          {(limits.water.set || limits.return.set) && (
            <li>
              <span className="farm-key farm-key--limit" aria-hidden="true" />
              Your limit
            </li>
          )}
        </ul>
        <p className="farm-small muted">
          Years 1 to 12 are not calendar years: every year has the average weather of {settings.window[0]}–{settings.window[1]},
          so the chart shows how the rotations' good and lean years line up across the farm.
        </p>
        <details className="farm-details" data-testid="farm-years-details">
          <summary>The 12 years in numbers</summary>
          <div className="farm-scroll">
            <table className="farm-years-table tabular" data-testid="farm-years-table">
              <thead>
                <tr>
                  <th scope="col">Year</th>
                  <th scope="col">Return over direct costs</th>
                  <th scope="col">Water, acre-inches</th>
                </tr>
              </thead>
              <tbody>
                {plan.years.map((year) => (
                  <tr key={year.n} data-year={year.n}>
                    <th scope="row">{year.n}</th>
                    <td>
                      {formatFarmDollars(year.return.mid)}
                      <span className="muted"> ({rangeWords(year.return.low, year.return.high, formatFarmDollars)})</span>
                    </td>
                    <td>
                      {formatWhole(year.water_acre_in.mid)}
                      <span className="muted"> ({rangeWords(year.water_acre_in.low, year.water_acre_in.high, formatWhole)})</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="farm-small muted">In brackets: the lowest and the highest of the {models} climate models, for the whole farm.</p>
        </details>
      </div>

      <div className="farm-block">
        <div className="farm-block-head">
          <h4>The plan for each field</h4>
          <p className="farm-small muted tabular" data-testid="farm-changed">
            {plan.changed_fields} with a different rotation, {plan.shifted_fields} with another start year
          </p>
        </div>
        <ul className="farm-crops" aria-label="Crop colours">
          {crops.map((crop) => (
            <li key={crop}>
              <span className="swatch" style={{ background: cropBackground(crop) }} aria-hidden="true" />
              {cropLabel(crop)}
            </li>
          ))}
        </ul>
        <ul className="farm-rows" data-testid="farm-rows">
          {answer.fields.map((field) => (
            <FieldRow key={field.field_id} field={field} onOpen={() => onOpenField(field.field_id)} />
          ))}
        </ul>
        <p className="farm-small muted">Click a field to open it. Close it again to come back to the farm plan.</p>
      </div>

      {!answer.summary.includes(answer.solver.note) && (
        <p className="farm-small muted" data-testid="farm-solver">
          {answer.solver.note}
        </p>
      )}
      <details className="farm-details" data-testid="farm-notes">
        <summary>What this plan assumes and leaves out</summary>
        <ul className="farm-notes">
          {answer.notes.map((note, index) => (
            <li key={index}>{note}</li>
          ))}
        </ul>
        <p className="farm-notes-title">Not in the plan:</p>
        <ul className="farm-notes">
          {answer.not_modelled.map((note, index) => (
            <li key={index}>{note}</li>
          ))}
        </ul>
        <p className="farm-small muted">Worked out with {answer.solver.name}.</p>
      </details>
    </>
  );
}

function LimitLine({ name, limit }: { name: "water" | "return"; limit: FarmLimit }) {
  const tone = limitTone(limit);
  return (
    <p className="farm-limit-line" data-testid={`farm-limit-${name}`} data-tone={tone}>
      <span className={`farm-tag farm-tag--${tone}`}>{LIMIT_TAGS[tone]}</span> {limit.sentence}
    </p>
  );
}

interface CompareRowProps {
  name: string;
  label: string;
  hint?: string;
  plan: string;
  alone: string;
  /** For the yes / no rows: true, false or null (not set) for the plan and for "alone". */
  tones?: [boolean | null, boolean | null];
}

function CompareRow({ name, label, hint, plan, alone, tones }: CompareRowProps) {
  const tone = (ok: boolean | null | undefined) => (ok === false ? "farm-no" : undefined);
  return (
    <tr data-row={name}>
      <th scope="row">
        {label}
        {hint && <span className="farm-row-hint"> {hint}</span>}
      </th>
      <td className={tone(tones?.[0])} data-col="plan">
        {plan}
      </td>
      <td className={tone(tones?.[1])} data-col="alone">
        {alone}
      </td>
    </tr>
  );
}

function FieldRow({ field, onOpen }: { field: FarmField; onOpen: () => void }) {
  const change = fieldChange(field);
  return (
    <li>
      <button type="button" className="farm-row" data-testid="farm-row" data-field-id={field.field_id} data-change={change} onClick={onOpen}>
        <span className="farm-row-head">
          <span className="tabular">
            <strong>{field.field_id}</strong>
            <span className="muted">
              {" "}
              · {formatAcres(field.area_acres)} acres · {field.irrigated ? "irrigated" : "dryland"}
            </span>
          </span>
          <span className={`farm-tag farm-tag--${change}`} data-testid="farm-row-tag">
            {FIELD_TAGS[change]}
          </span>
        </span>
        <span className="farm-row-name" data-testid="farm-row-plan">
          {field.plan.name}
        </span>
        <span className="farm-row-years" role="img" aria-label={`Years 1 to ${field.plan.years.length}: ${field.plan.years.map(cropLabel).join(", ")}`}>
          {field.plan.years.map((crop, index) => (
            <span key={index} className="farm-row-year" style={{ background: cropBackground(crop) }} title={`Year ${index + 1}: ${cropLabel(crop)}`} data-crop={crop} />
          ))}
        </span>
        <span className="farm-row-sentence" data-testid="farm-row-sentence">
          {field.sentence}
        </span>
      </button>
    </li>
  );
}

// ---------------------------------------------------------------------------------------------
// The per-year strip: one measure, one axis. Bar = lowest to highest climate model, dot = their
// average, tick = each field planned alone, dashed line = the limit.
// ---------------------------------------------------------------------------------------------

const CHART = { width: 360, height: 150, left: 42, right: 6, top: 14, bottom: 24 };

interface YearChartProps {
  testId: string;
  title: string;
  years: FarmYear[];
  aloneYears: FarmYear[];
  pick: (year: FarmYear) => FarmBand;
  ok: (year: FarmYear) => boolean | null;
  limit: number | null;
  limitWord: string;
  /** full figures for the tooltips */
  format: (n: number) => string;
  /** short figures for the axis */
  axis: (n: number) => string;
}

function YearChart({ testId, title, years, aloneYears, pick, ok, limit, limitWord, format, axis }: YearChartProps) {
  const bands = years.map(pick);
  const others = aloneYears.map((year) => pick(year).mid);
  const scale = yearScale(bands, others, limit);
  const plotWidth = CHART.width - CHART.left - CHART.right;
  const plotHeight = CHART.height - CHART.top - CHART.bottom;
  const column = plotWidth / Math.max(1, years.length);
  const y = (value: number) => CHART.top + (1 - scale.at(value)) * plotHeight;
  const x = (index: number) => CHART.left + (index + 0.5) * column;
  const lows = bands.map((band) => band.mid);
  const label = `${title}. Farm plan: ${format(Math.min(...lows))} to ${format(Math.max(...lows))} across the ${years.length} years${
    limit === null ? "" : `; ${limitWord} ${format(limit)}`
  }.`;

  return (
    <figure className="farm-chart" data-testid={testId}>
      <figcaption>
        <span>{title}</span>
        {limit !== null && (
          <span className="farm-chart-limit tabular" data-testid={`${testId}-limit`}>
            <span className="farm-key farm-key--limit" aria-hidden="true" />
            {limitWord} {format(limit)}
          </span>
        )}
      </figcaption>
      <svg viewBox={`0 0 ${CHART.width} ${CHART.height}`} role="img" aria-label={label}>
        {scale.ticks.map((tick) => (
          <g key={tick}>
            <line className={tick === 0 ? "farm-zero" : "farm-grid"} x1={CHART.left} x2={CHART.width - CHART.right} y1={y(tick)} y2={y(tick)} />
            <text className="farm-axis" x={CHART.left - 6} y={y(tick) + 3.5} textAnchor="end">
              {axis(tick)}
            </text>
          </g>
        ))}
        {years.map((year, index) => {
          const band = pick(year);
          const other = others[index];
          const tip = `Year ${year.n}: ${format(band.mid)} (models ${rangeWords(band.low, band.high, format)})${
            other === undefined ? "" : `; each field alone ${format(other)}`
          }`;
          return (
            <g key={year.n} data-year={year.n}>
              <rect className="farm-hit" x={x(index) - column / 2} y={CHART.top} width={column} height={plotHeight}>
                <title>{tip}</title>
              </rect>
              <rect className="farm-range" x={x(index) - 4} y={y(band.high)} width={8} height={Math.max(2, y(band.low) - y(band.high))} rx={4} />
              {other !== undefined && <line className="farm-alone" x1={x(index) - 9} x2={x(index) + 9} y1={y(other)} y2={y(other)} />}
              <circle className={ok(year) === false ? "farm-dot farm-dot--short" : "farm-dot"} cx={x(index)} cy={y(band.mid)} r={4} />
              <text className="farm-axis" x={x(index)} y={CHART.height - 9} textAnchor="middle">
                {year.n}
              </text>
            </g>
          );
        })}
        <text className="farm-axis" x={CHART.left - 6} y={CHART.height - 9} textAnchor="end">
          Year
        </text>
        {limit !== null && <line className="farm-limit-rule" x1={CHART.left} x2={CHART.width - CHART.right} y1={y(limit)} y2={y(limit)} />}
      </svg>
    </figure>
  );
}

function FarmSection({ title, id, meta, children }: { title: string; id: string; meta?: ReactNode; children: ReactNode }) {
  return (
    <section className="section" aria-labelledby={id}>
      <div className="section-head">
        <h3 id={id}>{title}</h3>
        {meta ? <div className="section-meta">{meta}</div> : null}
      </div>
      {children}
    </section>
  );
}

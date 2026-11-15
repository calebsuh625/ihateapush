import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api } from "../api";
import { cropLabel } from "../crops";
import { usePlans } from "../hooks/usePlans";
import type { PlansState } from "../hooks/usePlans";
import { useResource } from "../hooks/useResource";
import type { Resource } from "../hooks/useResource";
import { formatOneDecimal, formatPercent, hectaresToAcres, plural } from "../lib/format";
import { emptySlots, slotsFrom } from "../lib/custom";
import type { Slot } from "../lib/custom";
import { plansQuery, spreadDomain } from "../lib/planner";
import type { PlannerChoices } from "../lib/planner";
import { organicMatterClass, slopeClass } from "../thresholds";
import type { FieldDetail, FieldSatellite, FieldSoil, Plan, Streak } from "../types";
import { Neighbors } from "./Neighbors";
import { OwnRotation } from "./OwnRotation";
import { PlanCard } from "./PlanCard";
import { CurrentPatternRow, ScoringNote } from "./PlanExtras";
import { PlannerControls } from "./PlannerControls";
import { SatelliteChart } from "./Satellite";
import { SoilTimelineChart } from "./SoilTimeline";
import { Timeline } from "./Timeline";

const EN_DASH = "–";
/** Height of the loaded satellite block at the 424 px panel (chart, summary, note). */
const SATELLITE_SKELETON_HEIGHT = 253;
/** Height of the loaded soil-timeline block at the 424 px panel. */
const SOIL_SKELETON_HEIGHT = 410;
/** "Try your own rotation" before the farmer has picked anything: four empty years. */
const NO_SLOTS: Slot[] = emptySlots();

interface FieldPanelProps {
  fieldId: string;
  /** Area from the map layer, shown at once while the detail request is in flight. */
  areaHa: number | null;
  onClose: () => void;
  /** Planner sliders, future and scenario: owned by the app, so they persist across fields. */
  plannerChoices: PlannerChoices;
  onPlannerChoices: (next: PlannerChoices) => void;
}

export function FieldPanel({ fieldId, areaHa, onClose, plannerChoices, onPlannerChoices }: FieldPanelProps) {
  // Two independent requests, started together; each can fail and be retried on its own.
  const [detail, retryDetail] = useResource(fieldId, api.field);
  // The irrigated / dryland override belongs to one field: it is forgotten when another is opened.
  const [override, setOverride] = useState<{ fieldId: string; value: boolean } | null>(null);
  const irrigationOverride = override !== null && override.fieldId === fieldId ? override.value : null;
  const plannerQuery = plansQuery(plannerChoices, irrigationOverride);
  const planner = usePlans(fieldId, plannerQuery);
  const plans = planner.resource;
  // The farmer's own rotation also belongs to one field. `ownJump` counts the times a plan card sent
  // its rotation there, so the section can bring itself into view.
  const [own, setOwn] = useState<{ fieldId: string; slots: Slot[] } | null>(null);
  const ownSlots = own !== null && own.fieldId === fieldId ? own.slots : NO_SLOTS;
  const [ownJump, setOwnJump] = useState(0);
  const editPlan = (plan: Plan) => {
    const slots = slotsFrom(plan.cycle ?? plan.rotation);
    if (slots === null) return;
    setOwn({ fieldId, slots });
    setOwnJump((n) => n + 1);
  };
  // Only the future-weather planner can score a rotation of the farmer's own.
  const smartPlans = plans.status === "ready" && plans.data.engine === "smart" && plans.data.settings !== undefined ? plans.data : null;
  // A third, optional one: the satellite numbers. Its failure stays inside its own section.
  const [satellite, retrySatellite] = useResource(fieldId, api.satellite);
  // And a fourth: the yearly soil condition table. Same rule: its failure stays in its own section.
  const [soil, retrySoil] = useResource(fieldId, api.soil);

  const bodyRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (bodyRef.current) bodyRef.current.scrollTop = 0;
  }, [fieldId]);

  const area = detail.status === "ready" ? detail.data.area_ha : areaHa;
  // A link to a field that does not exist: say so once, and do not offer plans for it.
  const unknownField = detail.status === "error" && detail.notFound;
  const state =
    detail.status === "ready" && plans.status === "ready"
      ? "ready"
      : detail.status === "error" || plans.status === "error"
        ? "error"
        : "loading";

  return (
    <aside
      className="panel"
      aria-labelledby="panel-title"
      data-testid="panel"
      data-field-id={fieldId}
      data-state={state}
    >
      <header className="panel-head">
        <div className="panel-heading">
          <p className="eyebrow">Field</p>
          <h2 id="panel-title" className="tabular" data-testid="panel-title">
            {fieldId}
          </h2>
          <p className="panel-area tabular" data-testid="panel-area">
            {area === null ? (
              detail.status === "loading" && <span className="skeleton skeleton-text" style={{ width: 132 }} />
            ) : (
              <>
                {formatOneDecimal(area)} ha <span className="muted">· {formatOneDecimal(hectaresToAcres(area))} acres</span>
              </>
            )}
          </p>
        </div>
        <button type="button" className="icon-button" onClick={onClose} aria-label="Close field panel" title="Close (Esc)">
          <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
            <path d="M3.5 3.5l9 9m0-9l-9 9" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" fill="none" />
          </svg>
        </button>
      </header>

      <div className="panel-body" ref={bodyRef} aria-busy={state === "loading"}>
        <DetailSections detail={detail} onRetry={retryDetail} satellite={satellite} onRetrySatellite={retrySatellite} soil={soil} onRetrySoil={retrySoil} />
        {!unknownField && (
          <PlansSection
            planner={planner}
            choices={plannerChoices}
            onChoices={onPlannerChoices}
            irrigationOverride={irrigationOverride}
            onIrrigationOverride={(value) => setOverride(value === null ? null : { fieldId, value })}
            onEditPlan={editPlan}
          />
        )}
        {!unknownField && smartPlans && (
          <OwnRotation
            key={fieldId}
            fieldId={fieldId}
            plansQuery={plannerQuery}
            slots={ownSlots}
            onSlots={(slots) => setOwn({ fieldId, slots })}
            currentCycle={slotsFrom(smartPlans.current?.cycle ?? smartPlans.current?.rotation)}
            jump={ownJump}
          />
        )}
        {!unknownField && <Neighbors fieldId={fieldId} />}
      </div>
    </aside>
  );
}

// ---------------------------------------------------------------------------------------------
// Crop history + soil (GET /field/{id})
// ---------------------------------------------------------------------------------------------

interface DetailSectionsProps {
  detail: Resource<FieldDetail>;
  onRetry: () => void;
  satellite: Resource<FieldSatellite>;
  onRetrySatellite: () => void;
  soil: Resource<FieldSoil>;
  onRetrySoil: () => void;
}

function DetailSections({ detail, onRetry, satellite, onRetrySatellite, soil, onRetrySoil }: DetailSectionsProps) {
  if (detail.status === "error") {
    return (
      <Section title="Crop history and soil" id="section-detail">
        {detail.notFound ? (
          <ErrorNote
            title="This field is not in the current data set"
            message={`${detail.message} The link may have been made with different data — pick a field on the map instead.`}
          />
        ) : (
          <ErrorNote title="Could not load this field" message={detail.message} onRetry={onRetry} />
        )}
      </Section>
    );
  }
  if (detail.status === "loading") {
    return (
      <>
        <Section title="Crop history" id="section-history">
          {/* Sized like the real content (one legend row), so little moves when it arrives. */}
          <div className="skeleton-block" aria-hidden="true">
            <div className="skeleton" style={{ height: 34 }} />
            <div className="skeleton skeleton-text" style={{ width: "100%", marginTop: 8 }} />
            <div className="skeleton skeleton-text" style={{ width: "62%", marginTop: 14 }} />
            <div className="skeleton" style={{ height: 105, marginTop: 16 }} />
          </div>
          <LoadingNote what="crop history" />
        </Section>
        <SatelliteSection history={null} satellite={satellite} onRetry={onRetrySatellite} />
        <SoilSection history={null} soil={soil} onRetry={onRetrySoil} />
        <Section title="Soil and terrain" id="section-soil">
          <div className="tiles skeleton-block" aria-hidden="true">
            <div className="skeleton" style={{ height: 93 }} />
            <div className="skeleton" style={{ height: 93 }} />
          </div>
        </Section>
      </>
    );
  }

  const field = detail.data;
  const years = Object.keys(field.crop_history)
    .map(Number)
    .filter((y) => Number.isInteger(y));
  const firstYear = Math.min(...years);
  const lastYear = Math.max(...years);
  const span = years.length ? lastYear - firstYear + 1 : 0;
  const cornYears = Object.values(field.crop_history).filter((crop) => crop === "corn").length;
  const { stats } = field;

  return (
    <>
      <Section
        title="Crop history"
        id="section-history"
        meta={
          years.length > 0 && (
            <span className="tabular" data-testid="history-range">
              {firstYear === lastYear ? firstYear : `${firstYear}${EN_DASH}${lastYear}`} ·{" "}
              {years.length === span ? `${span} ${plural(span, "year")}` : `${years.length} of ${span} years on record`}
            </span>
          )
        }
      >
        <Timeline key={field.field_id} history={field.crop_history} />
        <dl className="facts">
          <div>
            <dt>Distinct crops</dt>
            <dd className="tabular" data-testid="stat-distinct">
              {stats.distinct_crop_count}
            </dd>
          </div>
          <div>
            <dt title="Most years in a row with the same crop">Longest streak</dt>
            <dd className="tabular" data-testid="stat-streak">
              {streakText(stats.longest_streak)}
            </dd>
          </div>
          <div>
            <dt>Years in corn</dt>
            <dd className="tabular" data-testid="stat-corn">
              {formatPercent(stats.pct_years_corn)}{" "}
              <span className="muted">
                · {cornYears} of {stats.years_covered}
              </span>
            </dd>
          </div>
        </dl>
      </Section>

      <SatelliteSection history={field.crop_history} satellite={satellite} onRetry={onRetrySatellite} />

      <SoilSection history={field.crop_history} soil={soil} onRetry={onRetrySoil} />

      <Section title="Soil and terrain" id="section-soil">
        <div className="tiles">
          <SoilTile
            label="Organic matter"
            hint="Topsoil, share by weight"
            missing="Not in the soil survey"
            value={field.organic_matter_pct}
            classify={organicMatterClass}
            testId="stat-om"
          />
          <SoilTile
            label="Slope"
            hint="Average rise over run"
            missing="No elevation data here"
            value={field.slope_pct}
            classify={slopeClass}
            testId="stat-slope"
          />
        </div>
      </Section>
    </>
  );
}

// ---------------------------------------------------------------------------------------------
// Satellite (GET /field/{id}/satellite) — optional, fails on its own
// ---------------------------------------------------------------------------------------------

const UNAVAILABLE_TEXT = {
  not_built: "Satellite data has not been built yet (make satellite).",
  no_data_for_field: "No satellite data for this field.",
} as const;

/** `history` is null while the crop history is still loading (the chart needs it to line up). */
function SatelliteSection({
  history,
  satellite,
  onRetry,
}: {
  history: Record<string, string> | null;
  satellite: Resource<FieldSatellite>;
  onRetry: () => void;
}) {
  const waiting = satellite.status === "loading" || (satellite.status === "ready" && satellite.data.available && history === null);
  const state = waiting ? "loading" : satellite.status === "error" ? "error" : "ready";
  const data = satellite.status === "ready" ? satellite.data : null;
  return (
    <Section title="Satellite" id="section-satellite" meta="Landsat · yearly">
      <div data-testid="satellite" data-state={state} data-available={data ? String(data.available) : undefined}>
        {waiting && (
          <>
            {/* Same height as the loaded chart + summary + note, so nothing jumps. */}
            <div className="skeleton-block" aria-hidden="true">
              <div className="skeleton" style={{ height: SATELLITE_SKELETON_HEIGHT }} />
            </div>
            <LoadingNote what="satellite numbers" />
          </>
        )}
        {satellite.status === "error" && (
          <ErrorNote title="Could not load satellite data" message={satellite.message} onRetry={onRetry} />
        )}
        {data && !data.available && (
          <p className="muted" data-testid="sat-unavailable">
            {UNAVAILABLE_TEXT[data.reason ?? "no_data_for_field"]}
          </p>
        )}
        {data && data.available && history !== null && <SatelliteChart history={history} satellite={data} />}
      </div>
    </Section>
  );
}

// ---------------------------------------------------------------------------------------------
// Soil over time (GET /field/{id}/soil) — optional, fails on its own
// ---------------------------------------------------------------------------------------------

const SOIL_UNAVAILABLE_TEXT = {
  not_built: "Soil timeline has not been built yet (make soil-health).",
  no_data_for_field: "No soil timeline for this field.",
} as const;

function SoilSection({
  history,
  soil,
  onRetry,
}: {
  history: Record<string, string> | null;
  soil: Resource<FieldSoil>;
  onRetry: () => void;
}) {
  const waiting = soil.status === "loading" || (soil.status === "ready" && soil.data.available && history === null);
  const state = waiting ? "loading" : soil.status === "error" ? "error" : "ready";
  const data = soil.status === "ready" ? soil.data : null;
  return (
    <Section title="Soil over time" id="section-soil-timeline" meta="Satellite estimate · yearly">
      <div data-testid="soil-timeline" data-state={state} data-available={data ? String(data.available) : undefined}>
        {waiting && (
          <>
            <div className="skeleton-block" aria-hidden="true">
              <div className="skeleton" style={{ height: SOIL_SKELETON_HEIGHT }} />
            </div>
            <LoadingNote what="the soil timeline" />
          </>
        )}
        {soil.status === "error" && (
          <ErrorNote title="Could not load the soil timeline" message={soil.message} onRetry={onRetry} />
        )}
        {data && !data.available && (
          <p className="muted" data-testid="soil-unavailable">
            {SOIL_UNAVAILABLE_TEXT[data.reason ?? "no_data_for_field"]}
          </p>
        )}
        {data && data.available && history !== null && <SoilTimelineChart history={history} soil={data} />}
      </div>
    </Section>
  );
}

/** "Corn × 9, 2010–2018" */
function streakText(streak: Streak | null): string {
  if (!streak) return "No data";
  if (streak.years <= 1) return "No crop grown two years running";
  return `${cropLabel(streak.crop)} × ${streak.years}, ${streak.start_year}${EN_DASH}${streak.end_year}`;
}

interface SoilTileProps {
  label: string;
  /** What the number is. */
  hint: string;
  /** Shown instead of the hint when there is no number. */
  missing: string;
  value: number | null;
  classify: (value: number) => string;
  testId: string;
}

function SoilTile({ label, hint, missing, value, classify, testId }: SoilTileProps) {
  return (
    <div className="tile">
      <p className="tile-label">{label}</p>
      <p className="tile-value" data-testid={testId}>
        {value === null ? (
          <span className="tile-none">No data</span>
        ) : (
          <>
            <span className="tile-number">{formatPercent(value)}</span>
            <span className="tag">{classify(value)}</span>
          </>
        )}
      </p>
      <p className="tile-hint">{value === null ? missing : hint}</p>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// Rotation plans (GET /field/{id}/plans)
// ---------------------------------------------------------------------------------------------

interface PlansSectionProps {
  planner: PlansState;
  choices: PlannerChoices;
  onChoices: (next: PlannerChoices) => void;
  irrigationOverride: boolean | null;
  onIrrigationOverride: (value: boolean | null) => void;
  /** Send a plan's rotation to "Try your own rotation". */
  onEditPlan: (plan: Plan) => void;
}

function PlansSection({ planner, choices, onChoices, irrigationOverride, onIrrigationOverride, onEditPlan }: PlansSectionProps) {
  const plans = planner.resource;
  const data = plans.status === "ready" ? plans.data : null;
  // The future-weather planner answered: show its controls and the extra parts of the cards.
  const smart = data !== null && data.engine === "smart" && data.settings !== undefined && data.climate !== undefined;
  const settings = smart ? data.settings : undefined;
  const climate = smart ? data.climate : undefined;
  const profitDomain = data && smart ? spreadDomain(data.plans.map((plan) => plan.spread?.profit_per_acre)) : undefined;
  return (
    <Section title="Rotation plans" id="section-plans" meta="Next 4 years · scores 0–1, higher is better">
      {plans.status === "error" && (
        <ErrorNote title="Could not load rotation plans" message={plans.message} onRetry={planner.retry} />
      )}
      {plans.status === "loading" && (
        <>
          <div className="plans skeleton-block" aria-hidden="true">
            <div className="skeleton" style={{ height: 268 }} />
            <div className="skeleton" style={{ height: 268 }} />
            <div className="skeleton" style={{ height: 268 }} />
          </div>
          <LoadingNote what="rotation plans" />
        </>
      )}
      {data && (
        <>
          {settings && climate && (
            <PlannerControls
              choices={choices}
              onChange={onChoices}
              settings={settings}
              climate={climate}
              irrigationOverride={irrigationOverride}
              onIrrigationOverride={onIrrigationOverride}
            />
          )}
          {planner.updateError !== null && (
            <p className="plans-update-error" role="alert" data-testid="plans-update-error">
              <span>Could not update the plans, so these are the last ones we worked out. {planner.updateError}</span>
              <button type="button" className="link-button" onClick={planner.retry}>
                Try again
              </button>
            </p>
          )}
          {/* The previous cards stay up, dimmed, while a newer answer is on its way. */}
          <div
            className={planner.updating ? "plans-live is-updating" : "plans-live"}
            aria-live="polite"
            aria-busy={planner.updating}
            data-testid="plans-region"
            data-engine={data.engine ?? "basic"}
            data-updating={planner.updating ? "true" : "false"}
            data-query={planner.shownQuery ?? undefined}
          >
            {data.plans.length === 0 ? (
              <p className="muted">No rotation plans are available for this field.</p>
            ) : (
              <ol className="plans" data-testid="plans">
                {data.plans.slice(0, 3).map((plan, index) => (
                  <PlanCard
                    key={index}
                    plan={plan}
                    rank={index + 1}
                    irrigated={settings?.irrigated}
                    profitDomain={profitDomain}
                    candidates={data.candidates}
                    irrigationLimitIn={settings?.irrigation_limit_in}
                    onEdit={smart ? () => onEditPlan(plan) : undefined}
                  />
                ))}
              </ol>
            )}
            {settings && <CurrentPatternRow current={data.current} note={data.current_note} candidates={data.candidates} settings={settings} />}
          </div>
          {settings ? (
            <ScoringNote settings={settings} />
          ) : (
            <p className="plans-basic-note muted" data-testid="plans-basic-note">
              Future-weather planning is not set up on this server, so these plans do not look at the climate ahead.
            </p>
          )}
        </>
      )}
    </Section>
  );
}

// ---------------------------------------------------------------------------------------------
// Small pieces
// ---------------------------------------------------------------------------------------------

function Section({ title, id, meta, children }: { title: string; id: string; meta?: ReactNode; children: ReactNode }) {
  return (
    <section className="section" aria-labelledby={id}>
      <div className="section-head">
        <h3 id={id}>{title}</h3>
        {meta ? <p className="section-meta">{meta}</p> : null}
      </div>
      {children}
    </section>
  );
}

/** Announced to screen readers; sighted users see the skeleton. */
function LoadingNote({ what }: { what: string }) {
  return (
    <p className="sr-only" role="status">
      Loading {what}…
    </p>
  );
}

/** A failure, in words, with a Retry button when trying again could help. */
export function ErrorNote({ title, message, onRetry }: { title: string; message: string; onRetry?: () => void }) {
  return (
    <div className="error-note" role="alert" data-testid="error-note">
      <div>
        <p className="error-title">{title}</p>
        <p className="error-message">{message}</p>
      </div>
      {onRetry && (
        <button type="button" className="button" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

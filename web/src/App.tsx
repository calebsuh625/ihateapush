import { useCallback, useEffect, useMemo, useState } from "react";
import { API_BASE, api, apiUrl, getJson } from "./api";
import { AdvisorPanel } from "./components/AdvisorPanel";
import { FarmPanel } from "./components/FarmPanel";
import { TwinView } from "./components/TwinView";
import { FieldMap } from "./components/FieldMap";
import type { FarmTool } from "./components/FieldMap";
import { ErrorNote, FieldPanel } from "./components/FieldPanel";
import type { FieldFacts } from "./components/FieldPanel";
import { LayerControl } from "./components/LayerControl";
import { APP_NAME, REGION_LABEL } from "./config";
import { useAdvisor } from "./hooks/useAdvisor";
import { useResource } from "./hooks/useResource";
import type { Resource } from "./hooks/useResource";
import { useMapLayers } from "./hooks/useMapLayers";
import { useSelection } from "./hooks/useSelection";
import { advisorContext, exampleQuestions } from "./lib/advisor";
import type { Subject } from "./lib/advisor";
import { addFields, toggleField } from "./lib/farm";
import { sheetHeight } from "./lib/twin";
import type { TwinAnswer } from "./lib/twin";
import { formatCount, plural } from "./lib/format";
import { DEFAULT_CHOICES } from "./lib/planner";
import type { PlannerChoices } from "./lib/planner";
import type { Health } from "./types";

const loadFields = (_key: string, signal: AbortSignal) => api.fields(signal);
const loadHealth = (_key: string, signal: AbortSignal) => api.health(signal);
const loadAdvisorStatus = (_key: string, signal: AbortSignal) => api.advisorStatus(signal);
const loadTwin = (fieldId: string, signal: AbortSignal) =>
  getJson<TwinAnswer>(
    `/field/${encodeURIComponent(fieldId)}/twin`,
    (b) => typeof b === "object" && b !== null && (b as { field_id?: unknown }).field_id === fieldId && "has_twin" in b,
    signal,
  );

/** The farm being planned: its fields in the order they were picked, and the two limit boxes as typed. */
interface Farm {
  ids: string[];
  water: string;
  minimum: string;
}

const NO_FARM: Farm = { ids: [], water: "", minimum: "" };
const FARM_STORE = "crop-rotation-farm";

/** The farm kept in this browser, so a reload does not lose a farm picked field by field. */
function storedFarm(): Farm {
  try {
    const saved: unknown = JSON.parse(window.localStorage.getItem(FARM_STORE) ?? "null");
    if (typeof saved !== "object" || saved === null) return NO_FARM;
    const { ids, water, minimum } = saved as Record<string, unknown>;
    return {
      ids: Array.isArray(ids) ? [...new Set(ids.filter((id): id is string => typeof id === "string"))] : [],
      water: typeof water === "string" ? water : "",
      minimum: typeof minimum === "string" ? minimum : "",
    };
  } catch {
    return NO_FARM; // no storage, or something else's text under our key
  }
}

const FARM_TOOLS: readonly { id: FarmTool; label: string; title: string }[] = [
  { id: "click", label: "Click", title: "Click a field to add it to the farm or take it out" },
  { id: "loop", label: "Lasso", title: "Draw a loop: every field whose centre is inside is added" },
  { id: "box", label: "Box", title: "Drag a box: every field whose centre is inside is added" },
];

export default function App() {
  // Loaded once; selecting a field never refetches these.
  const [fields, retryFields] = useResource("fields", loadFields);
  const [health, retryHealth] = useResource("health", loadHealth);
  const { selection, select, clear } = useSelection();
  const [drawn, setDrawn] = useState<number | null>(null);
  // The planner sliders, future and scenario live here so they carry over from field to field,
  // and between a field and the farm.
  const [plannerChoices, setPlannerChoices] = useState<PlannerChoices>(DEFAULT_CHOICES);
  // "Plan a farm": while it is on, the map picks fields for the farm instead of opening one.
  const [farmMode, setFarmMode] = useState(false);
  const [farm, setFarm] = useState<Farm>(storedFarm);
  const [tool, setTool] = useState<FarmTool>("click");
  // "Compare with its twin": the field whose twin view is open. It is only open while that field is
  // the selected one, so opening another field (the twin, from the map) closes it.
  const [compare, setCompare] = useState<string | null>(null);
  const compareId = compare !== null && selection?.id === compare ? compare : null;
  const [twinRes, retryTwin] = useResource(compareId, loadTwin);
  const twinId = compareId !== null && twinRes.status === "ready" && twinRes.data.has_twin ? (twinRes.data.twin?.field_id ?? null) : null;
  const twinHeight = sheetHeight(window.innerHeight);
  // The farm advisor: the chat is about the open field, or about the farm when no field is open. Its
  // conversation lives here, so it is kept while the panel is closed.
  const [advisorOpen, setAdvisorOpen] = useState(false);
  const [advisorOpens, setAdvisorOpens] = useState(0); // asks /advisor/status again every time the panel is opened
  const [facts, setFacts] = useState<FieldFacts | null>(null);

  const fieldData = fields.status === "ready" ? fields.data : null;
  // The map layer (colour by bare days, erosion risk …) and its year live here, so they stay as they are
  // when a field is opened or closed.
  const layers = useMapLayers(fieldData);
  const areaById = useMemo(() => {
    const areas = new Map<string, number>();
    for (const feature of fieldData?.features ?? []) {
      const id = feature.id ?? feature.properties?.field_id;
      const area = feature.properties?.area_ha;
      if (id && typeof area === "number") areas.set(String(id), area);
    }
    return areas;
  }, [fieldData]);
  // A farm saved with other data may name fields that are gone: only the ones on the map count.
  const farmIds = useMemo(() => farm.ids.filter((id) => areaById.has(id)), [farm.ids, areaById]);

  const selectedId = selection?.id ?? null;
  const subject = useMemo<Subject>(() => (selectedId !== null ? { kind: "field", id: selectedId } : farmMode ? { kind: "farm" } : null), [selectedId, farmMode]);
  const factsNow = facts !== null && facts.fieldId === selectedId ? facts : null;
  const advisor = useAdvisor(subject, advisorContext(subject, farmIds, plannerChoices, factsNow?.irrigationOverride ?? null));
  const advisorShown = advisorOpen && subject !== null;
  const [advisorStatus, retryAdvisorStatus] = useResource(advisorOpens > 0 && advisorShown ? `advisor-${advisorOpens}` : null, loadAdvisorStatus);
  const toggleAdvisor = useCallback(() => {
    setAdvisorOpen((open) => !open);
    setAdvisorOpens((n) => n + 1);
  }, []);
  // Nothing open (the field panel was closed): the advisor goes with it, its conversation stays.
  useEffect(() => {
    if (subject === null) setAdvisorOpen(false);
  }, [subject]);

  useEffect(() => {
    try {
      window.localStorage.setItem(FARM_STORE, JSON.stringify(farm));
    } catch {
      // storage is full or switched off: the farm then lasts until the page is reloaded
    }
  }, [farm]);

  useEffect(() => {
    if (!selection && !advisorShown) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      if (advisorShown) setAdvisorOpen(false); // the advisor closes first, then the comparison, then the field
      else if (compareId !== null) setCompare(null);
      else clear();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [selection, clear, compareId, advisorShown]);

  // A comparison left open for a field that is no longer selected is forgotten.
  useEffect(() => {
    if (compare !== null && selection?.id !== compare) setCompare(null);
  }, [compare, selection]);

  useEffect(() => {
    document.title = selection ? `${selection.id} · ${APP_NAME}` : farmMode ? `Farm plan · ${APP_NAME}` : APP_NAME;
  }, [selection, farmMode]);

  const retryAll = useCallback(() => {
    retryFields();
    retryHealth();
  }, [retryFields, retryHealth]);

  // In farm mode the map edits the farm: a click adds or removes a field and brings the farm
  // panel back if a field was open. Outside it a click opens the field, as ever.
  const pick = useCallback(
    (id: string) => {
      if (!farmMode) {
        select(id);
        return;
      }
      setFarm((current) => ({ ...current, ids: toggleField(current.ids, id) }));
      clear();
    },
    [farmMode, select, clear],
  );
  const lasso = useCallback(
    (ids: string[]) => {
      if (ids.length > 0) setFarm((current) => ({ ...current, ids: addFields(current.ids, ids) }));
      clear();
    },
    [clear],
  );
  const toggleFarmMode = () => {
    setTool("click");
    setFarmMode((on) => !on);
    clear(); // entering shows the farm panel; leaving ends with the plain map
  };
  const clearFarm = () => setFarm((current) => ({ ...current, ids: [] }));

  const fieldCount = fieldData ? fieldData.features.length : null;
  const mode = farmMode ? "farm" : "field";

  return (
    <div
      className={advisorShown ? "app app--advisor" : "app"}
      data-testid="app"
      data-api-base={API_BASE}
      data-fields={fields.status}
      data-fields-drawn={fieldData && drawn !== null ? drawn : undefined}
      data-mode={mode}
      data-advisor={advisorShown ? "open" : "closed"}
    >
      <main className="map-wrap">
        <FieldMap
          fields={fieldData}
          selection={selection}
          onSelect={pick}
          onRendered={setDrawn}
          farm={farmMode ? farmIds : null}
          tool={tool}
          onLasso={lasso}
          twin={twinId}
          twinCover={twinHeight}
          fills={layers.fills}
          onView={layers.setViewFields}
        />

        <div className="masthead">
          <h1>{APP_NAME}</h1>
          {fields.status !== "error" && <DataBadge health={health} fieldCount={fieldCount} />}
          {fields.status === "loading" && (
            <p className="masthead-note" role="status">
              <span className="spinner" aria-hidden="true" />
              Loading fields…
            </p>
          )}
          {fieldData && !selection && !farmMode && (
            <p className="masthead-note" data-testid="hint">
              {fieldCount === 0
                ? "The data set has no fields yet."
                : "Click a field to see its crop history, soil and rotation plans"}
            </p>
          )}
          {fieldData && fieldCount !== 0 && (
            <div className="farm-tools" data-testid="farm-tools">
              <div className="farm-tools-line">
                <button
                  type="button"
                  className={farmMode ? "toggle-button is-on" : "toggle-button"}
                  aria-pressed={farmMode}
                  data-testid="farm-toggle"
                  onClick={toggleFarmMode}
                >
                  Plan a farm
                </button>
                {farmMode && (
                  <p className="masthead-note">
                    <span data-testid="farm-count" className="tabular">
                      {formatCount(farmIds.length)} {plural(farmIds.length, "field")} picked
                    </span>
                    <button type="button" className="link-button" data-testid="farm-clear" disabled={farmIds.length === 0} onClick={clearFarm}>
                      Clear
                    </button>
                  </p>
                )}
              </div>
              {farmMode && (
                <>
                  <div className="segmented segmented--three" role="radiogroup" aria-label="How to pick fields" data-testid="farm-tool">
                    {FARM_TOOLS.map((option) => (
                      <label key={option.id} className="segmented-option" title={option.title}>
                        <input
                          type="radio"
                          name="farm-tool"
                          value={option.id}
                          checked={tool === option.id}
                          data-testid={`farm-tool-${option.id}`}
                          onChange={() => setTool(option.id)}
                        />
                        <span>{option.label}</span>
                      </label>
                    ))}
                  </div>
                  <p className="masthead-note farm-hint" data-testid="farm-hint">
                    {tool === "click"
                      ? "Click a field to add it to the farm or take it out."
                      : tool === "loop"
                        ? "Draw a loop on the map. Fields whose centre is inside are added."
                        : "Drag a box on the map. Fields whose centre is inside are added."}
                  </p>
                </>
              )}
            </div>
          )}
        </div>

        {fieldData && fieldCount !== 0 && <LayerControl state={layers} />}

        {fields.status === "error" && (
          <div className="map-error" data-testid="fields-error">
            <ErrorNote
              title="Could not load the fields"
              message={`${fields.message}. The app expects the API at ${apiUrl()} — check that it is running (make api), then retry.`}
              onRetry={retryAll}
            />
          </div>
        )}
      </main>

      {selection ? (
        <FieldPanel
          fieldId={selection.id}
          areaHa={areaById.get(selection.id) ?? null}
          onClose={clear}
          plannerChoices={plannerChoices}
          onPlannerChoices={setPlannerChoices}
          back={farmMode ? { label: "Back to the farm plan", onClick: clear } : undefined}
          twin={{ open: compareId !== null, onToggle: () => setCompare(compareId !== null ? null : selection.id) }}
          advisor={{ open: advisorShown, onToggle: toggleAdvisor }}
          onFacts={setFacts}
        />
      ) : (
        farmMode && (
          <FarmPanel
            ids={farmIds}
            areaById={areaById}
            choices={plannerChoices}
            onChoices={setPlannerChoices}
            water={farm.water}
            minimum={farm.minimum}
            onLimits={(water, minimum) => setFarm((current) => ({ ...current, water, minimum }))}
            onRemove={(id) => setFarm((current) => ({ ...current, ids: current.ids.filter((other) => other !== id) }))}
            onClear={clearFarm}
            onOpenField={select}
            onClose={toggleFarmMode}
            advisor={{ open: advisorShown, onToggle: toggleAdvisor }}
          />
        )
      )}

      {advisorShown && subject !== null && (
        <AdvisorPanel
          subject={subject}
          status={advisorStatus}
          onRetryStatus={retryAdvisorStatus}
          advisor={advisor}
          examples={exampleQuestions(subject, factsNow?.irrigated ?? null)}
          onClose={() => setAdvisorOpen(false)}
        />
      )}

      {compareId !== null && (
        <TwinView
          fieldId={compareId}
          res={twinRes}
          onRetry={retryTwin}
          onClose={() => setCompare(null)}
          onOpenField={select}
          height={twinHeight}
        />
      )}
    </div>
  );
}

/** "Demo data — 30 fake fields" or "8,412 fields · Finney County, KS". */
function DataBadge({ health, fieldCount }: { health: Resource<Health>; fieldCount: number | null }) {
  if (health.status === "ready") {
    const { is_fake: isFake, field_count: count } = health.data;
    return isFake ? (
      <p className="badge badge--demo" data-testid="badge" data-fake="true">
        Demo data — {formatCount(count)} fake {plural(count, "field")}
      </p>
    ) : (
      <p className="badge" data-testid="badge" data-fake="false">
        {formatCount(count)} {plural(count, "field")} · {REGION_LABEL}
      </p>
    );
  }
  // /health is slow or down: say what we know from the fields themselves, and no more.
  if (fieldCount !== null) {
    return (
      <p className="badge" data-testid="badge">
        {formatCount(fieldCount)} {plural(fieldCount, "field")}
      </p>
    );
  }
  return null;
}

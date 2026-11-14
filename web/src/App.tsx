import { useCallback, useEffect, useMemo, useState } from "react";
import { API_BASE, api, apiUrl } from "./api";
import { FieldMap } from "./components/FieldMap";
import { ErrorNote, FieldPanel } from "./components/FieldPanel";
import { APP_NAME, REGION_LABEL } from "./config";
import { useResource } from "./hooks/useResource";
import type { Resource } from "./hooks/useResource";
import { useSelection } from "./hooks/useSelection";
import { formatCount, plural } from "./lib/format";
import type { Health } from "./types";

const loadFields = (_key: string, signal: AbortSignal) => api.fields(signal);
const loadHealth = (_key: string, signal: AbortSignal) => api.health(signal);

export default function App() {
  // Loaded once; selecting a field never refetches these.
  const [fields, retryFields] = useResource("fields", loadFields);
  const [health, retryHealth] = useResource("health", loadHealth);
  const { selection, select, clear } = useSelection();
  const [drawn, setDrawn] = useState<number | null>(null);

  const fieldData = fields.status === "ready" ? fields.data : null;
  const areaById = useMemo(() => {
    const areas = new Map<string, number>();
    for (const feature of fieldData?.features ?? []) {
      const id = feature.id ?? feature.properties?.field_id;
      const area = feature.properties?.area_ha;
      if (id && typeof area === "number") areas.set(String(id), area);
    }
    return areas;
  }, [fieldData]);

  useEffect(() => {
    if (!selection) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.defaultPrevented) clear();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [selection, clear]);

  useEffect(() => {
    document.title = selection ? `${selection.id} · ${APP_NAME}` : APP_NAME;
  }, [selection]);

  const retryAll = useCallback(() => {
    retryFields();
    retryHealth();
  }, [retryFields, retryHealth]);

  const fieldCount = fieldData ? fieldData.features.length : null;

  return (
    <div
      className="app"
      data-testid="app"
      data-api-base={API_BASE}
      data-fields={fields.status}
      data-fields-drawn={fieldData && drawn !== null ? drawn : undefined}
    >
      <main className="map-wrap">
        <FieldMap fields={fieldData} selection={selection} onSelect={select} onRendered={setDrawn} />

        <div className="masthead">
          <h1>{APP_NAME}</h1>
          {fields.status !== "error" && <DataBadge health={health} fieldCount={fieldCount} />}
          {fields.status === "loading" && (
            <p className="masthead-note" role="status">
              <span className="spinner" aria-hidden="true" />
              Loading fields…
            </p>
          )}
          {fieldData && !selection && (
            <p className="masthead-note" data-testid="hint">
              {fieldCount === 0
                ? "The data set has no fields yet."
                : "Click a field to see its crop history, soil and rotation plans"}
            </p>
          )}
        </div>

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

      {selection && (
        <FieldPanel fieldId={selection.id} areaHa={areaById.get(selection.id) ?? null} onClose={clear} />
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

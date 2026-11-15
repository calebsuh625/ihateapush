import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, isAbort } from "../api";
import { hectaresToAcres } from "../lib/format";
import {
  FIELDS_LAYER,
  classColours,
  classIndexes,
  countyStats,
  fieldColours,
  sameFields,
  seriesFor,
  viewStats,
  yearFor,
} from "../lib/layers";
import type { LayerAnswer, LayerCatalog, LayerInfo, LayerSeries, ViewStats } from "../lib/layers";
import type { FieldCollection } from "../types";
import { useResource } from "./useResource";

const loadCatalog = (_key: string, signal: AbortSignal) => api.layers(signal);

type LayerData =
  | { status: "idle" }
  | { status: "loading"; name: string; progress: { done: number; total: number } | null }
  | { status: "ready"; name: string; answer: LayerAnswer }
  | { status: "error"; name: string; message: string };

const wait = (ms: number, signal: AbortSignal) =>
  new Promise<void>((done, fail) => {
    const timer = window.setTimeout(done, ms);
    signal.addEventListener("abort", () => {
      window.clearTimeout(timer);
      fail(new DOMException("aborted", "AbortError"));
    });
  });

/** Load one layer (every year of it). A layer still being made answers "building": ask again until it is ready. */
function useLayerData(name: string | null): [LayerData, () => void] {
  const [attempt, setAttempt] = useState(0);
  const [data, setData] = useState<LayerData>({ status: "idle" });

  useEffect(() => {
    if (name === null) {
      setData({ status: "idle" });
      return;
    }
    const controller = new AbortController();
    setData({ status: "loading", name, progress: null });
    (async () => {
      for (;;) {
        const answer = await api.layer(name, controller.signal);
        if (controller.signal.aborted) return;
        if (answer.status !== "building") {
          setData({ status: "ready", name, answer });
          return;
        }
        setData({ status: "loading", name, progress: answer.progress ?? null });
        await wait(Math.max(1, answer.retry_after_s ?? 2) * 1000, controller.signal);
      }
    })().catch((err: unknown) => {
      if (controller.signal.aborted || isAbort(err)) return;
      setData({ status: "error", name, message: err instanceof Error ? err.message : String(err) });
    });
    return () => controller.abort();
  }, [name, attempt]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);
  return [data, retry];
}

export interface MapLayerState {
  catalog: LayerCatalog | null;
  catalogError: string | null;
  /** FIELDS_LAYER or a layer name */
  name: string;
  setName: (name: string) => void;
  layer: LayerInfo | null;
  /** loading, ready, or why not */
  state: "fields" | "loading" | "ready" | "unavailable" | "error" | "mismatch";
  message: string | null;
  progress: { done: number; total: number } | null;
  retry: () => void;
  /** the year on the slider (null for a layer with one value) */
  year: number | null;
  years: number[] | null;
  setYear: (year: number) => void;
  playing: boolean;
  play: () => void;
  pause: () => void;
  /** the colour of every field, in the order of the fields; null for the plain map */
  fills: readonly string[] | null;
  colours: readonly string[];
  /** the numbers for the fields in view and for the county; null until both exist */
  view: ViewStats | null;
  county: ViewStats | null;
  /** the map tells us which fields are in view */
  setViewFields: (indexes: Uint32Array) => void;
}

/** The map layer, its year and the time-lapse, with the colours and numbers that follow from them. */
export function useMapLayers(fields: FieldCollection | null): MapLayerState {
  const [catalogRes] = useResource("layers", loadCatalog);
  const [name, setNameState] = useState(FIELDS_LAYER);
  const [wanted, setWanted] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);
  const [inView, setInView] = useState<Uint32Array | null>(null);
  const [etag, setEtag] = useState<string | null>(null);

  const catalog = catalogRes.status === "ready" ? catalogRes.data : null;
  const [data, retry] = useLayerData(name === FIELDS_LAYER ? null : name);

  const fieldCount = fields ? fields.features.length : 0;
  const acres = useMemo(() => Float64Array.from((fields?.features ?? []).map((f) => hectaresToAcres(f.properties?.area_ha ?? 0))), [fields]);

  // The ETag of the fields the map holds, to check a layer was made for the same fields.
  useEffect(() => {
    if (!fields || name === FIELDS_LAYER) return;
    const controller = new AbortController();
    void api.fieldsEtag(controller.signal).then((tag) => {
      if (!controller.signal.aborted) setEtag(tag);
    });
    return () => controller.abort();
  }, [fields, name]);

  const answer = data.status === "ready" && data.name === name ? data.answer : null;
  const info: LayerInfo | null = answer ?? catalog?.layers.find((l) => l.name === name) ?? null;
  const matches = answer !== null && fields !== null && sameFields(answer, fieldCount, etag);
  const year = info ? yearFor(info, wanted) : null;
  const years = info?.years ?? null;

  const series: LayerSeries | null = answer && matches && answer.status === "ready" ? seriesFor(answer, year) : null;
  const classes = useMemo(() => (answer && series ? classIndexes(answer, series) : null), [answer, series]);
  const colours = useMemo(() => (answer ? classColours(answer) : []), [answer]);
  const fills = useMemo(() => (classes ? fieldColours(classes, colours) : null), [classes, colours]);
  const view = useMemo(
    () => (answer && series && classes && inView ? viewStats(answer, series, classes, acres, inView) : null),
    [answer, series, classes, acres, inView],
  );
  const county = useMemo(() => (answer && series ? countyStats(answer, series) : null), [answer, series]);

  let state: MapLayerState["state"] = "fields";
  let message: string | null = null;
  let progress: { done: number; total: number } | null = null;
  if (name !== FIELDS_LAYER) {
    if (data.status === "error" && data.name === name) {
      state = "error";
      message = data.message;
    } else if (answer && answer.status === "unavailable") {
      state = "unavailable";
      message = answer.reason;
    } else if (answer && !matches && fields !== null) {
      state = "mismatch";
      message = "This layer was made for a different set of fields than the map shows. Reload the page.";
    } else if (series) {
      state = "ready";
    } else {
      state = "loading";
      progress = data.status === "loading" && data.name === name ? data.progress : null;
    }
  }

  // The time-lapse: one year a second, to the last year, then it stops.
  const yearRef = useRef<number | null>(null);
  yearRef.current = year;
  useEffect(() => {
    if (!playing || !years || years.length < 2) return;
    const timer = window.setInterval(() => {
      const at = years.indexOf(yearRef.current ?? -1);
      const next = years[at + 1];
      if (next === undefined) {
        setPlaying(false);
        return;
      }
      setWanted(next);
      if (years.indexOf(next) === years.length - 1) setPlaying(false);
    }, 1000);
    return () => window.clearInterval(timer);
  }, [playing, years]);

  const setName = useCallback((next: string) => {
    setPlaying(false);
    setNameState(next);
  }, []);
  const setYear = useCallback((next: number) => {
    setPlaying(false); // the user took hold of the slider
    setWanted(next);
  }, []);
  const play = useCallback(() => {
    // From the last year, play starts again from the first.
    if (years && years.length > 1 && yearRef.current === years[years.length - 1]) setWanted(years[0] ?? null);
    setPlaying(true);
  }, [years]);
  const pause = useCallback(() => setPlaying(false), []);

  return {
    catalog,
    catalogError: catalogRes.status === "error" ? catalogRes.message : null,
    name,
    setName,
    layer: info,
    state,
    message,
    progress,
    retry,
    year,
    years,
    setYear,
    playing: playing && year !== null,
    play,
    pause,
    fills: state === "ready" ? fills : null,
    colours,
    view,
    county,
    setViewFields: setInView,
  };
}

import { useEffect, useRef } from "react";
import L from "leaflet";
import { MapContainer, TileLayer, ZoomControl, useMap } from "react-leaflet";
import { REGION_CENTER, REGION_ZOOM } from "../config";
import { CountyOutlines } from "./CountyOutlines";
import type { Selection } from "../hooks/useSelection";
import { boxLoop, fieldsInside, loopArea } from "../lib/farm";
import { collectionBounds, pointInRings, ringBounds } from "../lib/geo";
import { NO_VALUE_COLOUR, boxesOf, visibleIndexes } from "../lib/layers";
import type { FieldCollection } from "../types";

declare global {
  interface Window {
    /** The Leaflet map, exposed for end-to-end tests (latLng → pixel). */
    __map?: L.Map;
  }
}

// ---------------------------------------------------------------------------------------------
// Styling. Outlines are a dark green that holds up against OpenStreetMap's pale land colours;
// the selected field is blue, which stays distinct from green for colour-blind readers too.
// Fields picked for a farm ("Plan a farm") are orange, which stays apart from both.
// Far out, fields are a few pixels wide, so the fill does the work; close in, the outline does.
// ---------------------------------------------------------------------------------------------

type ZoomBand = "far" | "mid" | "near";
type FieldState = "base" | "hover" | "selected" | "twin" | "farm" | "farmHover";

/** How the pointer picks fields in farm mode: one at a time, a freehand loop, or a box. */
export type FarmTool = "click" | "loop" | "box";

const bandFor = (zoom: number): ZoomBand => (zoom < 12 ? "far" : zoom < 14 ? "mid" : "near");

const OUTLINE = "#1b5a3b";
const OUTLINE_HOVER = "#0c3523";
const FILL = "#2f8f5b";
const SELECTED_OUTLINE = "#1d4ed8";
const SELECTED_FILL = "#3b82f6";
// The twin in the "Compare with its twin" view: amber and dashed, so it stays apart from the blue
// selected field by pattern as well as by colour (the charts in that view use the same amber).
const TWIN_OUTLINE = "#b45309";
const TWIN_FILL = "#f59e0b";
const FARM_OUTLINE = "#9a3412";
const FARM_FILL = "#f97316";

const STYLES: Record<ZoomBand, Record<FieldState, L.PathOptions>> = {
  far: {
    base: { color: OUTLINE, weight: 0.75, opacity: 0.7, fillColor: FILL, fillOpacity: 0.3 },
    hover: { color: OUTLINE_HOVER, weight: 2.5, opacity: 1, fillColor: FILL, fillOpacity: 0.55 },
    selected: { color: SELECTED_OUTLINE, weight: 3, opacity: 1, fillColor: SELECTED_FILL, fillOpacity: 0.45 },
    twin: { color: TWIN_OUTLINE, weight: 3, opacity: 1, fillColor: TWIN_FILL, fillOpacity: 0.5, dashArray: "6 3" },
    farm: { color: FARM_OUTLINE, weight: 1.5, opacity: 1, fillColor: FARM_FILL, fillOpacity: 0.65 },
    farmHover: { color: FARM_OUTLINE, weight: 3, opacity: 1, fillColor: FARM_FILL, fillOpacity: 0.8 },
  },
  mid: {
    base: { color: OUTLINE, weight: 1.25, opacity: 0.9, fillColor: FILL, fillOpacity: 0.2 },
    hover: { color: OUTLINE_HOVER, weight: 3, opacity: 1, fillColor: FILL, fillOpacity: 0.4 },
    selected: { color: SELECTED_OUTLINE, weight: 3.5, opacity: 1, fillColor: SELECTED_FILL, fillOpacity: 0.35 },
    twin: { color: TWIN_OUTLINE, weight: 3.5, opacity: 1, fillColor: TWIN_FILL, fillOpacity: 0.4, dashArray: "6 3" },
    farm: { color: FARM_OUTLINE, weight: 2.5, opacity: 1, fillColor: FARM_FILL, fillOpacity: 0.5 },
    farmHover: { color: FARM_OUTLINE, weight: 3.5, opacity: 1, fillColor: FARM_FILL, fillOpacity: 0.65 },
  },
  near: {
    base: { color: OUTLINE, weight: 2, opacity: 1, fillColor: FILL, fillOpacity: 0.12 },
    hover: { color: OUTLINE_HOVER, weight: 3.5, opacity: 1, fillColor: FILL, fillOpacity: 0.28 },
    selected: { color: SELECTED_OUTLINE, weight: 4, opacity: 1, fillColor: SELECTED_FILL, fillOpacity: 0.25 },
    twin: { color: TWIN_OUTLINE, weight: 4, opacity: 1, fillColor: TWIN_FILL, fillOpacity: 0.3, dashArray: "8 4" },
    farm: { color: FARM_OUTLINE, weight: 3, opacity: 1, fillColor: FARM_FILL, fillOpacity: 0.4 },
    farmHover: { color: FARM_OUTLINE, weight: 4, opacity: 1, fillColor: FARM_FILL, fillOpacity: 0.55 },
  },
};

// ---------------------------------------------------------------------------------------------
// A map layer colours the fields by a value. The fill is the layer's colour in every state; the
// outline and weight still say hovered, selected, twin or picked for the farm. Painted fills are
// stronger than the plain ones (the colour is the message), and the plain outline is thinner so it
// does not cover the colour of a field a few pixels wide.
// ---------------------------------------------------------------------------------------------

const PAINT_OPACITY: Record<ZoomBand, number> = { far: 0.85, mid: 0.8, near: 0.7 };
const PAINT_OUTLINE = "#2a352f";
const PAINT_BASE: Record<ZoomBand, L.PathOptions> = {
  far: { color: PAINT_OUTLINE, weight: 0.3, opacity: 0.45 },
  mid: { color: PAINT_OUTLINE, weight: 0.7, opacity: 0.6 },
  near: { color: PAINT_OUTLINE, weight: 1.5, opacity: 0.8 },
};
const paintedCache = new Map<string, L.PathOptions>();

/** The style of a field in `state` when a layer gives it `fill`; the plain style when it gives none. */
function styleOf(band: ZoomBand, state: FieldState, fill: string | null): L.PathOptions {
  if (fill === null) return STYLES[band][state];
  const key = `${band}|${state}|${fill}`;
  let style = paintedCache.get(key);
  if (!style) {
    const strong = state === "hover" || state === "farmHover" || state === "selected";
    style = {
      ...(state === "base" ? PAINT_BASE[band] : STYLES[band][state]),
      fillColor: fill,
      fillOpacity: Math.min(1, PAINT_OPACITY[band] + (strong ? 0.1 : 0)),
    };
    paintedCache.set(key, style);
  }
  return style;
}

/** Leave room for the title card in the top-left corner when fitting the view. */
const FIT_ALL: L.FitBoundsOptions = { paddingTopLeft: [24, 104], paddingBottomRight: [24, 28] };
const FIT_FARM: L.FitBoundsOptions = { paddingTopLeft: [48, 150], paddingBottomRight: [48, 48], maxZoom: 15, animate: false };
const FIT_FIELD: L.FitBoundsOptions = { paddingTopLeft: [48, 120], paddingBottomRight: [48, 48], maxZoom: 15 };

/**
 * Pan just far enough to bring `bounds` inside the map, like scrollIntoView({ block: "nearest" }).
 * Never zooms. A field too big for the view is left where it is unless none of it shows.
 */
function nudgeIntoView(map: L.Map, bounds: L.LatLngBounds, margin = 32) {
  const size = map.getSize();
  const topLeft = map.latLngToContainerPoint(bounds.getNorthWest());
  const bottomRight = map.latLngToContainerPoint(bounds.getSouthEast());
  const shift = (low: number, high: number, extent: number) => {
    const hidden = high < margin || low > extent - margin;
    if (high - low > extent - 2 * margin) return hidden ? (low + high) / 2 - extent / 2 : 0;
    if (low < margin) return low - margin;
    if (high > extent - margin) return high - (extent - margin);
    return 0;
  };
  const dx = Math.round(shift(topLeft.x, bottomRight.x, size.x));
  const dy = Math.round(shift(topLeft.y, bottomRight.y, size.y));
  if (dx !== 0 || dy !== 0) map.panBy([dx, dy]);
}

// ---------------------------------------------------------------------------------------------
// The fields layer, kept outside React: one canvas renderer, one GeoJSON layer, built once.
// Hover and selection restyle at most two polygons each, and a change to the farm restyles only
// the fields that joined or left it; nothing here runs on a React render.
// ---------------------------------------------------------------------------------------------

interface FieldsLayer {
  readonly count: number;
  /** Put the polygons on the map. Call after the view is set, so they are projected once. */
  show(): void;
  /** Mark `id` as the selected field (null clears). Unknown ids select nothing. */
  select(id: string | null): void;
  /** The twin shown in the "Compare with its twin" view (null clears). Unknown ids mark nothing. */
  setTwin(id: string | null): void;
  /** The fields picked for the farm (empty outside farm mode). Only the difference is restyled. */
  setFarm(ids: Iterable<string>): void;
  /**
   * Colour the fields by a layer: one colour per field, in the order of the fields (null for the plain
   * map). Only the fields whose colour changed are restyled.
   */
  paint(fills: readonly string[] | null): void;
  /** Indexes (into the fields) of the fields any part of which is in the current view. */
  inView(): Uint32Array;
  /** Fields whose centre lies inside a loop of [lon, lat] points. */
  inside(loop: readonly (readonly number[])[]): string[];
  bounds(id: string): L.LatLngBounds | null;
  destroy(): void;
}

/** Outline of one field in lon/lat, with its bounding box, for exact hit-testing. */
interface Shape {
  id: string;
  west: number;
  south: number;
  east: number;
  north: number;
  rings: number[][][];
  /** the centre the lasso tests: the middle of the bounding box */
  lon: number;
  lat: number;
}

const featureId = (feature: { id?: string | number; properties?: unknown }): string =>
  String(feature.id ?? (feature.properties as { field_id?: unknown } | null)?.field_id ?? "");

function createFieldsLayer(map: L.Map, fields: FieldCollection, onPick: (id: string) => void): FieldsLayer {
  // `padding` draws a margin around the viewport so a drag does not reveal blank canvas;
  // `tolerance` lets a click that lands a few pixels outside a field still select it.
  const renderer = L.canvas({ padding: 0.25, tolerance: 3 });
  const byId = new Map<string, L.Polygon>();
  const idOf = new Map<L.Layer, string>();
  let band = bandFor(map.getZoom());
  let selected: string | null = null;
  let twin: string | null = null;
  let hovered: string | null = null;
  let farm = new Set<string>();
  let fills: readonly string[] | null = null;
  const indexOf = new Map<string, number>();
  fields.features.forEach((feature, i) => {
    const id = featureId(feature);
    if (id && !indexOf.has(id)) indexOf.set(id, i);
  });
  const fillOf = (id: string): string | null => {
    const at = indexOf.get(id);
    return fills === null || at === undefined ? null : (fills[at] ?? NO_VALUE_COLOUR);
  };
  const boxes = boxesOf(fields.features);

  const stateOf = (id: string): FieldState =>
    id === selected ? "selected" : id === twin ? "twin" : farm.has(id) ? (id === hovered ? "farmHover" : "farm") : id === hovered ? "hover" : "base";
  const restyle = (id: string | null) => {
    if (id === null) return;
    byId.get(id)?.setStyle(styleOf(band, stateOf(id), fillOf(id)));
  };

  const options: L.GeoJSONOptions & { renderer: L.Renderer } = {
    renderer,
    style: () => styleOf(band, "base", null),
    onEachFeature: (feature, layer) => {
      const id = featureId(feature);
      if (!id) return;
      byId.set(id, layer as L.Polygon);
      idOf.set(layer, id);
    },
  };
  const group = L.geoJSON(fields as unknown as Parameters<typeof L.geoJSON>[0], options);

  // Far out a field is only a few pixels wide, so the selected one also gets a locator ring.
  const ring = L.circleMarker([0, 0], {
    renderer,
    radius: 14,
    color: SELECTED_OUTLINE,
    weight: 2.5,
    fill: false,
    interactive: false,
  });
  const syncRing = () => {
    const chosen = selected !== null ? byId.get(selected) : undefined;
    if (chosen && band === "far" && map.hasLayer(group)) ring.setLatLng(chosen.getBounds().getCenter()).addTo(map);
    else ring.remove();
  };

  const shapes: Shape[] = [];
  for (const feature of fields.features) {
    const id = featureId(feature);
    const rings = feature.geometry?.coordinates;
    const box = rings ? ringBounds(rings) : null;
    if (id && rings && box && byId.has(id)) {
      shapes.push({ id, rings, ...box, lon: (box.west + box.east) / 2, lat: (box.south + box.north) / 2 });
    }
  }

  /**
   * Which field is the pointer on? Leaflet's canvas hit-test grows every polygon by the click
   * tolerance and answers with the top-most match, so near a shared edge — or anywhere in a small
   * field at county zoom — it can name the neighbour. Prefer the field that really contains the
   * point; fall back to Leaflet's answer for a near miss.
   */
  const fieldAt = (event: L.LeafletEvent): string | null => {
    const candidate = idOf.get(event.propagatedFrom as L.Layer) ?? null;
    const at = (event as L.LeafletMouseEvent).latlng;
    if (!at) return candidate;
    let exact: string | null = null;
    for (const shape of shapes) {
      if (at.lng < shape.west || at.lng > shape.east || at.lat < shape.south || at.lat > shape.north) continue;
      if (!pointInRings(at.lng, at.lat, shape.rings)) continue;
      if (shape.id === candidate) return candidate;
      exact = shape.id;
    }
    return exact ?? candidate;
  };

  const hover = (id: string | null) => {
    if (id === hovered) return;
    const previous = hovered;
    hovered = id;
    restyle(previous);
    restyle(id);
    if (id === null) return;
    // Draw the thick outline over its neighbours — but keep the selected field on top of it.
    const layer = byId.get(id);
    layer?.bringToFront();
    const chosen = selected !== null && selected !== id ? byId.get(selected) : undefined;
    if (layer && chosen && chosen.getBounds().intersects(layer.getBounds())) chosen.bringToFront();
  };

  // One listener per event on the group, not three per polygon.
  group.on("mousemove", (event) => hover(fieldAt(event)));
  group.on("mouseout", () => hover(null));
  group.on("click", (event) => {
    const id = fieldAt(event);
    if (id !== null) onPick(id);
  });

  // Outline weight and fill strength change in three steps with zoom. Leaflet redraws every
  // polygon after a zoom anyway; this only runs when a step boundary is crossed.
  const syncBand = () => {
    const next = bandFor(map.getZoom());
    if (next === band) return;
    band = next;
    byId.forEach((layer, id) => layer.setStyle(styleOf(band, stateOf(id), fillOf(id))));
    syncRing();
  };

  return {
    count: byId.size,
    show() {
      syncBand();
      map.on("zoomend", syncBand);
      group.addTo(map);
      if (selected !== null) byId.get(selected)?.bringToFront();
      syncRing();
    },
    select(id) {
      const next = id !== null && byId.has(id) ? id : null;
      if (next === selected) return;
      const previous = selected;
      selected = next;
      restyle(previous);
      restyle(next);
      if (next !== null) byId.get(next)?.bringToFront();
      syncRing();
    },
    setTwin(id) {
      const next = id !== null && byId.has(id) ? id : null;
      if (next === twin) return;
      const previous = twin;
      twin = next;
      restyle(previous);
      restyle(next);
      if (next !== null) byId.get(next)?.bringToFront();
      if (selected !== null) byId.get(selected)?.bringToFront();
    },
    setFarm(ids) {
      const next = new Set<string>();
      for (const id of ids) if (byId.has(id)) next.add(id);
      const previous = farm;
      farm = next;
      previous.forEach((id) => {
        if (!next.has(id)) restyle(id);
      });
      next.forEach((id) => {
        if (previous.has(id)) return;
        restyle(id);
        byId.get(id)?.bringToFront(); // its thicker outline goes over the neighbours'
      });
      if (selected !== null) byId.get(selected)?.bringToFront();
    },
    paint(next) {
      const previous = fills;
      fills = next;
      if (previous === next) return;
      // Restyle in place: the polygons stay, only the ones whose colour changed are touched
      // (all of them when a layer goes on or off or the classes change meaning).
      fields.features.forEach((feature, i) => {
        if (previous !== null && next !== null && previous[i] === next[i]) return;
        restyle(featureId(feature) || null);
      });
      if (selected !== null) byId.get(selected)?.bringToFront();
    },
    inView() {
      const b = map.getBounds();
      return visibleIndexes(boxes, b.getWest(), b.getSouth(), b.getEast(), b.getNorth());
    },
    inside: (loop) => fieldsInside(shapes, loop),
    bounds: (id) => byId.get(id)?.getBounds() ?? null,
    destroy() {
      map.off("zoomend", syncBand);
      ring.remove();
      group.remove();
      renderer.remove();
    },
  };
}

// ---------------------------------------------------------------------------------------------
// The lasso, also outside React: one element over the map that takes the pointer while a lasso
// tool is on, and one SVG path for the line being drawn. The fields canvas is not touched until
// the pointer is let go, so drawing never repaints the fields.
// ---------------------------------------------------------------------------------------------

/** Loops smaller than this many square pixels are a stray click, not a selection. */
const MIN_LASSO_AREA = 36;

function createLasso(map: L.Map, tool: "loop" | "box", onDone: (loop: [number, number][]) => void): () => void {
  const container = map.getContainer();
  const surface = document.createElement("div");
  surface.className = "lasso";
  surface.dataset.testid = "lasso-surface";
  surface.dataset.tool = tool;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  svg.setAttribute("aria-hidden", "true");
  svg.appendChild(path);
  surface.appendChild(svg);
  container.appendChild(surface);

  let points: [number, number][] | null = null;
  const at = (event: PointerEvent): [number, number] => {
    const box = surface.getBoundingClientRect();
    return [event.clientX - box.left, event.clientY - box.top];
  };
  const outline = (): [number, number][] => {
    if (!points) return [];
    const first = points[0];
    const last = points[points.length - 1];
    return tool === "box" && first && last ? boxLoop(first, last) : points;
  };
  const draw = () => {
    const loop = outline();
    path.setAttribute("d", loop.length > 1 ? `M${loop.map((p) => `${p[0]} ${p[1]}`).join("L")}Z` : "");
  };
  const stop = () => {
    points = null;
    draw();
  };

  const onDown = (event: PointerEvent) => {
    if (event.button !== 0) return;
    event.preventDefault();
    points = [at(event)];
    surface.setPointerCapture(event.pointerId);
  };
  const onMove = (event: PointerEvent) => {
    if (!points) return;
    const next = at(event);
    const last = points[points.length - 1];
    if (tool === "box") points = [points[0] ?? next, next];
    else if (!last || Math.hypot(next[0] - last[0], next[1] - last[1]) >= 2) points.push(next);
    draw();
  };
  const onUp = () => {
    const loop = outline();
    stop();
    if (loop.length < 3 || loopArea(loop) < MIN_LASSO_AREA) return;
    onDone(
      loop.map((point) => {
        const where = map.containerPointToLatLng(point);
        return [where.lng, where.lat];
      }),
    );
  };
  const onKey = (event: KeyboardEvent) => {
    if (event.key === "Escape" && points) {
      event.preventDefault(); // drop the loop being drawn; do not also close a panel
      stop();
    }
  };
  // Keep the map from dragging, zooming on a double click or picking a field under the lasso.
  const swallow = (event: Event) => event.stopPropagation();
  const swallowed = ["pointerdown", "mousedown", "touchstart", "click", "dblclick", "contextmenu"];

  surface.addEventListener("pointerdown", onDown);
  surface.addEventListener("pointermove", onMove);
  surface.addEventListener("pointerup", onUp);
  surface.addEventListener("pointercancel", stop);
  swallowed.forEach((type) => surface.addEventListener(type, swallow));
  window.addEventListener("keydown", onKey, true);

  return () => {
    window.removeEventListener("keydown", onKey, true);
    surface.remove();
  };
}

// ---------------------------------------------------------------------------------------------
// React wrappers
// ---------------------------------------------------------------------------------------------

interface FieldsProps {
  fields: FieldCollection;
  selection: Selection | null;
  onSelect: (id: string) => void;
  /** Called once the polygons have been drawn, with how many there are. */
  onRendered?: (count: number) => void;
  /** The fields picked for the farm; null outside farm mode. */
  farm: readonly string[] | null;
  /** Farm mode only: a click picks one field, a loop or a box picks every field centred inside. */
  tool: FarmTool;
  onLasso?: (ids: string[]) => void;
  /** The twin to outline in a second colour, or null. */
  twin: string | null;
  /** Pixels of the bottom of the viewport the twin view covers (the map is fitted above it). */
  twinCover: number;
  /** One colour per field, in the order of the fields, from the chosen map layer; null for the plain map. */
  fills: readonly string[] | null;
  /** Called, after the view has stopped changing, with the fields in view. */
  onView?: (indexes: Uint32Array) => void;
}

const NO_FARM: readonly string[] = [];

function Fields({ fields, selection, onSelect, onRendered, farm, tool, onLasso, twin, twinCover, fills, onView }: FieldsProps) {
  const map = useMap();
  const layerRef = useRef<FieldsLayer | null>(null);
  /** The selection the view has already been moved for. */
  const shownRef = useRef<Selection | null>(null);
  const latest = useRef({ selection, onSelect, onRendered, farm, onLasso, twin, twinCover, fills, onView });
  useEffect(() => {
    latest.current = { selection, onSelect, onRendered, farm, onLasso, twin, twinCover, fills, onView };
  });

  // Build the layer once per collection, then frame it: the linked field if the URL names one
  // that exists, otherwise everything.
  useEffect(() => {
    const layer = createFieldsLayer(map, fields, (id) => latest.current.onSelect(id));
    layerRef.current = layer;

    const current = latest.current.selection;
    layer.select(current?.id ?? null);
    layer.setFarm(latest.current.farm ?? NO_FARM);
    layer.setTwin(latest.current.twin);
    layer.paint(latest.current.fills);
    shownRef.current = current;
    const linked = current ? layer.bounds(current.id) : null;
    const all = collectionBounds(fields);
    if (linked) map.fitBounds(linked, { ...FIT_FIELD, animate: false });
    else if (all) map.fitBounds(all, { ...FIT_ALL, animate: false });
    layer.show();

    // The canvas paints on the next animation frame; this callback is queued after that paint.
    const frame = L.Util.requestAnimFrame(() => {
      performance.mark("fields-rendered");
      latest.current.onRendered?.(layer.count);
    });

    return () => {
      L.Util.cancelAnimFrame(frame);
      layer.destroy();
      layerRef.current = null;
    };
  }, [map, fields]);

  // Follow the map layer: restyle in place, never rebuild the polygons.
  useEffect(() => {
    layerRef.current?.paint(fills);
  }, [fills, fields]);

  // Tell the layer control which fields are in view, once the map has stopped moving for a moment.
  useEffect(() => {
    let timer = 0;
    const send = () => {
      const layer = layerRef.current;
      if (layer) latest.current.onView?.(layer.inView());
    };
    const soon = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(send, 150);
    };
    map.on("moveend", soon);
    map.on("resize", soon);
    soon();
    return () => {
      window.clearTimeout(timer);
      map.off("moveend", soon);
      map.off("resize", soon);
    };
  }, [map, fields]);

  // Follow the selection: restyle two polygons, and make sure the field can be seen.
  useEffect(() => {
    const layer = layerRef.current;
    if (!layer) return;
    layer.select(selection?.id ?? null);
    if (shownRef.current === selection) return;
    shownRef.current = selection;
    if (!selection) return;

    map.invalidateSize({ pan: false }); // the panel may just have opened and narrowed the map
    const bounds = layer.bounds(selection.id);
    if (!bounds) return;
    if (selection.via === "url") map.fitBounds(bounds, FIT_FIELD); // arrived by link or Back button: go there
    else nudgeIntoView(map, bounds); // clicked on the map: keep the user's view, just make sure it shows
  }, [map, selection]);

  // Follow the twin: outline it, and bring this field and its twin into the part of the map the
  // twin view leaves uncovered.
  useEffect(() => {
    const layer = layerRef.current;
    if (!layer) return;
    layer.setTwin(twin);
    const own = latest.current.selection;
    if (twin === null || !own) return;
    const a = layer.bounds(own.id);
    const b = layer.bounds(twin);
    if (!a || !b) return;
    map.invalidateSize({ pan: false });
    const rect = map.getContainer().getBoundingClientRect();
    const covered = Math.max(0, rect.bottom - (window.innerHeight - latest.current.twinCover));
    const narrow = rect.width < 560;
    map.fitBounds(L.latLngBounds(a.getSouthWest(), a.getNorthEast()).extend(b), {
      paddingTopLeft: [narrow ? 24 : 48, narrow ? 72 : 120],
      paddingBottomRight: [narrow ? 24 : 48, covered + 24],
      maxZoom: 16,
      animate: false,
    });
  }, [map, twin, fields]);

  // Follow the farm: only the fields that joined or left it are restyled.
  useEffect(() => {
    layerRef.current?.setFarm(farm ?? NO_FARM);
  }, [farm, fields]);

  // Farm mode starting with fields already picked (a farm kept from before): bring them into view.
  const farmOn = farm !== null;
  useEffect(() => {
    const layer = layerRef.current;
    const ids = latest.current.farm;
    if (!farmOn || !layer || !ids || ids.length === 0) return;
    let all: L.LatLngBounds | null = null;
    for (const id of ids) {
      const one = layer.bounds(id);
      if (one) all = all ? all.extend(one) : L.latLngBounds(one.getSouthWest(), one.getNorthEast());
    }
    if (!all) return;
    map.invalidateSize({ pan: false }); // the farm panel has just opened and narrowed the map
    map.fitBounds(all, FIT_FARM);
  }, [map, farmOn, fields]);

  // The lasso is only there in farm mode while a lasso tool is on; otherwise the map is as ever.
  const lassoTool = farm !== null && tool !== "click" ? tool : null;
  useEffect(() => {
    if (lassoTool === null) return;
    return createLasso(map, lassoTool, (loop) => {
      const ids = layerRef.current?.inside(loop) ?? [];
      latest.current.onLasso?.(ids);
    });
  }, [map, lassoTool]);

  return null;
}

/** Keep Leaflet's idea of the map size in step with the layout (the panel opens and closes). */
function KeepSized() {
  const map = useMap();
  useEffect(() => {
    const observer = new ResizeObserver(() => map.invalidateSize({ pan: false }));
    observer.observe(map.getContainer());
    return () => observer.disconnect();
  }, [map]);
  return null;
}

function ExposeMap() {
  const map = useMap();
  useEffect(() => {
    window.__map = map;
    return () => {
      if (window.__map === map) delete window.__map;
    };
  }, [map]);
  return null;
}

interface FieldMapProps {
  fields: FieldCollection | null;
  selection: Selection | null;
  onSelect: (id: string) => void;
  onRendered?: (count: number) => void;
  /** The fields picked for the farm; null (the default) outside farm mode. */
  farm?: readonly string[] | null;
  tool?: FarmTool;
  onLasso?: (ids: string[]) => void;
  /** The twin to outline in a second colour (the "Compare with its twin" view); null (the default) for none. */
  twin?: string | null;
  /** How many pixels of the viewport's bottom that view covers, so the map is fitted above it. */
  twinCover?: number;
  /** One colour per field from the chosen map layer; null (the default) for the plain map. */
  fills?: readonly string[] | null;
  onView?: (indexes: Uint32Array) => void;
}

export function FieldMap({ fields, selection, onSelect, onRendered, farm = null, tool = "click", onLasso, twin = null, twinCover = 0, fills = null, onView }: FieldMapProps) {
  return (
    <MapContainer
      className="map"
      center={REGION_CENTER}
      zoom={REGION_ZOOM}
      minZoom={4}
      maxZoom={18}
      zoomControl={false}
      preferCanvas
    >
      <TileLayer
        url="https://tile.openstreetmap.org/{z}/{x}/{y}.png"
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
        maxZoom={19}
      />
      <ZoomControl position="topright" />
      <KeepSized />
      <ExposeMap />
      <CountyOutlines />
      {fields && (
        <Fields
          fields={fields}
          selection={selection}
          onSelect={onSelect}
          onRendered={onRendered}
          farm={farm}
          tool={tool}
          onLasso={onLasso}
          twin={twin}
          twinCover={twinCover}
          fills={fills}
          onView={onView}
        />
      )}
    </MapContainer>
  );
}

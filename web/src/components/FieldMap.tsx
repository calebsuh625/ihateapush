import { useEffect, useRef } from "react";
import L from "leaflet";
import { MapContainer, TileLayer, ZoomControl, useMap } from "react-leaflet";
import { REGION_CENTER, REGION_ZOOM } from "../config";
import type { Selection } from "../hooks/useSelection";
import { collectionBounds, pointInRings, ringBounds } from "../lib/geo";
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
// Far out, fields are a few pixels wide, so the fill does the work; close in, the outline does.
// ---------------------------------------------------------------------------------------------

type ZoomBand = "far" | "mid" | "near";
type FieldState = "base" | "hover" | "selected";

const bandFor = (zoom: number): ZoomBand => (zoom < 12 ? "far" : zoom < 14 ? "mid" : "near");

const OUTLINE = "#1b5a3b";
const OUTLINE_HOVER = "#0c3523";
const FILL = "#2f8f5b";
const SELECTED_OUTLINE = "#1d4ed8";
const SELECTED_FILL = "#3b82f6";

const STYLES: Record<ZoomBand, Record<FieldState, L.PathOptions>> = {
  far: {
    base: { color: OUTLINE, weight: 0.75, opacity: 0.7, fillColor: FILL, fillOpacity: 0.3 },
    hover: { color: OUTLINE_HOVER, weight: 2.5, opacity: 1, fillColor: FILL, fillOpacity: 0.55 },
    selected: { color: SELECTED_OUTLINE, weight: 3, opacity: 1, fillColor: SELECTED_FILL, fillOpacity: 0.45 },
  },
  mid: {
    base: { color: OUTLINE, weight: 1.25, opacity: 0.9, fillColor: FILL, fillOpacity: 0.2 },
    hover: { color: OUTLINE_HOVER, weight: 3, opacity: 1, fillColor: FILL, fillOpacity: 0.4 },
    selected: { color: SELECTED_OUTLINE, weight: 3.5, opacity: 1, fillColor: SELECTED_FILL, fillOpacity: 0.35 },
  },
  near: {
    base: { color: OUTLINE, weight: 2, opacity: 1, fillColor: FILL, fillOpacity: 0.12 },
    hover: { color: OUTLINE_HOVER, weight: 3.5, opacity: 1, fillColor: FILL, fillOpacity: 0.28 },
    selected: { color: SELECTED_OUTLINE, weight: 4, opacity: 1, fillColor: SELECTED_FILL, fillOpacity: 0.25 },
  },
};

/** Leave room for the title card in the top-left corner when fitting the view. */
const FIT_ALL: L.FitBoundsOptions = { paddingTopLeft: [24, 104], paddingBottomRight: [24, 28] };
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
// Hover and selection restyle at most two polygons each; nothing here runs on a React render.
// ---------------------------------------------------------------------------------------------

interface FieldsLayer {
  readonly count: number;
  /** Put the polygons on the map. Call after the view is set, so they are projected once. */
  show(): void;
  /** Mark `id` as the selected field (null clears). Unknown ids select nothing. */
  select(id: string | null): void;
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
  let hovered: string | null = null;

  const stateOf = (id: string): FieldState => (id === selected ? "selected" : id === hovered ? "hover" : "base");
  const restyle = (id: string | null) => {
    if (id === null) return;
    byId.get(id)?.setStyle(STYLES[band][stateOf(id)]);
  };

  const options: L.GeoJSONOptions & { renderer: L.Renderer } = {
    renderer,
    style: () => STYLES[band].base,
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
    if (id && rings && box && byId.has(id)) shapes.push({ id, rings, ...box });
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
    byId.forEach((layer, id) => layer.setStyle(STYLES[band][stateOf(id)]));
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
// React wrappers
// ---------------------------------------------------------------------------------------------

interface FieldsProps {
  fields: FieldCollection;
  selection: Selection | null;
  onSelect: (id: string) => void;
  /** Called once the polygons have been drawn, with how many there are. */
  onRendered?: (count: number) => void;
}

function Fields({ fields, selection, onSelect, onRendered }: FieldsProps) {
  const map = useMap();
  const layerRef = useRef<FieldsLayer | null>(null);
  /** The selection the view has already been moved for. */
  const shownRef = useRef<Selection | null>(null);
  const latest = useRef({ selection, onSelect, onRendered });
  useEffect(() => {
    latest.current = { selection, onSelect, onRendered };
  });

  // Build the layer once per collection, then frame it: the linked field if the URL names one
  // that exists, otherwise everything.
  useEffect(() => {
    const layer = createFieldsLayer(map, fields, (id) => latest.current.onSelect(id));
    layerRef.current = layer;

    const current = latest.current.selection;
    layer.select(current?.id ?? null);
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
}

export function FieldMap({ fields, selection, onSelect, onRendered }: FieldMapProps) {
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
      {fields && <Fields fields={fields} selection={selection} onSelect={onSelect} onRendered={onRendered} />}
    </MapContainer>
  );
}

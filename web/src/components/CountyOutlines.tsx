// Every county the site offers, drawn as an outline with its name; click one to open it (the page
// reloads with ?region=<fips>, see RegionLabel.goTo). Only one county's fields are loaded at a time, so
// this is how a visitor finds and opens the others. The outlines come from GET /regions/shapes at the
// API root (deploy/vercel/index.py); a server without that endpoint simply draws none.
import { useEffect } from "react";
import L from "leaflet";
import { useMap } from "react-leaflet";
import { API_ROOT, SELECTED_REGION } from "../api";
import { goTo } from "./RegionLabel";

interface CountyProps {
  fips: string;
  label: string;
  field_count?: number;
}

const PANE = "counties";
const LABEL_MIN_ZOOM = 8; // below this the names crowd each other

export function CountyOutlines() {
  const map = useMap();

  useEffect(() => {
    const ctrl = new AbortController();
    let layer: L.GeoJSON | null = null;
    let onZoom: (() => void) | null = null;

    if (!map.getPane(PANE)) {
      const pane = map.createPane(PANE);
      pane.style.zIndex = "450"; // above the field canvas, so another county's area can be clicked
      pane.style.pointerEvents = "none"; // only the outlines themselves take clicks
    }

    fetch(`${API_ROOT}/regions/shapes`, { signal: ctrl.signal })
      .then((res) => (res.ok ? res.json() : null))
      .then((shapes: GeoJSON.FeatureCollection | null) => {
        if (!shapes || !Array.isArray(shapes.features) || shapes.features.length < 2) return;
        const current = SELECTED_REGION;
        const defaultFips = "20055";
        const here = (p: CountyProps) => p.fips === (current ?? defaultFips);
        // SVG (not the map's canvas) so each outline takes its own clicks and the empty map does not
        const options = {
          pane: PANE,
          renderer: L.svg({ pane: PANE }),
          style: (f) => {
            const p = f?.properties as CountyProps;
            return here(p)
              ? { color: "#1f5fbf", weight: 2, fill: false, interactive: false }
              : { color: "#5f5e5a", weight: 1.2, dashArray: "5 4", fillColor: "#5f5e5a", fillOpacity: 0.06 };
          },
          onEachFeature: (f, lyr) => {
            const p = f.properties as CountyProps;
            const name = p.label.replace(/, KS$/, "");
            const count = typeof p.field_count === "number" ? ` · ${p.field_count.toLocaleString("en-US")} fields` : "";
            lyr.bindTooltip(here(p) ? name : `${name}${count}<br><b>Click to open</b>`, {
              permanent: false,
              sticky: true,
              className: "county-tip",
            });
            if (!here(p)) {
              lyr.on("click", () => goTo(p.fips, defaultFips));
              lyr.on("mouseover", () => (lyr as L.Path).setStyle({ fillOpacity: 0.18, weight: 2 }));
              lyr.on("mouseout", () => (lyr as L.Path).setStyle({ fillOpacity: 0.06, weight: 1.2 }));
            }
          },
        } as L.GeoJSONOptions;
        layer = L.geoJSON(shapes, options).addTo(map);
        // permanent names, only when zoomed in enough to read them
        const names = L.layerGroup(
          shapes.features
            .filter((f) => !here(f.properties as CountyProps))
            .map((f) => {
              const p = f.properties as CountyProps;
              const centre = L.geoJSON(f).getBounds().getCenter();
              return L.marker(centre, {
                pane: PANE,
                interactive: false,
                icon: L.divIcon({ className: "county-name", html: p.label.replace(/ County, KS$/, ""), iconSize: undefined }),
              });
            }),
        );
        onZoom = () => {
          if (map.getZoom() >= LABEL_MIN_ZOOM) names.addTo(map);
          else names.remove();
        };
        onZoom();
        map.on("zoomend", onZoom);
        layer.on("remove", () => names.remove());
      })
      .catch(() => {});

    return () => {
      ctrl.abort();
      if (onZoom) map.off("zoomend", onZoom);
      layer?.remove();
    };
  }, [map]);

  return null;
}

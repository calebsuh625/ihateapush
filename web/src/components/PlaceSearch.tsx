// The search box in the masthead: type a county, a town, an address or "lat, lon" and go there.
// Counties match as you type (from GET /regions/shapes). Towns and addresses are looked up in
// OpenStreetMap's Nominatim only when Enter is pressed (its rules forbid search-as-you-type), limited
// to Kansas. A place in the open county is flown to and pinned; a place in another county opens that
// county at the place (?region=<fips>&at=lat,lon).
import { useEffect, useId, useMemo, useRef, useState } from "react";
import L from "leaflet";
import { SELECTED_REGION } from "../api";
import { KANSAS_VIEWBOX, countiesFromShapes, countyAt, matchCounties, parseLatLon, placesFromNominatim } from "../lib/places";
import type { CountyShape, Place } from "../lib/places";
import { formatCount } from "../lib/format";
import { countyShapes } from "./countyShapes";
import { goTo } from "./RegionLabel";

const DEFAULT_FIPS = "20055";
const PLACE_ZOOM = 14;

type Option =
  | { kind: "county"; county: CountyShape }
  | { kind: "place"; place: Place; county: CountyShape | null }
  | { kind: "search" };

type Lookup = { status: "idle" } | { status: "loading"; q: string } | { status: "done"; q: string; places: Place[] } | { status: "error"; q: string };

let pin: L.Marker | null = null;

/** One pin at a time, for the place a search went to. */
export function showPin(map: L.Map, lat: number, lon: number, label?: string) {
  pin?.remove();
  pin = L.marker([lat, lon], {
    icon: L.divIcon({ className: "search-pin", iconSize: [18, 18] }),
    interactive: false,
    keyboard: false,
  }).addTo(map);
  if (label) pin.bindTooltip(label, { permanent: true, direction: "top", offset: [0, -10], className: "search-pin-label" });
}

export function PlaceSearch({ map }: { map: L.Map | null }) {
  const [counties, setCounties] = useState<CountyShape[]>([]);
  const [text, setText] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const [lookup, setLookup] = useState<Lookup>({ status: "idle" });
  const ctrl = useRef<AbortController | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const listId = useId();
  const current = SELECTED_REGION ?? DEFAULT_FIPS;

  useEffect(() => {
    let live = true;
    countyShapes().then((s) => live && setCounties(countiesFromShapes(s)));
    return () => {
      live = false;
      ctrl.current?.abort();
    };
  }, []);

  // close the list on a click anywhere else
  useEffect(() => {
    const away = (e: PointerEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", away);
    return () => document.removeEventListener("pointerdown", away);
  }, []);

  const q = text.trim();
  const options = useMemo<Option[]>(() => {
    const out: Option[] = [];
    const coords = parseLatLon(q);
    if (coords) {
      const [lat, lon] = coords;
      out.push({ kind: "place", place: { name: `${lat.toFixed(4)}, ${lon.toFixed(4)}`, detail: "coordinates", lat, lon }, county: countyAt(counties, lon, lat) });
    }
    for (const county of matchCounties(counties, q)) out.push({ kind: "county", county });
    if (lookup.status === "done" && lookup.q === q) {
      for (const place of lookup.places) out.push({ kind: "place", place, county: countyAt(counties, place.lon, place.lat) });
    }
    if (!coords && q.length >= 2 && !(lookup.status !== "idle" && lookup.q === q)) out.push({ kind: "search" });
    return out;
  }, [q, counties, lookup]);

  const searchPlaces = () => {
    if (q.length < 2) return;
    ctrl.current?.abort();
    const c = new AbortController();
    ctrl.current = c;
    setLookup({ status: "loading", q });
    setOpen(true);
    const url = new URL("https://nominatim.openstreetmap.org/search");
    url.search = new URLSearchParams({
      q, format: "jsonv2", countrycodes: "us", viewbox: KANSAS_VIEWBOX, bounded: "1", limit: "8", "accept-language": "en",
    }).toString();
    fetch(url, { signal: c.signal, headers: { Accept: "application/json" } })
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error(String(res.status)))))
      .then((body: unknown) => {
        // the viewbox is a rectangle: keep only places inside a Kansas county when the outlines are known
        const places = placesFromNominatim(body).filter((p) => counties.length === 0 || countyAt(counties, p.lon, p.lat));
        setLookup({ status: "done", q, places: places.slice(0, 6) });
        setActive(-1);
      })
      .catch((err: unknown) => {
        if ((err as Error)?.name !== "AbortError") setLookup({ status: "error", q });
      });
  };

  const choose = (o: Option) => {
    if (o.kind === "search") {
      searchPlaces();
      return;
    }
    setOpen(false);
    if (o.kind === "county") {
      if (o.county.fips === current) map?.flyToBounds(o.county.bounds, { padding: [24, 24], duration: 0.8 });
      else goTo(o.county.fips, DEFAULT_FIPS, { label: o.county.label });
      return;
    }
    const { place, county } = o;
    if (county && county.fips !== current) {
      goTo(county.fips, DEFAULT_FIPS, { label: county.label, at: [place.lat, place.lon] });
      return;
    }
    if (map) {
      map.flyTo([place.lat, place.lon], PLACE_ZOOM, { duration: 1 });
      showPin(map, place.lat, place.lon, place.name);
    }
  };

  const onKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      setOpen(true);
      if (options.length === 0) return;
      const step = e.key === "ArrowDown" ? 1 : -1;
      // -1 is the text box itself; the list wraps round through it
      setActive((i) => ((i + 1 + step + options.length + 1) % (options.length + 1)) - 1);
    } else if (e.key === "Enter") {
      e.preventDefault();
      const picked = options[active];
      if (picked) choose(picked);
      else searchPlaces();
    } else if (e.key === "Escape") {
      setOpen(false);
      setActive(-1);
    }
  };

  const shown = open && q.length > 0;
  return (
    <div className="place-search" ref={box} data-testid="place-search">
      <input
        type="search"
        className="place-search-input"
        placeholder="Search a town, county or address"
        aria-label="Search a town, county or address in Kansas"
        role="combobox"
        aria-expanded={shown}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={shown && active >= 0 && active < options.length ? `${listId}-${active}` : undefined}
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          setOpen(true);
          setActive(-1);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={onKey}
        autoComplete="off"
        spellCheck={false}
      />
      {shown && (
        <ul className="place-search-list" id={listId} role="listbox" aria-label="Search results">
          {options.map((o, i) => (
            <li
              key={o.kind === "county" ? `c${o.county.fips}` : o.kind === "place" ? `p${o.place.lat},${o.place.lon}` : "search"}
              id={`${listId}-${i}`}
              role="option"
              aria-selected={i === active}
              className={i === active ? "place-search-option is-active" : "place-search-option"}
              data-kind={o.kind}
              onPointerDown={(e) => e.preventDefault()} // keep the focus in the box
              onClick={() => choose(o)}
              onMouseEnter={() => setActive(i)}
            >
              <OptionText option={o} q={q} current={current} />
            </li>
          ))}
          {lookup.status === "loading" && lookup.q === q && (
            <li className="place-search-note" role="presentation">
              <span className="spinner" aria-hidden="true" /> Searching towns and addresses…
            </li>
          )}
          {lookup.status === "done" && lookup.q === q && lookup.places.length === 0 && (
            <li className="place-search-note" role="presentation">No town or address in Kansas matches “{q}”.</li>
          )}
          {lookup.status === "error" && lookup.q === q && (
            <li className="place-search-note" role="presentation">The place search did not answer. Try again, or pick a county.</li>
          )}
          {options.length === 0 && lookup.status === "idle" && (
            <li className="place-search-note" role="presentation">Type at least 2 letters.</li>
          )}
        </ul>
      )}
    </div>
  );
}

function OptionText({ option: o, q, current }: { option: Option; q: string; current: string }) {
  if (o.kind === "search") {
    return (
      <>
        <span className="place-search-name">Search towns and addresses for “{q}”</span>
        <span className="place-search-detail">Enter</span>
      </>
    );
  }
  if (o.kind === "county") {
    const here = o.county.fips === current;
    const count = o.county.fieldCount !== null ? `${formatCount(o.county.fieldCount)} fields` : "";
    return (
      <>
        <span className="place-search-name">{o.county.label.replace(/, KS$/, "")}</span>
        <span className="place-search-detail">{here ? "open now" : count ? `County · ${count}` : "County"}</span>
      </>
    );
  }
  const where = o.county ? o.county.label.replace(/, KS$/, "") : o.place.detail;
  const opens = o.county && o.county.fips !== current ? " · opens it" : "";
  return (
    <>
      <span className="place-search-name">{o.place.name}</span>
      <span className="place-search-detail">{where}{opens}</span>
    </>
  );
}

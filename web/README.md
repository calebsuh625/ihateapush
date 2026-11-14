# web — the map

Vite + React + TypeScript + react-leaflet, on OpenStreetMap tiles. No API keys.

Click a field → a panel with its crop history, soil and the three ranked rotation plans.

## Run

From the repo root, `make dev` starts the API (:8010) and this app (:5173). On its own:

```bash
npm install
API_PORT=8010 npm run dev        # http://localhost:5173, proxies /api → http://127.0.0.1:8010
npm run build                    # strict type-check (tsc -b) + production build into dist/
API_PORT=8010 npm run preview    # serve dist/ with the same /api proxy
```

The app calls the API at `import.meta.env.VITE_API_URL ?? "/api"`. Leave `VITE_API_URL` unset for
local work; set it at build time to an absolute URL to talk to an API elsewhere (it must allow CORS).

A selected field is kept in the URL (`/#KS-000123`), so a field can be linked to.

## Check

```bash
npm test                # unit tests for colours, formatting, thresholds, geometry (node --test)
npm run e2e             # headless Chromium against a running web + API: really clicks fields on
                        #   the map and compares the panel with the API; prints timings
npm run e2e:states      # loading / failure / retry / unknown field / awkward data, forced by
                        #   intercepting requests in the browser
```

Both e2e scripts take `WEB_URL` (default `http://localhost:5173`); `npm run e2e` also takes
`N_FIELDS` (default 4). First time only: `npx playwright install chromium`.
Screenshots land in `e2e/screenshots/` (git-ignored).

## Where things are

| file | what |
|------|------|
| `src/api.ts` | the only code that talks to the backend (typed, abortable) |
| `src/crops.ts` | crop colours, the fallback palette for unseen crop names, labels |
| `src/thresholds.ts` | the cut-offs behind "Low", "Gently sloping", … |
| `src/components/FieldMap.tsx` | Leaflet map; all fields in one canvas layer, restyled imperatively |
| `src/components/FieldPanel.tsx` | the panel: header, crop history, soil, plans, loading and error states |
| `src/components/Timeline.tsx` | the year strip, its tooltip and legend |
| `src/components/PlanCard.tsx` | one rotation plan |
| `src/hooks/useResource.ts` | fetch-with-abort-and-retry; never shows one field's data under another |
| `src/hooks/useSelection.ts` | selected field ⇄ URL hash |
| `src/styles.css` | all styling (CSS variables at the top) |

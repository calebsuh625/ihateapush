// The loading card shown from the first moment the page exists until the fields are drawn, so a
// visitor can see the site is working while a county's server starts (10-30 s the first time).
// index.html shows the same card before this code has even downloaded (its styles live there too, as
// .boot*); React then takes over with the real steps. showLeaving() puts it up again the moment a
// visitor opens another county, before the page reloads.
// Motion: the pivot keeps sweeping and the bar keeps moving on purpose, because "it is not frozen" is
// the whole message; with prefers-reduced-motion they only run slower (index.html).
import { useEffect, useState } from "react";
import { formatCount } from "../lib/format";

export type StepState = "done" | "now" | "todo";

export interface BootStep {
  label: string;
  state: StepState;
}

/** Seconds since the page started loading (not since React started), so the count never restarts. */
const sinceStart = () => Math.max(0, Math.floor(performance.now() / 1000));

function Pivot() {
  // a centre-pivot field seen from above, its arm sweeping round
  return (
    <svg className="boot-pivot" viewBox="0 0 64 64" aria-hidden="true">
      <circle cx="32" cy="32" r="29" className="boot-pivot-field" />
      <circle cx="32" cy="32" r="19" className="boot-pivot-ring" />
      <g className="boot-pivot-arm">
        <path d="M32 32 L32 3 A29 29 0 0 1 52.5 11.5 Z" className="boot-pivot-wet" />
        <line x1="32" y1="32" x2="32" y2="3" />
      </g>
      <circle cx="32" cy="32" r="3" className="boot-pivot-hub" />
    </svg>
  );
}

export function BootCard({ title, steps, hint }: { title: string; steps: BootStep[]; hint?: string }) {
  const [seconds, setSeconds] = useState(sinceStart);
  useEffect(() => {
    const id = window.setInterval(() => setSeconds(sinceStart()), 500);
    return () => window.clearInterval(id);
  }, []);
  return (
    <div className="boot" data-testid="boot" role="status" aria-live="polite">
      <div className="boot-card">
        <div className="boot-head">
          <Pivot />
          <div>
            <p className="boot-title">{title}</p>
            <p className="boot-time">
              <span className="boot-seconds">{seconds}</span> s
            </p>
          </div>
        </div>
        <ol className="boot-steps">
          {steps.map((s) => (
            <li key={s.label} className={`boot-step is-${s.state}`} data-state={s.state}>
              <span className="boot-mark" aria-hidden="true">{s.state === "done" ? "✓" : ""}</span>
              {s.label}
            </li>
          ))}
        </ol>
        <div className="boot-bar" aria-hidden="true"><span /></div>
        {hint && <p className="boot-hint">{hint}</p>}
      </div>
    </div>
  );
}

const SLOW_AFTER_S = 10;

/** The steps of opening the page, from what has arrived so far. */
export function bootSteps(opts: {
  county: boolean;
  healthReady: boolean;
  fieldCount: number | null;
  fieldsReady: boolean;
  drawn: boolean;
}): BootStep[] {
  const { county, healthReady, fieldCount, fieldsReady, drawn } = opts;
  const server = healthReady || fieldsReady;
  const at = (done: boolean, ready: boolean): StepState => (done ? "done" : ready ? "now" : "todo");
  const fields = fieldCount !== null ? `${formatCount(fieldCount)} fields` : "the fields";
  return [
    { label: "Load the app", state: "done" },
    { label: county ? "Start this county's data server" : "Wake up the data server", state: at(server, true) },
    { label: `Download ${fields}`, state: at(fieldsReady, server) },
    { label: "Draw them on the map", state: at(drawn, fieldsReady) },
  ];
}

export function BootLoader({ county, ...rest }: Parameters<typeof bootSteps>[0]) {
  const [slow, setSlow] = useState(() => sinceStart() >= SLOW_AFTER_S);
  useEffect(() => {
    if (slow) return;
    const id = window.setTimeout(() => setSlow(true), Math.max(0, SLOW_AFTER_S - sinceStart()) * 1000);
    return () => window.clearTimeout(id);
  }, [slow]);
  const hint = slow
    ? county
      ? "The first visit to a county starts its own server, which takes 10 to 30 seconds. Still working."
      : "The server was asleep and is starting up, which takes up to 30 seconds. Still working."
    : undefined;
  return <BootCard title={county ? "Opening the county" : "Loading the map"} steps={bootSteps({ county, ...rest })} hint={hint} />;
}

/** Put the loading card up right away when the page is about to reload into another county. */
export function showLeaving(title: string) {
  if (document.querySelector(".boot--leaving")) return;
  const el = document.createElement("div");
  el.className = "boot boot--leaving";
  el.setAttribute("role", "status");
  el.innerHTML = `<div class="boot-card"><div class="boot-head">
    <svg class="boot-pivot" viewBox="0 0 64 64" aria-hidden="true"><circle cx="32" cy="32" r="29" class="boot-pivot-field"/>
    <circle cx="32" cy="32" r="19" class="boot-pivot-ring"/><g class="boot-pivot-arm"><path d="M32 32 L32 3 A29 29 0 0 1 52.5 11.5 Z" class="boot-pivot-wet"/>
    <line x1="32" y1="32" x2="32" y2="3"/></g><circle cx="32" cy="32" r="3" class="boot-pivot-hub"/></svg>
    <div><p class="boot-title"></p><p class="boot-time">Loading the page</p></div></div>
    <div class="boot-bar" aria-hidden="true"><span></span></div></div>`;
  const t = el.querySelector(".boot-title");
  if (t) t.textContent = title;
  document.body.appendChild(el);
  // a Back navigation can restore this page from the cache with the card still up
  window.addEventListener("pageshow", (e) => { if (e.persisted) el.remove(); }, { once: true });
}

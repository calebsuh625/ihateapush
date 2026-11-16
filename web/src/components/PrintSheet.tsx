// The printed plan: a clean sheet that is hidden on screen and is all that prints (see Share.css). It is
// rendered into its own node under <body>, so printing does not depend on the panel's scrolling. It only
// repeats numbers the page already has; nothing is worked out here.
import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";
import { cropBackground, cropLabel } from "../crops";
import { PRINT_CAVEATS, farmSettingsText, settingsLine } from "../lib/exportplan";
import { fieldChange, FIELD_TAGS, formatAcres, formatFarmDollars, formatFarmScore, formatWhole, limitTone, LIMIT_TAGS, metWords } from "../lib/farm";
import { formatScore, hectaresToAcres, formatOneDecimal } from "../lib/format";
import { RANKING_SCORE_LABEL, formatDollars, formatInches, formatShare, holdsText } from "../lib/planner";
import type { FarmPlan, FieldPlans, FieldScore, Plan } from "../types";
import { REGION_LABEL } from "../config";
import { tableRows } from "./OwnRotation";
import "./Share.css";

function printRoot(): HTMLElement {
  let node = document.getElementById("print-root");
  if (!node) {
    node = document.createElement("div");
    node.id = "print-root";
    document.body.appendChild(node);
  }
  return node;
}

/** Puts `children` in the print node and tells the style sheet that there is something to print. */
function Sheet({ kind, title, children }: { kind: "field" | "farm"; title: string; children: ReactNode }) {
  const [root] = useState(printRoot);
  useEffect(() => {
    document.documentElement.classList.add("has-print-sheet");
    return () => document.documentElement.classList.remove("has-print-sheet");
  }, []);
  const printed = new Date().toLocaleDateString("en-US", { year: "numeric", month: "long", day: "numeric" });
  return createPortal(
    <article className="sheet" data-testid="print-sheet" data-kind={kind} aria-hidden="true">
      <header className="sheet-head">
        <h1>{title}</h1>
        <p className="sheet-printed">
          {REGION_LABEL} · printed {printed}
        </p>
      </header>
      {children}
    </article>,
    root,
  );
}

function Years({ crops }: { crops: readonly string[] }) {
  return (
    <span className="sheet-years">
      {crops.map((crop, index) => (
        <span key={index} className="sheet-year">
          <span className="sheet-swatch" style={{ background: cropBackground(crop) }} />
          {cropLabel(crop)}
        </span>
      ))}
    </span>
  );
}

function PlanBlock({ plan, label, irrigated }: { plan: Plan; label: string; irrigated: boolean }) {
  const n = plan.numbers;
  const profit = plan.spread?.profit_per_acre;
  const robust = typeof plan.scores.robust === "number" ? plan.scores.robust : plan.scores.overall;
  return (
    <li className="sheet-plan" data-testid="print-plan">
      <p className="sheet-plan-head">
        <strong>{label}</strong> <span data-testid="print-plan-name">{plan.name}</span>
        <span className="sheet-score">
          {RANKING_SCORE_LABEL} <b data-testid="print-plan-score">{formatScore(robust)}</b>
        </span>
      </p>
      <p>
        <Years crops={plan.rotation} />
      </p>
      <p className="sheet-line" data-testid="print-plan-scores">
        Soil protection {formatScore(plan.scores.soil)} · Water {formatScore(plan.scores.water)} · Money {formatScore(plan.scores.money)}
        {typeof plan.scores.risk === "number" ? ` · Risk ${formatScore(plan.scores.risk)}` : ""} (0 to 1, higher is better)
      </p>
      <p className="sheet-line" data-testid="print-plan-figures">
        {plan.futures ? `${holdsText(plan.futures.holds, plan.futures.total)}. ` : ""}
        {n ? `Return ${formatDollars(n.profit_per_acre)} an acre a year over direct costs` : ""}
        {profit ? ` (across the climate models: low ${formatDollars(profit.low)}, middle ${formatDollars(profit.mid)}, high ${formatDollars(profit.high)})` : ""}
        {n ? `. ${Math.round(n.bare_days)} bare days a year. ` : ""}
        {n ? (irrigated ? `Pumps ${formatInches(n.irrigation_in, n.irrigation_mm)} a year` : `Rain covers ${formatShare(n.water_met_share)} of crop water need`) : ""}
        {n ? `. ${formatShare(n.failed_share)} of planted crops fail.` : ""}
      </p>
    </li>
  );
}

interface FieldSheetProps {
  fieldId: string;
  areaHa: number | null;
  answer: FieldPlans;
  /** the build-your-own result, when there is one on screen */
  score: FieldScore | null;
  url: string;
}

export function FieldPrintSheet({ fieldId, areaHa, answer, score, url }: FieldSheetProps) {
  const { settings } = answer;
  const irrigated = Boolean(settings?.irrigated);
  const rows = score ? tableRows(score) : null;
  const columns = score ? [score.yours, ...score.plans.slice(0, 3)] : [];
  const limits = settings?.limits ?? [];
  return (
    <Sheet kind="field" title={`Rotation plan for field ${fieldId}`}>
      <p className="sheet-field" data-testid="print-field">
        {fieldId}
        {areaHa !== null ? ` · ${formatOneDecimal(areaHa)} ha (${formatOneDecimal(hectaresToAcres(areaHa))} acres)` : ""}
      </p>
      <p className="sheet-settings" data-testid="print-settings">
        <strong>Settings and assumptions.</strong> {settings ? settingsLine(settings) : "Future-weather planning is not set up on this server, so these plans do not look at the climate ahead."}
      </p>
      <h2>Our top {Math.min(3, answer.plans.length)}</h2>
      <ol className="sheet-plans">
        {answer.plans.slice(0, 3).map((plan, index) => (
          <PlanBlock key={index} plan={plan} label={`${index + 1}. ${index === 0 ? "Recommended" : "Alternative"}`} irrigated={irrigated} />
        ))}
      </ol>
      {answer.current && (
        <>
          <h2>What this field grows now</h2>
          <ol className="sheet-plans">
            <PlanBlock plan={answer.current} label="Now" irrigated={irrigated} />
          </ol>
        </>
      )}
      {score && rows && (
        <section data-testid="print-own">
          <h2>Your own rotation: {score.yours.name}</h2>
          <table className="sheet-table">
            <thead>
              <tr>
                <th />
                {columns.map((_, column) => (
                  <th key={column}>{column === 0 ? "Yours" : `Our ${column}`}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {[...rows.scores, ...rows.figures].map((row) => (
                <tr key={row.key}>
                  <th scope="row">{row.label}</th>
                  {row.cells.map((cell, column) => (
                    <td key={column}>
                      {cell.text}
                      {cell.sub ? <span className="sheet-sub"> {cell.sub}</span> : null}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          {score.comparison.headline.map((sentence, index) => (
            <p key={index} className="sheet-line">
              {sentence}
            </p>
          ))}
        </section>
      )}
      <Caveats extra={limits} />
      <p className="sheet-link" data-testid="print-link">
        Open this exact view again: {url}
      </p>
    </Sheet>
  );
}

function Caveats({ extra, notes, title = "What to keep in mind" }: { extra?: readonly string[]; notes?: readonly string[]; title?: string }) {
  return (
    <section className="sheet-caveats" data-testid="print-caveats">
      <h2>{title}</h2>
      <ul>
        {PRINT_CAVEATS.map((sentence, index) => (
          <li key={`c${index}`}>{sentence}</li>
        ))}
        {(extra ?? []).map((sentence, index) => (
          <li key={`e${index}`}>{sentence}</li>
        ))}
        {(notes ?? []).map((sentence, index) => (
          <li key={`n${index}`}>{sentence}</li>
        ))}
      </ul>
    </section>
  );
}

export function FarmPrintSheet({ answer, url }: { answer: FarmPlan; url: string }) {
  const { plan, alone, limits } = answer;
  const years = plan.years.length;
  return (
    <Sheet kind="farm" title="Farm plan">
      <p className="sheet-field" data-testid="print-field">
        {formatWhole(answer.farm.fields)} fields · {formatWhole(answer.farm.area_acres)} acres · {answer.farm.irrigated_fields} irrigated, {answer.farm.dryland_fields} dryland
      </p>
      <p className="sheet-settings" data-testid="print-settings">
        <strong>Settings and assumptions.</strong> {farmSettingsText(answer)}
      </p>
      <h2 data-testid="print-farm-status">
        {answer.status === "limits_met" ? "Limits met" : answer.status === "limits_not_met" ? "Limits cannot all be met" : "No limits set"}
      </h2>
      {answer.summary.map((sentence, index) => (
        <p key={index} className="sheet-line">
          {sentence}
        </p>
      ))}
      {[["Water", limits.water], ["Return", limits.return]].map(([name, limit]) =>
        typeof limit === "object" && limit.set ? (
          <p key={name as string} className="sheet-line">
            <strong>{LIMIT_TAGS[limitTone(limit)]}.</strong> {limit.sentence}
          </p>
        ) : null,
      )}
      <table className="sheet-table" data-testid="print-farm-compare">
        <thead>
          <tr>
            <th />
            <th>Farm plan</th>
            <th>Each field alone</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <th scope="row">Ranking score, by acres</th>
            <td>{formatFarmScore(plan.score)}</td>
            <td>{formatFarmScore(alone.score)}</td>
          </tr>
          <tr>
            <th scope="row">Most water pumped in a year (acre-inches)</th>
            <td>{formatWhole(plan.peak_water.value)} (year {plan.peak_water.n})</td>
            <td>{formatWhole(alone.peak_water.value)} (year {alone.peak_water.n})</td>
          </tr>
          <tr>
            <th scope="row">Return in the leanest year, over direct costs</th>
            <td>{formatFarmDollars(plan.worst_return.value)} (year {plan.worst_return.n})</td>
            <td>{formatFarmDollars(alone.worst_return.value)} (year {alone.worst_return.n})</td>
          </tr>
          <tr>
            <th scope="row">Return in an average year, over direct costs</th>
            <td>{formatFarmDollars(plan.average.return)}</td>
            <td>{formatFarmDollars(alone.average.return)}</td>
          </tr>
          <tr>
            <th scope="row">Within the water budget</th>
            <td>{metWords(plan.water_ok, plan.water_years_over, years, "No budget set")}</td>
            <td>{metWords(alone.water_ok, alone.water_years_over, years, "No budget set")}</td>
          </tr>
          <tr>
            <th scope="row">Meets the least return</th>
            <td>{metWords(plan.return_ok, plan.return_years_under, years, "No minimum set")}</td>
            <td>{metWords(alone.return_ok, alone.return_years_under, years, "No minimum set")}</td>
          </tr>
        </tbody>
      </table>
      <h2>Field by field (years 1 to {years})</h2>
      <table className="sheet-table sheet-fields" data-testid="print-farm-fields">
        <thead>
          <tr>
            <th>Field</th>
            <th>Plan</th>
            <th>Crops by year</th>
          </tr>
        </thead>
        <tbody>
          {answer.fields.map((field) => (
            <tr key={field.field_id} data-testid="print-farm-row">
              <td>
                <strong>{field.field_id}</strong>
                <br />
                {formatAcres(field.area_acres)} acres, {field.irrigated ? "irrigated" : "dryland"}
              </td>
              <td>
                {field.plan.name}
                <br />
                <span className="sheet-sub">{FIELD_TAGS[fieldChange(field)]}</span>
              </td>
              <td>
                <Years crops={field.plan.years} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <Caveats title="What this plan assumes and leaves out" notes={[...answer.notes, ...answer.not_modelled.map((n) => `Not in the plan: ${n}`)]} />
      <p className="sheet-link" data-testid="print-link">
        Open this exact view again: {url}
      </p>
    </Sheet>
  );
}

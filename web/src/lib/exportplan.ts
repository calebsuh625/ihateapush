// Export: the top plans and the farm plan as CSV text, the settings line, and the fixed caveats the
// printed sheet carries. Pure (type imports only), so the unit tests load this file directly.
//
// The CSV holds what the cards and the farm table show, at the precision they show it: scores to two
// decimals, dollars whole, bare days whole, water to a tenth of an inch, shares in whole percent.
// Text is plain ASCII (typographic dashes and minus signs are folded) so Excel and plain parsers agree.

import type { FarmPlan, Plan, PlannerSettings } from "../types";

// ---------------------------------------------------------------------------------------------
// CSV basics
// ---------------------------------------------------------------------------------------------

const FOLD: Record<string, string> = {
  "−": "-", "–": "-", "—": "-", "‐": "-", "‑": "-",
  "×": "x", "‘": "'", "’": "'", "“": '"', "”": '"',
  "…": "...", " ": " ", "→": "->", "≥": ">=", "≤": "<=", "°": " degrees ", "≈": "about ",
};

/** Typographic characters to plain ones; anything else outside ASCII is dropped. */
export function asciiText(text: string): string {
  let out = "";
  for (const ch of text) {
    if (ch.charCodeAt(0) < 128) out += ch;
    else out += FOLD[ch] ?? "";
  }
  return out.replace(/\s+/g, " ").trim();
}

/** One text cell: folded to ASCII, a spreadsheet formula cannot start in it, quoted when needed. */
export function csvText(text: string | null | undefined): string {
  let value = asciiText(text ?? "");
  if (/^[=+@\t\r-]/.test(value)) value = `'${value}`;
  return /[",\r\n]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value;
}

/** One number cell: empty when there is no number. */
export const csvNumber = (n: number | null | undefined, digits = 0): string =>
  typeof n === "number" && Number.isFinite(n) ? (Math.round(n * 10 ** digits) / 10 ** digits).toFixed(digits) : "";

/** A comment row: starts with "#", no commas (so a spreadsheet keeps it in one cell). */
export const csvComment = (text: string): string => `# ${asciiText(text).replace(/,/g, ";")}`;

export const csvRow = (cells: readonly string[]): string => cells.join(",");

export const CSV_EOL = "\r\n";

// ---------------------------------------------------------------------------------------------
// Settings in words
// ---------------------------------------------------------------------------------------------

const SCENARIO_WORDS: Record<string, string> = { ssp245: "middle road", ssp585: "high emissions" };

const percent = (share: number) => `${Math.round(share * 100)}%`;
const signed = (n: number) => (Math.round(n) < 0 ? `-${Math.abs(Math.round(n))}%` : `+${Math.round(n)}%`);

/** The weights as the planner used them (they add up to 1). */
export function weightsText(weights: Record<string, number>): string {
  const labels: [string, string][] = [["soil", "soil protection"], ["water", "water"], ["money", "money"], ["risk", "risk"]];
  return labels.filter(([key]) => typeof weights[key] === "number").map(([key, label]) => `${label} ${percent(weights[key] as number)}`).join(", ");
}

export function futureText(horizon: number, scenario: string, window?: readonly number[]): string {
  if (horizon <= 2025) return "today's climate";
  const years = window && window.length === 2 ? `, averaging ${window[0]}-${window[1]}` : "";
  return `${horizon}, ${SCENARIO_WORDS[scenario] ?? scenario} path${years}`;
}

/** The assumptions behind a field's answer, from the answer's own settings. */
export function settingsParts(settings: PlannerSettings): string[] {
  const parts = [`Priorities: ${weightsText(settings.weights)}`, `Future: ${futureText(settings.horizon, settings.scenario, settings.window)}`];
  if (settings.irrigated) {
    const why = settings.irrigation_source === "guess" ? "our guess from the crop history" : "set by you";
    parts.push(`Irrigated (${why})`);
    parts.push(
      typeof settings.irrigation_limit_in === "number" && settings.irrigation_limit_in > 0
        ? `Well assumed to deliver at most ${Math.round(settings.irrigation_limit_in * 10) / 10} inches a year`
        : "No limit on pumping",
    );
  } else {
    parts.push(`Dryland (${settings.irrigation_source === "guess" ? "our guess from the crop history" : "set by you"})`);
  }
  const whatIf = settings.what_if;
  if (whatIf && whatIf.active) parts.push(`What-if: crop prices ${signed(whatIf.price_pct)}, direct costs ${signed(whatIf.cost_pct)} against today's`);
  else parts.push("Prices and costs as of today's data");
  return parts;
}

export const settingsLine = (settings: PlannerSettings): string => `${settingsParts(settings).join(". ")}.`;

/** The wording that goes on every printed sheet. */
export const PRINT_CAVEATS: readonly string[] = [
  "The soil score is soil protection: fewer bare days a year and lower wind-erosion risk. It is not a measure of soil health.",
  "Dollar figures are returns over direct costs, not profit or income. On dryland the sign can flip with the price years and the fallow cost.",
  "Each future is one climate model. The models agree on heat, not on rain.",
  "Scores rank the choices against each other; they are not forecasts. Prices and costs are today's unless a what-if is shown.",
];

// ---------------------------------------------------------------------------------------------
// The top plans
// ---------------------------------------------------------------------------------------------

export interface PlansCsvInput {
  fieldId: string;
  areaAcres: number | null;
  settings: PlannerSettings | null;
  plans: readonly Plan[];
  /** "Carrying on as now" */
  current?: Plan | null;
  /** the farmer's own rotation, when it has been scored */
  yours?: Plan | null;
  /** how many patterns were compared */
  candidates?: number;
  /** "today's prices" line for the what-if note */
  link?: string;
}

const PLAN_COLUMNS = [
  "rank", "name", "year_1", "year_2", "year_3", "year_4", "cycle", "place_of_candidates", "ranking_score",
  "soil_protection_score", "water_score", "money_score", "risk_score", "holds_up_in_futures", "futures_total",
  "return_low_per_acre_year", "return_middle_per_acre_year", "return_high_per_acre_year", "bare_days_per_year",
  "water_pumped_inches_per_year", "crop_water_need_met_percent", "planted_crops_failing_percent", "reason",
];

function planRow(label: string, plan: Plan, irrigated: boolean): string {
  const n = plan.numbers;
  const profit = plan.spread?.profit_per_acre;
  const robust = typeof plan.scores.robust === "number" ? plan.scores.robust : plan.scores.overall;
  const years = [0, 1, 2, 3].map((i) => csvText(plan.rotation[i] ?? ""));
  return csvRow([
    csvText(label), csvText(plan.name), ...years, csvText((plan.cycle ?? plan.rotation).join(" > ")),
    csvNumber(plan.rank ?? null), csvNumber(robust, 2), csvNumber(plan.scores.soil, 2), csvNumber(plan.scores.water, 2),
    csvNumber(plan.scores.money, 2), csvNumber(plan.scores.risk, 2), csvNumber(plan.futures?.holds ?? null), csvNumber(plan.futures?.total ?? null),
    csvNumber(profit?.low ?? null), csvNumber(profit?.mid ?? null), csvNumber(profit?.high ?? null),
    csvNumber(n?.bare_days ?? null), irrigated && n ? csvNumber(n.irrigation_in ?? n.irrigation_mm / 25.4, 1) : "",
    n ? csvNumber(n.water_met_share * 100) : "", n ? csvNumber(n.failed_share * 100) : "", csvText(plan.reason),
  ]);
}

/** The CSV for a field: comment rows with the settings, a header row, our top plans (and the field's own pattern and the farmer's own rotation when given). */
export function plansCsv(input: PlansCsvInput): string {
  const { settings } = input;
  const irrigated = Boolean(settings?.irrigated);
  const lines = [csvComment(`Rotation plans for field ${input.fieldId}${input.areaAcres !== null ? ` (${Math.round(input.areaAcres)} acres)` : ""}`)];
  lines.push(csvComment(settings ? `Settings: ${settingsLine(settings)}` : "Settings: the basic planner answered (no future-weather settings)"));
  lines.push(csvComment("Scores are 0 to 1; higher is better. The soil score is soil protection (fewer bare days; lower wind-erosion risk)."));
  lines.push(csvComment("Returns are dollars per acre per year over direct costs; not profit. Low and high are the lowest and highest climate model; the middle is their average."));
  if (input.candidates) lines.push(csvComment(`Plans are chosen from ${input.candidates} patterns compared; place_of_candidates is a plan's place among them`));
  lines.push(csvRow(PLAN_COLUMNS));
  input.plans.slice(0, 3).forEach((plan, index) => lines.push(planRow(String(index + 1), plan, irrigated)));
  if (input.current) lines.push(planRow("as now", input.current, irrigated));
  if (input.yours) lines.push(planRow("yours", input.yours, irrigated));
  return lines.join(CSV_EOL) + CSV_EOL;
}

// ---------------------------------------------------------------------------------------------
// The farm plan
// ---------------------------------------------------------------------------------------------

const CHANGE_WORDS = { same: "own first pick", shifted: "own pick, other start year", changed: "different rotation" } as const;

const dollars = (n: number) => `${Math.round(n) < 0 ? "-" : ""}$${Math.abs(Math.round(n))}`;

export function farmSettingsText(answer: FarmPlan): string {
  const s = answer.settings;
  const water = s.water_budget_acre_in === null ? "no water budget" : `water budget ${Math.round(s.water_budget_acre_in)} acre-inches a year`;
  const minimum = s.min_income === null ? "no minimum return" : `least return ${dollars(s.min_income)} a year over direct costs`;
  return `Priorities: ${weightsText(s.weights)}. Future: ${futureText(s.horizon, s.scenario, s.window)}. ${water}; ${minimum}.`;
}

export function farmCsv(answer: FarmPlan): string {
  const years = Math.max(...answer.fields.map((f) => f.plan.years.length), answer.settings.span_years || 0, 1);
  const lines = [csvComment(`Farm plan: ${answer.farm.fields} fields; ${Math.round(answer.farm.area_acres)} acres; ${answer.farm.irrigated_fields} irrigated and ${answer.farm.dryland_fields} dryland`)];
  lines.push(csvComment(`Settings: ${farmSettingsText(answer)}`));
  lines.push(csvComment(`Status: ${answer.status.replace(/_/g, " ")}. Farm plan average return ${dollars(answer.plan.average.return)} a year over direct costs; leanest year ${dollars(answer.plan.worst_return.value)} (year ${answer.plan.worst_return.n}); most water ${Math.round(answer.plan.peak_water.value)} acre-inches (year ${answer.plan.peak_water.n})`));
  lines.push(csvComment("Years are 1 to 12 from the planning start, not calendar years. Returns are over direct costs, not profit. The soil score is soil protection."));
  const header = [
    "field_id", "acres", "practice", "compared_with_own_first_pick", "plan_name",
    ...Array.from({ length: years }, (_, i) => `year_${i + 1}`),
    "plan_ranking_score", "plan_return_per_acre_year", "plan_bare_days_per_year", "plan_water_pumped_inches_per_year", "own_first_pick_name", "sentence",
  ];
  lines.push(csvRow(header));
  for (const f of answer.fields) {
    const change = !f.same_rotation ? "changed" : f.same_start ? "same" : "shifted";
    const n = f.plan.numbers;
    const robust = typeof f.plan.scores.robust === "number" ? f.plan.scores.robust : f.plan.scores.overall;
    lines.push(
      csvRow([
        csvText(f.field_id), csvNumber(f.area_acres, 1), f.irrigated ? "irrigated" : "dryland", CHANGE_WORDS[change], csvText(f.plan.name),
        ...Array.from({ length: years }, (_, i) => csvText(f.plan.years[i] ?? "")),
        csvNumber(robust, 2), n ? csvNumber(n.profit_per_acre) : "", n ? csvNumber(n.bare_days) : "",
        f.irrigated && n ? csvNumber(n.irrigation_in ?? n.irrigation_mm / 25.4, 1) : "", csvText(f.own_pick.name), csvText(f.sentence),
      ]),
    );
  }
  return lines.join(CSV_EOL) + CSV_EOL;
}

/** A file name safe on every system: "rotation-plans-KS-014376.csv". */
export const csvFileName = (kind: "plans" | "farm", id?: string): string =>
  kind === "farm" ? "farm-plan.csv" : `rotation-plans-${(id ?? "field").replace(/[^A-Za-z0-9_-]+/g, "_")}.csv`;

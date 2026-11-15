// Pure helpers for the farm advisor panel: what is sent as context, the example questions, the words for
// errors and tools, and how an answer is laid out with its unmatched numbers marked. No value imports, so
// the unit tests can load this file directly.
import type { PlannerChoices } from "./planner";

export interface AdvisorStatus {
  available: boolean;
  provider?: string;
  model?: string;
  /** null when available, else "add GEMINI_API_KEY to .env". */
  reason?: string | null;
  /** Only when there is no key: the file, the exact line, and the sentence the panel shows. */
  env_file?: string;
  env_line?: string;
  message?: string;
  how?: string;
}

export interface ToolCall {
  name: string;
  args: Record<string, unknown>;
  summary: string;
  ok: boolean;
}

export interface UnmatchedNumber {
  text: string;
  value: number;
  /** Positions in the answer as JavaScript counts them: answer.slice(start, end) === text. */
  start: number;
  end: number;
}

export interface Grounding {
  checked: number;
  matched: number;
  unmatched: UnmatchedNumber[];
  passed: boolean;
}

export interface AdvisorReply {
  answer: string;
  tool_calls: ToolCall[];
  grounding: Grounding;
  model?: string;
}

export type ChatMessage =
  | { id: number; role: "user"; text: string }
  | { id: number; role: "assistant"; text: string; toolCalls: ToolCall[]; grounding: Grounding; model: string | null };

/** What the page sends with every question: the open field or farm and the current settings. */
export interface AdvisorContext {
  field_id?: string;
  farm_field_ids?: string[];
  weights: Record<string, number>;
  scenario: string;
  horizon: number;
  irrigated?: boolean;
  limit_in?: number;
}

/** What the advisor is talking about: one field, or the farm being planned. `null` = nothing is open. */
export type Subject = { kind: "field"; id: string } | { kind: "farm" } | null;

export const subjectKey = (subject: Subject): string | null =>
  subject === null ? null : subject.kind === "field" ? `field:${subject.id}` : "farm";

const WEIGHTS = ["soil", "water", "money", "risk"] as const;

export function advisorContext(subject: Subject, farmIds: readonly string[], choices: PlannerChoices, irrigationOverride: boolean | null): AdvisorContext {
  const context: AdvisorContext = {
    weights: Object.fromEntries(WEIGHTS.map((key) => [key, choices.weights[key]])),
    scenario: choices.scenario,
    horizon: choices.horizon,
  };
  if (subject?.kind === "field") {
    context.field_id = subject.id;
    if (irrigationOverride !== null) context.irrigated = irrigationOverride;
  } else if (subject?.kind === "farm") {
    context.farm_field_ids = [...farmIds];
  }
  if (typeof choices.limitIn === "number" && Number.isFinite(choices.limitIn)) context.limit_in = choices.limitIn;
  return context;
}

/** The conversation as the API wants it: only what was said (failed or cancelled questions stay on screen only). */
export function apiMessages(messages: readonly ChatMessage[]): { role: "user" | "assistant"; text: string }[] {
  return messages.map((message) => ({ role: message.role, text: message.text }));
}

// ---------------------------------------------------------------------------------------------
// Example questions
// ---------------------------------------------------------------------------------------------

/** Three or four questions that fit what is open. `irrigated` is null while it is not known yet. */
export function exampleQuestions(subject: Subject, irrigated: boolean | null): string[] {
  if (subject === null) return [];
  if (subject.kind === "farm") {
    return [
      "Which rotation should each field on this farm get?",
      "How much water would this whole farm pump in a year?",
      "What happens if I cut the farm's water use by a quarter?",
      "Does this farm still hold up in a hotter future?",
    ];
  }
  const first = "What should I plant next on this field, and why?";
  const weather = "How will the weather here change by 2040 and 2060?";
  if (irrigated === true) {
    return [first, "What if I could only pump half as much water?", "How fast is the water level dropping near this field?", weather];
  }
  if (irrigated === false) {
    return [first, "Which plan is safest if a dry year comes?", weather, "How does this field compare with a look-alike neighbour?"];
  }
  return [first, weather, "How does this field compare with a look-alike neighbour?"];
}

// ---------------------------------------------------------------------------------------------
// Words
// ---------------------------------------------------------------------------------------------

const TOOL_LABELS: Record<string, string> = {
  get_field: "Field record",
  get_plans: "Top rotation plans",
  score_rotation: "Score a rotation",
  transition_path: "Path to a rotation",
  farm_plan: "Whole-farm plan",
  compare_futures: "Compare futures",
  get_climate: "Future weather",
  get_twin: "Look-alike neighbour",
  water_outlook: "Groundwater",
};

export const toolLabel = (name: string): string => TOOL_LABELS[name] ?? name;

/** {field_id: "KS-1", weights: {soil: 3}} → 'field_id: KS-1, weights: soil 3'. */
export function argsText(args: Record<string, unknown>): string {
  const show = (value: unknown): string => {
    if (Array.isArray(value)) return value.length > 6 ? `${value.slice(0, 6).map(show).join(", ")} … (${value.length})` : value.map(show).join(", ");
    if (value !== null && typeof value === "object") {
      return Object.entries(value as Record<string, unknown>)
        .map(([key, inner]) => `${key} ${show(inner)}`)
        .join(", ");
    }
    return String(value);
  };
  const parts = Object.entries(args)
    .filter(([, value]) => value !== null && value !== undefined)
    .map(([key, value]) => `${key}: ${show(value)}`);
  return parts.length > 0 ? parts.join(" · ") : "no arguments";
}

/** An error as a plain sentence for the panel (the API's own sentence where it sent one). */
export function errorWords(status: number | null, detail: string): string {
  if (status === null) return "Could not reach the app's server. Check that it is running, then try again.";
  if (status === 404) return "This server does not have the advisor. Restart it from the latest code and try again.";
  return detail && !/^https?:\/\//.test(detail) ? detail : "The advisor could not answer. Try again.";
}

export const NOT_MATCHED_NOTE = "We could not match this number to the app's data. Check it before you rely on it.";

export function unmatchedNote(unmatched: readonly UnmatchedNumber[]): string | null {
  if (unmatched.length === 0) return null;
  const seen = [...new Set(unmatched.map((item) => item.text))];
  return `Not matched to our data: ${seen.join(", ")}. ${NOT_MATCHED_NOTE}`;
}

/** "Checked 17 numbers against the app's own answers; all matched." for the foot of an answer. */
export function groundingLine(grounding: Grounding): string {
  if (grounding.checked === 0) return "No numbers in this answer.";
  const n = grounding.checked;
  const noun = n === 1 ? "number" : "numbers";
  return grounding.passed
    ? `Checked ${n} ${noun} against the app's own answers: all matched.`
    : `Checked ${n} ${noun} against the app's own answers: ${grounding.matched} matched, ${grounding.unmatched.length} did not.`;
}

// ---------------------------------------------------------------------------------------------
// Laying out an answer
// ---------------------------------------------------------------------------------------------

export interface Run {
  text: string;
  bold: boolean;
  /** Index into the unmatched list when this run is a number we could not match, else null. */
  flag: number | null;
}

export interface Block {
  kind: "p" | "li";
  runs: Run[];
}

const BULLET = /^\s*(?:[*\-•]|\d{1,2}[.)])\s+/;
const HEADING = /^\s*#{1,6}\s+/;

/** Cut [from, to) of `text` at the unmatched ranges; each piece says which range it is, if any. */
function cut(text: string, from: number, to: number, bold: boolean, unmatched: readonly UnmatchedNumber[]): Run[] {
  const runs: Run[] = [];
  let at = from;
  const inside = unmatched
    .map((item, index) => ({ ...item, index }))
    .filter((item) => item.end > from && item.start < to)
    .sort((a, b) => a.start - b.start);
  for (const item of inside) {
    const start = Math.max(item.start, at);
    const end = Math.min(item.end, to);
    if (end <= start) continue;
    if (start > at) runs.push({ text: text.slice(at, start), bold, flag: null });
    runs.push({ text: text.slice(start, end), bold, flag: item.index });
    at = end;
  }
  if (at < to) runs.push({ text: text.slice(at, to), bold, flag: null });
  return runs;
}

/**
 * The answer as blocks of runs: paragraphs and list items, **bold** honoured, the marks themselves removed,
 * and every unmatched number (positions in `text`) cut out as its own run. Nothing else of the markdown
 * is interpreted, and no character of the answer is lost except the `**`, bullet and heading marks.
 */
export function renderAnswer(text: string, unmatched: readonly UnmatchedNumber[] = []): Block[] {
  const blocks: Block[] = [];
  let lineStart = 0;
  for (const line of text.split("\n")) {
    const offset = lineStart;
    lineStart += line.length + 1;
    if (line.trim() === "") continue;
    const bullet = BULLET.exec(line);
    const heading = bullet ? null : HEADING.exec(line);
    const lead = bullet ?? heading;
    let from = offset + (lead ? lead[0].length : 0);
    const end = offset + line.replace(/\s+$/, "").length;
    const runs: Run[] = [];
    let bold = heading !== null;
    for (;;) {
      const mark = text.indexOf("**", from);
      if (mark === -1 || mark >= end) break;
      if (mark > from) runs.push(...cut(text, from, mark, bold, unmatched));
      bold = !bold;
      from = mark + 2;
    }
    if (from < end) runs.push(...cut(text, from, end, bold, unmatched));
    if (runs.length > 0) blocks.push({ kind: bullet ? "li" : "p", runs });
  }
  return blocks;
}

// "Try your own rotation": the year slots the farmer fills, and the request they turn into.
// Pure helpers with no imports, so the unit tests can load this file directly.

/** The six crops a rotation can be made of, in the planner's order. */
export const OWN_CROPS: readonly string[] = ["corn", "soybeans", "winter wheat", "sorghum", "alfalfa", "fallow"];
export const MIN_YEARS = 2;
export const MAX_YEARS = 6;
/** How many empty years a new rotation starts with. */
export const START_YEARS = 4;

/** One year of the rotation: a crop, or null while the farmer has not picked one. */
export type Slot = string | null;

export const emptySlots = (years: number = START_YEARS): Slot[] =>
  Array.from({ length: Math.min(MAX_YEARS, Math.max(MIN_YEARS, Math.round(years) || START_YEARS)) }, () => null);

const isCrop = (crop: unknown): crop is string => typeof crop === "string" && OWN_CROPS.includes(crop);

/** A copy with year `index` set to `crop` (anything that is not one of the six clears the year). */
export function setSlot(slots: readonly Slot[], index: number, crop: string | null): Slot[] {
  return slots.map((slot, i) => (i === index ? (isCrop(crop) ? crop : null) : slot));
}

/** A copy with the year at `from` taken out and put back at `to`; the others keep their order. */
export function moveSlot(slots: readonly Slot[], from: number, to: number): Slot[] {
  const next = [...slots];
  if (!Number.isInteger(from) || !Number.isInteger(to) || from < 0 || from >= next.length) return next;
  const target = Math.min(next.length - 1, Math.max(0, to));
  if (target === from) return next;
  const [moved] = next.splice(from, 1);
  next.splice(target, 0, moved ?? null);
  return next;
}

export const canAdd = (slots: readonly Slot[]): boolean => slots.length < MAX_YEARS;
export const canRemove = (slots: readonly Slot[]): boolean => slots.length > MIN_YEARS;

/** A copy with one more empty year at the end (never more than six). */
export const addSlot = (slots: readonly Slot[]): Slot[] => (canAdd(slots) ? [...slots, null] : [...slots]);

/** A copy without year `index` (never fewer than two). */
export const removeSlot = (slots: readonly Slot[], index: number): Slot[] =>
  canRemove(slots) && index >= 0 && index < slots.length ? slots.filter((_, i) => i !== index) : [...slots];

/**
 * Slots for a rotation the API sent (a plan's cycle, the field's own pattern, what it understood
 * from typed text). Null when it is not 2 to 6 of the six crops, so nothing unscorable is loaded.
 */
export function slotsFrom(cycle: readonly unknown[] | null | undefined): Slot[] | null {
  if (!Array.isArray(cycle) || cycle.length < MIN_YEARS || cycle.length > MAX_YEARS) return null;
  return cycle.every(isCrop) ? [...(cycle as string[])] : null;
}

/** True when every year has a crop. */
export const isComplete = (slots: readonly Slot[]): boolean =>
  slots.length >= MIN_YEARS && slots.length <= MAX_YEARS && slots.every(isCrop);

/** "corn,sorghum,winter wheat,fallow", or null while a year is still empty. */
export const rotationParam = (slots: readonly Slot[]): string | null => (isComplete(slots) ? slots.join(",") : null);

/**
 * The query string for GET /field/{id}/score, without the "?": the rotation, then the same
 * settings the plan cards were asked with (`plansQuery`, see lib/planner.ts). Null while a year is
 * empty: there is nothing to ask yet. `rotation` may also be raw typed text.
 */
export function scoreQuery(plansQuery: string, rotation: readonly Slot[] | string): string | null {
  const text = typeof rotation === "string" ? rotation.trim() : rotationParam(rotation);
  if (!text) return null;
  return `rotation=${encodeURIComponent(text)}${plansQuery ? `&${plansQuery}` : ""}`;
}

export const sameSlots = (a: readonly Slot[] | null, b: readonly Slot[] | null): boolean =>
  a === b || (a !== null && b !== null && a.length === b.length && a.every((slot, i) => slot === b[i]));

/** Column headers of the side-by-side table: "Yours", "Our 1", "Our 2", ... */
export const columnLabel = (column: number): string => (column === 0 ? "Yours" : `Our ${column}`);

export type RowVerdict = "ours" | "yours" | "too_close" | "same";

/** The few words in front of a comparison sentence. */
export function verdictLabel(verdict: string): string {
  if (verdict === "ours") return "Ours is ahead";
  if (verdict === "yours") return "Yours is ahead";
  if (verdict === "too_close") return "Too close to call";
  return "The same";
}

/** "$17 to $54" for the low-high line under a return; null when the two ends are the same figure. */
export function rangeText(low: string, high: string): string | null {
  return low === high ? null : `${low} to ${high}`;
}

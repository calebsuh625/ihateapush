const ACRES_PER_HECTARE = 2.47105;

const count = new Intl.NumberFormat("en-US");
const oneDecimal = new Intl.NumberFormat("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });

/** 8412 → "8,412" */
export const formatCount = (n: number) => count.format(n);

/** 52.34 → "52.3" */
export const formatOneDecimal = (n: number) => oneDecimal.format(n);

/** 1.83 → "1.8%" */
export const formatPercent = (n: number) => `${oneDecimal.format(n)}%`;

/** 0.714 → "0.71", 0.575 → "0.58" (halves round up, despite binary floating point). */
export const formatScore = (n: number) => (Math.round((n + Number.EPSILON) * 100) / 100).toFixed(2);

export const hectaresToAcres = (ha: number) => ha * ACRES_PER_HECTARE;

/** Clamp a 0..1 score for drawing; anything unusable becomes 0. */
export const clamp01 = (n: number) => (Number.isFinite(n) ? Math.min(1, Math.max(0, n)) : 0);

/** "field" / "fields" */
export const plural = (n: number, one: string, many = `${one}s`) => (n === 1 ? one : many);

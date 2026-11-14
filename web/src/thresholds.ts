// The only place where a number is turned into a word. These are rules of thumb to help read the
// values at a glance, not agronomic advice — change the cut-offs here and nowhere else.

interface Band {
  /** Upper bound, exclusive. The last band has none. */
  below?: number;
  label: string;
}

function classify(value: number, bands: readonly Band[]): string {
  for (const band of bands) {
    if (band.below === undefined || value < band.below) return band.label;
  }
  return bands[bands.length - 1]?.label ?? "";
}

/** Soil organic matter, % by weight in the surface horizon (common extension-service classes). */
const ORGANIC_MATTER_BANDS: readonly Band[] = [
  { below: 1, label: "Very low" },
  { below: 2, label: "Low" },
  { below: 4, label: "Moderate" },
  { label: "High" },
];

/** Slope, percent rise (simplified USDA-NRCS slope classes). */
const SLOPE_BANDS: readonly Band[] = [
  { below: 2, label: "Nearly level" },
  { below: 6, label: "Gently sloping" },
  { below: 12, label: "Sloping" },
  { label: "Steep" },
];

export const organicMatterClass = (pct: number) => classify(pct, ORGANIC_MATTER_BANDS);
export const slopeClass = (pct: number) => classify(pct, SLOPE_BANDS);

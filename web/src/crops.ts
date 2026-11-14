// Crop colours and labels — the single source for the timeline, its legend and the plan chips.
//
// The seven named colours follow the usual associations (corn gold, soybeans green, wheat brown,
// sorghum rust, alfalfa purple, fallow pale beige, cover crop teal). They and the fallback colours
// were chosen together so that any two of the fifteen stay apart for red-green colour-blind
// readers too: worst pair ΔE 7.5 (OKLab ×100, simulated protanopia / deuteranopia), 12.9 with
// normal vision. Change one and re-check the rest.

const INK = "#1a1f1c";
const WHITE = "#ffffff";

export const CROP_COLORS: Readonly<Record<string, string>> = {
  corn: "#e0b81a",
  soybeans: "#2fa36b",
  "winter wheat": "#86501e",
  sorghum: "#e8702a",
  alfalfa: "#be72d2",
  fallow: "#e6dfd0",
  "cover crop": "#14727f",
};

/** For names without a fixed colour — any other Cropland Data Layer class ("cotton", "oats", …). */
const FALLBACK_COLORS: readonly string[] = [
  "#654abb", // violet
  "#36caf1", // sky
  "#8e0049", // wine
  "#798898", // slate
  "#65e287", // mint
  "#520277", // deep purple
  "#bda3a6", // mauve grey
  "#194224", // dark green
];

const normalise = (crop: string) => crop.trim().toLowerCase();

/** FNV-1a, 32-bit: small, and the same in every browser and session. */
function hash(text: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

const fallbackIndex = (key: string) => hash(key) % FALLBACK_COLORS.length;
const fallbackAt = (index: number) => FALLBACK_COLORS[index % FALLBACK_COLORS.length] ?? "#798898";

/** Colour for a crop by name alone: its fixed colour, or a fallback picked by hashing the name. */
export function cropColor(crop: string): string {
  const key = normalise(crop);
  return CROP_COLORS[key] ?? fallbackAt(fallbackIndex(key));
}

/** "double crop: winter wheat/sorghum" → ["winter wheat", "sorghum"]; anything else → null. */
export function doubleCropParts(crop: string): [string, string] | null {
  const match = /^double crop: (.+)\/(.+)$/.exec(normalise(crop));
  const first = match?.[1]?.trim();
  const second = match?.[2]?.trim();
  return first && second ? [first, second] : null;
}

/**
 * Colours for crops that are shown side by side (one field's history).
 *
 * Named crops keep their fixed colour. Every other crop starts from its hashed colour; if a crop
 * in the same set already has that colour it moves to the next free one, so two different crops
 * never look alike in one timeline (until all the fallback colours are in use). Names are handled
 * in alphabetical order, so the result does not depend on the order of the years.
 */
export function cropPalette(crops: Iterable<string>): Map<string, string> {
  const names = new Set<string>();
  for (const crop of crops) {
    for (const name of doubleCropParts(crop) ?? [normalise(crop)]) names.add(name);
  }
  const palette = new Map<string, string>();
  const taken = new Set<number>();
  for (const name of [...names].sort()) {
    const fixed = CROP_COLORS[name];
    if (fixed) {
      palette.set(name, fixed);
      continue;
    }
    let index = fallbackIndex(name);
    for (let tries = 0; tries < FALLBACK_COLORS.length && taken.has(index); tries++) {
      index = (index + 1) % FALLBACK_COLORS.length;
    }
    taken.add(index);
    palette.set(name, fallbackAt(index));
  }
  return palette;
}

/**
 * CSS `background` for a crop: its colour, or for a double crop a diagonal split of the two
 * crops' colours. Pass the `palette` of the view the swatch belongs to, when there is one.
 */
export function cropBackground(crop: string, palette?: ReadonlyMap<string, string>): string {
  const colorOf = (name: string) => palette?.get(normalise(name)) ?? cropColor(name);
  const parts = doubleCropParts(crop);
  if (!parts) return colorOf(crop);
  return `linear-gradient(135deg, ${colorOf(parts[0])} 50%, ${colorOf(parts[1])} 50%)`;
}

function luminance(hex: string): number {
  const channel = (i: number) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5);
}

const contrast = (a: number, b: number) => (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);

/** Dark ink or white, whichever reads better on `background` (a #rrggbb colour). */
export function textColorOn(background: string): string {
  const bg = luminance(background);
  return contrast(bg, luminance(INK)) >= contrast(bg, luminance(WHITE)) ? INK : WHITE;
}

/** "winter wheat" → "Winter Wheat", "grass/pasture" → "Grass/Pasture". */
export function cropLabel(crop: string): string {
  return crop.replace(/(^|[\s/(-])([a-z])/g, (_match, before: string, letter: string) => before + letter.toUpperCase());
}

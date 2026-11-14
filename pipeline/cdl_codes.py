"""USDA Cropland Data Layer (CDL) class codes -> the lowercase names used in crop histories.

`CDL_CLASSES` is the official legend, transcribed from and checked against the
"Attribute Domain Values and Definitions" table of the NASS FGDC metadata
"Cropland Data Layer - Kansas 2023" (metadata date 2024-01-31):
https://www.nass.usda.gov/research-and-science/cropland/metadata/metadata-ks23
(the same table is in the NASS CDL FAQ and in the 2008/2010/2013/2018 Kansas metadata,
which only lack code 228 and spell 176 "Grass/Pasture" in 2010). Every class value that
occurs in the 2008-2024 Finney County rasters is in this legend.

`crop_name(code)` gives the name written to `crop_history`:

* six core names are fixed by schema.md: 1 corn, 5 soybeans, 24 winter wheat,
  4 sorghum, 36 alfalfa, 61 fallow (CDL "Fallow/Idle Cropland");
* everything else is the lowercase CDL class name, lightly normalised
  ("Dbl Crop WinWht/Sorghum" -> "double crop: winter wheat/sorghum",
  "Other Hay/Non Alfalfa" -> "other hay", "Grassland/Pasture" -> "grass/pasture",
  all "Developed/..." -> "developed", "Open Water" -> "water", ...);
* a code that is not in the legend becomes "cdl class <code>".

`is_crop(code)` is True for the classes NASS groups under "CROPS" in that legend
(1-60, 66-77, 204-254) plus 61 Fallow/Idle Cropland, and False for everything else
(grass/pasture, forest, shrubland, barren, developed, water, wetlands, aquaculture,
clouds/no data, non-ag/undefined, background and unknown codes).
"""
from __future__ import annotations

import numpy as np

# --- the official legend ---------------------------------------------------------------
CDL_CLASSES: dict[int, str] = {
    0: "Background",
    # CROPS 1-60
    1: "Corn",
    2: "Cotton",
    3: "Rice",
    4: "Sorghum",
    5: "Soybeans",
    6: "Sunflower",
    10: "Peanuts",
    11: "Tobacco",
    12: "Sweet Corn",
    13: "Pop or Orn Corn",
    14: "Mint",
    21: "Barley",
    22: "Durum Wheat",
    23: "Spring Wheat",
    24: "Winter Wheat",
    25: "Other Small Grains",
    26: "Dbl Crop WinWht/Soybeans",
    27: "Rye",
    28: "Oats",
    29: "Millet",
    30: "Speltz",
    31: "Canola",
    32: "Flaxseed",
    33: "Safflower",
    34: "Rape Seed",
    35: "Mustard",
    36: "Alfalfa",
    37: "Other Hay/Non Alfalfa",
    38: "Camelina",
    39: "Buckwheat",
    41: "Sugarbeets",
    42: "Dry Beans",
    43: "Potatoes",
    44: "Other Crops",
    45: "Sugarcane",
    46: "Sweet Potatoes",
    47: "Misc Vegs & Fruits",
    48: "Watermelons",
    49: "Onions",
    50: "Cucumbers",
    51: "Chick Peas",
    52: "Lentils",
    53: "Peas",
    54: "Tomatoes",
    55: "Caneberries",
    56: "Hops",
    57: "Herbs",
    58: "Clover/Wildflowers",
    59: "Sod/Grass Seed",
    60: "Switchgrass",
    # NON-CROP 61-65
    61: "Fallow/Idle Cropland",
    63: "Forest",
    64: "Shrubland",
    65: "Barren",
    # CROPS 66-80
    66: "Cherries",
    67: "Peaches",
    68: "Apples",
    69: "Grapes",
    70: "Christmas Trees",
    71: "Other Tree Crops",
    72: "Citrus",
    74: "Pecans",
    75: "Almonds",
    76: "Walnuts",
    77: "Pears",
    # OTHER 81-109
    81: "Clouds/No Data",
    82: "Developed",
    83: "Water",
    87: "Wetlands",
    88: "Nonag/Undefined",
    92: "Aquaculture",
    # NLCD-DERIVED CLASSES 110-195
    111: "Open Water",
    112: "Perennial Ice/Snow",
    121: "Developed/Open Space",
    122: "Developed/Low Intensity",
    123: "Developed/Med Intensity",
    124: "Developed/High Intensity",
    131: "Barren",
    141: "Deciduous Forest",
    142: "Evergreen Forest",
    143: "Mixed Forest",
    152: "Shrubland",
    176: "Grassland/Pasture",
    190: "Woody Wetlands",
    195: "Herbaceous Wetlands",
    # CROPS 195-255
    204: "Pistachios",
    205: "Triticale",
    206: "Carrots",
    207: "Asparagus",
    208: "Garlic",
    209: "Cantaloupes",
    210: "Prunes",
    211: "Olives",
    212: "Oranges",
    213: "Honeydew Melons",
    214: "Broccoli",
    215: "Avocados",
    216: "Peppers",
    217: "Pomegranates",
    218: "Nectarines",
    219: "Greens",
    220: "Plums",
    221: "Strawberries",
    222: "Squash",
    223: "Apricots",
    224: "Vetch",
    225: "Dbl Crop WinWht/Corn",
    226: "Dbl Crop Oats/Corn",
    227: "Lettuce",
    228: "Dbl Crop Triticale/Corn",
    229: "Pumpkins",
    230: "Dbl Crop Lettuce/Durum Wht",
    231: "Dbl Crop Lettuce/Cantaloupe",
    232: "Dbl Crop Lettuce/Cotton",
    233: "Dbl Crop Lettuce/Barley",
    234: "Dbl Crop Durum Wht/Sorghum",
    235: "Dbl Crop Barley/Sorghum",
    236: "Dbl Crop WinWht/Sorghum",
    237: "Dbl Crop Barley/Corn",
    238: "Dbl Crop WinWht/Cotton",
    239: "Dbl Crop Soybeans/Cotton",
    240: "Dbl Crop Soybeans/Oats",
    241: "Dbl Crop Corn/Soybeans",
    242: "Blueberries",
    243: "Cabbage",
    244: "Cauliflower",
    245: "Celery",
    246: "Radishes",
    247: "Turnips",
    248: "Eggplants",
    249: "Gourds",
    250: "Cranberries",
    254: "Dbl Crop Barley/Soybeans",
}

# Codes that only occur in CDLs as originally released before January 2014. NASS has since
# recoded all three to 176 in every year, so they are not in the current legend; they are
# kept here so an old file still gets a sensible name.
LEGACY_CLASSES: dict[int, str] = {
    62: "Pasture/Grass",
    171: "Grassland Herbaceous",
    181: "Pasture/Hay",
}

# --- names used in crop_history ----------------------------------------------------------
CORE_CODES: dict[int, str] = {
    1: "corn",
    5: "soybeans",
    24: "winter wheat",
    4: "sorghum",
    36: "alfalfa",
    61: "fallow",
}

_OVERRIDES: dict[int, str] = {
    **CORE_CODES,
    13: "popcorn or ornamental corn",
    37: "other hay",
    47: "misc vegetables & fruits",
    62: "grass/pasture",
    171: "grass/pasture",
    176: "grass/pasture",
    181: "grass/pasture",
    82: "developed",
    121: "developed",
    122: "developed",
    123: "developed",
    124: "developed",
    83: "water",
    111: "water",
}

_ABBREVIATIONS = {"WinWht": "winter wheat", "Durum Wht": "durum wheat"}


def _normalise(official: str) -> str:
    name = official
    for short, full in _ABBREVIATIONS.items():
        name = name.replace(short, full)
    if name.startswith("Dbl Crop "):
        name = "double crop: " + name[len("Dbl Crop ") :]
    return " ".join(name.lower().split())


CDL_NAMES: dict[int, str] = {
    code: _OVERRIDES.get(code, _normalise(official)) for code, official in {**CDL_CLASSES, **LEGACY_CLASSES}.items()
}

# --- crop / non-crop ---------------------------------------------------------------------
FALLOW_CODE = 61
CROP_CODES: frozenset[int] = frozenset(
    code
    for code in CDL_CLASSES
    if 1 <= code <= 60 or 66 <= code <= 80 or 196 <= code <= 255 or code == FALLOW_CODE
)

# Lookup tables over all 256 byte values, for vectorised use on rasters / code arrays.
IS_CROP_LUT = np.zeros(256, dtype=bool)
IS_CROP_LUT[sorted(CROP_CODES)] = True
NAME_LUT: list[str] = [CDL_NAMES.get(code, f"cdl class {code}") for code in range(256)]

CROP_NAMES: frozenset[str] = frozenset(CDL_NAMES[code] for code in CROP_CODES)


def crop_name(code: int) -> str:
    """Lowercase history name for a CDL code ("cdl class <code>" if the code is not in the legend)."""
    code = int(code)
    if 0 <= code < 256:
        return NAME_LUT[code]
    return f"cdl class {code}"


def is_crop(code: int) -> bool:
    """True for cultivated crop classes and fallow/idle cropland; False for everything else."""
    code = int(code)
    return 0 <= code < 256 and bool(IS_CROP_LUT[code])


def is_crop_name(name: str) -> bool:
    """`is_crop` for a history name (as produced by `crop_name`)."""
    return name in CROP_NAMES

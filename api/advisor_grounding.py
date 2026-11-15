"""Phase 4, item 7: the number check behind the farm advisor.

Every number in the advisor's answer has to be one the tools gave it. `check(answer, sources)` pulls the
numbers out of the answer and looks each one up among the numbers of this request's tool results (and the
ones the farmer typed). What it cannot find is returned so the page can mark it.

What counts as a match:
  * the tool's figure rounded to the digits the answer shows ("$286" for 286.0, "21 inches" for 20.9,
    "+1.6" for 1.57); the sign is ignored ("29.6 fewer days" for -29.6);
  * a share written as a percentage ("90%" for 0.903), only when the answer says % or percent;
  * a figure rounded to a round number ("about $2,300" for 2288), within 5% and never looser than half of the
    last digit kept;
  * "k", "thousand" and "million" are multiplied out.
Care taken:
  * a year (a plain whole number from 1900 to 2100) must equal a number of the tools exactly, never by rounding;
  * the number of items in a list of a tool result counts as a number the tools gave ("our 3 picks");
  * field ids, well ids, climate-model and scenario names (KS-014376, ACCESS-CM2, SSP2-4.5) and the "1." of
    a numbered list are not numbers.
Not caught: numbers written as words ("two hundred"), a wrong sign, and a small whole number that happens
to appear somewhere in a tool result for another reason.
"""
from __future__ import annotations

import math
import re
from typing import Any, Iterable

YEAR_MIN, YEAR_MAX = 1900, 2100
ROUND_NUMBER_SHARE = 0.05         # "about 2,300" may stand for a figure within 5% of it ...
SCALES = {"k": 1e3, "thousand": 1e3, "million": 1e6}

# A number: not glued to a letter, digit, dot or comma on its left; optional $; thousands commas; decimals;
# then an optional %, percent, k, thousand or million.
NUMBER = re.compile(
    r"(?<![A-Za-z0-9_.,])(?P<dollar>\$\s?)?(?P<int>\d{1,3}(?:,\d{3})+|\d+)(?P<frac>\.\d+)?"
    r"(?P<suffix>\s?%|\s?percent\b|k\b|\s?thousand\b|\s?million\b)?",
    re.IGNORECASE,
)
# Things that hold digits but are names, blanked out before the numbers are read (same length, so the
# positions still point into the answer).
NOT_NUMBERS = (
    re.compile(r"\b[A-Z][A-Z0-9]+(?:-[A-Za-z0-9]+)+\b"),                         # KS-014376, ACCESS-CM2, USGS-3747...
    re.compile(r"\b(?:SSP|RCP)\s?\d(?:[-.]\d(?:\.\d)?)?\b", re.IGNORECASE),      # SSP2-4.5, ssp245, RCP 8.5
    re.compile(r"(?m)^[ \t>*_#-]*\d{1,2}[.)](?=\s)"),                            # "1." of a numbered list
)


def _blank(text: str) -> str:
    for pattern in NOT_NUMBERS:
        text = pattern.sub(lambda m: " " * len(m.group(0)), text)
    return text


def numbers_in(text: str) -> list[dict[str, Any]]:
    """Every number written in `text`: {text, value, decimals, zeros, percent, year, start, end}
    (start and end are Python string positions)."""
    found = []
    for m in NUMBER.finditer(_blank(text)):
        whole = m.group("int").replace(",", "")
        frac = m.group("frac") or ""
        suffix = (m.group("suffix") or "").strip().lower()
        scale = SCALES.get(suffix, 1.0)
        value = float(whole + frac) * scale
        decimals = len(frac) - 1 if frac else 0
        zeros = len(whole) - len(whole.rstrip("0")) if not frac and whole.strip("0") else 0
        plain = not m.group("dollar") and not suffix and not frac and "," not in m.group("int")
        end = m.end()
        found.append({
            "text": text[m.start():end].strip(),
            "value": value,
            "unit": scale * 10.0 ** -decimals,                # the size of the last digit written
            "zeros": zeros,
            "percent": suffix in ("%", "percent"),
            "year": plain and YEAR_MIN <= value <= YEAR_MAX,
            "start": m.start(),
            "end": m.start() + len(text[m.start():end].rstrip()),
        })
    return found


def collect(source: Any, into: set[float] | None = None) -> set[float]:
    """The numbers of a tool result (or of any JSON-like value): its numbers, the numbers written in its
    strings and keys, and the length of each of its lists. Absolute values."""
    numbers = set() if into is None else into
    if isinstance(source, bool) or source is None:
        return numbers
    if isinstance(source, (int, float)):
        if math.isfinite(source):
            numbers.add(abs(float(source)))
    elif isinstance(source, str):
        for item in numbers_in(source):
            numbers.add(item["value"])
    elif isinstance(source, dict):
        for key, value in source.items():
            collect(str(key), numbers)
            collect(value, numbers)
    elif isinstance(source, (list, tuple)):
        if source:
            numbers.add(float(len(source)))
        for value in source:
            collect(value, numbers)
    return numbers


def _matches(item: dict[str, Any], known: Iterable[float]) -> bool:
    x = item["value"]
    if item["year"]:
        return any(abs(t - x) < 1e-9 for t in known)
    tolerance = item["unit"] / 2
    if item["zeros"]:                                          # 2,300: perhaps rounded to the hundreds
        place = item["unit"] * 10.0 ** item["zeros"]
        tolerance = max(tolerance, min(place / 2, ROUND_NUMBER_SHARE * x))
    slack = tolerance + 1e-9 * max(1.0, x)
    for t in known:
        if abs(t - x) <= slack:
            return True
        if item["percent"] and 0 < t <= 1 and abs(t * 100 - x) <= slack:
            return True
    return False


def _utf16(text: str, position: int) -> int:
    """A Python string position as a JavaScript one (UTF-16 units), so the page can slice the answer."""
    return len(text[:position].encode("utf-16-le")) // 2


def check(answer: str, sources: Iterable[Any]) -> dict[str, Any]:
    """Look every number of `answer` up in `sources` (tool results, tool arguments, what the farmer typed).

    -> {"checked": numbers found in the answer, "matched": how many are in the sources,
        "unmatched": [{"text", "value", "start", "end"}], "passed": no number is unmatched}
    `start` and `end` are positions in the answer as JavaScript counts them: answer.slice(start, end) == text.
    """
    known: set[float] = set()
    for source in sources:
        collect(source, known)
    items = numbers_in(answer)
    unmatched = [
        {"text": item["text"], "value": item["value"], "start": _utf16(answer, item["start"]),
         "end": _utf16(answer, item["end"])}
        for item in items if not _matches(item, known)
    ]
    return {"checked": len(items), "matched": len(items) - len(unmatched), "unmatched": unmatched,
            "passed": not unmatched}

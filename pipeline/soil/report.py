"""Text output of pipeline/soil/build.py: the short terminal summary and the markdown report.

Both are rendered from the meta dict that build.py writes to data/soil_health_meta.json, so the
report never shows a number that is not in the meta file. No computation happens here.
"""
from __future__ import annotations

import math

from pipeline.soil import model, noise
from pipeline.soil.features import SENSOR_LABEL, SENSORS

SCHEME_NAME = {
    "map_unit": "grouped by map unit, tuned inside folds",
    "map_unit_fixed": "grouped by map unit, fixed size",
    "series": "grouped by soil series (stricter), fixed size",
    "block": f"grouped by {model.BLOCK_KM:g} km blocks",
    "field": "grouped by field",
    "random": "random K-fold (leaks)",
}
MODEL_NAME = {"hgb": "gradient boosting", "ridge": "ridge on bands", "mean": "training mean"}
METRIC_HEADER = [
    "validation / model", "rows", "R2", "RMSE", "MAE", "rank r", "calib. slope", "sensitivity",
    "fields", "R2 (field)", "RMSE (field)", "rank r (field)", "calib. slope (field)", "sensitivity (field)",
]  # fmt: skip


# --------------------------------------------------------------------------- formatting


def f(value, digits: int = 2, signed: bool = False) -> str:
    """A number for people: fixed decimals, "n/a" for None / NaN."""
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "n/a"
    return f"{value:+.{digits}f}" if signed else f"{value:.{digits}f}"


def pct(value, digits: int = 0) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "n/a"
    return f"{100 * value:.{digits}f} %"


def span(pair, digits: int = 2) -> str:
    """"lo to hi" of a two-number interval, or "n/a"."""
    return f"{f(pair[0], digits)} to {f(pair[1], digits)}" if pair else "n/a"


def open_span(pair, digits: int = 2) -> str:
    """A range whose upper end may be None (no upper limit): "1.7 to 10.5", "1.7 to no limit"."""
    if not pair:
        return "n/a"
    return f"{f(pair[0], digits)} to {f(pair[1], digits) if pair[1] is not None else 'no limit'}"


def yes_no(value) -> str:
    return "not tested" if value is None else "yes" if value else "NO"


def by_year(mapping: dict, fmt) -> str:
    return ", ".join(f"{year} {fmt(value)}" for year, value in mapping.items())


def md_table(header: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def _when(shift: dict) -> str:
    return "this" if shift["when"] == "this_year" else "last"


# --------------------------------------------------------------------------- terminal summary


def summary_lines(meta: dict, out_path, meta_path, report_path, seconds: float) -> list[str]:
    """The lines build.py prints: about 15, one fact each."""
    m, n, c, b, inp = meta["model"], meta["noise"], meta["confidence"], meta["bare_days"], meta["inputs"]
    head, lab, det, v = m["headline"], m["labels"], n["detectability"], n["verdict"]
    grouped = m["combined"].get("map_unit", {})
    random_row = m["combined"].get("random", {}).get("hgb", {}).get("row", {})
    series_row = m["combined"].get("series", {}).get("hgb", {}).get("row", {})
    mean_row = grouped.get("mean", {}).get("row", {})
    ridge_row = grouped.get("ridge", {}).get("row", {})
    rows = {row["n_fields"]: row for row in det.get("by_n_fields", [])}
    mdc = {k: row.get("mdc_real_om") for k, row in rows.items()}
    rep, art, checks, passes = n.get("repeatability", {}), n.get("artefacts", {}), b["checks"], b.get("passes", {})
    sens, crops = checks.get("sensors", {}), checks.get("by_crop", {})
    stable = checks.get("stable_crop") or {}
    best = art.get("largest_clear_shift")
    raw = n["jumps"].get("combined", {}).get("raw", {})
    within = n.get("within_type") or {}
    chk = c["check"]
    lo, hi = n["hoped_per_decade"]
    if best:
        artefact = (
            f"om_pred shifts by {f(best['shift'], 3, True)} (SE {f(best['se'], 3)}) when {best['crop']} is {_when(best)} year's crop"
            f" instead of {art.get('reference_this_year')} = {f(art.get('clear_shift_as_real_om'))} points of real OM"
            f" ({open_span(art.get('clear_shift_as_real_om_range'))}), {f(art.get('clear_shift_over_decade_signal'), 1)} x a decade of signal"
        )
    else:
        artefact = f"no crop shift beyond {noise.CLEAR_T:g} SE; spread over all crops {f(art.get('largest_range'), 3)}"
    jumps = ", ".join(f"{crop} {f(v['robust_sd_of_jump'], 0)}" for crop, v in list(stable.get("by_crop", {}).items())[:5])
    lines = [
        f"soil health: {inp['n_fields']:,} fields x {inp['years'][0]}-{inp['years'][1]}; om_pred in {pct(m['coverage'])} of field-years,"
        f" bare_days in {pct(b['coverage'])}  ({seconds:.0f}s)",
        f"labels: {lab['n_map_units']} map units ({lab.get('n_series', 0)} soil series), {lab['n_label_values']} distinct values, largest map unit"
        f" {pct(lab['largest_map_unit_share'])} of fields, {pct(lab['label_variance_between_map_units'])} of label variance is between map units",
        f"accuracy vs SSURGO, grouped by map unit: R2 {f(head['row']['r2'])} [90 % {span(head['row'].get('r2_interval90'))}],"
        f" RMSE {f(head['row']['rmse'], 3)}, rank r {f(head['row']['spearman'])}; grouped by soil series (stricter): R2 {f(series_row.get('r2'))}",
        f"  same model, random K-fold (leaks): R2 {f(random_row.get('r2'))}; baselines grouped: mean RMSE {f(mean_row.get('rmse'), 3)},"
        f" ridge R2 {f(ridge_row.get('r2'))} RMSE {f(ridge_row.get('rmse'), 3)}",
        f"  sensitivity {f(head['field']['sensitivity'])} [90 % {span(det.get('sensitivity_interval90'))}] between soils;"
        f" within a soil type {f(within.get('sensitivity'))} [{span(within.get('sensitivity_interval90'))}], rank r {f(within.get('spearman'))}",
        f"noise: year-to-year jump on unchanged-crop fields {f(n['om_noise_sd'], 3)} robust, {f(raw.get('sd'), 3)} plain"
        f" (n = {raw.get('n', 0)} pairs; crop-balanced {f(raw_balanced(n), 3)}, all pairs {f(raw.get('all_pairs', {}).get('robust_sd'), 3)})",
        f"  Landsat vs Sentinel-2 same field-year: r {f(rep.get('r'))}, robust SD of difference {f(rep.get('robust_sd_of_difference'), 3)}",
        f"signal hoped for: {lo:g}-{hi:g} points per decade; minimum detectable real change per decade: 1 field {f(mdc.get(1))}"
        f" ({open_span(rows.get(1, {}).get('mdc_real_om_range'))}), 10 {f(mdc.get(10))}, 100 {f(mdc.get(100))}"
        f" ({open_span(rows.get(100, {}).get('mdc_real_om_range'))}), 1000 {f(mdc.get(1000))};"
        f" 1 field in om_pred units {f(det.get('mdc_one_field_om_pred_units'))}",
        f"crop artefact: {artefact}",
        f"confidence (repeatability, not accuracy): median {f(c['quantiles'].get('p50'))}, max {f(c['quantiles'].get('max'))};"
        f" rank r with |error| {f(chk.get('spearman_confidence_vs_abs_error'))} (constant guess {f(chk.get('spearman_confidence_vs_abs_error_constant_guess'))},"
        f" sands {f(chk.get('spearman_confidence_vs_abs_error_lower_heap'))}, within a field {f(chk.get('spearman_confidence_vs_abs_error_within_field'))}),"
        f" with |jump| {f(chk.get('spearman_confidence_vs_abs_jump'))}",
        f"bare_days: Landsat vs Sentinel-2 r {f(sens.get('r'), 3)}, noise of one sensor {f(sens.get('noise_sd_one_sensor'), 1)} days;"
        f" fallow median {f(crops.get('fallow', {}).get('median'), 0)}, alfalfa {f(crops.get('alfalfa', {}).get('median'), 0)};"
        f" r with green cover {f(checks.get('cover', {}).get('r'))}",
        f"  unchanged-crop jump {f(stable.get('robust_sd_of_jump'), 1)} days robust, {f(stable.get('sd_of_jump'), 1)} plain; by crop: {jumps or 'n/a'};"
        f" fallow vs cropped = {f((checks.get('fallow_vs_cropped') or {}).get('gap_in_noise_sds'), 1)} noise SDs; own checks pass: {yes_no(passes.get('all_pass'))}",
        f"verdict: single field {v['single_field']}; 100 fields {v['hundred_fields']}; 1000 fields {v['thousand_fields']};"
        f" follows OM within a soil type: {yes_no(v.get('follows_organic_matter_within_soil_type'))};"
        f" crop artefact rivals the signal: {v['artefact_rivals_signal']}; recommended: {meta['recommended']}",
        f"  {meta['verdict']}",
    ]
    lines += [f"WARNING: {w}" for w in meta["warnings"]]
    lines.append(f"wrote {out_path}, {meta_path}, {report_path}")
    return lines


def raw_balanced(n: dict):
    return n["jumps"].get("combined", {}).get("crop_balanced_robust_sd")


# --------------------------------------------------------------------------- markdown report


def _metric_row(name: str, block: dict) -> list:
    row, fld = block.get("row", {}), block.get("field", {})
    return [
        name, row.get("n", 0), f(row.get("r2")), f(row.get("rmse"), 3), f(row.get("mae"), 3), f(row.get("spearman")),
        f(row.get("calibration_slope")), f(row.get("sensitivity")),
        fld.get("n", 0), f(fld.get("r2")), f(fld.get("rmse"), 3), f(fld.get("spearman")),
        f(fld.get("calibration_slope")), f(fld.get("sensitivity")),
    ]  # fmt: skip


def _verdict(meta: dict, out_path) -> list[str]:
    inp, v = meta["inputs"], meta["noise"]["verdict"]
    within, det = meta["noise"].get("within_type") or {}, meta["noise"]["detectability"]
    out = [
        "# Soil health table: model, noise and verdict\n",
        f"Generated {meta['generated_utc']} by `python -m pipeline.soil.build` for `{out_path}`. {inp['n_fields']:,} fields"
        f" (of {inp['n_fields_in_fields_file']:,} in the fields file), {inp['years'][0]}-{inp['years'][1]}.\n",
        "## Verdict\n",
        f"**{meta['verdict']}**\n",
        f"- Single field: **{v['single_field']}**. Ten fields: {v['ten_fields']}. A hundred: {v['hundred_fields']}."
        f" A thousand: {v['thousand_fields']}.",
        f"- Single field buried even for a model that showed every real change in full: **{yes_no(v.get('single_field_buried_at_any_sensitivity'))}**.",
        f"- om_pred follows organic matter within a soil type (needed to show change in a field): **{yes_no(v.get('follows_organic_matter_within_soil_type'))}**"
        f" (sensitivity there {f(within.get('sensitivity'))}, 90 % interval {span(within.get('sensitivity_interval90'))};"
        f" between soils {f(det.get('sensitivity'))}, {span(det.get('sensitivity_interval90'))}).",
        f"- Crop artefact as large as the hoped signal: **{'yes' if v['artefact_rivals_signal'] else 'no'}**.",
        f"- bare_days passes its own checks: **{yes_no(v.get('bare_days_checks_pass'))}**"
        + (f" (failed: {', '.join(v['bare_days_failed_checks'])})." if v.get("bare_days_failed_checks") else "."),
        f"- Column to use: **{meta['recommended']}**"
        + (
            "."
            if v.get("bare_days_checks_pass") is not False or meta["recommended"] != "bare_days"
            else " only because the contract has no third value: **neither column is trustworthy in this build**."
        ),
        f"- {meta['notes']['bare_days']}",
        f"- {meta['notes']['confidence']}",
    ]
    out += [f"- Warning: {w}" for w in meta["warnings"]]
    return [*out, ""]


def _inputs(meta: dict) -> list[str]:
    inp, m = meta["inputs"], meta["model"]
    rows = [
        [SENSOR_LABEL[s], inp["composites"].get(s, 0), inp["usable_composites"].get(s, 0), inp["composites_dropped_as_snow"].get(s, 0)]
        for s in SENSORS
    ]
    return [
        "## 1. Inputs\n",
        md_table(["sensor family", "composites", "usable", "dropped as mostly snow / bright cloud"], rows),
        "",
        f"om_pred exists for {pct(m['coverage'], 1)} of field-years; {m['fields_never_predicted']} field(s) never get one."
        f" By year: {by_year(m['coverage_by_year'], pct)}.\n",
    ]


def _labels(meta: dict) -> list[str]:
    m = meta["model"]
    lab = m["labels"]
    largest = ", ".join(f"{key} ({count})" for key, count in lab["largest_map_units"])
    return [
        "## 2. What is predicted\n",
        f"{m['what_accuracy_means']}\n",
        f"- {lab['n_fields']} labelled fields on **{lab['n_map_units']} dominant map units**"
        f" ({f(lab['effective_map_units'], 1)} effective, counting sizes); **{lab['n_label_values']} distinct label values**.",
        f"- Lopsided: the largest map unit holds {pct(lab['largest_map_unit_share'])} of the fields;"
        f" {lab['map_units_with_under_5_fields']} map units have fewer than 5 fields, {lab['map_units_with_one_field']} have one."
        f" Largest: {largest}.",
        f"- Label mean {f(lab['label_mean'])} %, SD {f(lab['label_sd'])}, range {f(lab['label_min'])}-{f(lab['label_max'])}."
        f" {pct(lab['label_variance_between_map_units'])} of its variance lies between dominant map units, so a field's map unit"
        f" nearly is its label. Median share of a field on its dominant map unit: {pct(lab['median_dominant_share'])}.\n",
    ]


def _accuracy(meta: dict) -> list[str]:
    m = meta["model"]
    row, fld = m["headline"]["row"], m["headline"]["field"]
    out = [
        "## 3. Accuracy against SSURGO\n",
        "Headline (om_pred of the table: both families combined, each field predicted by models that never saw its map unit):"
        f" **R2 {f(row['r2'])}** per field-year (90 % interval from resampling map units: {span(row.get('r2_interval90'))}),"
        f" RMSE {f(row['rmse'], 3)}, MAE {f(row['mae'], 3)}, rank correlation {f(row['spearman'])},"
        f" calibration slope {f(row['calibration_slope'])}, sensitivity {f(row['sensitivity'])}."
        f" Per field (years averaged): R2 {f(fld['r2'])} ({span(fld.get('r2_interval90'))}), RMSE {f(fld['rmse'], 3)},"
        f" rank correlation {f(fld['spearman'])}.\n",
    ]
    series = m["combined"].get("series", {})
    if m.get("honest_r2_range"):
        hgb, base = series.get("hgb", {}).get("row", {}), series.get("mean", {}).get("row", {})
        fixed = m["combined"].get("map_unit_fixed", {}).get("hgb", {}).get("row", {})
        no_better = hgb.get("rmse") is not None and base.get("rmse") is not None and hgb["rmse"] >= 0.98 * base["rmse"]
        out.append(
            f"**Honest accuracy: R2 {span(m['honest_r2_range'])}, depending on what 'a soil it has not seen' means.** Map units of one"
            " soil series (the same soil on another slope, or in a complex) carry nearly the same label and spectra, and under the"
            f" map-unit split they sit on both sides. Grouped by soil series instead ({m['labels'].get('n_series', 0)} series; the series"
            f" is the map unit's largest component) the same model scores R2 {f(hgb.get('r2'))}, RMSE {f(hgb.get('rmse'), 3)}, rank"
            f" correlation {f(hgb.get('spearman'))}, against R2 {f(fixed.get('r2'))} for its like-for-like map-unit row and"
            f" R2 {f(base.get('r2'))}, RMSE {f(base.get('rmse'), 3)} for the training mean under the same series split."
            + (
                " **For a soil series it has never seen the model is no better than guessing the mean**: what skill it shows under"
                " the map-unit split comes from having seen a sister map unit of the same series (for example one Valent sand when"
                " predicting the other)."
                if no_better
                else " So part of the map-unit figure comes from having seen a sister map unit of the same series."
            )
            + " The table itself stays out-of-fold by map unit, as specified.\n"
        )
    for key, title in [("combined", "Both families combined"), *[(s, SENSOR_LABEL[s]) for s in m["families"]]]:
        block = m["combined"] if key == "combined" else m["families"][key]["schemes"]
        rows = [
            _metric_row(f"{SCHEME_NAME.get(scheme, scheme)} / {MODEL_NAME[name]}", by_model[name])
            for scheme, by_model in block.items()
            for name in model.MODELS
            if name in by_model and (name == "hgb" or scheme in ("map_unit", "random"))
        ]
        out += [f"### {title}\n", md_table(METRIC_HEADER, rows)]
        if key != "combined":
            fam = m["families"][key]
            chosen = ", ".join(f"{k} x {count}" for k, count in fam["configs_chosen_in_headline_folds"].items()) or "n/a"
            out.append(
                f"\n{fam['n_composites']:,} usable composites of {fam['n_fields']:,} fields."
                f" Model size chosen in the headline folds: {chosen}."
            )
            fm = fam.get("field_median_check", {})
            if "hgb" in fm:
                bands = ", ".join(f"{band} {f(r, 2, True)}" for band, r in fm["band_correlation_with_label"].items())
                out.append(
                    f"Would averaging the years first help? One multi-year median spectrum per field ({fm['n_fields']} fields),"
                    f" same map-unit folds: gradient boosting R2 {f(fm['hgb']['r2'])}, rank r {f(fm['hgb']['spearman'])},"
                    f" sensitivity {f(fm['hgb']['sensitivity'])}; ridge R2 {f(fm['ridge']['r2'])}."
                    f" Correlation of each band with the label: {bands}."
                )
        out.append("")
    sl, pf = m["sand_loam"], m["pure_fields"]
    low, high = sl["below"], sl["above"]
    out += [
        "calib. slope = slope of label on prediction (1 = calibrated). sensitivity = slope of prediction on label:"
        " how much of a real difference the prediction shows.\n",
        f"**Is it more than sand against loam?** Fields below {sl['split_at']:g} % (n = {low['n_fields']},"
        f" mean label {f(low['mean_label'])}, mean prediction {f(low['mean_pred'])}): rank correlation {f(low['spearman'])}."
        f" Fields above (n = {high['n_fields']}, mean label {f(high['mean_label'])}, mean prediction {f(high['mean_pred'])}):"
        f" rank correlation {f(high['spearman'])}, sensitivity {f(high['sensitivity'])}."
        f" Both heaps pooled after removing each heap's means: sensitivity **{f(sl['within_type'].get('sensitivity'))}**"
        f" (90 % interval from resampling map units: {span(sl['within_type'].get('sensitivity_interval90'))}),"
        f" rank correlation {f(sl['within_type'].get('spearman'))}. A field does not change its soil type, so this, not the"
        f" between-soil sensitivity of {f(fld['sensitivity'])} ({span(fld.get('sensitivity_interval90'))}), is what change in a field would have to show through.\n",
        f"**Fields that are at least {pct(pf['min_dominant_share'])} one map unit** (no minor map unit can leak):"
        f" row R2 {f(pf['row']['r2'])}, RMSE {f(pf['row']['rmse'], 3)} (n = {pf['row']['n']});"
        f" field R2 {f(pf['field']['r2'])} (n = {pf['field']['n']}).\n",
    ]
    return out


def _sensors(meta: dict) -> list[str]:
    n, c = meta["noise"], meta["confidence"]
    rep = n.get("repeatability", {})
    out = ["## 4. The two sensor families\n"]
    if rep.get("n_pairs", 0) >= 10:
        out.append(
            f"Same field, same year, Landsat-based against Sentinel-2-based om_pred ({rep['n_pairs']:,} pairs):"
            f" correlation {f(rep['r'])}, mean difference {f(rep['bias_s2_minus_landsat'], 3, True)},"
            f" robust SD of the difference **{f(rep['robust_sd_of_difference'], 3)}**"
            f" (so one family's number carries about {f(rep['implied_noise_sd_one_family'], 3)} of noise)."
            f" Field means over the years correlate {f(rep['r_of_field_means'])};"
            f" yearly deviations from the field mean correlate {f(rep['r_of_yearly_deviations'])}.\n"
        )
    else:
        out.append("Fewer than 10 field-years have both families.\n")
    rows = [
        [
            SENSOR_LABEL[s], f(nm["a"], 4), f(nm["b"], 4), f(nm["c"], 4),
            f(math.sqrt(nm["a"] + nm["b"]), 3), f(math.sqrt(nm["a"] + nm["b"] / 2), 3), f(math.sqrt(nm["a"] + nm["b"] / 4), 3), nm["n_rows"],
        ]
        for s, nm in c.get("noise_model", {}).items()
    ]  # fmt: skip
    out += [
        md_table(["family", "a (floor)", "b (few dates)", "c (part of field)", "noise SD at 1 view", "at 4 views", "at 16 views", "rows"], rows),
        "\nNoise variance of one family's prediction: v = a + b / sqrt(views per bare pixel) + c x (1 - share of field seen bare)."
        f" Shared between the families in a field-year: rho = {f(c.get('rho'))}.\n",
    ]
    step = n.get("sensor_step")
    if step and step.get("n", 0) >= 10:
        first = step["first_s2_year"]
        out.append(
            f"**Step when Sentinel-2 joins ({first}).** From then on om_pred differs from the Landsat-only value by"
            f" {f(step['mean_shift'], 3, True)} on average (county level), and by {f(step['field_shift_sd'], 3)} (SD) for an"
            f" individual field, which is how far a field's {first - 1} point sits from the level of its later points."
            f" Change {first - 1} -> {first} on the {step['n_same_fields']} fields with a Landsat value in both years:"
            f" om_pred {f(step['combined_change_same_fields'], 3, True)}, Landsat alone {f(step['landsat_change_same_fields'], 3, True)}."
            " Mean of whatever exists, by year (also moves when Sentinel-2 adds fields Landsat never saw bare):"
            f" {by_year(step['mean_of_available_by_year'], lambda value: f(value, 3))}.\n"
        )
    return out


def _confidence(meta: dict) -> list[str]:
    c = meta["confidence"]
    chk = c["check"]
    tau_sd = math.sqrt(c["tau2"]) if c.get("tau2") else None
    quantiles = ", ".join(f"{key} {f(value)}" for key, value in c["quantiles"].items())
    out = [
        "## 5. confidence: repeatability, not accuracy\n",
        f"**{c['meaning']}**\n",
        "`confidence = skill x tau2 / (tau2 + v x g)` (formula and reasons: `pipeline/soil/confidence.py`)."
        f" skill = {f(c['skill'], 3)} (squared correlation of field means with SSURGO, r = {f(c['r_field_means_vs_ssurgo'])});"
        f" tau2 = {f(c['tau2'], 4)} (SD between fields of the stable part {f(tau_sd, 3)}); kappa = {f(c.get('kappa'))}."
        f" Quantiles: {quantiles}.\n",
    ]
    if "bins" not in chk:
        return out
    split = f"{chk['upper_heap_from']:g}"
    header = [
        "confidence", "rows", "median abs error vs SSURGO", "same, guessing the county mean", f"share of rows on soils >= {split} %",
        f"median abs error, soils >= {split} %", f"median abs error, soils < {split} %", "stable-crop pairs", "year-to-year jump (robust SD)",
    ]  # fmt: skip
    rows = [
        [
            f"{f(r['from'])}-{f(r['to'])}", r["n"], f(r["median_abs_error"], 3), f(r.get("median_abs_error_constant_guess"), 3),
            pct(r["share_upper_heap"]), f(r["median_abs_error_upper_heap"], 3), f(r.get("median_abs_error_lower_heap"), 3),
            r["n_stable_pairs"], f(r["jump_robust_sd"], 3),
        ]
        for r in chk["bins"]
    ]  # fmt: skip
    out += [
        md_table(header, rows),
        "\nRank correlation of confidence with (negative = higher confidence, smaller error):\n",
        md_table(
            ["what", "rank correlation", "reading"],
            [
                ["absolute error, all rows", f(chk["spearman_confidence_vs_abs_error"]), "looks useful"],
                [
                    "error of a constant guess (the county mean for every row)", f(chk.get("spearman_confidence_vs_abs_error_constant_guess")),
                    "this much of the line above is soil mix: fields seen bare often lie on loam near the county mean",
                ],
                [f"absolute error, soils >= {split} % only", f(chk["spearman_confidence_vs_abs_error_upper_heap"]), "what is left on loams"],
                [
                    f"absolute error, soils < {split} % only ({chk.get('n_rows_lower_heap', 0)} rows)", f(chk.get("spearman_confidence_vs_abs_error_lower_heap")),
                    "positive = on sands a confident row is further from SSURGO",
                ],
                [
                    f"absolute error between the years of one field ({chk.get('n_rows_within_field', 0)} rows)",
                    f(chk.get("spearman_confidence_vs_abs_error_within_field")), "near zero = it cannot rank a field's years",
                ],
                ["absolute year-to-year jump, unchanged-crop pairs", f(chk["spearman_confidence_vs_abs_jump"]), "what it does track: repeatability"],
                ["n_obs", f(chk.get("spearman_confidence_vs_n_obs")), "confidence is mostly how often the field was seen bare"],
            ],
        ),
        "\nA pair of years is binned by the lower of its two confidences. Use confidence to drop rows that will not repeat"
        " (few bare views, part of the field, sensors disagreeing). Do not use it to rank the years of one field or to pick"
        " the 'accurate' rows.\n",
    ]
    return out


def _noise(meta: dict) -> list[str]:
    n = meta["noise"]
    rows = []
    for key, title in (("combined", "combined om_pred"), ("landsat", "Landsat only"), ("s2", "Sentinel-2 only")):
        j = n["jumps"].get(key)
        if not j:
            continue
        mono = n["single_crop"].get(key, {})
        rows.append(
            [
                title, j["raw"]["n"], f(j["raw"]["robust_sd"], 3), f(j["raw"]["sd"], 3), f(j["year_effect_removed"]["robust_sd"], 3),
                f(j["raw"]["all_pairs"]["robust_sd"], 3), f(j.get("year_effect_sd"), 3), mono.get("n_fields", 0), f(mono.get("equivalent_jump_sd"), 3),
            ]
        )  # fmt: skip
    header = [
        "series", "same-crop pairs", "jump robust SD", "jump SD", "robust SD, year effect removed", "all pairs (any crop)",
        "SD of county year effect", "single-crop fields", "their jump SD",
    ]  # fmt: skip
    out = [
        "## 6. Noise: year-to-year jumps where the crop did not change\n",
        md_table(header, rows),
        f"\nom_noise_sd in the meta file = **{f(n['om_noise_sd'], 3)}** (combined, raw, robust)."
        f" One value therefore carries about {f(n['noise_sd_of_one_value'], 3)} of noise.\n",
    ]
    combined = n["jumps"].get("combined", {})
    by_crop = combined.get("by_crop")
    if by_crop:
        mix = combined.get("crop_mix", {})
        top = ", ".join(f"{crop} {pct(share)}" for crop, share in list(mix.items())[:4])
        out += [
            md_table(
                ["crop (both years)", "pairs", "jump robust SD", "jump SD"],
                [[crop, s["n"], f(s["robust_sd"], 3), f(s["sd"], 3)] for crop, s in by_crop.items()],
            ),
            f"\nThe unchanged-crop pairs are not a fair sample of fields: {top}. Giving each crop in the table the same weight the jump is"
            f" {f(combined.get('crop_balanced_robust_sd'), 3)}; over all consecutive pairs, whatever the crop,"
            f" {f(combined.get('raw', {}).get('all_pairs', {}).get('robust_sd'), 3)}. A typical field sits nearer those than the headline"
            " figure; the verdict is the same with any of the three.\n",
        ]
    rows = []
    for key, title in (("combined", "combined"), ("landsat", "Landsat"), ("s2", "Sentinel-2")):
        for r in n["by_n_obs"].get(key, []):
            views = f"{r['n_obs_from']:g}-{r['n_obs_to']:g}" if r["n_obs_to"] is not None else f"{r['n_obs_from']:g}+"
            rows.append([title, views, r["n"], f(r["robust_sd"], 3)])
    out += [
        md_table(["series", "bare views per pixel (lower of the two years)", "pairs", "jump robust SD"], rows),
        "\nUnchanged-crop pairs are a particular mix of fields (much continuous corn), so this table mixes the number of views"
        " with the crop. The noise-model table in section 4 measures the same dependence on every field: scatter around the"
        " field's own mean against views.\n",
    ]
    return out


def _signal(meta: dict) -> list[str]:
    n = meta["noise"]
    det = n["detectability"]
    lo, hi = n["hoped_per_decade"]
    vc = det.get("variance_components", {})
    within = n.get("within_type") or {}
    v = n["verdict"]
    in_pred = det.get("hoped_in_om_pred_units")
    in_pred = f"{f(in_pred[0], 3)}-{f(in_pred[1], 3)}" if in_pred else "n/a"
    rows = [
        [
            r["n_fields"], f(r["trend_se"], 3), f(r["mdc_om_pred"], 3), f(r["mdc_real_om"]), open_span(r.get("mdc_real_om_range")),
            f(r["mdc_real_om_difference"]), open_span(r.get("mdc_real_om_difference_range")), noise._grade(r["mdc_real_om"]),
        ]
        for r in det.get("by_n_fields", [])
    ]
    header = [
        "fields averaged", "trend SE (om_pred / decade)", "min. detectable in om_pred", "min. detectable real change / decade",
        "range over the sensitivity interval", "same, comparing two groups in the county", "its range", f"against {lo:g}-{hi:g} hoped",
    ]  # fmt: skip
    return [
        "## 7. Signal against noise\n",
        f"Hoped-for signal: **{lo:g}-{hi:g} percentage points of organic matter per decade** (centre {n['hoped_central']:g});"
        f" reasoning in `pipeline/soil/noise.py`. With sensitivity {f(det['sensitivity'])} that is {in_pred} per decade in om_pred.\n",
        f"**The sensitivity is the weak link.** Between soils it is {f(det['sensitivity'])}, 90 % interval"
        f" {span(det.get('sensitivity_interval90'))} from resampling map units, so every figure in real organic matter below is"
        f" given with its range. Within a soil type it is {f(within.get('sensitivity'))} ({span(within.get('sensitivity_interval90'))};"
        f" rank correlation {f(within.get('spearman'))}): the between-soil figure is sand against loam, and a field does not"
        f" change its soil type. The cautious sensitivity for change used by the verdict is {f(v.get('sensitivity_for_change'), 3)};"
        f" om_pred follows organic matter within a soil type: **{yes_no(v.get('follows_organic_matter_within_soil_type'))}**."
        " The figures in the table use the between-soil value and are therefore optimistic.\n",
        f"**One field needs no sensitivity to be judged.** Its trend over the window has a standard error of"
        f" **{f(det['trend_se_one_field'], 3)}** per decade in om_pred (robust SD of the fitted trends of {det['n_fields_with_trend']}"
        f" fields with at least {det['min_years_for_trend']} values; {f(det['trend_se_one_field_from_jump_sd'], 3)} from the jump SD for a"
        f" complete series), so the smallest detectable trend is {f(det.get('mdc_one_field_om_pred_units'))} per decade in om_pred's own"
        f" units. The hoped change is at most {hi:g}: buried even if om_pred showed every real change in full:"
        f" **{yes_no(det.get('single_field_buried_at_any_sensitivity'))}**.\n",
        f"Means of several fields use the plain SD of the same trends ({f(det.get('trend_sd_plain'), 3)}), because a mean feels the"
        " tails that the robust figure ignores."
        f" Year-to-year scatter splits into: county-wide {pct(vc.get('share_county_year'))} (SD {f(vc.get('sd_county_year'), 3)}),"
        f" {model.BLOCK_KM:g} km neighbourhood {pct(vc.get('share_block_year'))} (SD {f(vc.get('sd_block_year'), 3)}),"
        f" the field's own {pct(vc.get('share_field_year'))} (SD {f(vc.get('sd_field_year'), 3)}).\n",
        md_table(header, rows),
        f"\nMinimum detectable = {det['z_detect']:.2f} x standard error (5 % false alarms, 80 % power), divided by the sensitivity."
        " 'no limit' = at the low end of the sensitivity interval the model does not follow organic matter at all."
        f" Floor from the county-wide yearly shift, however many fields: {f(det.get('floor_real_om'))}."
        " That shift is estimated from nine yearly values, so the floor is known only roughly.\n",
    ]


def _artefacts(meta: dict) -> list[str]:
    art = meta["noise"].get("artefacts", {})
    out = ["## 8. Artefacts: does om_pred follow the crop?\n"]
    if "this_year" not in art:
        return [*out, "Too few field-years with a crop history to estimate.\n"]
    this, last = art["this_year"], art["last_year"]
    rows = [
        [
            name, this.get(name, {}).get("n", ""), f(this.get(name, {}).get("shift"), 3, True), f(this.get(name, {}).get("se"), 3),
            f(last.get(name, {}).get("shift"), 3, True), f(last.get(name, {}).get("se"), 3),
        ]
        for name in dict.fromkeys([*this, *last])
    ]  # fmt: skip
    out += [
        f"Field and year fixed effects, {art['n']:,} field-years of {art['n_fields']:,} fields."
        f" Shift of om_pred relative to {art['reference_this_year']}:\n",
        md_table(["crop", "field-years (this year)", "shift when it is this year's crop", "SE", "shift when it was last year's crop", "SE"], rows),
    ]
    if not art.get("last_year_identified", True):
        out.append("\nEvery field follows one fixed rotation here, so last year's crop cannot be told from this year's: only this year's is estimated.")
    best = art.get("largest_clear_shift")
    if best:
        out.append(
            f"\nLargest shift beyond doubt (at least {noise.CLEAR_T:g} standard errors; {art['n_clear_shifts']} such):"
            f" **{f(best['shift'], 3, True)}** (SE {f(best['se'], 3)}) when {best['crop']} is {_when(best)} year's crop instead of"
            f" {art['reference_this_year']}. At the model's sensitivity that reads as"
            f" **{f(art.get('clear_shift_as_real_om'))} points of real organic matter** (over the sensitivity interval:"
            f" {open_span(art.get('clear_shift_as_real_om_range'))}),"
            f" **{f(art.get('clear_shift_over_decade_signal'), 1)} times** a decade of the hoped signal"
            f" ({f(art.get('signal_in_om_pred_units_per_decade'), 3)} in om_pred)."
        )
    else:
        out.append(f"\nNo single shift is at least {noise.CLEAR_T:g} standard errors from zero.")
    out.append(
        f"Spread between all crops: {f(art['largest_range'], 3)} (this year {f(art['range_this_year'], 3)},"
        f" last year {f(art['range_last_year'], 3)}) = {f(art.get('largest_range_as_real_om'))} points of real organic matter,"
        f" {f(art.get('range_over_decade_signal'), 1)} times a decade of signal; the spread of many noisy estimates overstates,"
        " so read it as an upper figure. Changing the rotation changes the crops, so these shifts arrive with the change itself"
        " and do not average out over fields. A crop term in the model could remove this shift; it could not give the model"
        " skill within a soil type, which is the stronger reason against om_pred (section 7).\n"
    )
    return out


def _bare_days(meta: dict) -> list[str]:
    b = meta["bare_days"]
    ck, scale = b["checks"], b["s2_to_landsat"]
    sens = ck.get("sensors", {})
    snow = f"; {b['s2_rows_dropped_for_snow']} Sentinel-2 values set aside as snow" if b["s2_rows_dropped_for_snow"] else ""
    out = [
        "## 9. bare_days (the fallback signal)\n",
        "Combined from both families (weights = clear views; Sentinel-2 put on Landsat's scale:"
        f" landsat = {f(scale['a'], 1)} + {f(scale['b'], 3)} x sentinel2, {scale['n_pairs']:,} pairs)."
        f" Present for {pct(b['coverage'], 1)} of field-years ({b['rows_both']:,} from both families,"
        f" {b['rows_landsat_only']:,} Landsat only, {b['rows_s2_only']:,} Sentinel-2 only{snow}).\n",
        f"- Landsat against Sentinel-2, same field-year ({sens.get('n_pairs', 0):,} pairs): correlation **{f(sens.get('r'), 3)}**,"
        f" Sentinel-2 minus Landsat {f(sens.get('bias_s2_minus_landsat'), 1, True)} days, robust SD of the difference"
        f" {f(sens.get('robust_sd_of_difference'), 1)} days, median absolute difference {f(sens.get('median_abs_difference'), 1)}."
        f" Noise of one family: about {f(sens.get('noise_sd_one_sensor'), 1)} days."
        " Both families use the same definition of bare, so this shows that the number repeats, not that the definition is right.",
    ]
    cover = ck.get("cover")
    if cover:
        out.append(
            f"- Against green cover (cover_frac of field_satellite.parquet, {cover['n']:,} field-years): correlation {f(cover['r'])},"
            f" within a field over the years {f(cover['r_within_field'])}; Sentinel-2 bare_days alone, which shares no image with"
            f" cover_frac: {f(cover['r_s2_only'])}; Landsat alone: {f(cover['r_landsat_only'])}."
            " Negative is right: more green cover, fewer bare days."
        )
    stable = ck.get("stable_crop")
    if stable:
        level = ck.get("year_effect", {})
        out += [
            f"- Year-to-year jump on unchanged-crop fields, all crops pooled: robust SD {f(stable['robust_sd_of_jump'], 1)} days,"
            f" plain SD {f(stable.get('sd_of_jump'), 1)} ({stable['n_pairs']:,} pairs; this is weather as well as measurement)."
            " The pooled robust figure flatters: crops that are never bare sit at zero every year and pull it down. Per crop, below.",
            "- The county-wide level moves from year to year (median by year:"
            f" {by_year(level.get('median_by_year', {}), lambda value: f(value, 0))}; SD {f(level.get('sd'), 1)} days)."
            " bare_days needs dry soil, so a dry year adds days on fields that have bare periods and none under alfalfa:"
            " it is not one shift for all. Compare fields within the same years.",
            f"- Fallow against the median of the other core crops: {f(ck.get('fallow_minus_other_core_median'), 0)} days."
            f" Crop order as expected (fallow highest, above alfalfa): {ck.get('crop_order_ok')}.",
        ]
    if ck.get("by_crop"):
        jumps = (stable or {}).get("by_crop", {})
        rows = [
            [
                crop, s["n"], f(s["median"], 0), f(s["mean"], 0), jumps.get(crop, {}).get("n_pairs", ""),
                f(jumps.get(crop, {}).get("robust_sd_of_jump"), 1), f(jumps.get(crop, {}).get("sd_of_jump"), 1),
            ]
            for crop, s in ck["by_crop"].items()
        ]
        header = ["crop that year", "field-years", "median bare_days", "mean", "unchanged-crop pairs", "year-to-year jump (robust SD)", "jump (plain SD)"]
        out += ["", md_table(header, rows)]
    fallow = ck.get("fallow_vs_cropped")
    if fallow:
        out.append(
            f"\n**What one field-year can tell.** A fallow year stands {f(fallow['gap_days'], 0)} days above a cropped year. Comparing one"
            f" year of each on the same field carries {f(fallow['noise_sd_of_one_comparison'], 0)} days of noise (from the jumps of fallow"
            f" and of the noisiest other core crop), so the gap is **{f(fallow['gap_in_noise_sds'], 1)} noise SDs**: one field-year"
            " separates fallow from cropped, and no more than that."
        )
    if ck.get("separation"):
        rows = [
            [
                f"{r['high']} - {r['low']}", f(r["gap_days"], 0), f(r["noise_sd_of_one_comparison"], 0), f(r["gap_in_noise_sds"], 1),
                r["field_years_needed_per_group"] if r["field_years_needed_per_group"] is not None else "n/a",
            ]
            for r in ck["separation"]
        ]
        header = ["crops compared", "gap of medians (days)", "noise of one comparison (days)", "gap in noise SDs", "field-years needed in each group"]
        out += [
            "",
            md_table(header, rows),
            "\nField-years needed = for the gap to be told from zero with 5 % false alarms and 80 % power, from the plain SDs,"
            " comparing within the same years. Gaps between the cropped crops are smaller than one field-year's jump:"
            " **compare rotations on multi-year means over many fields within the same years, never on one field-year.**",
        ]
    ps = b.get("passes")
    if ps:
        names = {
            "sensors_agree": "the two sensor families agree (r >= 0.9)",
            "crop_order": "fallow is the barest core crop and above alfalfa",
            "fallow_stands_out": "a fallow year stands at least 2 noise SDs above a cropped year",
            "follows_green_cover": "falls when green cover rises (r <= -0.3)",
        }
        out += ["", md_table(["check bare_days has to pass", "result"], [[text, yes_no(ps.get(key))] for key, text in names.items()])]
    return [*out, ""]


def _sentinel2(meta: dict) -> list[str]:
    a = meta.get("sentinel2_adds")
    if not a:
        return ["## 10. What Sentinel-2 adds\n", "This table was built from one sensor family, so the two cannot be compared here.\n"]
    worst = sorted(a["bare_days_missing_landsat_alone_by_year"].items(), key=lambda kv: -kv[1])[:3]
    r2m, r2s, jump = a["r2_map_unit"], a["r2_series"], a["om_pred_jump_robust_sd"]
    return [
        "## 10. What Sentinel-2 adds\n",
        "Sentinel-2 is roughly two thirds of the Earth Engine cost of the extraction (`python -m pipeline.soil.bare_soil --estimate`)."
        " What this table gains from it over Landsat alone:\n",
        f"- **bare_days coverage**: {pct(a['bare_days_coverage_landsat_alone'], 1)} of field-years with Landsat alone,"
        f" {pct(a['bare_days_coverage_both'], 1)} with both. Landsat's gaps by year: {', '.join(f'{y} {pct(v, 1)}' for y, v in worst)}.",
        f"- **bare_days noise**: the two families correlate {f(a['bare_days_r_between_sensors'], 3)}; one family carries about"
        f" {f(a['bare_days_noise_sd_one_sensor'], 1)} days of noise, the two together about {f(a['bare_days_noise_sd_two_sensors'], 1)}."
        " Both are small against the year-to-year jumps in section 9, so the second reading changes little.",
        f"- **om_pred coverage**: {pct(a['om_pred_coverage_landsat_alone'], 1)} with Landsat alone, {pct(a['om_pred_coverage_both'], 1)} with both.",
        f"- **om_pred accuracy**: grouped by map unit, Landsat R2 {f(r2m.get('landsat'))}, Sentinel-2 {f(r2m.get('s2'))}, combined {f(r2m.get('combined'))};"
        f" grouped by soil series, Landsat {f(r2s.get('landsat'))}, Sentinel-2 {f(r2s.get('s2'))}.",
        f"- **om_pred noise**: unchanged-crop jump Landsat {f(jump.get('landsat'), 3)}, Sentinel-2 {f(jump.get('s2'), 3)}, combined {f(jump.get('combined'), 3)}.",
        "- So Sentinel-2 buys a second reading and some coverage of the recommended signal, a cross-check that is independent of"
        " Landsat, and nothing for the organic-matter model. A Landsat-only table is a complete table; Sentinel-2 can be added"
        " later from the same cache.\n",
    ]


def _what_to_use(meta: dict) -> list[str]:
    ps = meta["bare_days"].get("passes", {})
    v = meta["noise"]["verdict"]
    out = ["## 11. What to use\n"]
    if meta["recommended"] == "bare_days":
        if ps.get("all_pass"):
            untested = f" Not tested here: {', '.join(ps['untested'])}." if ps.get("untested") else ""
            out.append(
                "- **bare_days** is the column to use, and it passes its own checks (section 9)." + untested + " It measures what a"
                " rotation does to the soil's cover (exposure to wind, sun and rain), not what the soil has become. One field-year"
                " tells a fallow year from a cropped year and nothing finer: compare rotations on multi-year means over many"
                " fields, within the same years."
            )
        elif ps.get("all_pass") is False:
            out.append(
                f"- **Neither column is trustworthy in this build.** bare_days fails its own checks ({', '.join(ps['failed'])};"
                " section 9); it is named as recommended only because the contract has no third value. Find out why before using it."
            )
        else:
            out.append("- **bare_days** is the better of the two columns, but none of its checks could be run here (section 9).")
        out.append(
            "- **om_pred** is a rough, noisy picture of the kind of soil (sand against loam), averaged over several years."
            " Do not read a field's year-to-year movement or its trend as soil change, and do not rank rotations by it:"
            + (
                " within a soil type it does not follow organic matter, and it moves with the surface the crop leaves behind."
                if v.get("follows_organic_matter_within_soil_type") is False
                else " it moves with the surface the crop leaves behind."
            )
        )
    else:
        out += [
            "- **om_pred**, averaged over a hundred or more fields and several years, is steady enough to compare rotations;"
            " on a single field it is still noise.",
            "- **bare_days** remains the simpler and more direct check of what a rotation does to soil cover.",
        ]
    out.append(
        "- **confidence** says which rows will repeat, not which are right. Use it to drop weak rows; do not rank the years of one field by it."
    )
    return [*out, ""]


def render_report(meta: dict, out_path) -> str:
    """The markdown report: every table behind the verdict."""
    sections = (_inputs, _labels, _accuracy, _sensors, _confidence, _noise, _signal, _artefacts, _bare_days, _sentinel2, _what_to_use)
    lines = _verdict(meta, out_path)
    for section in sections:
        lines += section(meta)
    return "\n".join(lines)

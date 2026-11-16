"""Rotation effects on yield (Phase 5, item A): api/planner/rotation.py, rotation_effects.json and how the
engine uses them. Made-up tables, so these tests always run; the last ones use the real tables and skip
without data/. Nothing here needs the network or writes outside pytest's tmp_path."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from api.planner import candidates as cand  # noqa: E402
from api.planner import config, money, rotation, smart, soil_effect  # noqa: E402
from api.planner.crops import CROPS, PRACTICES  # noqa: E402

from test_api_smart import clean_engine, client, fields_file, tables  # noqa: E402,F401  (fixtures)
from test_planner_smart import B, C, F, IRRIGATED_HISTORY, S, W, a_field, climate_tables  # noqa: E402

IRR, DRY = PRACTICES.index("irrigated"), PRACTICES.index("dryland")


def effect(id, crop, after, mid, low=None, high=None, **extra):
    return {"id": id, "crop": crop, "after": after, "multiplier": mid, "low": mid if low is None else low,
            "high": mid if high is None else high, "status": "verified", "basis": "made up",
            "sources": [{"name": "a made-up trial", "url": "https://example.org"}], **extra}


def effects_of(*entries, enabled=True):
    fx = rotation.parse({"version": 1, "effects": list(entries)})
    return fx if enabled else rotation.RotationEffects(enabled=False, effects=fx.effects)


CORN_DRAG = effect("cc", C, C, 0.9, 0.85, 0.95)


def build(fx=None, mix_counts=None):
    yearly, monthly = climate_tables()
    table = soil_effect.builtin_table()
    if mix_counts is not None:
        table = soil_effect.BareDayTable(table.by_crop, table.by_prev, n_prev=mix_counts, source="builtin")
    return smart.Engine(yearly, monthly, economics=money.builtin_economics(),
                        soil=soil_effect.CropAverageProvider(table), rotation_effects=fx)


@pytest.fixture(scope="module")
def plain():
    return build()


@pytest.fixture(scope="module")
def with_drag():
    return build(effects_of(CORN_DRAG))


def yields_of(engine, irrigated, crop, prev, scenario="ssp245", horizon=2025):
    return np.asarray(engine.crop_year(irrigated, scenario, horizon, crop, prev)["yield"])


# --- the file ------------------------------------------------------------------------------------------

def test_the_tracked_file_has_a_source_a_status_and_a_range_for_every_effect():
    raw = json.loads(rotation.EFFECTS_PATH.read_text())
    fx = rotation.load_effects()
    assert fx.problem == "" and fx.active and len(fx.effects) == len(raw["effects"]) >= 4
    for e in fx.effects:
        assert e.status in rotation.STATUSES and e.sources and all(s["name"] and s["url"] for s in e.sources)
        assert 0.5 < e.low <= e.multiplier <= e.high <= 1.0 and e.crop == e.after, e.id    # a drag of a crop after itself
    ids = {e.id for e in fx.effects}
    assert {"corn_after_corn", "sorghum_after_sorghum", "soybeans_after_soybeans", "wheat_after_wheat"} <= ids
    assert any("alfalfa" in x["id"] for x in raw["not_applied"]) and all(x["reason"] for x in raw["not_applied"])


@pytest.mark.parametrize("change, why", [
    ({"multiplier": 0.8, "low": 0.85}, "low <= multiplier <= high"),
    ({"multiplier": -1.0}, "positive number"),
    ({"crop": "cover crop"}, "expected two of"),
    ({"crop": "fallow"}, "expected two of"),
    ({"status": "probably"}, "status"),
    ({"sources": []}, "named source"),
    ({"practice": "paddy"}, "practice"),
])
def test_a_bad_effect_is_an_error_not_a_guess(change, why):
    with pytest.raises(rotation.RotationEffectsError, match=why):
        rotation.parse({"effects": [{**CORN_DRAG, **change}]})


def test_repeated_ids_and_a_missing_list_are_errors():
    with pytest.raises(rotation.RotationEffectsError, match="repeated"):
        rotation.parse({"effects": [CORN_DRAG, CORN_DRAG]})
    with pytest.raises(rotation.RotationEffectsError, match="no 'effects'"):
        rotation.parse({"version": 1})


def test_a_missing_or_broken_file_leaves_the_planner_as_it_was(tmp_path):
    gone = rotation.load_effects(tmp_path / "none.json")
    assert not gone.active and "FileNotFoundError" in gone.problem and gone.describe()["applied"] is False
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert not rotation.load_effects(bad).active
    off = rotation.load_effects(None, enabled=False)
    assert not off.active and off.describe()["setting"] == "off" and off.describe()["effects"] == []


# --- the switch ----------------------------------------------------------------------------------------

def test_the_setting_reads_on_and_off_and_refuses_anything_else():
    assert config.settings({}).rotation_effects is True
    assert config.settings({"ROTATION_EFFECTS": "on"}).rotation_effects is True
    assert config.settings({"ROTATION_EFFECTS": " OFF "}).rotation_effects is False
    with pytest.raises(ValueError, match="expected on or off"):
        config.settings({"ROTATION_EFFECTS": "maybe"})
    assert config.settings({}).rotation_effects_path.name == "rotation_effects.json"


# --- levelling against the county mix ----------------------------------------------------------------------

def test_without_a_mix_the_multipliers_are_the_files():
    fx = effects_of(CORN_DRAG)
    m = fx.multipliers("mid", None)
    assert m[IRR, CROPS.index(C), CROPS.index(C)] == 0.9
    assert (m[IRR, CROPS.index(C)][np.arange(len(CROPS)) != CROPS.index(C)] == 1.0).all()
    assert fx.multipliers("strong")[DRY, CROPS.index(C), CROPS.index(C)] == 0.85
    assert fx.multipliers("weak")[DRY, CROPS.index(C), CROPS.index(C)] == 0.95
    assert (fx.multipliers("mid")[:, CROPS.index(W)] == 1.0).all()            # nothing listed for wheat


def test_levelling_keeps_the_county_mix_at_its_own_yield():
    """Weighted by the county's previous crops, the multipliers of a crop average to exactly 1."""
    fx = effects_of(CORN_DRAG)
    counts = {p: {c: {q: 0 for q in CROPS} for c in CROPS} for p in PRACTICES}
    counts["irrigated"][C] = {C: 600, B: 100, W: 200, S: 100, "alfalfa": 0, F: 0}
    m = fx.multipliers("mid", counts)[IRR, CROPS.index(C)]
    weights = np.array([counts["irrigated"][C][q] for q in CROPS], dtype=float)
    assert (weights * m).sum() / weights.sum() == pytest.approx(1.0)
    assert m[CROPS.index(C)] == pytest.approx(0.9 / 0.94) and m[CROPS.index(B)] == pytest.approx(1 / 0.94)
    assert m[CROPS.index(C)] < 1.0 < m[CROPS.index(B)]
    # dryland has no counts: it keeps the file's numbers
    assert fx.multipliers("mid", counts)[DRY, CROPS.index(C), CROPS.index(C)] == 0.9


def test_a_thin_mix_is_not_trusted():
    fx = effects_of(CORN_DRAG)
    counts = {"irrigated": {C: {C: 20, B: 10}}}
    assert fx.multipliers("mid", counts)[IRR, CROPS.index(C), CROPS.index(C)] == 0.9


# --- the engine ------------------------------------------------------------------------------------------

def test_off_is_exactly_the_old_numbers(plain):
    off = build(effects_of(CORN_DRAG, enabled=False))
    for irrigated in (True, False):
        a, b = plain.evaluate(irrigated), off.evaluate(irrigated)
        for name in ("profit", "failed", "water", "money", "risk", "soil", "irrigation"):
            assert np.array_equal(getattr(a, name), getattr(b, name)), name
        assert not b.range_evaluations()
    assert np.array_equal(yields_of(plain, True, C, C), yields_of(off, True, C, C))


def test_the_previous_crop_changes_only_the_listed_pairs(plain, with_drag):
    for crop, prev in [(C, B), (C, W), (C, F), (W, C), (S, S), (B, B)]:
        assert np.array_equal(yields_of(plain, True, crop, prev), yields_of(with_drag, True, crop, prev)), (crop, prev)
    assert np.allclose(yields_of(with_drag, True, C, C), 0.9 * yields_of(plain, True, C, C))
    assert np.allclose(yields_of(with_drag, False, C, C), 0.9 * yields_of(plain, False, C, C))


def test_corn_after_corn_makes_less_money_than_corn_after_soybeans(plain, with_drag):
    for irrigated in (True, False):
        cc = with_drag.crop_year(irrigated, "ssp245", 2025, C, C)
        cs = with_drag.crop_year(irrigated, "ssp245", 2025, C, B)
        assert (np.asarray(cc["profit"]) < np.asarray(cs["profit"])).all()
        old = plain.crop_year(irrigated, "ssp245", 2025, C, C)
        assert (np.asarray(cc["profit"]) < np.asarray(old["profit"])).all()
        assert (np.asarray(cc["irrigation_mm"]) == np.asarray(old["irrigation_mm"])).all()    # water is not a yield matter
        assert (np.asarray(cc["fail_prob"]) >= np.asarray(old["fail_prob"]) - 1e-12).all()


def test_continuous_corn_loses_rank_and_a_real_rotation_does_not(plain, with_drag):
    def rank_of(engine, rotation_crops):
        ev = engine.evaluate(True, rotations=[tuple(rotation_crops)] + list(cand.candidates(True)))
        robust = ev.robust(smart.Weights().normalised())
        return 1 + int((robust[1:] > robust[0]).sum())

    assert rank_of(with_drag, [C, C, C, C]) > rank_of(plain, [C, C, C, C])
    ev_a = plain.evaluate(True, rotations=[(C, B, C, W)])
    ev_b = with_drag.evaluate(True, rotations=[(C, B, C, W)])
    assert float(ev_b.profit[0].mean()) == pytest.approx(float(ev_a.profit[0].mean()))      # no repeated crop: not touched


def test_the_range_widens_the_spread_and_never_the_middle(with_drag):
    ev = with_drag.evaluate(True, rotations=[(C, C, C, C)])
    strong, weak = ev.range_evaluations()
    assert float(strong.profit[0].mean()) < float(ev.profit[0].mean()) < float(weak.profit[0].mean())
    low, high = smart.profit_extremes(ev, 0)
    assert low == round(float(strong.profit[0].min())) and high == round(float(weak.profit[0].max()))
    plain_low, plain_high = round(float(ev.profit[0].min())), round(float(ev.profit[0].max()))
    assert low < plain_low and high > plain_high
    result = smart._plan_result(ev, 0, smart.Weights().normalised(), 1, True)
    spread = result.spread["profit_per_acre"]
    assert (spread["low"], spread["high"]) == (low, high) and spread["low"] <= spread["mid"] <= spread["high"]
    assert result.numbers["profit_per_acre"] == round(float(ev.profit[0].mean()))          # the middle is the middle's


def test_no_range_means_no_widening():
    fixed = build(effects_of(effect("cc", C, C, 0.9)))
    ev = fixed.evaluate(True, rotations=[(C, C, C, C)])
    assert not ev.range_evaluations()
    assert smart.profit_extremes(ev, 0) == (round(float(ev.profit[0].min())), round(float(ev.profit[0].max())))


def test_the_mix_levels_the_engine(with_drag):
    counts = {p: {c: {q: 0 for q in CROPS} for c in CROPS} for p in PRACTICES}
    counts["irrigated"][C] = {C: 600, B: 100, W: 200, S: 100, "alfalfa": 0, F: 0}
    leveled = build(effects_of(CORN_DRAG), counts)
    base = build()
    assert np.allclose(yields_of(leveled, True, C, C), yields_of(base, True, C, C) * 0.9 / 0.94)
    assert np.allclose(yields_of(leveled, True, C, B), yields_of(base, True, C, B) / 0.94)


def test_plans_carry_the_effects_they_run_into_and_the_settings_say_so(with_drag):
    field = a_field(IRRIGATED_HISTORY)
    body = smart.rank_plans_smart(field, irrigated=True, engine=with_drag).to_dict()
    block = body["settings"]["rotation_effects"]
    assert block["setting"] == "on" and block["applied"] is True and block["effects"][0]["id"] == "cc"
    assert block["effects"][0]["low"] == 0.85 and block["effects"][0]["high"] == 0.95
    assert body["settings"]["limits"] == smart.limits_for(with_drag.irrigation_limit_mm, True)
    cc = smart.rank_plans_smart(a_field({str(2020 + i): C for i in range(5)}), irrigated=True, engine=with_drag).to_dict()
    assert [e["id"] for e in cc["current"]["rotation_effects"]] == ["cc"]
    for plan in body["plans"]:
        assert all(set(e) == {"id", "crop", "after", "multiplier", "low", "high", "status", "sentence"} for e in plan.get("rotation_effects", []))


def test_each_effect_has_one_plain_sentence():
    fx = effects_of(CORN_DRAG, effect("ss", S, S, 0.93))
    assert fx.effects[0].words() == "Corn after corn is counted at about 10% lower yield than after another crop (5% to 15% in the trials)."
    assert fx.effects[1].words() == "Sorghum after sorghum is counted at about 7% lower yield than after another crop."
    assert [row["sentence"] for row in fx.describe()["effects"]] == [e.words() for e in fx.effects]


def test_the_limits_sentence_follows_the_switch():
    on, off = smart.limits_for(610.0, True), smart.limits_for(610.0, False)
    assert "after itself" in on[0] and "fertilizer, not bushels" in on[0]
    assert off[0].startswith("Yields do not change with the crop grown before") and "switched off" in off[0]
    assert on[1:] == off[1:]


# --- through the API ----------------------------------------------------------------------------------

def test_the_api_echoes_the_setting_and_off_keeps_the_old_behaviour(client, monkeypatch):
    on = client.get("/field/KS-000001/plans").json()
    assert on["settings"]["rotation_effects"]["setting"] == "on" and on["settings"]["rotation_effects"]["applied"] is True
    assert any("after itself" in s for s in on["settings"]["limits"])
    monkeypatch.setenv("ROTATION_EFFECTS", "off")
    off = client.get("/field/KS-000001/plans").json()
    block = off["settings"]["rotation_effects"]
    assert block["setting"] == "off" and block["applied"] is False and block["effects"] == []
    assert any(s.startswith("Yields do not change with the crop grown before") for s in off["settings"]["limits"])
    assert all("rotation_effects" not in p for p in off["plans"])
    # the old keys of a plan, exactly
    assert all(set(p) == {"name", "rotation", "cycle", "scores", "reason", "futures", "spread", "numbers", "rank"}
               for p in off["plans"])


def test_the_effects_endpoint_lists_every_effect_with_its_sources(client):
    body = client.get("/rotation-effects").json()
    assert body["setting"] == "on" and body["applied"] is True
    assert {"corn_after_corn", "wheat_after_wheat"} <= {e["id"] for e in body["effects"]}
    for e in body["effects"]:
        assert e["sources"] and e["status"] in rotation.STATUSES and e["basis"]
    assert body["not_applied"] and all("reason" in x for x in body["not_applied"])
    assert body["baseline"]


def test_an_unreadable_setting_is_one_plain_error(client, monkeypatch):
    monkeypatch.setenv("ROTATION_EFFECTS", "sometimes")
    r = client.get("/rotation-effects")
    assert r.status_code == 400 and "ROTATION_EFFECTS" in r.text and "expected on or off" in r.text
    # like a bad SOIL_EFFECT_SOURCE, the planner is unavailable and the basic planner answers
    assert client.get("/field/KS-000001/plans").json()["engine"] == "basic"


# --- the real tables --------------------------------------------------------------------------------------

def real_engine():
    paths = [ROOT / "data" / "climate_futures.parquet", ROOT / "data" / "climate_monthly.parquet",
             ROOT / "data" / "fields.parquet"]
    if not all(p.is_file() for p in paths):
        pytest.skip("data/ has no climate tables or fields (a fresh clone)")
    smart.reset()
    return smart.get_engine()


def test_real_county_mix_levels_each_crop_to_one(monkeypatch):
    monkeypatch.delenv("ROTATION_EFFECTS", raising=False)
    engine = real_engine()
    mix = rotation.mix_from_soil(engine.soil)
    assert engine.rotation.active and mix
    m = engine.rotation.multipliers("mid", mix)
    for p, practice in enumerate(PRACTICES):
        for crop in (C, S, W):
            cells = mix[practice][crop]
            weights = np.array([cells.get(q, 0) for q in CROPS], dtype=float)
            if weights.sum() >= rotation.MIN_MIX_FIELD_YEARS:
                assert (weights * m[p, CROPS.index(crop)]).sum() / weights.sum() == pytest.approx(1.0)
    assert m[IRR, CROPS.index(C), CROPS.index(C)] < 1.0 < m[IRR, CROPS.index(C), CROPS.index(B)]


def test_real_continuous_irrigated_corn_is_no_longer_second_of_the_candidates(monkeypatch):
    engine = real_engine()
    cc = engine.evaluate(True, rotations=[(C, C, C, C)] + list(cand.candidates(True)))
    weights = smart.Weights().normalised()
    robust = cc.robust(weights)
    rank = 1 + int((robust[1:] > robust[0]).sum())
    assert rank > 2, f"continuous irrigated corn still ranks {rank}"

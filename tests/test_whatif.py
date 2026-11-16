"""Price and cost what-ifs (Phase 5, item F): api/planner/whatif.py, Engine.with_adjustment, the query parameters
and the break-even block of /plans. Made-up tables, so these always run; the last test uses the real tables and
skips without data/."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from api.planner import money, smart, soil_effect, whatif  # noqa: E402
from api.planner.crops import CROPS  # noqa: E402

from test_api_smart import DRY, clean_engine, client, fields_file, tables  # noqa: E402,F401  (fixtures)
from test_planner_smart import a_field, climate_tables  # noqa: E402


@pytest.fixture(scope="module")
def engine():
    yearly, monthly = climate_tables()
    return smart.Engine(yearly, monthly, economics=money.builtin_economics(),
                        soil=soil_effect.CropAverageProvider(soil_effect.builtin_table()))


def profit_of(eng, irrigated=False):
    return eng.evaluate(irrigated).profit


# --- parsing -----------------------------------------------------------------------------------------

def test_nothing_given_is_zero():
    assert whatif.parse().is_zero and whatif.parse(0, 0, {}).is_zero
    assert whatif.parse().describe()["active"] is False


@pytest.mark.parametrize("bad", [-50.1, 100.1, float("nan"), float("inf"), True, "10"])
def test_out_of_range_is_one_plain_sentence(bad):
    with pytest.raises(ValueError) as err:
        whatif.parse(price_pct=bad)
    assert "\n" not in str(err.value) and "-50" in str(err.value)


def test_limits_are_accepted():
    a = whatif.parse(-50, 100, {"corn": 100})
    assert a.price_factor("corn") == 2.0 and a.price_factor("sorghum") == 0.5 and a.cost_factor == 2.0


def test_own_crop_price_replaces_the_general_one():
    a = whatif.parse(price_pct=-20, crop_price_pct={"corn": 10})
    assert a.price_factor("corn") == pytest.approx(1.1) and a.price_factor("soybeans") == pytest.approx(0.8)


# --- the economics and the engine ---------------------------------------------------------------------

def test_economics_scale_prices_and_every_cost():
    base = money.builtin_economics()
    adj = whatif.apply_to_economics(base, whatif.parse(-20, 10))
    assert adj.price("corn") == pytest.approx(base.price("corn") * 0.8) and adj.price("fallow") == 0
    assert adj.cost("fallow", "dryland") == pytest.approx(base.cost("fallow", "dryland") * 1.1)
    assert adj.cost("corn", "irrigated", 200) == pytest.approx(base.cost("corn", "irrigated", 200) * 1.1)
    assert adj.pump_per_mm == pytest.approx(base.pump_per_mm * 1.1)


def test_zero_changes_nothing(engine):
    assert engine.with_adjustment(whatif.ZERO) is engine
    assert engine.with_adjustment(whatif.parse(0, 0)) is engine


def test_adjusted_engine_is_cached_and_keeps_the_base_untouched(engine):
    before = profit_of(engine).copy()
    a = engine.with_adjustment(whatif.parse(-10, 5))
    assert engine.with_adjustment(whatif.parse(-10, 5)) is a
    assert a is not engine and a.adjustment.price_pct == -10
    assert np.array_equal(profit_of(engine), before)


def test_profit_is_linear_in_the_price_and_cost_moves(engine):
    base = profit_of(engine, True)
    up = profit_of(engine.with_adjustment(whatif.parse(price_pct=50)), True)
    dn = profit_of(engine.with_adjustment(whatif.parse(price_pct=-50)), True)
    assert np.allclose((up + dn) / 2, base)
    costly = profit_of(engine.with_adjustment(whatif.parse(cost_pct=100)), True)
    assert (costly < base).mean() > 0.9


def test_higher_prices_never_lower_a_return_and_costs_never_raise_one(engine):
    base = profit_of(engine)
    assert (profit_of(engine.with_adjustment(whatif.parse(price_pct=30))) >= base - 1e-9).all()
    assert (profit_of(engine.with_adjustment(whatif.parse(cost_pct=30))) <= base + 1e-9).all()


def test_one_crop_price_only_moves_plans_that_grow_it(engine):
    ev0 = engine.evaluate(False)
    ev1 = engine.with_adjustment(whatif.parse(crop_price_pct={"corn": 100})).evaluate(False)
    grows = np.array([CROPS.index("corn") in set(c) for c in ev0.plans.crop.tolist()])
    uses_corn = ev0.plans.shares[:, CROPS.index("corn")] > 0
    moved = np.abs(ev1.profit - ev0.profit).max(axis=1) > 1e-9
    assert (moved == uses_corn).all() and uses_corn.any() and not uses_corn.all() and grows is not None


def test_limit_engine_keeps_the_what_if(engine):
    adj = engine.with_adjustment(whatif.parse(-20, 0))
    limited = adj.with_limit(300.0)
    assert limited.adjustment == adj.adjustment
    assert limited.with_adjustment(whatif.ZERO).adjustment.is_zero


# --- break-even ---------------------------------------------------------------------------------------

def test_break_even_is_exact_for_a_linear_return():
    now = np.array([100.0, 50.0])              # revenue 300 / 200, costs 200 / 150
    doubled = np.array([400.0, 250.0])
    out = whatif.break_even(now, doubled, whatif.ZERO, "Plan")
    assert out["status"] == "ok" and out["basis"] == "today"
    assert out["break_even_pct"] == round((350 / 500 - 1) * 100)
    assert out["low_pct"] == round((200 / 300 - 1) * 100) and out["high_pct"] == round((150 / 200 - 1) * 100)


def test_break_even_is_against_today_when_prices_were_moved_together():
    out = whatif.break_even(np.array([100.0]), np.array([400.0]), whatif.parse(price_pct=-20), "P")
    assert out["break_even_pct"] == round((200 / 300 * 0.8 - 1) * 100)


def test_break_even_against_set_prices_with_a_crop_override():
    out = whatif.break_even(np.array([100.0]), np.array([400.0]), whatif.parse(crop_price_pct={"corn": 10}), "P")
    assert out["basis"] == "your_prices" and out["break_even_pct"] == round((200 / 300 - 1) * 100)


def test_stays_above_zero_for_any_drop_shown():
    out = whatif.break_even(np.array([500.0, 480.0]), np.array([1000.0, 960.0]), whatif.ZERO, "P")
    assert out["status"] == "stays_above_zero" and "any price drop shown" in out["sentence"]


def test_already_below_zero_and_no_revenue():
    out = whatif.break_even(np.array([-50.0]), np.array([50.0]), whatif.ZERO, "P")
    assert out["status"] == "below_zero_now" and out["break_even_pct"] == round((150 / 100 - 1) * 100)
    out = whatif.break_even(np.array([-14.0]), np.array([-14.0]), whatif.ZERO, "P")
    assert out["status"] == "no_revenue" and out["break_even_pct"] is None


def test_the_sentence_never_says_profit(engine):
    out = whatif.break_even(np.array([100.0]), np.array([400.0]), whatif.ZERO, "P")
    assert "profit" not in out["sentence"].lower() and "direct costs" in out["sentence"]


def test_break_even_return_is_zero_in_the_model(engine):
    """Take the model's own break-even for the top dryland plan and re-score it: the mean return is about zero."""
    field = a_field()
    res = smart.rank_plans_smart(field, engine=engine, irrigated=False)
    sens = res.sensitivity
    assert sens is not None and sens["status"] in ("ok", "stays_above_zero", "below_zero_now")
    if sens["status"] != "ok":
        return
    pct = sens["break_even_pct"]
    moved = engine.with_adjustment(whatif.parse(price_pct=max(-50, min(100, pct))))
    ev = moved.evaluate(False)
    i = res.plans[0].rotation
    idx = [k for k, c in enumerate(ev.plans.rotations) if list(c) == list(i)][0]
    assert abs(float(ev.profit[idx].mean())) < 0.03 * abs(float(engine.evaluate(False).profit[idx].mean())) + 3


# --- the API ------------------------------------------------------------------------------------------

def test_api_zero_is_the_same_answer_and_echoes_settings(client):
    base = client.get(f"/field/{DRY}/plans").json()
    zero = client.get(f"/field/{DRY}/plans", params={"price_pct": 0, "cost_pct": 0}).json()
    assert base["plans"] == zero["plans"] and "what_if" not in base["settings"] and "what_if" not in zero["settings"]
    assert base["sensitivity"] == zero["sensitivity"]


def test_api_what_if_changes_dollars_and_is_echoed(client):
    base = client.get(f"/field/{DRY}/plans").json()
    low = client.get(f"/field/{DRY}/plans", params={"price_pct": -40, "cost_pct": 20}).json()
    assert low["settings"]["what_if"] == {**low["settings"]["what_if"], "active": True, "price_pct": -40.0,
                                          "cost_pct": 20.0}
    assert low["plans"][0]["numbers"]["profit_per_acre"] < base["plans"][0]["numbers"]["profit_per_acre"]
    assert "what-if" in " ".join(low["settings"]["limits"])


@pytest.mark.parametrize("query", [{"price_pct": 101}, {"cost_pct": -51}, {"price_corn_pct": 500}])
def test_api_bad_values_are_400_with_one_sentence(client, query):
    r = client.get(f"/field/{DRY}/plans", params=query)
    assert r.status_code == 400 and isinstance(r.json()["detail"], str) and "\n" not in r.json()["detail"]


def test_api_score_transition_replay_take_and_echo_it(client):
    q = {"price_pct": -10, "cost_pct": 10}
    for path, extra in ((f"/field/{DRY}/score", {"rotation": "corn,soybeans"}),
                        (f"/field/{DRY}/transition", {"plan": 1})):
        body = client.get(path, params={**q, **extra}).json()
        assert body["settings"]["what_if"]["price_pct"] == -10.0, path
    r = client.get(f"/field/{DRY}/replay", params={**q, "plan": 1})
    assert r.status_code in (200, 503)
    if r.status_code == 200:
        assert r.json()["settings"]["what_if"]["cost_pct"] == 10.0


def test_api_farm_takes_it_and_rejects_bad(client):
    ok = client.post("/farm/plan", json={"field_ids": [DRY], "price_pct": -20})
    assert ok.status_code == 200 and ok.json()["settings"]["what_if"]["price_pct"] == -20.0
    assert client.post("/farm/plan", json={"field_ids": [DRY], "price_pct": 300}).status_code == 400
    assert client.post("/farm/plan", json={"field_ids": [DRY], "crop_price_pct": {"rye": 5}}).status_code == 400


def test_api_score_matches_plans_under_the_same_what_if(client):
    q = {"price_pct": -25, "cost_pct": 10}
    plans = client.get(f"/field/{DRY}/plans", params=q).json()
    rot = ",".join(plans["plans"][0]["rotation"])
    mine = client.get(f"/field/{DRY}/score", params={**q, "rotation": rot}).json()
    assert mine["yours"]["numbers"]["profit_per_acre"] == plans["plans"][0]["numbers"]["profit_per_acre"]


# --- real tables --------------------------------------------------------------------------------------

def test_real_tables_break_even_is_consistent():
    from api.planner import config as pc
    if not (Path(pc.settings().climate_futures_path).is_file() and Path(pc.settings().climate_monthly_path).is_file()):
        pytest.skip("no climate tables in data/")
    smart.reset()
    eng = smart.get_engine()
    res = smart.rank_plans_smart(a_field(field_id="KS-000001"), engine=eng, irrigated=False)
    assert res.sensitivity and res.sensitivity["status"] in ("ok", "stays_above_zero", "below_zero_now", "no_revenue")
    assert eng.with_adjustment(whatif.ZERO) is eng

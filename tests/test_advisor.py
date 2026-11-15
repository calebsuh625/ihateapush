"""Tests for the farm advisor (api/advisor.py, api/advisor_tools.py, api/advisor_grounding.py; Phase 4, item 7).

The model is never called: `advisor._generate` is replaced by a script of made-up Gemini replies, and the
HTTP error mapping is tried against an httpx mock transport. The tools run for real on the made-up fields and
climate tables of tests/test_api_smart.py. Nothing under data/ is read, and the .env file is pointed at a
temp folder, so no real key is touched. One last test reads the real tables and skips without them.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import pytest  # noqa: E402

from api import advisor, advisor_grounding as grounding, advisor_tools  # noqa: E402
import test_api_smart as api_tests  # noqa: E402
from test_api_smart import clean_engine, client, fields_file, tables  # noqa: E402,F401  (fixtures)

DRY, WET = api_tests.DRY, api_tests.WET
KEY = "AIza-test-key-0123456789"


@pytest.fixture()
def no_key(tmp_path, monkeypatch):
    monkeypatch.delenv(advisor.KEY_NAME, raising=False)
    monkeypatch.setattr(advisor, "ENV_PATH", tmp_path / "no.env")


@pytest.fixture()
def with_key(tmp_path, monkeypatch):
    monkeypatch.setenv(advisor.KEY_NAME, KEY)
    monkeypatch.setattr(advisor, "ENV_PATH", tmp_path / "no.env")


# --- the number check ----------------------------------------------------------------------------

def unmatched(answer, *sources):
    return [item["text"] for item in grounding.check(answer, sources)["unmatched"]]


def test_matches_rounded_signed_and_percent_numbers():
    tool = {"return": 286.0, "water": 20.9, "change": -29.6, "share": 0.903, "list": [1, 2, 3]}
    answer = "Returns $286 an acre, pumps 21 inches (20.9 exactly), 29.6 fewer days, holds up 90% of the time, our 3 picks."
    assert unmatched(answer, tool) == []


def test_flags_invented_numbers_and_wrong_precision():
    tool = {"return": 286.0}
    assert unmatched("It returns $312 an acre.", tool) == ["$312"]
    assert unmatched("It returns $286.4 an acre.", tool) == ["$286.4"]        # more digits than the tool gave


def test_years_must_match_exactly_and_ids_are_not_numbers():
    tool = {"horizon": 2040, "field": "KS-014376"}
    assert unmatched("In 2040 field KS-014376 on SSP2-4.5 and ACCESS-CM2.", tool) == []
    assert unmatched("In 2041 it is better.", tool) == ["2041"]


def test_round_numbers_and_scales():
    tool = {"x": 2288, "y": 1500000}
    assert unmatched("About $2,300 and 1.5 million", tool) == []
    assert unmatched("About $2,900", tool) == ["$2,900"]


def test_positions_point_into_the_answer_in_javascript_units():
    answer = "Emoji \U0001F33D then $999 here"
    out = grounding.check(answer, [{"a": 1}])["unmatched"]
    assert [item["text"] for item in out] == ["$999"]
    utf16 = answer.encode("utf-16-le")
    start, end = out[0]["start"], out[0]["end"]
    assert utf16[start * 2:end * 2].decode("utf-16-le") == "$999"


def test_numbers_the_farmer_typed_count_as_given():
    assert unmatched("Pumping 12 inches then.", {"q": "what if I can only pump 12 inches?"}) == []


# --- the key and the status ----------------------------------------------------------------------

def test_status_without_a_key_gives_the_exact_line(client, no_key):
    body = client.get("/advisor/status").json()
    assert body["available"] is False
    assert body["reason"] == "add GEMINI_API_KEY to .env" and body["provider"] == "Google Gemini"
    assert body["env_line"] == "GEMINI_API_KEY=your-key-here"
    assert body["message"] == "Add an API key to use the advisor"
    assert body["env_file"] == ".env"


def test_placeholder_line_is_not_a_key(client, tmp_path, monkeypatch):
    monkeypatch.delenv(advisor.KEY_NAME, raising=False)
    env = tmp_path / ".env"
    env.write_text("# comment\nGEMINI_API_KEY=your-key-here\n")
    monkeypatch.setattr(advisor, "ENV_PATH", env)
    assert client.get("/advisor/status").json()["available"] is False
    env.write_text(f"export GEMINI_API_KEY='{KEY}'\n")             # read again on every call; quotes and export allowed
    body = client.get("/advisor/status").json()
    assert body["available"] is True
    assert KEY not in json.dumps(body)


def test_status_with_a_key_never_shows_it(client, with_key):
    body = client.get("/advisor/status").json()
    assert body["available"] is True and "env_line" not in body and body["reason"] is None
    assert KEY not in json.dumps(body)
    assert set(body["tools"]) == set(advisor_tools.TOOLS)


def test_chat_without_a_key_is_503(client, no_key):
    response = client.post("/advisor/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 503
    assert response.json()["code"] == "no_key"
    assert response.json()["detail"] == "add GEMINI_API_KEY to .env"


# --- reading the request ---------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {}, {"messages": []}, {"messages": "hi"}, {"messages": [{"role": "assistant", "content": "x"}]},
    {"messages": [{"role": "system", "content": "x"}]}, {"messages": [{"role": "user", "content": 3}]},
    {"messages": [{"role": "user", "content": "ok"}], "context": "no"},
    {"messages": [{"role": "user", "content": "ok"}], "context": {"scenario": "ssp999"}},
    {"messages": [{"role": "user", "content": "ok"}], "context": {"field_id": "../etc"}},
    {"messages": [{"role": "user", "content": "ok"}], "context": {"limit_in": -2}},
    {"messages": [{"role": "user", "content": "ok"}], "context": {"horizon": 2040.5}},
])
def test_bad_requests_are_400_with_a_sentence(client, with_key, body):
    response = client.post("/advisor/chat", json=body)
    assert response.status_code == 400
    assert response.json()["detail"].endswith(".") and response.json()["code"] == "bad_request"


def test_context_is_read_and_described():
    ctx = advisor._clean_context({"field_id": WET, "weights": {"soil": 3, "water": 0, "bogus": 9}, "scenario": "ssp585",
                                  "horizon": 2040, "irrigated": False, "limit_in": 12})
    assert ctx.field_id == WET and ctx.weights == {"soil": 3.0, "water": 0.0}
    assert ctx.irrigated == {WET: False} and ctx.limit_in == 12.0 and ctx.horizon == 2040
    text = advisor._on_screen(ctx)
    assert WET in text and "dryland" in text and "12" in text and "high emissions" in text
    farm = advisor._clean_context({"farm_field_ids": [DRY, WET]})
    assert "farm of 2 fields" in advisor._on_screen(farm)


def test_conversation_must_start_and_end_with_the_farmer():
    out = advisor._clean_messages([{"role": "assistant", "content": "old"}, {"role": "user", "content": "a"},
                                   {"role": "assistant", "content": "b"}, {"role": "user", "content": " c "}])
    assert [m["content"] for m in out] == ["a", "b", "c"]


# --- the conversation, with the model scripted ---------------------------------------------------------

def scripted(monkeypatch, replies):
    """Replace the model: each call takes the next reply; every request body is kept."""
    seen = []

    async def fake(client, key, model, body):
        assert key == KEY
        seen.append(json.loads(json.dumps(body)))
        return replies[len(seen) - 1]

    monkeypatch.setattr(advisor, "_generate", fake)
    monkeypatch.setattr(advisor, "model_chain", lambda: ["scripted-model"])
    return seen


def call(name, **args):
    return {"candidates": [{"content": {"role": "model", "parts": [{"functionCall": {"name": name, "args": args}}]},
                            "finishReason": "STOP"}]}


def say(text):
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}]}


def ask(client, question, **context):
    return client.post("/advisor/chat", json={"messages": [{"role": "user", "content": question}], "context": context})


def test_an_answer_with_tool_calls_and_a_checked_number(client, with_key, monkeypatch):
    seen = scripted(monkeypatch, [call("get_plans"), say("We looked at 3 plans for this field.")])
    response = ask(client, "What should I plant?", field_id=DRY, scenario="ssp245", horizon=2025,
                   weights={"soil": 3, "water": 2, "money": 3, "risk": 2})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["answer"].startswith("We looked at 3 plans")
    assert [c["name"] for c in body["tool_calls"]] == ["get_plans"] and body["tool_calls"][0]["ok"] is True
    assert body["grounding"]["passed"] is True                      # "3" is the number of picks the tool listed
    # the second request carries the model's own call and the tool's answer; the page state leads the question
    assert seen[1]["contents"][-2]["parts"][0]["functionCall"]["name"] == "get_plans"
    result = seen[1]["contents"][-1]["parts"][0]["functionResponse"]["response"]["result"]
    assert result.get("field_id") == DRY and "plans" in result
    assert f"Open: field {DRY}" in seen[0]["contents"][0]["parts"][0]["text"]
    assert KEY not in json.dumps(body)


def test_an_invented_number_is_returned_as_unmatched(client, with_key, monkeypatch):
    scripted(monkeypatch, [call("get_field"), say("It would return $98765 an acre."),
                           say("It would return $98765 an acre, for sure.")])        # the correction fails to fix it
    body = ask(client, "How much?", field_id=DRY).json()
    assert body["grounding"]["passed"] is False
    assert [u["text"] for u in body["grounding"]["unmatched"]] == ["$98765"]
    start, end = body["grounding"]["unmatched"][0]["start"], body["grounding"]["unmatched"][0]["end"]
    assert body["answer"][start:end] == "$98765"


def test_a_tool_that_cannot_answer_is_told_to_the_model_not_raised(client, with_key, monkeypatch):
    seen = scripted(monkeypatch, [call("get_plans", field_id="KS-NOPE"), call("no_such_tool"), say("I could not find that field.")])
    body = ask(client, "Plans for KS-NOPE?").json()
    assert [c["ok"] for c in body["tool_calls"]] == [False, False]
    assert "error" in seen[1]["contents"][-1]["parts"][0]["functionResponse"]["response"]["result"]
    assert body["answer"] == "I could not find that field."


def test_the_screen_settings_are_the_tools_defaults(client, with_key, monkeypatch):
    seen = scripted(monkeypatch, [call("get_plans"), say("ok")])
    ask(client, "plans?", field_id=DRY, scenario="ssp585", horizon=2040, weights={"money": 9, "soil": 1})
    result = seen[1]["contents"][-1]["parts"][0]["functionResponse"]["response"]["result"]
    settings = result["settings"]
    assert settings["scenario"] == "high emissions" and settings["year"] == 2040
    direct = client.get(f"/field/{DRY}/plans", params={"scenario": "ssp585", "horizon": 2040, "money": 9, "soil": 1}).json()
    assert [p["name"] for p in result["plans"]] == [p["name"] for p in direct["plans"][:3]]


def test_gives_up_calling_tools_after_the_round_limit(client, with_key, monkeypatch):
    replies = [call("get_field")] * advisor.MAX_TOOL_ROUNDS + [say("Done.")]
    seen = scripted(monkeypatch, replies)
    body = ask(client, "loop", field_id=DRY).json()
    assert body["answer"] == "Done." and len(seen) == advisor.MAX_TOOL_ROUNDS + 1
    assert seen[-1]["toolConfig"]["functionCallingConfig"]["mode"] == "NONE"
    assert "toolConfig" not in seen[0]


def test_an_empty_answer_is_an_error_in_words(client, with_key, monkeypatch):
    scripted(monkeypatch, [{"candidates": [{"content": {"parts": []}, "finishReason": "STOP"}]}])
    response = ask(client, "hello")
    assert response.status_code == 502 and response.json()["code"] == "empty"


# --- what goes wrong upstream -----------------------------------------------------------------------------

def generate(status, body=None, raises=None):
    def handler(request):
        if raises:
            raise raises
        return httpx.Response(status, json=body or {})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await advisor._generate(c, KEY, "m", {"contents": []})

    return asyncio.run(go())


@pytest.mark.parametrize("status,body,code,http", [
    (400, {"error": {"message": f"API key not valid. Please pass a valid API key. {KEY}"}}, "key", 502),
    (429, {"error": {"message": "quota"}}, "busy", 429),
    (404, {"error": {"message": "no model"}}, "model", 502),
    (503, {"error": {"message": "overloaded"}}, "upstream", 502),
    (400, {"error": {"message": "bad shape"}}, "refused", 502),
])
def test_upstream_errors_become_plain_sentences(status, body, code, http):
    with pytest.raises(advisor.AdvisorError) as caught:
        generate(status, body)
    assert caught.value.code == code and caught.value.status == http
    assert KEY not in caught.value.message and caught.value.message.endswith(".")


def test_timeouts_and_network_errors():
    with pytest.raises(advisor.AdvisorError) as caught:
        generate(200, raises=httpx.ReadTimeout("slow"))
    assert caught.value.code == "timeout"
    with pytest.raises(advisor.AdvisorError) as caught:
        generate(200, raises=httpx.ConnectError("down"))
    assert caught.value.code == "network"


def test_the_key_goes_in_a_header_not_the_url():
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"candidates": []})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await advisor._generate(c, KEY, "some-model", {})

    asyncio.run(go())
    assert KEY not in str(sent[0].url) and sent[0].headers["x-goog-api-key"] == KEY
    assert "some-model:generateContent" in str(sent[0].url)


# --- the tools --------------------------------------------------------------------------------------------

def test_declarations_match_the_tools():
    declared = advisor_tools.declarations()
    assert [d["name"] for d in declared] == list(advisor_tools.TOOLS)
    assert all(d["description"] and d.get("parameters", {}).get("type", "OBJECT") == "OBJECT" for d in declared)


def test_wording_rules_are_in_the_instructions():
    text = advisor.SYSTEM
    for phrase in ("soil protection", "returns over direct costs", "not on rain", "not proof", "Never invent"):
        assert phrase in text


def test_a_tool_with_bad_arguments_answers_with_an_error(client, with_key):
    async def go():
        request = type("R", (), {"app": client.app})()
        return await advisor_tools.run("get_plans", {"horizon": "soon"}, request, advisor_tools.Context(field_id=DRY))

    result, summary, ok = asyncio.run(go())
    assert ok is False and "error" in result and summary


def test_no_field_open_asks_which_field(client, with_key):
    async def go():
        request = type("R", (), {"app": client.app})()
        return await advisor_tools.run("get_field", {}, request, advisor_tools.Context())

    result, _, ok = asyncio.run(go())
    assert ok is False and "No field is selected" in result["error"]


def test_real_tables_answer_through_the_tools():
    data = ROOT / "data"
    if not (data / "fields.parquet").exists() or not (data / "climate_futures.parquet").exists():
        pytest.skip("needs the real data")
    from fastapi.testclient import TestClient
    import api.main as main

    with TestClient(main.create_app()) as c:
        request = type("R", (), {"app": c.app})()
        result, _, ok = asyncio.run(advisor_tools.run("get_plans", {}, request, advisor_tools.Context(field_id="KS-014376")))
        direct = c.get("/field/KS-014376/plans").json()
    assert ok and result["field_id"] == "KS-014376"
    assert result["plans"][0]["name"] == direct["plans"][0]["name"]


# --- text or content, the model name, the second model, the correction round, the time limit ---------------

def test_messages_may_say_text_or_content():
    out = advisor._clean_messages([{"role": "user", "text": " a "}, {"role": "assistant", "content": "b"},
                                   {"role": "user", "text": "c"}])
    assert [m["content"] for m in out] == ["a", "b", "c"]
    with pytest.raises(advisor.BadRequest):
        advisor._clean_messages([{"role": "user", "text": 3}])


def test_model_name_comes_from_gemini_model(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("ADVISOR_MODEL", raising=False)
    assert advisor.model_name() == "gemini-flash-latest"
    assert advisor.model_chain() == ["gemini-flash-latest", *advisor.FALLBACK_MODELS]
    monkeypatch.setenv("GEMINI_MODEL", advisor.FALLBACK_MODELS[0])
    assert advisor.model_chain() == [advisor.FALLBACK_MODELS[0], advisor.FALLBACK_MODELS[1]]


def test_a_number_the_tools_never_gave_is_corrected_once(client, with_key, monkeypatch):
    seen = scripted(monkeypatch, [call("get_field"), say("It would return $98765 an acre."),
                                  say("I can only say what the plans show: 3 plans.")])
    body = ask(client, "How much?", field_id=DRY).json()
    assert body["corrected"] is True and body["grounding"]["passed"] is True
    assert body["answer"].startswith("I can only say") and len(seen) == 3
    note = seen[2]["contents"][-1]["parts"][0]["text"]               # the model is told which numbers failed
    assert "$98765" in note and "tool result" in note


def test_a_second_wrong_answer_is_returned_with_its_numbers_marked(client, with_key, monkeypatch):
    seen = scripted(monkeypatch, [call("get_field"), say("It returns $98765."), say("It returns $87654.")])
    body = ask(client, "How much?", field_id=DRY).json()
    assert len(seen) == 3                                            # asked once, not again
    assert body["corrected"] is True and body["grounding"]["passed"] is False
    assert [u["text"] for u in body["grounding"]["unmatched"]] == ["$87654"]


def test_a_failed_correction_keeps_the_first_answer_marked(client, with_key, monkeypatch):
    seen = []

    async def fake(client, key, model, body):
        seen.append(body)
        if len(seen) == 1:
            return call("get_field")
        if len(seen) == 2:
            return say("It returns $98765.")
        raise advisor.AdvisorError("The advisor service had a problem on its side.", 502, "upstream")

    monkeypatch.setattr(advisor, "_generate", fake)
    body = ask(client, "How much?", field_id=DRY).json()
    assert body["answer"] == "It returns $98765." and body["grounding"]["passed"] is False


def test_a_clean_answer_is_not_corrected(client, with_key, monkeypatch):
    seen = scripted(monkeypatch, [call("get_plans"), say("We looked at 3 plans.")])
    body = ask(client, "plans?", field_id=DRY).json()
    assert body["corrected"] is False and len(seen) == 2


def test_the_second_model_answers_when_the_first_is_overloaded(client, with_key, monkeypatch):
    models = []

    async def fake(client, key, model, body):
        models.append(model)
        if model == "first":
            raise advisor.AdvisorError("busy", 502, "upstream")
        return say("Hello there.")

    monkeypatch.setattr(advisor, "_generate", fake)
    monkeypatch.setattr(advisor, "model_chain", lambda: ["first", "second"])
    body = ask(client, "hi").json()
    assert models == ["first", "second"] and body["model"] == "second"


def test_a_bad_key_does_not_try_the_second_model(client, with_key, monkeypatch):
    models = []

    async def fake(client, key, model, body):
        models.append(model)
        raise advisor.AdvisorError("The API key was not accepted.", 502, "key")

    monkeypatch.setattr(advisor, "_generate", fake)
    monkeypatch.setattr(advisor, "model_chain", lambda: ["first", "second"])
    response = ask(client, "hi")
    assert models == ["first"] and response.status_code == 502 and response.json()["code"] == "key"


def test_the_total_time_limit_ends_a_slow_conversation(client, with_key, monkeypatch):
    async def slow(client, key, model, body):
        await asyncio.sleep(5)
        return say("late")

    monkeypatch.setattr(advisor, "_generate", slow)
    monkeypatch.setattr(advisor, "TOTAL_TIMEOUT_S", 0.3)
    monkeypatch.setattr(advisor, "model_chain", lambda: ["only"])
    response = ask(client, "hi")
    assert response.status_code == 504 and response.json()["code"] == "timeout"


def test_long_conversations_and_messages_are_cut_down():
    many = [{"role": "user" if i % 2 == 0 else "assistant", "text": "x" * 5000} for i in range(60)] + \
           [{"role": "user", "text": "last"}]
    out = advisor._clean_messages(many)
    assert len(out) <= advisor.MAX_MESSAGES and out[0]["role"] == "user" and out[-1]["content"] == "last"
    assert all(len(m["content"]) <= advisor.MAX_MESSAGE_CHARS for m in out)


@pytest.mark.skipif(advisor.api_key() is None, reason="no GEMINI_API_KEY (environment or .env)")
def test_live_one_question_with_the_real_key(client):
    """The only test that calls Google. Skipped without a key. Quota or overload is a skip, not a failure."""
    response = client.post("/advisor/chat", json={
        "messages": [{"role": "user", "text": "What does the climate in Finney County look like in 2040? One sentence."}],
        "context": {"scenario": "ssp245", "horizon": 2040}})
    if response.status_code in (429, 502, 504):
        pytest.skip(f"model service not available right now: {response.json().get('code')}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["answer"] and isinstance(body["tool_calls"], list) and "passed" in body["grounding"]
    assert advisor.api_key() not in response.text

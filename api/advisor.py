"""Phase 4, item 7: the farm advisor (chat).

    GET  /advisor/status   {"available": bool, "provider", "model", "env_line", ...}: is there an API key?
    POST /advisor/chat     one question in, one answer out (the answer, the tools it used, the number check)

The model (Google Gemini, because the owner's key is a Gemini key; the model name is `GEMINI_MODEL`,
default gemini-flash-latest, then gemini-3.5-flash and gemini-3-flash-preview when that one is overloaded or over quota) answers only from tools. Each tool runs one of this API's own endpoint functions
in-process (api/advisor_tools.py), so the advisor can say only what the app itself would show. After the
answer is written, api/advisor_grounding.py looks every number of it up among the numbers the tools gave
(and the ones the farmer typed or has on screen) and returns the ones it cannot find, so the page can mark
them. Nothing is stored: the page keeps the conversation and sends it again with every question.

The key is `GEMINI_API_KEY`, read from the environment or from the git-ignored `.env` file at the repo
root (read again on every call, so adding the key needs no restart). It is never returned, logged or put
in an error message.

Request body of POST /advisor/chat:
    {"messages": [{"role": "user" | "assistant", "text": "..."}, ...],         # last one is the question
                                                                               # ("content" is accepted for "text")
     "context":  {"field_id": "KS-014376" | null,                              # the open field, if any
                  "farm_field_ids": ["KS-...", ...],                           # the open farm, if any
                  "weights": {"soil": 1, "water": 1, "money": 1, "risk": 1},   # the sliders
                  "scenario": "ssp245" | "ssp585", "horizon": 2040,
                  "irrigated": true | false | null,                            # the irrigation switch (open field)
                  "limit_in": 12.0 | null}}                                    # the pumping what-if
Every context key is optional.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from api import advisor_grounding, advisor_tools
from api.planner import config as planner_config

log = logging.getLogger("api.advisor")
router = APIRouter()

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
KEY_NAME = "GEMINI_API_KEY"
ENV_LINE = f"{KEY_NAME}=your-key-here"
NO_KEY_REASON = "add GEMINI_API_KEY to .env"
DEFAULT_MODEL = "gemini-flash-latest"
FALLBACK_MODELS = ("gemini-3.5-flash", "gemini-3-flash-preview")   # tried in this order when the first is overloaded, over quota, missing or too slow
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

MAX_MESSAGES = 24                 # turns kept from the page's conversation (the newest ones)
MAX_MESSAGE_CHARS = 2000
MAX_TOOL_ROUNDS = 6               # rounds of tool calls before the model must answer
MAX_CALLS_PER_ROUND = 6
CALL_TIMEOUT_S = 55.0
FIRST_MODEL_CALL_TIMEOUT_S = 25.0   # while a fallback model is still waiting, do not sit on a slow first one
TOTAL_TIMEOUT_S = 60.0
FALLBACK_CODES = ("upstream", "timeout", "model", "busy", "empty")
MAX_OUTPUT_TOKENS = 8192

SYSTEM = """You are the farm advisor inside a crop-rotation planning tool for Finney County, Kansas. You talk with a farmer, in short plain English: no jargon, no headings, at most about 150 words unless the farmer asks for more. A short list is fine.

Where your facts come from:
- Every number you write must come from a tool you called in this conversation, or from what the farmer typed. Call a tool before you answer any question about a field, a rotation, the weather or the water. Do not do arithmetic of your own and do not round to a different precision than the tool gave; quote the figure as the tool gave it.
- Never invent a number, a price, a threshold, a source or a yield. If the tools do not have it, say so in one sentence and say what the tool does cover.
- Where a tool gives a range (the lowest and highest of the six climate models, a 95% interval, a count such as 5 of 6), give it next to the figure.
- Use the figures for the field the farmer has open, and the settings shown under "On screen" (priorities, scenario, year, irrigation, pumping what-if) unless the farmer asks for something else.

Wording rules that always apply:
- The soil score is soil protection: fewer bare days and lower wind-erosion risk. Never write "soil health", "improves the soil" or "builds the soil".
- Dollar figures are returns over direct costs, not profit or income. On dryland their sign depends on the price years and the cost of fallow, so for dryland talk about yield changes first.
- The six climate models agree on heat, not on rain. Do not write "less rain" or "more rain" unless the tool says at least 5 of 6 models agree.
- The neighbour comparisons (twin view) are differences between look-alike neighbours, not proof that a rotation caused anything. Say "differs by", not "caused".
- Groundwater figures are measured levels carried forward at the measured rate, not a forecast.
- When we say a plan "holds up", it means in how many of the six climate models it comes out ahead; say the count.

Be honest about limits: you advise on the rotation options the tools can score; you do not give legal, insurance or financial advice and you do not know the farmer's own costs, contracts or well permits. If a tool returns an error, tell the farmer in plain words and say what to try instead."""


# --------------------------------------------------------------------------------------
# The key
# --------------------------------------------------------------------------------------


def _read_env_file() -> str | None:
    try:
        text = ENV_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].strip()
        if line.startswith(KEY_NAME + "="):
            value = line.split("=", 1)[1].strip().strip("'\"")
            return value or None
    return None


def api_key() -> str | None:
    """The key, or None when there is none (or only the placeholder)."""
    value = (os.environ.get(KEY_NAME) or "").strip() or _read_env_file()
    if not value or value.lower() in ("your-key-here", "your_key_here", "changeme"):
        return None
    return value


def model_name() -> str:
    return (os.environ.get("GEMINI_MODEL") or os.environ.get("ADVISOR_MODEL") or "").strip() or DEFAULT_MODEL


def model_chain() -> list[str]:
    """The model asked first, then the fallbacks (leaving out any that is the first)."""
    first = model_name()
    return [first] + [m for m in FALLBACK_MODELS if m != first]


def _scrub(text: str, key: str | None) -> str:
    return text.replace(key, "<key>") if key else text


@router.get("/advisor/status", summary="Is the farm advisor set up (is there an API key)?")
def status() -> dict[str, Any]:
    available = api_key() is not None
    out: dict[str, Any] = {
        "available": available,
        "provider": "Google Gemini",
        "model": model_name(),
        "reason": None if available else NO_KEY_REASON,
        "tools": list(advisor_tools.TOOLS),
    }
    if not available:
        out.update({
            "env_file": ".env",
            "env_line": ENV_LINE,
            "message": "Add an API key to use the advisor",
            "how": f"Put this line in the file named .env in the project folder, with your own key after the "
                   f"equals sign, then ask again: {ENV_LINE}",
        })
    return out


# --------------------------------------------------------------------------------------
# Reading the request
# --------------------------------------------------------------------------------------


class BadRequest(Exception):
    pass


def _clean_messages(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list) or not raw:
        raise BadRequest("Send the conversation as a list of messages, the last one a question.")
    out = []
    for item in raw[-MAX_MESSAGES:]:
        if not isinstance(item, dict) or item.get("role") not in ("user", "assistant") \
                or not isinstance(item.get("text", item.get("content")), str):
            raise BadRequest("Each message needs a role (user or assistant) and some text.")
        content = item.get("text", item.get("content")).strip()
        if content:
            out.append({"role": item["role"], "content": content[:MAX_MESSAGE_CHARS]})
    while out and out[0]["role"] != "user":          # the model's turns must start with the farmer's
        out.pop(0)
    if not out or out[-1]["role"] != "user":
        raise BadRequest("The last message has to be the farmer's question.")
    return out


def _clean_context(raw: Any) -> advisor_tools.Context:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise BadRequest("context must be an object.")
    ctx = advisor_tools.Context()
    field_id = raw.get("field_id")
    if field_id is not None:
        if not isinstance(field_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", field_id):
            raise BadRequest("context.field_id is not a field id.")
        ctx.field_id = field_id
    ids = raw.get("farm_field_ids")
    if ids is not None:
        if not isinstance(ids, list) or len(ids) > 200 \
                or not all(isinstance(i, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", i) for i in ids):
            raise BadRequest("context.farm_field_ids must be a list of field ids.")
        ctx.farm_field_ids = list(ids)
    weights = raw.get("weights")
    if weights is not None:
        if not isinstance(weights, dict):
            raise BadRequest("context.weights must be an object.")
        ctx.weights = {k: float(v) for k, v in weights.items()
                       if k in planner_config.SCORE_NAMES and isinstance(v, (int, float))
                       and not isinstance(v, bool) and v == v and v >= 0}
    scenario = raw.get("scenario")
    if scenario is not None:
        if scenario not in planner_config.SCENARIOS:
            raise BadRequest("context.scenario must be ssp245 or ssp585.")
        ctx.scenario = scenario
    horizon = raw.get("horizon")
    if horizon is not None:
        if isinstance(horizon, bool) or not isinstance(horizon, (int, float)) or horizon != int(horizon):
            raise BadRequest("context.horizon must be a year.")
        ctx.horizon = int(horizon)
    irrigated = raw.get("irrigated")
    if irrigated is not None:
        if not isinstance(irrigated, bool):
            raise BadRequest("context.irrigated must be true or false.")
        if ctx.field_id:
            ctx.irrigated[ctx.field_id] = irrigated
    limit = raw.get("limit_in")
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, (int, float)) or not limit > 0:
            raise BadRequest("context.limit_in must be a number above 0.")
        ctx.limit_in = float(limit)
    return ctx


def _on_screen(ctx: advisor_tools.Context) -> str:
    """The page state as a short text for the model (the numbers in it count as the farmer's own)."""
    lines = []
    if ctx.farm_field_ids:
        lines.append(f"Open: a farm of {len(ctx.farm_field_ids)} fields ({', '.join(ctx.farm_field_ids[:25])}"
                     f"{', ...' if len(ctx.farm_field_ids) > 25 else ''}). Use farm_plan for whole-farm questions.")
    elif ctx.field_id:
        lines.append(f"Open: field {ctx.field_id}.")
        if ctx.field_id in ctx.irrigated:
            lines.append("Irrigation switch: the farmer set it to "
                         + ("irrigated." if ctx.irrigated[ctx.field_id] else "dryland."))
    else:
        lines.append("Open: nothing (no field or farm). Ask which field if the question needs one.")
    if ctx.weights:
        lines.append("Priority sliders: " + ", ".join(f"{k} {v:g}" for k, v in ctx.weights.items())
                     + " (only the ratio matters).")
    if ctx.scenario:
        lines.append(f"Scenario: {advisor_tools.SCENARIO_WORDS.get(ctx.scenario, ctx.scenario)} ({ctx.scenario}).")
    if ctx.horizon is not None:
        lines.append(f"Year looked at: {ctx.horizon}.")
    if ctx.limit_in is not None:
        lines.append(f"Pumping what-if is on: at most {ctx.limit_in:g} inches an acre a year.")
    return "On screen:\n" + "\n".join(lines)


# --------------------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------------------


class AdvisorError(Exception):
    """What went wrong, in one plain sentence for the farmer, and the HTTP status to send."""

    def __init__(self, message: str, status: int = 502, code: str = "upstream"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


async def _generate(client: httpx.AsyncClient, key: str, model: str, body: dict) -> dict:
    try:
        response = await client.post(ENDPOINT.format(model=model), json=body,
                                     headers={"x-goog-api-key": key})
    except httpx.TimeoutException:
        raise AdvisorError("The advisor took too long to answer. Try again, or ask a shorter question.",
                           504, "timeout") from None
    except httpx.HTTPError:
        raise AdvisorError("Could not reach the advisor service. Check the internet connection and try again.",
                           502, "network") from None
    if response.status_code == 200:
        try:
            return response.json()
        except ValueError:
            raise AdvisorError("The advisor service sent something we could not read. Try again.") from None
    detail = ""
    try:
        detail = _scrub(str(response.json().get("error", {}).get("message", "")), key)[:200]
    except Exception:
        pass
    log.warning("advisor: Gemini answered %s %s", response.status_code, detail)
    if response.status_code in (400, 403) and re.search(r"api key|API_KEY|permission", detail, re.I):
        raise AdvisorError("The API key was not accepted. Check the key in .env and try again.", 502, "key")
    if response.status_code == 429:
        raise AdvisorError("The advisor service is busy or the key's limit is used up. Wait a minute and try again.",
                           429, "busy")
    if response.status_code == 404:
        raise AdvisorError(f"The model {model} is not available to this key. Set ADVISOR_MODEL in .env to another one.",
                           502, "model")
    if response.status_code >= 500:
        raise AdvisorError("The advisor service had a problem on its side. Try again in a moment.", 502, "upstream")
    raise AdvisorError("The advisor service refused the question. Try asking it another way.", 502, "refused")


def _parts(reply: dict) -> tuple[list[dict], str | None]:
    candidates = reply.get("candidates") or []
    if not candidates:
        block = (reply.get("promptFeedback") or {}).get("blockReason")
        if block:
            raise AdvisorError("The advisor service would not answer that question. Try asking it another way.",
                               502, "blocked")
        raise AdvisorError("The advisor gave no answer. Try again.", 502, "empty")
    candidate = candidates[0]
    return list((candidate.get("content") or {}).get("parts") or []), candidate.get("finishReason")


def _text_of(parts: list[dict]) -> str:
    return "".join(p.get("text", "") for p in parts if isinstance(p.get("text"), str) and not p.get("thought")).strip()


def _contents(messages: list[dict[str, str]], on_screen: str) -> list[dict]:
    contents = [{"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]}
                for m in messages]
    # the page state goes in front of the newest question, so it is always the current one
    last = contents[-1]["parts"][0]
    last["text"] = f"[{on_screen}]\n\n{last['text']}"
    return contents


def _wrong_numbers_note(unmatched: list[dict[str, Any]]) -> str:
    shown = ", ".join(dict.fromkeys(u["text"] for u in unmatched))
    return ("Check of your answer: these numbers are not in any tool result of this conversation: "
            f"{shown}. Write the answer again using only numbers copied from tool results (call a tool "
            "if you need a figure), with units and ranges, or leave a figure out. Do not mention this check.")


async def _conversation(request: Request, key: str, model: str, messages: list[dict[str, str]],
                        ctx: advisor_tools.Context, deadline: float, call_limit: float) -> dict[str, Any]:
    """One conversation with one model. -> the body of POST /advisor/chat."""
    on_screen = _on_screen(ctx)
    contents = _contents(messages, on_screen)
    base = {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "tools": [{"functionDeclarations": advisor_tools.declarations()}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": MAX_OUTPUT_TOKENS},
    }
    calls: list[dict[str, Any]] = []
    sources: list[Any] = [ctx.numbers(), [m["content"] for m in messages], on_screen]
    tool_rounds = 0
    corrected = False
    answer, grounding = "", {}
    async with httpx.AsyncClient(timeout=httpx.Timeout(call_limit)) as client:
        while True:
            body = dict(base, contents=contents)
            if tool_rounds >= MAX_TOOL_ROUNDS:         # out of rounds: it must answer with what it has
                body["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}
            if tool_rounds > 0 and await request.is_disconnected():     # the farmer cancelled: stop spending
                raise AdvisorError("Cancelled.", 499, "cancelled")
            left = deadline - time.monotonic()
            if left <= 0:
                raise AdvisorError("The advisor took too long to answer. Try again, or ask a shorter question.",
                                   504, "timeout")
            try:
                reply = await asyncio.wait_for(_generate(client, key, model, body), timeout=left)
            except asyncio.TimeoutError:
                if corrected and answer:
                    break
                raise AdvisorError("The advisor took too long to answer. Try again, or ask a shorter question.",
                                   504, "timeout") from None
            except AdvisorError:
                if corrected and answer:                         # the correction failed: keep the first answer, marked
                    break
                raise
            try:
                parts, finish = _parts(reply)
            except AdvisorError:
                if corrected and answer:
                    break
                raise
            wanted = [p["functionCall"] for p in parts if isinstance(p.get("functionCall"), dict)]
            if wanted:
                tool_rounds += 1
                contents.append({"role": "model", "parts": parts})       # as sent, so thought signatures survive
                responses = []
                for call in wanted[:MAX_CALLS_PER_ROUND]:
                    name, args = str(call.get("name", "")), call.get("args") or {}
                    result, summary, ok = await advisor_tools.run(name, args, request, ctx)
                    calls.append({"name": name, "args": args if isinstance(args, dict) else {},
                                  "summary": summary, "ok": ok})
                    if ok:
                        sources.append(result)
                    sources.append(args)
                    responses.append({"functionResponse": {"name": name, "response": {"result": result}}})
                for call in wanted[MAX_CALLS_PER_ROUND:]:
                    responses.append({"functionResponse": {"name": str(call.get("name", "")), "response": {
                        "result": {"error": "Too many tool calls at once; ask for fewer."}}}})
                contents.append({"role": "user", "parts": responses})
                continue
            text = _text_of(parts)
            if not text:
                if corrected:                                    # keep the first answer, marked
                    break
                if finish == "MAX_TOKENS":
                    raise AdvisorError("The advisor ran out of room before it finished. Try a narrower question.",
                                       502, "empty")
                raise AdvisorError("The advisor gave no answer. Try again.", 502, "empty")
            if finish == "MAX_TOKENS":
                text += " (The answer was cut short.)"
            answer, grounding = text, advisor_grounding.check(text, sources)
            if grounding["passed"] or corrected or deadline - time.monotonic() < 8:
                break
            # numbers the tools never gave: ask once for a corrected answer
            corrected = True
            contents.append({"role": "model", "parts": parts})
            contents.append({"role": "user", "parts": [{"text": _wrong_numbers_note(grounding["unmatched"])}]})
    return {"answer": answer, "tool_calls": calls, "grounding": grounding, "model": model, "corrected": corrected}


async def converse(request: Request, key: str, models: str | list[str], messages: list[dict[str, str]],
                   ctx: advisor_tools.Context) -> dict[str, Any]:
    """Run the conversation to an answer, with the second model when the first fails. -> the body of
    POST /advisor/chat. The whole thing, correction included, has to fit in TOTAL_TIMEOUT_S."""
    chain = [models] if isinstance(models, str) else list(models)
    deadline = time.monotonic() + TOTAL_TIMEOUT_S
    for i, model in enumerate(chain):
        last = i == len(chain) - 1
        try:
            return await _conversation(request, key, model, messages, ctx, deadline,
                                       CALL_TIMEOUT_S if last else FIRST_MODEL_CALL_TIMEOUT_S)
        except AdvisorError as exc:
            if last or exc.code not in FALLBACK_CODES or deadline - time.monotonic() < 10:
                raise
            log.warning("advisor: %s failed (%s); trying %s", model, exc.code, chain[i + 1])
    raise AdvisorError("The advisor gave no answer. Try again.", 502, "empty")   # not reached


def _error(message: str, status: int, code: str) -> JSONResponse:
    return JSONResponse({"detail": message, "code": code}, status_code=status)


@router.post("/advisor/chat", summary="Ask the farm advisor one question")
async def chat(request: Request) -> JSONResponse:
    key = api_key()
    if key is None:
        return _error(NO_KEY_REASON, 503, "no_key")
    try:
        body = await request.json()
    except ValueError:
        return _error("The request was not JSON.", 400, "bad_request")
    if not isinstance(body, dict):
        return _error("The request must be a JSON object.", 400, "bad_request")
    try:
        messages = _clean_messages(body.get("messages"))
        ctx = _clean_context(body.get("context"))
    except BadRequest as exc:
        return _error(str(exc), 400, "bad_request")
    try:
        return JSONResponse(await converse(request, key, model_chain(), messages, ctx))
    except AdvisorError as exc:
        return _error(exc.message, exc.status, exc.code)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("advisor: chat failed: %s: %s", type(exc).__name__, _scrub(str(exc), key)[:200])
        return _error("The advisor hit a problem on the server. Try again.", 500, "server")

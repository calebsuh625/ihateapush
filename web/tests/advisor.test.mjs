// Unit tests for the farm advisor helpers (src/lib/advisor.ts).
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  advisorContext, apiMessages, argsText, errorWords, exampleQuestions, groundingLine, renderAnswer, subjectKey, toolLabel, unmatchedNote,
} from "../src/lib/advisor.ts";
import { DEFAULT_CHOICES } from "../src/lib/planner.ts";

const text = (blocks) => blocks.map((b) => b.runs.map((r) => r.text).join("")).join("|");

test("the context carries the open field, the sliders, the future, the switch and the pumping what-if", () => {
  const choices = { weights: { soil: 3, water: 0, money: 5, risk: 2 }, horizon: 2040, scenario: "ssp585", limitIn: 12 };
  assert.deepEqual(advisorContext({ kind: "field", id: "KS-1" }, ["KS-2"], choices, false), {
    weights: { soil: 3, water: 0, money: 5, risk: 2 }, scenario: "ssp585", horizon: 2040, field_id: "KS-1", irrigated: false, limit_in: 12,
  });
  const farm = advisorContext({ kind: "farm" }, ["KS-2", "KS-3"], DEFAULT_CHOICES, null);
  assert.deepEqual(farm.farm_field_ids, ["KS-2", "KS-3"]);
  assert.equal("field_id" in farm, false);
  assert.equal("irrigated" in farm, false);
  assert.equal("limit_in" in farm, false);
  const field = advisorContext({ kind: "field", id: "KS-1" }, [], DEFAULT_CHOICES, null);
  assert.equal("irrigated" in field, false, "no switch pressed: nothing is sent");
});

test("the subject key tells fields and the farm apart", () => {
  assert.equal(subjectKey(null), null);
  assert.equal(subjectKey({ kind: "farm" }), "farm");
  assert.notEqual(subjectKey({ kind: "field", id: "KS-1" }), subjectKey({ kind: "field", id: "KS-2" }));
});

test("example questions fit irrigated, dryland and the farm", () => {
  const wet = exampleQuestions({ kind: "field", id: "a" }, true);
  const dry = exampleQuestions({ kind: "field", id: "a" }, false);
  const farm = exampleQuestions({ kind: "farm" }, null);
  for (const list of [wet, dry, farm]) assert.ok(list.length >= 3 && list.length <= 4);
  assert.ok(wet.some((q) => /pump|water level/.test(q)));
  assert.ok(!dry.some((q) => /pump|water level/.test(q)), "no pumping questions for dryland");
  assert.ok(farm.every((q) => /farm/.test(q)));
  assert.deepEqual(exampleQuestions(null, null), []);
  assert.ok(exampleQuestions({ kind: "field", id: "a" }, null).length >= 3, "irrigation not known yet: the questions that fit both");
});

test("only what was said goes back to the API", () => {
  const messages = [
    { id: 1, role: "user", text: "hi" },
    { id: 2, role: "assistant", text: "hello", toolCalls: [], grounding: { checked: 0, matched: 0, unmatched: [], passed: true }, model: null },
  ];
  assert.deepEqual(apiMessages(messages), [{ role: "user", text: "hi" }, { role: "assistant", text: "hello" }]);
});

test("an answer is laid out in paragraphs and list items with bold honoured and no marks left over", () => {
  const answer = "Plant **soybeans** next.\n\n* First: $216 an acre\n- Second\n\n## Heading";
  const blocks = renderAnswer(answer);
  assert.deepEqual(blocks.map((b) => b.kind), ["p", "li", "li", "p"]);
  assert.equal(text(blocks), "Plant soybeans next.|First: $216 an acre|Second|Heading");
  assert.deepEqual(blocks[0].runs.map((r) => [r.text, r.bold]), [["Plant ", false], ["soybeans", true], [" next.", false]]);
  assert.equal(blocks[3].runs[0].bold, true, "a heading is bold");
});

test("unmatched numbers are cut out as their own runs, wherever they sit", () => {
  const answer = "It returns **$999** an acre and 12 days.\n* also 7 here";
  const at = (needle) => ({ text: needle, value: 0, start: answer.indexOf(needle), end: answer.indexOf(needle) + needle.length });
  const blocks = renderAnswer(answer, [at("$999"), at("7")]);
  assert.equal(text(blocks), "It returns $999 an acre and 12 days.|also 7 here");
  const flagged = blocks.flatMap((b) => b.runs).filter((r) => r.flag !== null);
  assert.deepEqual(flagged.map((r) => [r.text, r.flag, r.bold]), [["$999", 0, true], ["7", 1, false]]);
  assert.equal(blocks.flatMap((b) => b.runs).filter((r) => r.flag === null && /999|\b7\b/.test(r.text)).length, 0);
});

test("a flagged number inside the text is marked once even when it appears twice", () => {
  const answer = "5 and 5";
  const blocks = renderAnswer(answer, [{ text: "5", value: 5, start: 6, end: 7 }]);
  assert.deepEqual(blocks[0].runs.map((r) => [r.text, r.flag]), [["5 and ", null], ["5", 0]]);
});

test("the notes for unmatched numbers and the check line", () => {
  assert.equal(unmatchedNote([]), null);
  const note = unmatchedNote([{ text: "$9", value: 9, start: 0, end: 2 }, { text: "$9", value: 9, start: 5, end: 7 }, { text: "4", value: 4, start: 9, end: 10 }]);
  assert.match(note, /Not matched to our data: \$9, 4\./);
  assert.equal(groundingLine({ checked: 0, matched: 0, unmatched: [], passed: true }), "No numbers in this answer.");
  assert.match(groundingLine({ checked: 17, matched: 17, unmatched: [], passed: true }), /17 numbers.*all matched/);
  assert.match(groundingLine({ checked: 4, matched: 3, unmatched: [{}], passed: false }), /3 matched, 1 did not/);
});

test("tool and argument words", () => {
  assert.equal(toolLabel("get_plans"), "Top rotation plans");
  assert.equal(toolLabel("something_new"), "something_new");
  assert.equal(argsText({}), "no arguments");
  assert.equal(argsText({ field_id: "KS-1", weights: { soil: 3, risk: 2 }, plan: null }), "field_id: KS-1 · weights: soil 3, risk 2");
});

test("errors read as plain sentences", () => {
  assert.match(errorWords(null, "No response from http://x"), /Could not reach/);
  assert.equal(errorWords(429, "The advisor service is busy."), "The advisor service is busy.");
  assert.match(errorWords(500, "http://x answered 500"), /could not answer/);
  assert.match(errorWords(404, "Not Found"), /does not have the advisor/);
});

// Unit tests for the "Try your own rotation" helpers (src/lib/custom.ts).
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  MAX_YEARS,
  MIN_YEARS,
  OWN_CROPS,
  addSlot,
  canAdd,
  canRemove,
  columnLabel,
  emptySlots,
  isComplete,
  moveSlot,
  rangeText,
  removeSlot,
  rotationParam,
  sameSlots,
  scoreQuery,
  setSlot,
  slotsFrom,
  verdictLabel,
} from "../src/lib/custom.ts";
import { DEFAULT_CHOICES, plansQuery } from "../src/lib/planner.ts";

const [C, B, W, S, A, F] = OWN_CROPS;

test("the six crops are the planner's, in its order", () => {
  assert.deepEqual([...OWN_CROPS], ["corn", "soybeans", "winter wheat", "sorghum", "alfalfa", "fallow"]);
  assert.equal(MIN_YEARS, 2);
  assert.equal(MAX_YEARS, 6);
});

test("a new rotation is four empty years; odd sizes snap into 2-6", () => {
  assert.deepEqual(emptySlots(), [null, null, null, null]);
  assert.equal(emptySlots(1).length, 2);
  assert.equal(emptySlots(9).length, 6);
  assert.equal(emptySlots(Number.NaN).length, 4);
  assert.equal(isComplete(emptySlots()), false);
  assert.equal(rotationParam(emptySlots()), null);
});

test("setting a year takes only the six crops and never changes the input", () => {
  const slots = emptySlots();
  const next = setSlot(slots, 1, S);
  assert.deepEqual(next, [null, S, null, null]);
  assert.deepEqual(slots, [null, null, null, null]);
  assert.deepEqual(setSlot(next, 1, "cotton"), [null, null, null, null]);
  assert.deepEqual(setSlot(next, 1, ""), [null, null, null, null]);
  assert.deepEqual(setSlot(next, 9, C), next);
});

test("moving a year takes it out and puts it back; the others keep their order", () => {
  const slots = [C, S, W, F];
  assert.deepEqual(moveSlot(slots, 0, 1), [S, C, W, F]);
  assert.deepEqual(moveSlot(slots, 3, 0), [F, C, S, W]);
  assert.deepEqual(moveSlot(slots, 0, 2), [S, W, C, F]);
  assert.deepEqual(moveSlot(slots, 1, 99), [C, W, F, S]);
  assert.deepEqual(moveSlot(slots, 1, -5), [S, C, W, F]);
  assert.deepEqual(moveSlot(slots, 2, 2), slots);
  assert.deepEqual(moveSlot(slots, 7, 0), slots);
  assert.deepEqual(moveSlot(slots, 0.5, 1), slots);
  assert.deepEqual(slots, [C, S, W, F]);
  assert.deepEqual(moveSlot([C, null, W], 1, 0), [null, C, W]);
});

test("years can be added up to six and removed down to two", () => {
  let slots = [C, S];
  assert.equal(canRemove(slots), false);
  assert.deepEqual(removeSlot(slots, 0), [C, S]);
  for (let n = 3; n <= 6; n++) {
    assert.equal(canAdd(slots), true);
    slots = addSlot(slots);
    assert.equal(slots.length, n);
    assert.equal(slots.at(-1), null);
  }
  assert.equal(canAdd(slots), false);
  assert.equal(addSlot(slots).length, 6);
  assert.deepEqual(removeSlot([C, S, W], 1), [C, W]);
  assert.deepEqual(removeSlot([C, S, W], 5), [C, S, W]);
});

test("a rotation from the API becomes slots only when it is 2 to 6 of the six crops", () => {
  assert.deepEqual(slotsFrom([W, S, F]), [W, S, F]);
  assert.deepEqual(slotsFrom([A, A, A, A, C, W]), [A, A, A, A, C, W]);
  assert.equal(slotsFrom([C]), null);
  assert.equal(slotsFrom([C, C, C, C, C, C, C]), null);
  assert.equal(slotsFrom([C, "cotton"]), null);
  assert.equal(slotsFrom(null), null);
  assert.equal(slotsFrom(undefined), null);
  assert.equal(slotsFrom("corn"), null);
});

test("the score query is the rotation, then the plan cards' own query", () => {
  const settings = plansQuery(DEFAULT_CHOICES);
  assert.equal(scoreQuery(settings, [C, S, W, F]), `rotation=corn%2Csorghum%2Cwinter%20wheat%2Cfallow&${settings}`);
  assert.equal(scoreQuery(settings, [C, null, W, F]), null);
  assert.equal(scoreQuery(settings, "  wheat, milo, idle "), `rotation=wheat%2C%20milo%2C%20idle&${settings}`);
  assert.equal(scoreQuery(settings, "   "), null);
  assert.equal(scoreQuery("", [C, B]), "rotation=corn%2Csoybeans");
  assert.equal(scoreQuery(plansQuery(DEFAULT_CHOICES, true), [C, B]).endsWith("&irrigated=true"), true);
  assert.equal(rotationParam([C, B]), "corn,soybeans");
});

test("two sets of slots are the same when every year matches", () => {
  assert.equal(sameSlots([C, null], [C, null]), true);
  assert.equal(sameSlots([C, null], [C, S]), false);
  assert.equal(sameSlots([C, S], [C, S, W]), false);
  assert.equal(sameSlots(null, null), true);
  assert.equal(sameSlots(null, [C, S]), false);
});

test("labels for the table and the comparison", () => {
  assert.deepEqual([0, 1, 2, 3].map(columnLabel), ["Yours", "Our 1", "Our 2", "Our 3"]);
  assert.equal(verdictLabel("ours"), "Ours is ahead");
  assert.equal(verdictLabel("yours"), "Yours is ahead");
  assert.equal(verdictLabel("too_close"), "Too close to call");
  assert.equal(verdictLabel("same"), "The same");
  assert.equal(rangeText("$17", "$54"), "$17 to $54");
  assert.equal(rangeText("−$14", "−$14"), null);
});

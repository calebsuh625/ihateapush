import { api } from "../api";
import type { FieldPlans } from "../types";
import { useLatest } from "./useLatest";
import type { LatestState } from "./useLatest";

/** `resource` holds the cards on screen; see useLatest for `shownQuery`, `updating` and `updateError`. */
export type PlansState = LatestState<FieldPlans>;

/**
 * Rotation plans for one field and one set of planner choices (`query`, see lib/planner.ts).
 *
 * The debounce, the stale-answer rule and "keep the previous cards while a newer answer is on its
 * way" live in useLatest, which the "Try your own rotation" section shares.
 */
export function usePlans(fieldId: string, query: string): PlansState {
  return useLatest(fieldId, query, api.plans);
}

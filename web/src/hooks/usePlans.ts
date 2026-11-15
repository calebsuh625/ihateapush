import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api, isAbort } from "../api";
import { DEBOUNCE_MS } from "../lib/planner";
import type { FieldPlans } from "../types";
import type { Resource } from "./useResource";

interface Answer {
  fieldId: string;
  query: string;
  data: FieldPlans;
}

interface Failure {
  fieldId: string;
  query: string;
  attempt: number;
  message: string;
  notFound: boolean;
}

export interface PlansState {
  /** Like useResource: "loading" until the first answer for this field, "error" if that fails. */
  resource: Resource<FieldPlans>;
  /** The query the cards on screen answer; null until there is an answer for this field. */
  shownQuery: string | null;
  /** True while the cards on screen are for older settings than the ones now chosen. */
  updating: boolean;
  /** The latest settings could not be loaded; the previous cards stay on screen. */
  updateError: string | null;
  retry: () => void;
}

/**
 * Rotation plans for one field and one set of planner choices (`query`, see lib/planner.ts).
 *
 * - The first request for a field goes out at once; later ones (a slider being dragged) wait
 *   DEBOUNCE_MS, so only the value the user stops on is asked for.
 * - A newer request aborts the one in flight, and an answer is only kept when it is still the
 *   latest (a sequence number), so a slow answer can never replace a newer one.
 * - While a newer answer is on its way the previous one for the same field stays in `resource`,
 *   with `updating` true, so the cards do not flicker or jump.
 */
export function usePlans(fieldId: string, query: string): PlansState {
  const [attempt, setAttempt] = useState(0);
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  const sequence = useRef(0);
  // The field whose first request has been sent: only that one skips the debounce.
  const started = useRef<string | null>(null);

  useEffect(() => {
    const mine = ++sequence.current;
    const controller = new AbortController();
    const latest = () => mine === sequence.current && !controller.signal.aborted;
    const send = () => {
      started.current = fieldId;
      api.plans(fieldId, controller.signal, query).then(
        (data) => {
          if (!latest()) return;
          setAnswer({ fieldId, query, data });
          setFailure(null);
        },
        (err: unknown) => {
          if (!latest() || isAbort(err)) return;
          const message = err instanceof Error ? err.message : String(err);
          setFailure({ fieldId, query, attempt, message, notFound: err instanceof ApiError && err.status === 404 });
        },
      );
    };
    const timer = window.setTimeout(send, started.current === fieldId ? DEBOUNCE_MS : 0);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [fieldId, query, attempt]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  const shown = answer !== null && answer.fieldId === fieldId ? answer : null;
  const failed = failure !== null && failure.fieldId === fieldId && failure.query === query && failure.attempt === attempt ? failure : null;

  if (shown === null) {
    return {
      resource: failed ? { status: "error", message: failed.message, notFound: failed.notFound } : { status: "loading" },
      shownQuery: null,
      updating: false,
      updateError: null,
      retry,
    };
  }
  const current = shown.query === query;
  return {
    resource: { status: "ready", data: shown.data },
    shownQuery: shown.query,
    updating: !current && failed === null,
    updateError: !current && failed !== null ? failed.message : null,
    retry,
  };
}

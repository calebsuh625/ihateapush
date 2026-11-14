import { useCallback, useEffect, useState } from "react";
import { ApiError, isAbort } from "../api";

export type Resource<T> =
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "error"; message: string; notFound: boolean };

const LOADING: Resource<never> = { status: "loading" };

interface Settled<T> {
  key: string;
  attempt: number;
  resource: Resource<T>;
}

/**
 * Load one thing identified by `key`, with retry.
 *
 * - A new key (or a retry) aborts the request in flight, so a slow answer for field A can never
 *   land after the user has moved on to field B.
 * - What is returned is derived from the key the answer belongs to: until the answer for the
 *   *current* key arrives the resource reads "loading", whatever is still in state.
 * - Safe under React StrictMode: the first of the doubled effects is aborted and ignored.
 *
 * `load` must be a stable function (module-level or memoised). Pass `key = null` to load nothing.
 */
export function useResource<T>(
  key: string | null,
  load: (key: string, signal: AbortSignal) => Promise<T>,
): [resource: Resource<T>, retry: () => void] {
  const [attempt, setAttempt] = useState(0);
  const [settled, setSettled] = useState<Settled<T> | null>(null);

  useEffect(() => {
    if (key === null) return;
    const controller = new AbortController();
    load(key, controller.signal).then(
      (data) => {
        if (controller.signal.aborted) return;
        setSettled({ key, attempt, resource: { status: "ready", data } });
      },
      (err: unknown) => {
        if (controller.signal.aborted || isAbort(err)) return;
        const message = err instanceof Error ? err.message : String(err);
        const notFound = err instanceof ApiError && err.status === 404;
        setSettled({ key, attempt, resource: { status: "error", message, notFound } });
      },
    );
    return () => controller.abort();
  }, [key, attempt, load]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);
  const current = settled !== null && settled.key === key && settled.attempt === attempt;
  return [current ? settled.resource : LOADING, retry];
}

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api, isAbort } from "../api";
import { apiMessages, errorWords, subjectKey } from "../lib/advisor";
import type { AdvisorContext, ChatMessage, Subject } from "../lib/advisor";

export interface Failure {
  kind: "error" | "cancelled";
  text: string;
}

interface State {
  /** The field or farm this conversation belongs to (`subjectKey`); null before anything was open. */
  key: string | null;
  messages: ChatMessage[];
  /** When the question in flight was sent (ms since 1970), or null. */
  pendingSince: number | null;
  failure: Failure | null;
  /** Said once when a conversation was cleared because another field or farm was opened. */
  notice: string | null;
}

const EMPTY: State = { key: null, messages: [], pendingSince: null, failure: null, notice: null };

export interface Advisor {
  messages: ChatMessage[];
  pendingSince: number | null;
  failure: Failure | null;
  notice: string | null;
  send: (text: string) => void;
  cancel: () => void;
  retry: () => void;
  clear: () => void;
}

const subjectWords = (subject: Subject) => (subject?.kind === "field" ? `field ${subject.id}` : "the farm");

/**
 * The farm advisor's conversation. It lives in the app, not in the panel, so it is kept while the panel is
 * closed; it belongs to one field or to the farm and is cleared (with a notice) when another is opened.
 * `subject` is null while nothing is open: the conversation is then left as it is.
 */
export function useAdvisor(subject: Subject, context: AdvisorContext): Advisor {
  const key = subjectKey(subject);
  const [state, setState] = useState<State>(EMPTY);
  const controller = useRef<AbortController | null>(null);
  const contextRef = useRef(context);
  const stateRef = useRef(state);
  const nextId = useRef(1);
  useEffect(() => {
    contextRef.current = context;
    stateRef.current = state;
  });

  // Another field or the farm: forget the conversation and say so. (Until this runs, `shown` below
  // already reads as empty, so the old answers never show under the new field.)
  useEffect(() => {
    if (key === null) return;
    setState((current) => {
      if (current.key === key) return current;
      controller.current?.abort();
      controller.current = null;
      const had = current.key !== null && current.messages.length > 0;
      return { ...EMPTY, key, notice: had ? `New conversation: you opened ${subjectWords(subject)}, so the earlier questions were cleared.` : null };
    });
  }, [key, subject]);

  useEffect(() => () => controller.current?.abort(), []);

  const run = useCallback(
    (messages: ChatMessage[]) => {
      if (key === null) return;
      controller.current?.abort();
      const mine = new AbortController();
      controller.current = mine;
      const body = JSON.stringify({ messages: apiMessages(messages), context: contextRef.current });
      setState((current) => ({ ...current, key, messages, pendingSince: Date.now(), failure: null, notice: null }));
      api.advisorChat(body, mine.signal).then(
        (reply) => {
          if (mine.signal.aborted) return;
          const answer: ChatMessage = {
            id: nextId.current++,
            role: "assistant",
            text: reply.answer,
            toolCalls: reply.tool_calls,
            grounding: reply.grounding,
            model: reply.model ?? null,
          };
          setState((current) => (current.key !== key ? current : { ...current, messages: [...current.messages, answer], pendingSince: null }));
        },
        (err: unknown) => {
          if (mine.signal.aborted || isAbort(err)) return;
          const status = err instanceof ApiError ? err.status : null;
          const detail = err instanceof Error ? err.message : "";
          setState((current) =>
            current.key !== key ? current : { ...current, pendingSince: null, failure: { kind: "error", text: errorWords(status, detail) } },
          );
        },
      );
    },
    [key],
  );

  const send = useCallback(
    (text: string) => {
      const clean = text.trim();
      const current = stateRef.current;
      if (clean === "" || current.pendingSince !== null || key === null) return;
      const base = current.key === key ? current.messages : [];
      run([...base, { id: nextId.current++, role: "user", text: clean }]);
    },
    [key, run],
  );

  const cancel = useCallback(() => {
    controller.current?.abort();
    controller.current = null;
    setState((current) =>
      current.pendingSince === null ? current : { ...current, pendingSince: null, failure: { kind: "cancelled", text: "Cancelled. Nothing was added." } },
    );
  }, []);

  /** Ask the last question again (after an error or a cancel). */
  const retry = useCallback(() => {
    const current = stateRef.current;
    if (current.key !== key || current.pendingSince !== null) return;
    const last = current.messages.at(-1);
    if (last?.role === "user") run(current.messages);
  }, [key, run]);

  const clear = useCallback(() => {
    controller.current?.abort();
    controller.current = null;
    setState((current) => ({ ...EMPTY, key: current.key }));
  }, []);

  const shown = key !== null && state.key !== key ? EMPTY : state;
  return { messages: shown.messages, pendingSince: shown.pendingSince, failure: shown.failure, notice: shown.notice, send, cancel, retry, clear };
}

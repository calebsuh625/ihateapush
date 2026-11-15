import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent, ReactNode } from "react";
import type { Resource } from "../hooks/useResource";
import type { Advisor } from "../hooks/useAdvisor";
import { NOT_MATCHED_NOTE, argsText, groundingLine, renderAnswer, toolLabel, unmatchedNote } from "../lib/advisor";
import type { AdvisorStatus, ChatMessage, Subject } from "../lib/advisor";
import "./Advisor.css";

interface AdvisorPanelProps {
  subject: Exclude<Subject, null>;
  status: Resource<AdvisorStatus>;
  onRetryStatus: () => void;
  advisor: Advisor;
  examples: string[];
  onClose: () => void;
}

const CLOSE_ICON = (
  <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
    <path d="M3.5 3.5l9 9m0-9l-9 9" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" fill="none" />
  </svg>
);

/** "Farm advisor": a chat that answers from the app's own tools. Docks beside the field panel on a wide
 *  screen, slides over it on a narrower one, fills the screen on a phone. */
export function AdvisorPanel({ subject, status, onRetryStatus, advisor, examples, onClose }: AdvisorPanelProps) {
  const title = subject.kind === "field" ? `About field ${subject.id}` : "About your farm";
  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const [draft, setDraft] = useState("");
  const available = status.status === "ready" && status.data.available;
  const pending = advisor.pendingSince !== null;

  // Newest at the bottom: keep it in view as messages, the waiting line or an error arrive.
  const count = advisor.messages.length;
  useEffect(() => {
    const list = listRef.current;
    if (list) list.scrollTop = list.scrollHeight;
  }, [count, pending, advisor.failure]);

  useEffect(() => {
    if (available) inputRef.current?.focus();
  }, [available]);

  const submit = (text: string) => {
    if (text.trim() === "" || pending) return;
    advisor.send(text);
    setDraft("");
  };
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit(draft);
    }
  };

  const state = status.status === "ready" ? (status.data.available ? "ready" : "no-key") : status.status;

  return (
    <aside className="advisor" aria-labelledby="advisor-title" data-testid="advisor" data-state={state} data-subject={subject.kind === "field" ? subject.id : "farm"}>
      <header className="advisor-head">
        <div>
          <p className="eyebrow">Farm advisor</p>
          <h2 id="advisor-title" data-testid="advisor-title">
            {title}
          </h2>
        </div>
        <button type="button" className="icon-button" onClick={onClose} aria-label="Close the farm advisor" title="Close the advisor (the conversation is kept)" data-testid="advisor-close">
          {CLOSE_ICON}
        </button>
      </header>

      {status.status === "loading" && (
        <p className="advisor-note" role="status" data-testid="advisor-checking">
          <span className="spinner" aria-hidden="true" /> Checking the advisor…
        </p>
      )}

      {status.status === "error" && (
        <div className="advisor-body">
          <div className="error-note" role="alert" data-testid="advisor-status-error">
            <div>
              <p className="error-title">Could not check the advisor</p>
              <p className="error-message">{status.message}</p>
            </div>
            <button type="button" className="button" onClick={onRetryStatus}>
              Retry
            </button>
          </div>
        </div>
      )}

      {status.status === "ready" && !status.data.available && (
        <div className="advisor-body" data-testid="advisor-no-key">
          <p className="advisor-nokey-title">{status.data.message ?? "Add an API key to use the advisor"}</p>
          <p>
            Put this line in the file named <code>{status.data.env_file ?? ".env"}</code> in the project folder, with your own key after the equals sign:
          </p>
          <pre className="advisor-line" data-testid="advisor-env-line">
            {status.data.env_line ?? "GEMINI_API_KEY=your-key-here"}
          </pre>
          <p className="muted">Then close this panel and open it again. Nothing else in the app depends on the advisor.</p>
        </div>
      )}

      {available && (
        <>
          <div className="advisor-list" ref={listRef} role="log" aria-live="polite" aria-label="Conversation with the farm advisor" data-testid="advisor-messages">
            {advisor.notice && (
              <p className="advisor-notice" data-testid="advisor-notice">
                {advisor.notice}
              </p>
            )}
            {advisor.messages.length === 0 && !pending && (
              <div className="advisor-empty" data-testid="advisor-empty">
                <p>
                  Ask about {subject.kind === "field" ? "this field" : "this farm"} in your own words. Answers use the same plans, climate models and
                  well records as the rest of the app, with the sliders and settings you have now.
                </p>
              </div>
            )}
            {advisor.messages.map((message) => (
              <Message key={message.id} message={message} />
            ))}
            {pending && <Waiting since={advisor.pendingSince ?? 0} onCancel={advisor.cancel} />}
            {advisor.failure && !pending && (
              <div className={advisor.failure.kind === "error" ? "error-note advisor-failure" : "advisor-failure advisor-cancelled"} role="alert" data-testid="advisor-error" data-kind={advisor.failure.kind}>
                <div>
                  {advisor.failure.kind === "error" && <p className="error-title">The advisor could not answer</p>}
                  <p className={advisor.failure.kind === "error" ? "error-message" : undefined}>{advisor.failure.text}</p>
                </div>
                <button type="button" className="button" onClick={advisor.retry} data-testid="advisor-retry">
                  {advisor.failure.kind === "error" ? "Retry" : "Ask again"}
                </button>
              </div>
            )}
          </div>

          <div className="advisor-foot">
            {advisor.messages.length === 0 && !pending && examples.length > 0 && (
              <ul className="advisor-chips" data-testid="advisor-examples" aria-label="Example questions">
                {examples.map((question) => (
                  <li key={question}>
                    <button type="button" className="advisor-chip" data-testid="advisor-example" onClick={() => submit(question)}>
                      {question}
                    </button>
                  </li>
                ))}
              </ul>
            )}
            <form
              className="advisor-form"
              onSubmit={(event) => {
                event.preventDefault();
                submit(draft);
              }}
            >
              <label className="sr-only" htmlFor="advisor-input">
                Your question for the farm advisor
              </label>
              <textarea
                id="advisor-input"
                ref={inputRef}
                className="advisor-input"
                data-testid="advisor-input"
                rows={2}
                maxLength={2000}
                value={draft}
                placeholder="Ask a question (Enter to send, Shift+Enter for a new line)"
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={onKeyDown}
              />
              <button type="submit" className="button" data-testid="advisor-send" disabled={pending || draft.trim() === ""}>
                Send
              </button>
            </form>
            <p className="advisor-small" data-testid="advisor-small">
              The advisor can make mistakes: numbers it cannot match to the app's data are marked. This conversation is kept while you close the panel and
              cleared when you open another field or the farm.
              {advisor.messages.length > 0 && (
                <>
                  {" "}
                  <button type="button" className="link-button" data-testid="advisor-clear" onClick={advisor.clear} disabled={pending}>
                    Clear now
                  </button>
                </>
              )}
            </p>
          </div>
        </>
      )}
    </aside>
  );
}

function Waiting({ since, onCancel }: { since: number; onCancel: () => void }) {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    const tick = () => setSeconds(Math.max(0, Math.floor((Date.now() - since) / 1000)));
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [since]);
  return (
    <div className="advisor-waiting" role="status" data-testid="advisor-waiting">
      <span className="spinner" aria-hidden="true" />
      <span>
        Looking at the plans and numbers… <span className="tabular">{seconds} s</span>
        <span className="muted"> (it can take up to a minute)</span>
      </span>
      <button type="button" className="link-button" data-testid="advisor-cancel" onClick={onCancel}>
        Cancel
      </button>
    </div>
  );
}

function Message({ message }: { message: ChatMessage }) {
  if (message.role === "user") {
    return (
      <div className="advisor-msg advisor-msg--user" data-testid="advisor-msg" data-role="user">
        <p>{message.text}</p>
      </div>
    );
  }
  const blocks = renderAnswer(message.text, message.grounding.unmatched);
  const note = unmatchedNote(message.grounding.unmatched);
  const calls = message.toolCalls;
  return (
    <div className="advisor-msg advisor-msg--assistant" data-testid="advisor-msg" data-role="assistant" data-unmatched={message.grounding.unmatched.length}>
      <div className="advisor-text" data-testid="advisor-answer">
        {renderBlocks(blocks)}
      </div>
      {note && (
        <p className="advisor-unmatched-note" data-testid="advisor-unmatched-note">
          <strong aria-hidden="true">?</strong> {note}
        </p>
      )}
      <details className="advisor-based" data-testid="advisor-based">
        <summary>
          Based on {calls.length === 0 ? "no tools" : `${calls.length} ${calls.length === 1 ? "tool call" : "tool calls"}`}
        </summary>
        {calls.length === 0 ? (
          <p className="advisor-small">The advisor answered without looking anything up, so none of its numbers come from the app.</p>
        ) : (
          <ol className="advisor-calls">
            {calls.map((call, index) => (
              <li key={index} data-testid="advisor-call" data-tool={call.name} data-ok={call.ok}>
                <p>
                  <strong>{toolLabel(call.name)}</strong> <code>{call.name}</code>
                  {!call.ok && <span className="advisor-failed"> · did not answer</span>}
                </p>
                <p className="advisor-args tabular">{argsText(call.args)}</p>
                <p className="muted">{call.summary}</p>
              </li>
            ))}
          </ol>
        )}
        <p className="advisor-small" data-testid="advisor-grounding">
          {groundingLine(message.grounding)}
        </p>
      </details>
    </div>
  );
}

function renderBlocks(blocks: ReturnType<typeof renderAnswer>) {
  const inline = (runs: (typeof blocks)[number]["runs"], key: number) =>
    runs.map((run, i) => {
      const body = run.flag === null ? run.text : (
        <mark className="advisor-unmatched" data-testid="advisor-unmatched" title={NOT_MATCHED_NOTE}>
          {run.text}
          <span className="sr-only"> (not matched to our data)</span>
        </mark>
      );
      return run.bold ? <strong key={`${key}-${i}`}>{body}</strong> : <span key={`${key}-${i}`}>{body}</span>;
    });
  const out: ReactNode[] = [];
  let items: { key: number; node: ReturnType<typeof inline> }[] = [];
  const flush = (at: number) => {
    if (items.length === 0) return;
    out.push(
      <ul key={`ul-${at}`}>
        {items.map((item) => (
          <li key={item.key}>{item.node}</li>
        ))}
      </ul>,
    );
    items = [];
  };
  blocks.forEach((block, index) => {
    if (block.kind === "li") {
      items.push({ key: index, node: inline(block.runs, index) });
      return;
    }
    flush(index);
    out.push(<p key={index}>{inline(block.runs, index)}</p>);
  });
  flush(blocks.length);
  return out;
}

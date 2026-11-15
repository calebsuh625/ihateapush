import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";

interface Props {
  /** What this part of the page is, for the sentence: "the water outlook". */
  what: string;
  /** When this changes (another field, say) the boundary tries its children again. */
  resetKey?: string | number | null;
  /** "page" is the last resort for the whole app; "panel" stands in for a side panel; "section" is a note in place of one section. */
  level?: "section" | "panel" | "page";
  children: ReactNode;
}

interface State {
  failed: boolean;
  key: Props["resetKey"];
}

/**
 * A drawing mistake in one part of the page (an answer shaped differently from what the page expects) must not
 * blank the whole app. The part that failed is replaced by one plain sentence and a button; everything else,
 * the map included, keeps working. The error is still logged to the console for whoever is looking.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { failed: false, key: this.props.resetKey };

  static getDerivedStateFromError(): Partial<State> {
    return { failed: true };
  }

  static getDerivedStateFromProps(props: Props, state: State): Partial<State> | null {
    return props.resetKey !== state.key ? { failed: false, key: props.resetKey } : null;
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`Could not show ${this.props.what}:`, error, info.componentStack);
  }

  render() {
    if (!this.state.failed) return this.props.children;
    const { what, level = "section" } = this.props;
    const note = (
      <div className={level === "page" ? "error-note boundary boundary--page" : "error-note boundary"} role="alert" data-testid="boundary">
        <div>
          <p className="error-title">Could not show {what}</p>
          <p className="error-message">
            Something in the answer was not what this page expects. The rest of the page still works.
            {level === "page" ? " Reload the page to start again." : ""}
          </p>
        </div>
        <button type="button" className="button" onClick={() => (level === "page" ? window.location.reload() : this.setState({ failed: false }))}>
          {level === "page" ? "Reload" : "Try again"}
        </button>
      </div>
    );
    return level === "panel" ? <aside className="panel boundary-panel">{note}</aside> : note;
  }
}

// "Copy link", "Print this plan" and "Download CSV": one row at the top of a field or farm panel.
import { useEffect, useRef, useState } from "react";
import "./Share.css";

interface ShareBarProps {
  /** The link that reopens this exact view. */
  url: string;
  /** What the print button is about: "plan" or "farm plan". */
  printWhat: string;
  /** False while there is no answer to print or export yet. */
  ready: boolean;
  /** The CSV text and its file name, made when the button is clicked. Null: no CSV for this view. */
  csv: (() => { text: string; name: string }) | null;
  testId?: string;
}

/** Hand the text to the browser as a file; nothing is sent anywhere. */
export function downloadText(text: string, name: string, type = "text/csv;charset=utf-8"): void {
  const blob = new Blob([text], { type });
  const href = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = href;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(href), 1000);
}

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // no clipboard permission (or an insecure page): fall back to selecting a hidden box
  }
  try {
    const box = document.createElement("textarea");
    box.value = text;
    box.setAttribute("readonly", "");
    box.style.position = "fixed";
    box.style.opacity = "0";
    document.body.appendChild(box);
    box.select();
    const ok = document.execCommand("copy");
    box.remove();
    return ok;
  } catch {
    return false;
  }
}

export function ShareBar({ url, printWhat, ready, csv, testId = "share-bar" }: ShareBarProps) {
  const [note, setNote] = useState<{ text: string; bad: boolean } | null>(null);
  const timer = useRef<number | null>(null);
  useEffect(() => () => { if (timer.current !== null) window.clearTimeout(timer.current); }, []);
  const say = (text: string, bad = false) => {
    setNote({ text, bad });
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setNote(null), bad ? 12000 : 3000);
  };
  const copy = async () => {
    const ok = await copyText(url);
    say(ok ? "Link copied" : "Could not copy. The link is in the box below.", !ok);
  };
  return (
    <div className="share-bar" data-testid={testId} role="group" aria-label="Share and export">
      <button type="button" className="button button--small" data-testid="share-copy" onClick={copy} title="Copy a link that opens this same view, with these settings">
        Copy link
      </button>
      <button type="button" className="button button--small" data-testid="share-print" disabled={!ready} onClick={() => window.print()} title={`Print this ${printWhat}, or save it as a PDF from the print window`}>
        Print this {printWhat}
      </button>
      {csv && (
        <button
          type="button"
          className="button button--small"
          data-testid="share-csv"
          disabled={!ready}
          onClick={() => {
            const file = csv();
            downloadText(file.text, file.name);
            say(`Saved ${file.name}`);
          }}
          title="Download the numbers on the cards as a spreadsheet file (CSV)"
        >
          Download CSV
        </button>
      )}
      <p className={note?.bad ? "share-note is-bad" : "share-note"} role="status" data-testid="share-note">
        {note?.text ?? ""}
      </p>
      {note?.bad && <input className="share-url" readOnly value={url} aria-label="Link to this view" data-testid="share-url" onFocus={(e) => e.currentTarget.select()} />}
    </div>
  );
}

import "./Advisor.css";

/** "Farm advisor" in the header of the field panel and the farm panel. */
export function AdvisorButton({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      className={open ? "toggle-button is-on advisor-open" : "toggle-button advisor-open"}
      aria-pressed={open}
      data-testid="advisor-open"
      onClick={onToggle}
    >
      Farm advisor
    </button>
  );
}

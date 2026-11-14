import { useCallback, useEffect, useState } from "react";

/**
 * The selected field. `via` records where the selection came from, because the map reacts
 * differently: a click on the map keeps the user's view, a link (#KS-000123) zooms to the field.
 */
export interface Selection {
  id: string;
  via: "map" | "url";
}

function readHash(): string | null {
  const raw = window.location.hash.replace(/^#/, "");
  if (!raw) return null;
  try {
    return decodeURIComponent(raw).trim() || null;
  } catch {
    return raw.trim() || null; // malformed escape sequence: use it as typed
  }
}

/** Selection state mirrored in the URL hash, so a field can be linked to and Back works. */
export function useSelection() {
  const [selection, setSelection] = useState<Selection | null>(() => {
    const id = readHash();
    return id ? { id, via: "url" } : null;
  });

  useEffect(() => {
    const syncFromUrl = () => {
      const id = readHash();
      setSelection((current) => {
        if ((current?.id ?? null) === id) return current;
        return id ? { id, via: "url" } : null;
      });
    };
    window.addEventListener("hashchange", syncFromUrl);
    window.addEventListener("popstate", syncFromUrl);
    return () => {
      window.removeEventListener("hashchange", syncFromUrl);
      window.removeEventListener("popstate", syncFromUrl);
    };
  }, []);

  const select = useCallback((id: string) => {
    setSelection((current) => (current?.id === id ? current : { id, via: "map" }));
    if (readHash() !== id) window.history.pushState(null, "", `#${encodeURIComponent(id)}`);
  }, []);

  const clear = useCallback(() => {
    setSelection(null);
    if (window.location.hash) {
      window.history.pushState(null, "", window.location.pathname + window.location.search);
    }
  }, []);

  return { selection, select, clear };
}

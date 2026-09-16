// jsdom has no layout and no window.matchMedia. Tests pick a viewport width instead, and media queries of the
// form "(min-width: Npx)" / "(max-width: Npx)", joined by "and", answer for it — and tell their listeners when a
// width change flips them, as a real browser would on resize.

/** The width every test starts at: the 1440 px desktop project. */
export const DEFAULT_VIEWPORT_WIDTH = 1440;

interface FakeMediaQueryList extends MediaQueryList {
  notify(): void;
}

let width = DEFAULT_VIEWPORT_WIDTH;
const lists = new Map<string, FakeMediaQueryList>();

function evaluate(query: string, viewport: number): boolean {
  return query.split(/\s+and\s+/i).every((part) => {
    const match = part.match(/\((min|max)-width:\s*(\d+(?:\.\d+)?)px\)/);
    if (!match) return false;
    const bound = Number(match[2]);
    return match[1] === "min" ? viewport >= bound : viewport <= bound;
  });
}

function createList(query: string): FakeMediaQueryList {
  const listeners = new Set<(event: MediaQueryListEvent) => void>();
  let last = evaluate(query, width);
  const list = {
    media: query,
    onchange: null,
    get matches() { return evaluate(query, width); },
    addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => { listeners.add(listener); },
    removeEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => { listeners.delete(listener); },
    addListener: (listener: (event: MediaQueryListEvent) => void) => { listeners.add(listener); },
    removeListener: (listener: (event: MediaQueryListEvent) => void) => { listeners.delete(listener); },
    dispatchEvent: () => true,
    notify() {
      const now = evaluate(query, width);
      if (now === last) return;
      last = now;
      const event = { matches: now, media: query } as MediaQueryListEvent;
      for (const listener of [...listeners]) listener(event);
    },
  };
  return list as unknown as FakeMediaQueryList;
}

/** Install the fake window.matchMedia at the default width. Called before every test by setup.ts. */
export function installViewport(): void {
  width = DEFAULT_VIEWPORT_WIDTH;
  lists.clear();
  window.matchMedia = (query: string) => {
    let list = lists.get(query);
    if (!list) {
      list = createList(query);
      lists.set(query, list);
    }
    return list;
  };
}

/** Resize the fake viewport. Wrap in act() once something is rendered: listeners re-render the components. */
export function setViewportWidth(next: number): void {
  width = next;
  for (const list of lists.values()) list.notify();
}

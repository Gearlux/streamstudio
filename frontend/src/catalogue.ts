/**
 * The catalogue — `GET api/palette`, every class the editor can describe — is asked for on FIRST NEED, never as the
 * page opens. Four surfaces list it: the Nodes sidebar, Add Node ▸ (the canvas's right-click menu), the palette (a
 * double-click on empty canvas, a wire dropped on nothing) and Set source…; each shows "loading the catalogue…" until it
 * has arrived. The first one shown asks; the answer is kept for the page's life.
 *
 * Why not at open: the server builds the catalogue by importing every installed package that declares classes (torch,
 * transformers, the detection backends — 6.5–8.4 s, measured 2026-10-02), and the request ran BESIDE the page's first
 * `GET api/graphs`, which then waited on the same imports: 7.2 s for the graph list in a host app, where the list alone
 * takes 1.2 s. A page opened to look at a graph, or to change a value, never pays for the catalogue now.
 */

/** The surfaces the App holds as state; Add Node ▸ asks when its menu is drawn (`menu/AddNodeMenu.tsx`). */
export interface CatalogueShown {
  /** The Nodes sidebar is open — a page that starts with it open (it is remembered) needs the catalogue at once. */
  sidebar: boolean;
  /** The palette is up. */
  palette: boolean;
  /** Set source… is up: its class picker lists the catalogue's classes that fit the slot. */
  setSource: boolean;
}

/** The page as it opens: nothing that lists the catalogue is shown. */
export const NOTHING_SHOWN: CatalogueShown = { sidebar: false, palette: false, setSource: false };

/** Does anything shown list the catalogue. */
export const needsCatalogue = (shown: CatalogueShown): boolean => shown.sidebar || shown.palette || shown.setSource;

/**
 * A need that asks `fetch` the first time it is called and then never again — while the answer is on its way and after
 * it arrived. A failed answer goes to `failed` and is forgotten, so the next need asks again.
 *
 * Returns the need: it answers whether THIS call asked.
 */
export function onFirstNeed<T>(fetch: () => Promise<T>, arrived: (value: T) => void, failed: (exc: unknown) => void): () => boolean {
  let state: "idle" | "asking" | "arrived" = "idle";
  return () => {
    if (state !== "idle") return false;
    state = "asking";
    fetch().then(
      (value) => {
        state = "arrived";
        arrived(value);
      },
      (exc: unknown) => {
        state = "idle";
        failed(exc);
      },
    );
    return true;
  };
}

/**
 * A class's settings (`GET api/palette/<class>`) are not in the catalogue's list — they were 94 % of its 11.6 MB, and a
 * person needs them for the one class they SELECT: the class picked in Set source…, the class whose card the Nodes
 * sidebar shows. `oncePerClass(fetch)` asks `fetch` the first time a class is selected and hands every later selection
 * of it the same answer; a refused answer is forgotten, so the next selection asks again.
 */
export function oncePerClass<T>(fetch: (classPath: string) => Promise<T>): (classPath: string) => Promise<T> {
  const asked = new Map<string, Promise<T>>();
  return (classPath) => {
    const known = asked.get(classPath);
    if (known !== undefined) return known;
    const answer = fetch(classPath);
    asked.set(classPath, answer);
    answer.catch(() => asked.delete(classPath));
    return answer;
  };
}

/**
 * The standalone editor's ⏻ and ↻ — stop the server, and restart it with the code on disk now.
 *
 * The buttons are not the page's own: the framework that serves the standalone editor ships a page shell
 * (`portail/shell.js`, beside the page) that knows the server, stops and restarts it as `streamstudio stop` /
 * `streamstudio restart` do, and tells every open page when the server goes. The page loads that script and names the
 * element the buttons go into.
 *
 * Only a page that is its OWN server's gets them: in a host's panel (`?embed=1`) and mounted in a host's app the server
 * is the host's, with the host's own buttons. And only where the shell is SERVED — `GET api/workspace` says so
 * (`shell`): an app built without it has no such buttons, and the page never asks for a file that is not there.
 */

/** The shell's files, RELATIVE to the page — never from the site's root: a path proxy serves the page under a prefix. */
export const SHELL_SCRIPT = "portail/shell.js";
export const SHELL_STYLE = "portail/shell.css";

/** The shell's two addresses for a page served at `page`. */
export function shellAddresses(page: string): { script: string; style: string } {
  return { script: new URL(SHELL_SCRIPT, page).href, style: new URL(SHELL_STYLE, page).href };
}

/** Whether `GET api/workspace` says the app serves the shell beside the page (`shell: true`). */
export function shellServed(workspace: unknown): boolean {
  return typeof workspace === "object" && workspace !== null && (workspace as { shell?: unknown }).shell === true;
}

/**
 * Does this page get the server's buttons? — where the shell is served (`served`: `false` until the page knows its
 * workspace), when it sits in no host's panel (`embed`) and is mounted in no host's app (`host`: the host's name,
 * `null` standalone).
 */
export function wantsServerButtons(served: boolean, embed: boolean, host: string | null): boolean {
  return served && !embed && host === null;
}

interface PageShell {
  start: (options: { buttons: HTMLElement }) => Promise<unknown>;
}

/**
 * Load the shell and let it put ↻ and ⏻ into `place`. Answers what takes them out again.
 */
export function mountServerButtons(place: HTMLElement): () => void {
  let gone = false;
  const start = () => {
    const shell = (window as unknown as { Portail?: PageShell }).Portail;
    if (!gone && shell && place.childElementCount === 0) void shell.start({ buttons: place }).catch(() => undefined);
  };
  const { script, style } = shellAddresses(document.baseURI);
  if ((window as unknown as { Portail?: PageShell }).Portail) {
    start();
  } else {
    const sheet = document.createElement("link");
    sheet.rel = "stylesheet";
    sheet.href = style;
    const loader = document.createElement("script");
    loader.src = script;
    loader.onload = start;
    document.head.append(sheet, loader);
  }
  return () => {
    gone = true;
    place.replaceChildren();
  };
}

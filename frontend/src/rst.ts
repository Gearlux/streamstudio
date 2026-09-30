/**
 * The descriptions the Nodes sidebar, its hover card, the palette and Add Node ▸ show come from Python docstrings
 * (`GET api/palette`, a class's first docstring line and its `Args:` entries), and docstrings spell code in
 * reStructuredText: ``` ``Mask`` ``` (an inline literal), `` `Mask` `` (interpreted text) and a Sphinx role in front of
 * one — `` :class:`DirectorySink` ``, `` :func:`load` ``, `` :py:meth:`~pkg.Class.run` ``. Shown as typed, a reader
 * sees the markup (``` ``Mask`` ```, `:class:DirectorySink`) instead of the name it marks (measured on the build's
 * catalogue: 121 descriptions carry a double-backtick literal, 45 a role).
 *
 * `docSpans` splits a description into plain text and CODE spans — a component draws a code span as inline code —
 * and `plainDoc` is the same description as plain text, for a tooltip (`title=`) that cannot hold markup. What a code
 * span holds is what a reader should see: the literal as written; a role's target WITHOUT the role (`:class:` is
 * markup, never text); a `~pkg.mod.Name` target by its last segment (Sphinx's own shortening); a `title <target>` by
 * its title. A hyperlink reference (`` `the guide`_ ``) is text, not code. A lone backtick is left as it is.
 */

/** One run of a description: plain text, or code a reader types or looks up. */
export interface DocSpan {
  code: boolean;
  text: string;
}

// Alternatives, in the order they are tried at each position:
//   1. ``literal``                                  → code, verbatim
//   2. :role:`target` / :domain:role:`target`       → code, the target as a reader sees it
//   3. `link`_ / `link`__                           → text (a hyperlink reference)
//   4. `interpreted`                                → code
const MARKUP = /``(.+?)``|((?::[A-Za-z][\w+.-]*)+):`([^`]+)`|`([^`]+)`(_{1,2})|`([^`]+)`/g;

/** A role's (or interpreted text's) target as shown: `~a.b.C` → `C`, `title <target>` → `title`, `!name` → `name`. */
export function shownTarget(target: string): string {
  const titled = /^(.*\S)\s*<[^<>]+>$/s.exec(target);
  if (titled) return titled[1];
  const bare = target.replace(/^!/, "");
  if (bare.startsWith("~")) return bare.slice(1).split(".").pop() ?? bare.slice(1);
  return bare;
}

/** `text` split into plain and code spans (adjacent plain text merged); an empty text is no span at all. */
export function docSpans(text: string): DocSpan[] {
  const spans: DocSpan[] = [];
  const push = (code: boolean, part: string) => {
    if (part === "") return;
    const last = spans[spans.length - 1];
    if (last && !last.code && !code) last.text += part;
    else spans.push({ code, text: part });
  };
  let at = 0;
  for (const match of text.matchAll(MARKUP)) {
    const index = match.index ?? 0;
    push(false, text.slice(at, index));
    const [, literal, _role, roleTarget, link, _underscores, interpreted] = match;
    if (literal !== undefined) push(true, literal);
    else if (roleTarget !== undefined) push(true, shownTarget(roleTarget));
    else if (link !== undefined) push(false, shownTarget(link));
    else if (interpreted !== undefined) push(true, shownTarget(interpreted));
    at = index + match[0].length;
  }
  push(false, text.slice(at));
  return spans;
}

/** The description as plain text — the markup dropped, the names it marks kept (a tooltip, a search hint). */
export const plainDoc = (text: string): string =>
  docSpans(text)
    .map((span) => span.text)
    .join("");

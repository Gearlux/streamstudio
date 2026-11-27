/** The standalone editor's ⏻ and ↻: who gets them, and where the page finds the shell that draws them. */
import { describe, expect, it } from "vitest";
import { SHELL_SCRIPT, SHELL_STYLE, shellAddresses, shellServed, wantsServerButtons } from "../shell";

describe("wantsServerButtons — only a page that is its own server's", () => {
  it("the standalone page, where the shell is served", () => {
    expect(wantsServerButtons(true, false, null)).toBe(true);
  });

  it("CON: not where no shell is served, and not before the page knows", () => {
    expect(wantsServerButtons(false, false, null)).toBe(false);
  });

  it("the workspace's answer says whether the shell is served", () => {
    expect(shellServed({ folder: "/g", shell: true })).toBe(true);
    expect(shellServed({ folder: "/g", shell: false })).toBe(false);
    expect(shellServed({ folder: "/g" })).toBe(false); // an older server: no buttons, and no request for the file
    expect(shellServed(null)).toBe(false);
  });

  it("CON: not in a host's panel, and not mounted in a host's app — that server is the host's", () => {
    expect(wantsServerButtons(true, true, null)).toBe(false);
    expect(wantsServerButtons(true, false, "the host")).toBe(false);
    expect(wantsServerButtons(true, true, "the host")).toBe(false);
  });
});

describe("shellAddresses — beside the page, wherever it is served", () => {
  it("at the root of its own server", () => {
    expect(shellAddresses("http://127.0.0.1:8190/")).toEqual({
      script: "http://127.0.0.1:8190/portail/shell.js",
      style: "http://127.0.0.1:8190/portail/shell.css",
    });
  });

  it("under a path proxy's prefix: relative, never from the site's root", () => {
    expect(shellAddresses("https://host/user/proxy/8190/?graph=scan").script).toBe(
      "https://host/user/proxy/8190/portail/shell.js",
    );
    expect(SHELL_SCRIPT.startsWith("/") || SHELL_STYLE.startsWith("/")).toBe(false);
  });
});

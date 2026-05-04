// Vitest setup: registers @testing-library/jest-dom matchers
// (`toBeInTheDocument`, `toBeChecked`, etc) on Vitest's `expect` and ensures
// every test starts from a clean DOM tree.
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
});

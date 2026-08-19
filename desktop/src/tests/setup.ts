/**
 * Vitest Setup - Global test configuration.
 *
 * Registers jest-dom matchers (toBeInTheDocument, etc.) for component
 * tests, and cleans up the DOM between tests (testing-library cannot
 * auto-register cleanup without vitest globals, so we do it explicitly
 * — otherwise rendered trees accumulate across test files).
 */
import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
});

import { coversetPlugin, coversetReporter, coversetSetup } from "coverset-vitest";
import { defineConfig } from "vitest/config";

export default defineConfig({
  // Instruments src/ for argument shapes (before Vite's TypeScript transform, lines unchanged).
  plugins: [coversetPlugin({ include: ["src"] })],
  test: {
    include: ["tests/**/*.test.ts"],
    // Per-test lines: the setup file diffs Istanbul's counters around every test.
    setupFiles: [coversetSetup],
    includeTaskLocation: true,
    reporters: ["default", coversetReporter()],
    coverage: {
      enabled: true,
      provider: "istanbul",
      include: ["src/**/*.ts"],
      reporter: ["text-summary"],
    },
  },
});

import { fileURLToPath } from "node:url";

import { defineConfig } from "vitest/config";

const web = fileURLToPath(new URL("./", import.meta.url));

export default defineConfig({
  resolve: { alias: [{ find: /^@\//, replacement: web }] }, // mirrors tsconfig "@/*"
  test: {
    environment: "node",
    include: ["tests/**/*.test.ts"],
  },
});

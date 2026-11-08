import { defineConfig } from "@playwright/test";
import path from "node:path";

const builtApp = process.env.TEST_BUILT_APP === "1";
const backendServer = {
  command: `${path.resolve("../.venv/bin/python")} -m uvicorn backend.main:app --app-dir .. --port 8008`,
  url: "http://127.0.0.1:8008/api/health", reuseExistingServer: false, timeout: 30000,
};

export default defineConfig({
  testDir: "./tests",
  timeout: 60000,
  workers: 1,
  use: {
    actionTimeout: 15000,
    baseURL: process.env.TEST_BASE_URL || (builtApp ? "http://127.0.0.1:8008" : "http://127.0.0.1:5178"),
    headless: true,
    launchOptions: { args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"] },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  webServer: process.env.TEST_BASE_URL ? undefined : builtApp ? [backendServer] : [
    backendServer,
    { command: "npm run dev -- --port 5178 --strictPort", env: { BACKEND_URL: "http://127.0.0.1:8008" }, url: "http://127.0.0.1:5178", reuseExistingServer: false, timeout: 30000 },
  ],
});

import { test, expect } from "@playwright/test";

// A consistently oriented 2 × 3 × 4 cm cuboid, independent of the generator.
const cubeOBJ = `v 0 0 0
v 2 0 0
v 2 3 0
v 0 3 0
v 0 0 4
v 2 0 4
v 2 3 4
v 0 3 4
f 1 3 2
f 1 4 3
f 5 6 7
f 5 7 8
f 1 2 6
f 1 6 5
f 2 3 7
f 2 7 6
f 3 4 8
f 3 8 7
f 4 1 5
f 4 5 8
`;

async function configure(page) {
  await page.goto("/");
  await expect(page.getByText("BACKEND CONNECTED")).toBeVisible();
  await page.getByLabel("AI Report", { exact: true }).uncheck();
  await density(page, 64);
}

async function density(page, value) {
  const input = page.locator("#density");
  await input.fill(String(value));
  await expect(input).toHaveValue(String(value));
}

async function run(page, endpoint = "generate-primitive") {
  const responsePromise = page.waitForResponse((r) => r.url().endsWith(`/api/${endpoint}`) && r.request().method() === "POST");
  await page.getByRole("button", { name: "RUN BENCHMARK" }).click();
  const response = await responsePromise;
  const data = await response.json();
  expect(response.status(), JSON.stringify(data)).toBe(200);
  await expect(page.getByTestId("viewport")).toHaveAttribute("data-mesh-ready", "true");
  await expect(page.getByRole("button", { name: "RUN BENCHMARK" })).toBeEnabled();
  return data;
}

test("all primitives generate real meshes, volume and area telemetry, and STL downloads", async ({ page }) => {
  test.setTimeout(120000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await configure(page);
  for (const shape of ["sphere", "cuboid", "torus", "cylinder", "pyramid_cone"]) {
    await page.getByLabel("Primitive shape").selectOption(shape);
    const data = await run(page);
    expect(data.source).toBe("primitive");
    expect(data.shape_type).toBe(shape);
    expect(data.telemetry.volume_error_percent).toBeLessThan(3);
    expect(data.telemetry.surface_area_error_percent).toBeLessThan(3);
    expect(data.ai_analysis.status).toBe("disabled");
    await expect(page.getByTestId("reference-volume")).not.toContainText("—");
    await expect(page.getByTestId("ray-area")).not.toContainText("—");
    await expect(page.getByRole("alert")).toHaveCount(0);
    const downloadPromise = page.waitForEvent("download");
    await page.getByRole("link", { name: "DOWNLOAD STL" }).click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toBe(data.filename);
    expect(await download.failure()).toBeNull();
    if (shape === "cuboid") {
      expect(data.telemetry.ground_truth_volume_cm3).toBeCloseTo(24, 8);
      expect(data.telemetry.raycast_surface_area_cm2).toBeCloseTo(52, 8);
    }
  }
  await page.getByRole("spinbutton", { name: "base sides", exact: true }).fill("32");
  const cone = await run(page);
  expect(cone.parameters.base_sides).toBe(32);
  expect(cone.telemetry.volume_error_percent).toBeLessThan(3);
  expect(errors).toEqual([]);
});

test("torus supports every scan axis and higher density animates more representative rays", async ({ page }) => {
  test.setTimeout(120000);
  await configure(page);
  await page.getByLabel("Primitive shape").selectOption("torus");
  await page.route("**/api/generate-primitive", async (route) => {
    const response = await route.fetch();
    await new Promise((resolve) => setTimeout(resolve, 900));
    await route.fulfill({ response });
  });
  for (const axis of ["X", "Y", "Z"]) {
    await page.getByLabel("Scan axis direction").selectOption(axis);
    const pending = run(page);
    await expect(page.getByTestId("viewport")).toHaveAttribute("data-scan-active", "true");
    await expect(page.getByTestId("viewport")).toHaveAttribute("data-ray-axis", axis);
    const data = await pending;
    expect(data.telemetry.direction).toBe(axis);
    expect(data.telemetry.volume_ray_count).toBe(64 ** 2);
    expect(data.telemetry.surface_area_ray_count).toBe(3 * 64 ** 2);
    await expect(page.getByTestId("measured-axis")).toContainText(`${axis} ·`);
  }
  const previous = Number(await page.getByTestId("viewport").getAttribute("data-visual-ray-count"));
  await density(page, 128);
  expect(Number(await page.getByTestId("viewport").getAttribute("data-visual-ray-count"))).toBeGreaterThan(previous);
  const pending = run(page);
  await expect(page.getByTestId("viewport")).toHaveAttribute("data-scan-active", "true");
  await page.screenshot({ path: "test-results/animated-torus.png", fullPage: true });
  const data = await pending;
  expect(data.telemetry.ray_density).toBe(128);
  await page.screenshot({ path: "test-results/benchmark-torus.png", fullPage: true });
});

test("OBJ drag-and-drop applies all scale factors and accepts the resulting STL as upload", async ({ page, request }) => {
  await configure(page);
  await page.getByRole("button", { name: "Custom Upload", exact: true }).click();
  const transfer = await page.evaluateHandle((text) => {
    const data = new DataTransfer();
    data.items.add(new File([text], "cuboid.obj", { type: "text/plain" }));
    return data;
  }, cubeOBJ);
  await page.getByTestId("dropzone").dispatchEvent("drop", { dataTransfer: transfer });
  await transfer.dispose();
  for (const [axis, value] of [["X", "2"], ["Y", "3"], ["Z", "4"]]) await page.getByLabel(`Scale ${axis}`, { exact: true }).fill(value);
  await page.getByLabel("Scan axis direction").selectOption("Y");
  const data = await run(page, "upload-and-analyze");
  expect(data.source).toBe("upload");
  expect(data.telemetry.bounding_box_dimensions_cm).toEqual([4, 9, 16]);
  expect(data.telemetry.raycast_volume_cm3).toBeCloseTo(576, 8);
  expect(data.telemetry.raycast_surface_area_cm2).toBeCloseTo(488, 8);
  await expect(page.getByTestId("reference-volume")).toContainText("576");
  const stl = await request.get(data.asset_path);
  expect(stl.ok()).toBe(true);
  await page.getByLabel("Mesh file").setInputFiles({ name: "roundtrip.stl", mimeType: "application/octet-stream", buffer: await stl.body() });
  for (const axis of ["X", "Y", "Z"]) await page.getByLabel(`Scale ${axis}`, { exact: true }).fill("1");
  const repeated = await run(page, "upload-and-analyze");
  expect(repeated.telemetry.ground_truth_volume_cm3).toBeCloseTo(576, 8);
  await page.screenshot({ path: "test-results/scaled-upload.png", fullPage: true });
});

test("live viewport updates immediately when primitive parameters or upload scale factors change", async ({ page }) => {
  await configure(page);
  await page.getByLabel("Primitive shape").selectOption("cuboid");
  await expect(page.getByTestId("viewport")).toHaveAttribute("data-logical-bounds", "2.00000,3.00000,4.00000");
  await expect(page.getByTestId("live-bounds")).toContainText("Bounds: 2 cm x 3 cm x 4 cm");
  await page.getByRole("spinbutton", { name: "length (cm)", exact: true }).fill("7");
  await expect(page.getByTestId("viewport")).toHaveAttribute("data-logical-bounds", "7.00000,3.00000,4.00000");
  await expect(page.getByTestId("live-bounds")).toContainText("Bounds: 7 cm x 3 cm x 4 cm");

  await page.getByRole("button", { name: "Custom Upload", exact: true }).click();
  await page.getByLabel("Mesh file").setInputFiles({ name: "cuboid.obj", mimeType: "text/plain", buffer: Buffer.from(cubeOBJ) });
  await expect(page.getByTestId("viewport")).toHaveAttribute("data-logical-bounds", "2.00000,3.00000,4.00000");
  await expect(page.getByTestId("live-bounds")).toContainText("Bounds: 2 cm x 3 cm x 4 cm");
  await page.getByLabel("Scale X", { exact: true }).fill("3");
  await expect(page.getByTestId("viewport")).toHaveAttribute("data-logical-bounds", "6.00000,3.00000,4.00000");
  await expect(page.getByTestId("live-bounds")).toContainText("Bounds: 6 cm x 3 cm x 4 cm");
});

test("AI report text renders safely, and an unavailable report preserves numerical results", async ({ page }) => {
  await configure(page);
  await page.getByLabel("Primitive shape").selectOption("cuboid");
  let available = true;
  await page.route("**/api/generate-primitive", async (route) => {
    // Exercise real numerical backend; only substitute the external AI service result.
    const response = await route.fetch();
    const data = await response.json();
    data.ai_analysis = available
      ? { status: "complete", report: "Volume agrees with the mesh.\n<script>window.unsafe = true</script>", model: "test-model", message: null }
      : { status: "unavailable", report: null, model: null, message: "Gemini is temporarily unavailable. Numerical results are complete." };
    await route.fulfill({ response, json: data });
  });
  await run(page);
  await expect(page.getByTestId("ai-report")).toContainText("Volume agrees with the mesh.");
  expect(await page.evaluate(() => window.unsafe)).toBeUndefined();
  available = false;
  await run(page);
  await expect(page.getByTestId("ai-report")).toContainText("Gemini is temporarily unavailable");
  await expect(page.getByTestId("reference-volume")).toContainText("24");
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("invalid inputs and backend failures are visible and allow retry", async ({ page }) => {
  await configure(page);
  await page.getByLabel("Primitive shape").selectOption("torus");
  await page.getByLabel("Inner radius (cm)").fill("12");
  await page.getByRole("button", { name: "RUN BENCHMARK" }).click();
  await expect(page.getByRole("alert")).toContainText("outer radius must be greater");
  await page.getByRole("button", { name: "Custom Upload", exact: true }).click();
  await page.getByLabel("Mesh file").setInputFiles({ name: "bad.txt", mimeType: "text/plain", buffer: Buffer.from("bad") });
  await expect(page.getByRole("alert")).toContainText("STL or OBJ");
  await page.getByLabel("Mesh file").setInputFiles({ name: "bad.obj", mimeType: "text/plain", buffer: Buffer.from("not a mesh") });
  await page.getByRole("button", { name: "RUN BENCHMARK" }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page.getByRole("button", { name: "RUN BENCHMARK" })).toBeEnabled();
  await page.getByRole("button", { name: "Parametric Primitives", exact: true }).click();
  await page.getByLabel("Primitive shape").selectOption("sphere");
  await page.route("**/api/generate-primitive", (route) => route.fulfill({ status: 429, contentType: "application/json", body: JSON.stringify({ detail: "Two benchmarks are already running. Please retry shortly." }) }));
  await page.getByRole("button", { name: "RUN BENCHMARK" }).click();
  await expect(page.getByRole("alert")).toContainText("Two benchmarks");
  await expect(page.getByRole("button", { name: "RUN BENCHMARK" })).toBeEnabled();
});

test("failed STL fetch is visible without hiding measured values", async ({ page }) => {
  await configure(page);
  await page.getByLabel("Primitive shape").selectOption("cuboid");
  await page.route("**/models/*.stl", (route) => route.fulfill({ status: 404, body: "not found" }));
  const responsePromise = page.waitForResponse((r) => r.url().endsWith("/api/generate-primitive"));
  await page.getByRole("button", { name: "RUN BENCHMARK" }).click();
  expect((await responsePromise).status()).toBe(200);
  await expect(page.getByRole("alert")).toContainText("Could not download the STL");
  await expect(page.getByTestId("reference-area")).toContainText("52");
});

test("mobile can generate and view a sphere without horizontal overflow", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await configure(page);
  await run(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: "test-results/mobile-benchmark.png", fullPage: true });
});

test("live Gemini interprets a completed benchmark in the browser", async ({ page }) => {
  test.skip(process.env.RUN_LIVE_GEMINI !== "1", "Opt in: uses the configured Gemini account.");
  await configure(page);
  await page.getByLabel("Primitive shape").selectOption("torus");
  await page.getByLabel("AI Report", { exact: true }).check();
  const data = await run(page);
  expect(data.ai_analysis.status, data.ai_analysis.message).toBe("complete");
  expect(data.ai_analysis.report.length).toBeGreaterThan(100);
  expect(data.ai_analysis.report).not.toMatch(/\\(?:text|mathrm|times)\b|\*\*/);
  await expect(page.getByTestId("ai-report")).toContainText(data.ai_analysis.report.slice(0, 70));
  await page.screenshot({ path: "test-results/live-ai-benchmark.png", fullPage: true });
});

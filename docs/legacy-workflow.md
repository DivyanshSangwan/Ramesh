# Legacy Volumetrix workflow (before the benchmark pivot)

This describes the earlier prompt-to-geometry UI and deprecated API.
See the root README for the current benchmark backend.

Generate solids and composite designs from a Gemini prompt, or enter primitive
dimensions directly to test locally without an API call. The backend
builds a watertight STL, estimates volume with parallel rays, and the frontend
renders that exact STL with Three.js.

## Run locally

Requires Python 3.12+ and Node.js 22.12+ (Node 24 LTS is suitable).

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -c constraints.txt
```

Create `.env` if you do not already have one. Use `.env.example` as a reference:

```dotenv
GEMINI_API_KEY=your-key
GEMINI_MODEL=gemini-3.5-flash-lite
GEMINI_FALLBACK_MODEL=gemini-3.8-flash
```

The backend loads `.env` automatically. Existing shell environment variables take
precedence. Restart the backend after changing `.env`. Manual dimensions works
without a Gemini key. Keep keys out of frontend files.

### One-server local trial

Build the frontend once, then serve both the UI and API on port 8000:

```bash
cd frontend
npm ci
npm run build
cd ..
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000**. If an older backend is running, stop it with
Ctrl+C first (Ctrl+Z suspends it and leaves the port occupied).

1. Choose **Manual dimensions**, select **sphere**, leave radius at **5**, and
   click **GENERATE SOLID**. This tests the API, mesh generation, volume estimator,
   STL serving, and browser rendering without an external AI request.
2. Expect approximately **522.47 cm³ mesh volume** and **522.61 cm³ ray volume**
   at an 80×80 grid. Download the STL or drag the model to orbit.
3. Choose **Gemini prompt**, enter `Sphere of radius 5 cm`, and generate again.
   This additionally tests your Gemini key, network, quota, and model access.
4. Try `A spur gear with 18 teeth, 4 cm outer radius, 3.1 cm root radius,
   1.2 cm thick, and a 0.6 cm bore radius`.
5. Try `torus with inner radius 5cm and outer radius 10cm`.
6. Choose **RAY AXIS** (+X, +Y, or +Z). For an existing result, click
   **Recast current mesh** to measure the same STL along that axis without
   another Gemini call. Telemetry records the axis actually measured; changing
   the selector alone does not change the previous result.

## Composite geometry

Gemini can now construct designs using boxes, cylinders, spheres, toruses,
cones, ellipsoids, capsules, and simplified gears. It can translate, rotate and
scale these solids, then combine them with union, difference (subtraction), and
intersection. A composite plan can contain up to 32 ordered construction steps;
the final step is the output. Overlapping unions merge volume correctly, and
subtractions preserve holes and enclosed cavities.

Examples:

- `A sphere of radius 5 cm with a centered cylindrical through-hole of radius
  2 cm along the X axis`.
- `A hollow sphere with outer radius 5 cm and inner radius 4 cm`.
- `Union two 2 cm cubes, with centers at (0,0,0) and (1,0,0) cm`.
- `A cone with base radius 3 cm and height 8 cm`.
- `An ellipsoid with X, Y, Z semi-axis lengths 4, 3, 2 cm`.

This is an extensible constructive geometry language, not unrestricted freeform
text-to-mesh generation. It supports many combinations beyond the named
primitives; arbitrary organic/sculpted surfaces may not be representable.
Missing essential dimensions or unsupported constructions produce errors rather
than silently substituting a different object. Boolean operations use Manifold3D,
included in `requirements.txt`.

For direct API calls, an assembly has `shape_type: "assembly"` and `steps`.
Primitive steps specify `shape_type` and `dimensions`. Boolean steps specify
`shape_type: "union"`, `"difference"`, or `"intersection"`, with `inputs` listing
zero-based indices of earlier steps. Difference subtracts all later inputs
from the first. All steps must contribute to the final output. Optional
`translation`, `rotation`, and `scale` are three-element arrays. Scale is applied
first, then rotation in degrees about fixed X, Y, Z axes, then translation in cm.

`POST /api/generate` accepts `ray_axis: "x" | "y" | "z"` (default `"z"`).
`POST /api/volume` accepts an existing `filename`, `ray_axis`, and
`ray_resolution`, and returns new volume metrics without generating another STL.

### Development with live frontend updates

Run `python -m uvicorn backend.main:app --reload --port 8000` in one terminal.
In another, run `cd frontend` and `npm run dev`. Open the URL printed by Vite
(normally **http://127.0.0.1:5173**). Vite proxies `/api` and `/models` to port
8000. Set `BACKEND_URL` before starting Vite if the backend uses a different port.
`npm run preview` only serves static files; use the one-server trial above for
testing the built frontend together with the API.

## Troubleshooting

- **BACKEND OFFLINE:** check the backend terminal and `/api/health`.
- **Gemini timeout, quota, or credentials error:** the UI shows the failure;
  switch to Manual dimensions to check whether local generation works. A key
  being configured does not guarantee Gemini service availability.
- **Request takes too long:** each Gemini attempt has a 30-second timeout with
  one fallback at most. The browser stops waiting at 90 seconds and also offers
  Cancel. Cancelling stops the browser request; a backend operation may finish.
- **STL cannot load:** the viewport displays an error. Check that `/models/...`
  is reachable through the same origin as `/api/generate`.
- **WebGL unavailable:** enable browser hardware acceleration. The API and STL
  download can still work without a 3D viewport.
- **Unexpected model setting:** shell variables override `.env`; unset an old
  `GEMINI_MODEL`/`GEMINI_FALLBACK_MODEL` export before restarting.

## Tests

The backend regression suite uses mocked Gemini responses and temporary model
storage. It checks actual mesh construction, numerical integration, API errors,
and export/download round trips:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Browser tests start separate servers on ports 8008 and 5178. They generate and
render real models, download STL files, check mobile layout, and simulate error
responses. They need local server/socket permissions:

```bash
cd frontend
npx playwright install chromium
npm run test:e2e
```

To additionally test the real Gemini account (may consume API quota):

```bash
RUN_LIVE_GEMINI=1 npm run test:e2e
```

To test a running built app instead of starting Vite:

```bash
TEST_BASE_URL=http://127.0.0.1:8000 npm run test:e2e
```

Or let the suite start the built app automatically after `npm run build`:

```bash
TEST_BUILT_APP=1 npm run test:e2e
```

## Geometry and accuracy

All coordinates and dimensions are centimeters. STL itself carries no units;
select centimeters when importing it elsewhere. Liters equal cm³ / 1000.
The displayed error compares numerical ray volume with the triangulated mesh's
volume, not an ideal analytic solid. A sphere of radius 5 cm has analytic volume
523.599 cm³; the tessellated sphere is approximately 522.467 cm³.

Torus inner/outer radii are distances from the origin to the innermost/outermost
surfaces. For inner=5 and outer=10 cm, the ring-center radius is 7.5 cm and the
tube radius is 2.5 cm. Its ideal analytic volume is approximately 925.275 cm³;
the mesh volume is approximately 923.419 cm³. Ellipsoid dimensions are semi-axis
lengths. Capsule height is its total end-to-end length, including the end caps.

Rays travel along the selected positive axis in STL coordinates: X uses a YZ
sampling grid, Y uses XZ, and Z uses XY. The viewport displays nine illustrative
direction arrows; the actual integration uses the full selected ray grid.
Ray-volume error may differ by axis and grid resolution. Recasting does not
rotate or modify the solid.

Gears have simplified flat teeth, not involute engineering profiles. Built-in
primitive bores run along local Z; use rotated subtraction cutters for other
directions. A prompt that specifies a bore *radius* is unambiguous;
an unqualified bore size is interpreted as its diameter. Unsupported shapes or
missing required measurements should produce a clarification error.

The API caps grids at 256×256 and permits two simultaneous generations per
worker. Generated files remain in `generated_models/` until removed. This local
demo does not yet include authentication, per-user quotas, or storage retention.

## Container packaging

The Dockerfile builds the frontend and serves it with the API as a non-root user.
It excludes `.env` from the image. No server deployment is performed by these
files. For a later container trial:

```bash
docker build -t volumetrix .
docker run --rm -p 127.0.0.1:8000:8000 --env-file .env \
  --mount type=volume,source=volumetrix-models,target=/app/generated_models \
  volumetrix
```

The local one-server trial exercises the same static-files/API routing. Docker
image construction still needs verification on a host with Docker installed.

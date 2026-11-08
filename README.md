# Ramesh · Ray-Cast Mesh Benchmarking

The backend generates deterministic primitives or loads uploaded STL/OBJ
files, validates and repairs the mesh, measures volume and surface area with
ray grids, and asks Gemini to interpret the telemetry. Geometry and numerical
results do not depend on Gemini availability.

The React/Three.js dashboard supports all five primitive families, STL/OBJ
drag-and-drop uploads, live viewport preview and scaling, integer ray density N
and axis controls, animated representative rays, volume and area telemetry, STL
downloads, and AI interpretation reports. Earlier prompt-generation endpoints
remain deprecated for compatibility; the dashboard uses the benchmark endpoints.

## Run locally

```bash
cd /home/div/Hackathon
source .venv/bin/activate
python -m pip install -r requirements.txt -c constraints.txt
cd frontend
npm ci
npm run build
cd ..
python -m uvicorn backend.main:app --reload --port 8000
```

For a fresh checkout, create the environment first with `python3 -m venv .venv`.
The tested host uses Python 3.14; the Dockerfile targets Python 3.12.
Node 22.12 or newer is required to build the frontend.
Open **http://127.0.0.1:8000** for the dashboard, or
**http://127.0.0.1:8000/docs** for interactive API requests. Build before starting
the backend so it detects `frontend/dist`. If a server is already running, stop
it with Ctrl+C and restart after building. Ctrl+Z suspends it and keeps its port
occupied; use `jobs`, then `fg` and Ctrl+C to stop a suspended server.

For frontend development, leave the backend running on port 8000 and run this
in another terminal:

```bash
cd /home/div/Hackathon/frontend
npm run dev
```

Open **http://127.0.0.1:5173**. Vite proxies API requests and meshes to the backend.
`npm run preview` alone does not proxy the API; use the backend to test the build.

### First full test

1. Choose **Parametric Primitives → Cuboid**. The default dimensions are 2 × 3 × 4 cm.
2. Disable **AI Report** for a purely local first run. Choose
   density 64 and axis X, then click **RUN BENCHMARK**.
3. Check volume **24 cm³**, surface area **52 cm²**, and errors approximately zero.
   Drag the mesh to orbit; scroll to zoom. Download its STL.
4. Choose **Custom Upload**, drop that STL, and set scales X=2, Y=3, Z=4.
   Run again: bounds should be 4 × 9 × 16 cm, volume **576 cm³**, area **488 cm²**.
5. Try a torus with inner radius 5 and outer radius 10. Compare completed runs
   at different densities and axes. Enable **AI Report** to test your key.

Rays animate while the request is pending, including the report-generation
stage. They illustrate the selected volume direction; they are a bounded visual
sample, not a replay of server intersections. The surface-area estimator always
uses all three axes. Controls configure the next run; telemetry and downloads
remain tied to the last completed benchmark. A previous mesh stays visible
while a new request is pending. Reports render as safe text.
Dense grids and detailed uploads may take several minutes. The dashboard allows
up to ten minutes per request and provides cancellation; cancelling stops the
browser wait, while the backend may still finish the current calculation.

The backend loads `.env` automatically, without overriding exported variables:

```dotenv
GEMINI_API_KEY=your-key
GEMINI_ANALYSIS_MODEL=gemini-3.5-flash-lite
```

Restart after changing `.env`. `GEMINI_ANALYSIS_MODEL` overrides `GEMINI_MODEL`
for reports. A key is optional for numerical benchmarking: use `include_ai:
false` for offline requests. An AI timeout or missing key returns numerical
results with `ai_analysis.status: "unavailable"`. The analyzer makes a single
attempt with a 20-second timeout.

## Modules

- `backend/mesh_generator.py`: parametric meshes and independent ideal-shape references.
- `backend/mesh_validator.py`: bounded uploads, scaling, hole repair, watertightness and orientation checks.
- `backend/ray_volume.py`: numerical volume/surface-area integration and error metrics.
- `backend/ai_analyzer.py`: engineering interpretation of measured telemetry.
- `backend/main.py`: endpoints, CORS, limits, and serving validated STL files.

## Generate a primitive

`POST /api/generate-primitive` accepts JSON:

```json
{
  "shape_type": "torus",
  "parameters": {"inner_radius": 5, "outer_radius": 10},
  "ray_density": 128,
  "direction": "Z",
  "include_ai": true
}
```

All lengths are centimeters:

| `shape_type` | Required `parameters` |
| --- | --- |
| `sphere` | `radius` |
| `cuboid` | `length`, `breadth`, `height` |
| `torus` | `inner_radius`, `outer_radius` |
| `cylinder` | `radius`, `height` |
| `pyramid_cone` | `height`, `base_sides`, `base_side_length` |

Torus radii measure center to inner/outer surface. The ring-center radius is
`(outer + inner)/2`; the tube radius is `(outer - inner)/2`. Require `0 < inner < outer`.

For `pyramid_cone`, 3–31 sides produce a regular pyramid; 32–256 sides produce
a finely tessellated circular cone. The base radius in either case is
`base_side_length / (2*sin(pi/base_sides))`. The side length means a regular
polygon edge, not a radius or diameter. The response explains this convention.

Example without Gemini:

```bash
curl http://127.0.0.1:8000/api/generate-primitive \
  -H 'Content-Type: application/json' \
  -d '{"shape_type":"cuboid","parameters":{"length":2,"breadth":3,"height":4},"ray_density":64,"direction":"X","include_ai":false}'
```

Expected volume: 24 cm³. Expected surface area: 52 cm². For this aligned cuboid,
the numerical estimates agree to floating-point precision.

## Upload and analyze

`POST /api/upload-and-analyze` accepts multipart form data:

```bash
curl http://127.0.0.1:8000/api/upload-and-analyze \
  -F 'file=@/absolute/path/to/part.stl' \
  -F 'scale_x=2' -F 'scale_y=1' -F 'scale_z=0.5' \
  -F 'ray_density=128' -F 'direction=Y' -F 'include_ai=false'
```

Scale defaults to (1,1,1); factors must be positive and at most 1000. Uploaded
coordinates after scaling are interpreted as centimeters. STL carries no units:
use uniform scale 0.1 to convert coordinates expressed in millimeters to cm.

Limits: 20 MiB per file, 200,000 triangles, and two concurrent benchmarks per
process. Multipart traffic, including chunked uploads, is bounded before disk
spooling. OBJ materials/textures are not fetched. Meshes still open after
small-hole repair are rejected. Repair details are returned, and references
describe the repaired, scaled mesh.

## Response fields

Both endpoints return:

- `filename`, `asset_path`: validated STL, served at `/models/{filename}`.
- `validation`: watertightness before/after, repairs, scale, winding and face counts.
- `telemetry`: estimated/reference volume and area, absolute/percentage errors,
  bounding-box dimensions, axis, density, ray counts and numerical runtime.
- `ai_analysis`: status (`complete`, `unavailable`, `disabled`), report text, model and status message.

`GET /api/health` returns service availability and primitive names.

## Numerical method

`ray_density=N` means N samples per grid edge: N² rays per axis, not rays per
square centimeter. Density accepts any positive integer N ≥ 1. Samples lie near cell centers
with a tiny deterministic offset to avoid exact silhouette tangencies. Boundary
intersections are sorted and deduplicated per ray. Paired entry/exit distances
times grid-cell area estimate volume along the selected X, Y or Z direction.

Full surface area cannot be measured from just one axis: faces parallel to that
axis have zero projected area. The estimator uses three orthogonal grids. Each
hit on axis `i` contributes `cell_area * abs(face_normal[i])`. Since projection
already introduces a factor `abs(normal[i])`, the estimate integrates
`normal[i]^2 * dA`. Summing X/Y/Z yields area because squared components of a
unit normal sum to one. The volume scan is reused: total unique rays = 3N².

`mesh.area` is never substituted for estimated surface area. Reference values
are `mesh.volume` and `mesh.area` of the validated triangulation. Percentage
error for each is `100 * abs(estimate - reference) / reference`.

These mesh references are distinct from ideal analytic curved shapes; separate
`analytic_reference` values make tessellation error explicit. Low sampling error
does not establish dimensional fidelity. Watertightness does not prove absence
of self-intersections or overlapping shells. Thin features may require denser
grids; quadrature error need not decrease monotonically. Numerical runtime
excludes the subsequent Gemini request.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
cd frontend
npx playwright install chromium
npm run test:e2e
# Verify the production build served directly by FastAPI:
npm run build
TEST_BUILT_APP=1 npm run test:e2e
# Optional: one real Gemini report through the browser (uses API quota):
TEST_BUILT_APP=1 RUN_LIVE_GEMINI=1 npm run test:e2e -- --grep "live Gemini"
```

Tests cover analytic primitive references, volume and full surface area on all
axes, rotated solids, pyramids/cones, scaled STL/OBJ uploads, repair, oversized
and invalid uploads, served STL files, and AI failures. They do not consume
Gemini quota. The older compatibility tests are retained.

Browser tests start isolated servers on ports 8008 and 5178, exercise real
primitive generation and upload/scale/round-trip downloads, check axis/density
controls, error recovery, safe AI-report rendering, and mobile layout. The live
Gemini test is skipped unless explicitly enabled. Screenshots are saved in
`frontend/test-results/`.

## Deploy to Vultr

Deploy to a Vultr Cloud Compute instance with a single command using `deploy.sh`:

```bash
# HTTP on port 80 via server IP:
./deploy.sh YOUR_VULTR_IP

# Or automatic HTTPS via Caddy with a custom domain pointing to YOUR_VULTR_IP:
./deploy.sh YOUR_VULTR_IP ramesh.yourdomain.com
```

The multi-stage `Dockerfile` and `docker-compose.yml` package the React
frontend and FastAPI backend together behind Caddy.

# Ramesh · Ray-Cast Mesh Benchmarking

The backend generates deterministic primitives or loads uploaded STL/OBJ
files, validates and repairs the mesh, measures volume and surface area with
ray grids, and asks Gemini to interpret the telemetry. Geometry and numerical
results do not depend on Gemini availability.

The React/Three.js dashboard supports all five primitive families, STL/OBJ
drag-and-drop uploads, live viewport preview and scaling, integer ray density N
and axis controls, animated representative rays, volume and area telemetry, STL
downloads, and AI interpretation reports.

## Run locally

```bash
cd /home/user/Hackathon
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
it and restart after building.

For frontend development, leave the backend running on port 8000 and run this
in another terminal:

```bash
cd /home/user/Hackathon/frontend
npm run dev
```

Open **http://127.0.0.1:5173**.

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

Percentage error for each is `100 * abs(estimate - reference) / reference`.

Watertightness does not prove absence of self-intersections or overlapping 
shells. Thin features may require denser grids.

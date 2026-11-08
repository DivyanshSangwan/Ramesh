"""FastAPI application for prompt-to-mesh generation and volume analysis."""

from __future__ import annotations

import os
import logging
from threading import BoundedSemaphore
from typing import Literal
from pathlib import Path
from uuid import uuid4

import trimesh
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .ai_parser import GeometryParseError, GeometrySpec, parse_geometry_prompt
from .mesh_builder import MeshBuildError, build_mesh, export_mesh
from .ray_volume import calculate_ray_volume
from .config import BASE_DIR, gemini_key
from .geometry_spec import BOOLEAN_OPERATIONS, MAX_STEPS, PRIMITIVE_FIELDS
from .mesh_generator import PARAMETER_MODELS, PrimitiveSpec, generate_primitive
from .mesh_validator import MAX_UPLOAD_BYTES, MeshValidationError, ValidationReport, load_uploaded_mesh, validate_mesh
from .ray_volume import BenchmarkMetrics, benchmark_mesh
from .ai_analyzer import EngineeringAnalysis, analyze_telemetry


logger = logging.getLogger(__name__)
# Output folder for exported STL files and semaphore limiting concurrent heavy benchmarks to 2.
MODEL_DIR = Path(os.getenv("MODEL_OUTPUT_DIR", BASE_DIR / "generated_models")).resolve()
MODEL_DIR.mkdir(parents=True, exist_ok=True)
generation_slots = BoundedSemaphore(2)

app = FastAPI(
    title="3D Mesh Ray-Casting Volume & Surface Area Benchmark Tool",
    version="2.0.0",
)
origins = [item.strip() for item in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",") if item.strip()]


class _UploadLimitExceeded(HTTPException):
    def __init__(self):
        super().__init__(413, "Upload exceeds the 20 MiB file limit")


class UploadSizeLimit:
    """Bound multipart traffic, including chunked requests, before disk spooling."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") != "/api/upload-and-analyze":
            return await self.app(scope, receive, send)
        limit = MAX_UPLOAD_BYTES + 65536  # multipart fields/headers overhead
        headers = dict(scope.get("headers", []))
        length = headers.get(b"content-length", b"0")
        if length.isdigit() and int(length) > limit:
            return await JSONResponse({"detail": "Upload exceeds the 20 MiB file limit"}, status_code=413)(scope, receive, send)
        received = 0

        # Count incoming request body bytes per chunk and abort immediately if limit is exceeded.
        async def limited_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > limit:
                raise _UploadLimitExceeded()
            return message

        try:
            return await self.app(scope, limited_receive, send)
        except _UploadLimitExceeded:
            return await JSONResponse({"detail": "Upload exceeds the 20 MiB file limit"}, status_code=413)(scope, receive, send)


app.add_middleware(UploadSizeLimit)
app.add_middleware(
    CORSMiddleware, allow_origins=origins, allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["*"],
)


# Request payload for parametric primitive generation and ray-cast benchmarking.
class PrimitiveBenchmarkRequest(PrimitiveSpec):
    ray_density: int = Field(default=128, ge=8, le=1024, description="Samples per edge of each N by N ray grid")
    direction: Literal["X", "Y", "Z"] = "Z"
    include_ai: bool = True


# Unified response schema returned by both primitive and custom-upload benchmark endpoints.
class BenchmarkResponse(BaseModel):
    filename: str
    asset_path: str
    source: Literal["primitive", "upload"]
    shape_type: str | None = None
    parameters: dict[str, float | int] | None = None
    validation: ValidationReport
    telemetry: BenchmarkMetrics
    ai_analysis: EngineeringAnalysis


# Run ray benchmark on validated mesh, atomically write STL to disk, and request optional AI report.
def _benchmark_response(validated, ray_density, direction, include_ai, source,
                        shape_type=None, parameters=None) -> BenchmarkResponse:
    telemetry = benchmark_mesh(validated.mesh, ray_density=ray_density, direction=direction)
    filename = f"{shape_type or 'upload'}-{uuid4().hex}.stl"
    path = MODEL_DIR / filename
    temporary = path.with_suffix(".stl.tmp")
    try:
        validated.mesh.export(temporary, file_type="stl")
        temporary.replace(path)
        # Keep only the 100 most recent STL files so public server disk space never fills up.
        stl_files = sorted(MODEL_DIR.glob("*.stl"), key=lambda p: p.stat().st_mtime)
        for stale in stl_files[:-100]:
            stale.unlink(missing_ok=True)
    except OSError as exc:
        logger.error("Failed to persist benchmark mesh (%s)", type(exc).__name__)
        raise HTTPException(500, "Could not save the benchmark mesh") from exc
    context = {"source": source, "shape_type": shape_type, "parameters": parameters,
               "validation": validated.report.model_dump()}
    analysis = analyze_telemetry(telemetry, context, enabled=include_ai)
    return BenchmarkResponse(
        filename=filename, asset_path=f"/models/{filename}", source=source,
        shape_type=shape_type, parameters=parameters, validation=validated.report,
        telemetry=telemetry, ai_analysis=analysis,
    )


# Endpoint: build parametric primitive, validate geometry, run ray integration, and return telemetry.
@app.post("/api/generate-primitive", response_model=BenchmarkResponse)
def generate_primitive_benchmark(request: PrimitiveBenchmarkRequest) -> BenchmarkResponse:
    if not generation_slots.acquire(blocking=False):
        raise HTTPException(429, "Two benchmarks are already running. Please retry shortly.")
    try:
        generated = generate_primitive(request)
        validated = validate_mesh(generated.mesh)
        return _benchmark_response(validated, request.ray_density, request.direction, request.include_ai,
                                   "primitive", request.shape_type, request.parameters)
    except (MeshValidationError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        generation_slots.release()


# Endpoint: parse uploaded STL/OBJ, apply scale factors, repair holes, and run ray benchmark.
@app.post("/api/upload-and-analyze", response_model=BenchmarkResponse)
def upload_and_analyze(
    file: UploadFile = File(..., description="STL or OBJ; coordinates interpreted as cm"),
    scale_x: float = Form(1.0, gt=0, le=100),
    scale_y: float = Form(1.0, gt=0, le=100),
    scale_z: float = Form(1.0, gt=0, le=100),
    ray_density: int = Form(128, ge=8, le=1024),
    direction: Literal["X", "Y", "Z"] = Form("Z"),
    include_ai: bool = Form(True),
) -> BenchmarkResponse:
    if not generation_slots.acquire(blocking=False):
        raise HTTPException(429, "Two benchmarks are already running. Please retry shortly.")
    try:
        if Path(file.filename or "").suffix.lower() not in {".stl", ".obj"}:
            raise HTTPException(415, "Upload an STL or OBJ file")
        if file.size is not None and file.size > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "File exceeds the 20 MiB limit")
        data = file.file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "File exceeds the 20 MiB limit")
        mesh = load_uploaded_mesh(data, file.filename)
        validated = validate_mesh(mesh, scale=(scale_x, scale_y, scale_z))
        return _benchmark_response(validated, ray_density, direction, include_ai, "upload")
    except MeshValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        file.file.close()
        generation_slots.release()


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str | None = Field(default=None, min_length=1, max_length=2000)
    geometry: GeometrySpec | None = None
    ray_resolution: int = Field(default=80, ge=8, le=256)
    ray_axis: Literal["x", "y", "z"] = "z"

    @field_validator("prompt")
    @classmethod
    def strip_prompt(cls, value):
        if value is not None and not value.strip():
            raise ValueError("prompt must not be blank")
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def one_input(self):
        if (self.prompt is None) == (self.geometry is None):
            raise ValueError("Provide exactly one of prompt or geometry")
        return self


class VolumeMetrics(BaseModel):
    actual_cm3: float
    ray_cast_cm3: float
    actual_liters: float
    ray_cast_liters: float
    absolute_error_cm3: float
    percent_error: float
    grid_resolution: int
    rays_cast: int
    odd_intersection_rays: int
    ray_axis: Literal["x", "y", "z"]


class RecalculateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(pattern=r"^[a-z]+-[0-9a-f]{32}\.stl$")
    ray_resolution: int = Field(default=80, ge=8, le=256)
    ray_axis: Literal["x", "y", "z"] = "z"


class GenerateResponse(BaseModel):
    geometry: GeometrySpec
    asset_path: str
    filename: str
    volume: VolumeMetrics
    source: Literal["gemini", "manual"]


# Endpoint: report server readiness, Gemini key status, and supported primitives/axes.
@app.get("/health")
@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "gemini_configured": bool(gemini_key()),
            "gemini_model": os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
            "supported_shapes": list(PARAMETER_MODELS),
            "benchmark_version": "2.0.0",
            "legacy_operations": list(BOOLEAN_OPERATIONS), "legacy_max_construction_steps": MAX_STEPS,
            "ray_axes": ["X", "Y", "Z"]}


@app.post("/api/generate", response_model=GenerateResponse, deprecated=True)
def generate(request: GenerateRequest) -> GenerateResponse:
    if not generation_slots.acquire(blocking=False):
        raise HTTPException(429, "Two generations are already running. Please retry shortly.")
    try:
        return _generate(request)
    finally:
        generation_slots.release()


def _generate(request: GenerateRequest) -> GenerateResponse:
    try:
        spec = request.geometry or parse_geometry_prompt(request.prompt)
        mesh = build_mesh(spec)
        ray_result = calculate_ray_volume(mesh, resolution=request.ray_resolution, axis=request.ray_axis)
    except GeometryParseError as exc:
        logger.warning("Geometry parsing failed: %s", exc)
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except (ValueError, MeshBuildError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ModuleNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Ray-casting dependency unavailable: {exc.name}",
        ) from exc
    except Exception as exc:
        logger.exception("Mesh generation failed")
        raise HTTPException(status_code=500, detail="Mesh generation failed") from exc

    filename = f"{spec.shape_type}-{uuid4().hex}.stl"
    try:
        export_mesh(mesh, MODEL_DIR / filename)
    except (OSError, ValueError, MeshBuildError) as exc:
        raise HTTPException(status_code=500, detail=f"Could not export mesh: {exc}") from exc

    return GenerateResponse(
        geometry=spec,
        asset_path=f"/models/{filename}",
        filename=filename,
        source="manual" if request.geometry is not None else "gemini",
        volume=_volume_metrics(mesh, ray_result),
    )


def _volume_metrics(mesh, ray_result) -> VolumeMetrics:
    actual = float(abs(mesh.volume))
    estimated = ray_result.volume_cm3
    absolute_error = abs(estimated - actual)
    percent_error = (absolute_error / actual * 100.0) if actual else 0.0
    return VolumeMetrics(
            actual_cm3=actual,
            ray_cast_cm3=estimated,
            actual_liters=actual / 1000.0,
            ray_cast_liters=estimated / 1000.0,
            absolute_error_cm3=absolute_error,
            percent_error=percent_error,
            grid_resolution=ray_result.grid_resolution,
            rays_cast=ray_result.rays_cast,
            odd_intersection_rays=ray_result.odd_intersection_rays,
            ray_axis=ray_result.ray_axis,
    )


def _model_path(filename: str) -> Path:
    # UUID filenames are flat; this resolution check also protects future naming changes.
    candidate = (MODEL_DIR / filename).resolve()
    if candidate.parent != MODEL_DIR or candidate.suffix.lower() != ".stl":
        raise HTTPException(status_code=404, detail="Model not found")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Model not found")
    return candidate


@app.post("/api/volume", response_model=VolumeMetrics, deprecated=True)
def recalculate_volume(request: RecalculateRequest) -> VolumeMetrics:
    path = _model_path(request.filename)
    if not generation_slots.acquire(blocking=False):
        raise HTTPException(429, "Two calculations are already running. Please retry shortly.")
    try:
        mesh = trimesh.load_mesh(path, file_type="stl")
        ray = calculate_ray_volume(mesh, resolution=request.ray_resolution, axis=request.ray_axis)
        return _volume_metrics(mesh, ray)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        generation_slots.release()


# Stream exported STL file by filename for Three.js viewport or browser download.
@app.get("/models/{filename}")
def get_model(filename: str) -> FileResponse:
    candidate = _model_path(filename)
    return FileResponse(candidate, media_type="model/stl", filename=candidate.name)


# A built frontend is served at the same origin as the API, including in Docker.
# Register this last so it cannot shadow /api or /models.
FRONTEND_DIST = BASE_DIR / "frontend" / "dist"
if (FRONTEND_DIST / "index.html").is_file():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")

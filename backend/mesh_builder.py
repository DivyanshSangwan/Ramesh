"""Procedural construction and export of watertight meshes."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import trimesh

from .geometry_spec import BOOLEAN_OPERATIONS, GeometrySpec, GeometryStep, SolidParameters


class MeshBuildError(RuntimeError):
    """Raised when valid geometry cannot be constructed."""


def _radial_extrusion(
    outer_radii: np.ndarray,
    height: float,
    inner_radius: float = 0.0,
    angles: np.ndarray | None = None,
) -> trimesh.Trimesh:
    """Extrude a star-shaped outline, optionally with a centered circular hole."""

    count = len(outer_radii)
    if angles is None:
        angles = np.linspace(0.0, 2.0 * np.pi, count, endpoint=False)
    outer_xy = np.column_stack((outer_radii * np.cos(angles), outer_radii * np.sin(angles)))
    z0, z1 = -height / 2.0, height / 2.0

    if inner_radius > 0:
        inner_xy = np.column_stack((inner_radius * np.cos(angles), inner_radius * np.sin(angles)))
        vertices = np.vstack(
            (
                np.column_stack((outer_xy, np.full(count, z0))),
                np.column_stack((outer_xy, np.full(count, z1))),
                np.column_stack((inner_xy, np.full(count, z0))),
                np.column_stack((inner_xy, np.full(count, z1))),
            )
        )
        ob, ot, ib, it = 0, count, count * 2, count * 3
        faces: list[list[int]] = []
        for i in range(count):
            j = (i + 1) % count
            faces.extend(
                [
                    [ob + i, ob + j, ot + j], [ob + i, ot + j, ot + i],
                    [ib + i, it + j, ib + j], [ib + i, it + i, it + j],
                    [ot + i, ot + j, it + j], [ot + i, it + j, it + i],
                    [ob + i, ib + j, ob + j], [ob + i, ib + i, ib + j],
                ]
            )
    else:
        vertices = np.vstack(
            (
                np.column_stack((outer_xy, np.full(count, z0))),
                np.column_stack((outer_xy, np.full(count, z1))),
                [[0.0, 0.0, z0], [0.0, 0.0, z1]],
            )
        )
        bottom_center, top_center = count * 2, count * 2 + 1
        faces = []
        for i in range(count):
            j = (i + 1) % count
            faces.extend(
                [
                    [i, j, count + j], [i, count + j, count + i],
                    [bottom_center, j, i], [top_center, count + i, count + j],
                ]
            )

    return trimesh.Trimesh(vertices=np.asarray(vertices), faces=np.asarray(faces), process=True)


def _box_with_bore(width: float, depth: float, height: float, bore: float) -> trimesh.Trimesh:
    """Create a rectangular prism with a centered Z-axis bore without booleans."""

    segments = 128
    angles = np.linspace(0.0, 2.0 * np.pi, segments, endpoint=False)
    # Include the exact corners; otherwise a non-square box gets chamfered.
    corners = np.mod(np.arctan2([depth, depth, -depth, -depth], [width, -width, -width, width]), 2 * np.pi)
    angles = np.unique(np.round(np.r_[angles, corners], 14))
    # Intersection of each radial line with the rectangle gives a matched outer loop.
    scale = np.minimum(
        (width / 2.0) / np.maximum(np.abs(np.cos(angles)), 1e-12),
        (depth / 2.0) / np.maximum(np.abs(np.sin(angles)), 1e-12),
    )
    return _radial_extrusion(scale, height, bore, angles=angles)


def build_mesh(spec: GeometrySpec) -> trimesh.Trimesh:
    """Build and validate the mesh represented by *spec*."""

    if spec.shape_type != "assembly":
        return _finish_mesh(_transform(_build_primitive(spec), spec))
    results = []
    for index, step in enumerate(spec.steps):
        if step.shape_type in BOOLEAN_OPERATIONS:
            inputs = [results[ref] for ref in step.inputs]
            try:
                mesh = getattr(trimesh.boolean, step.shape_type)(inputs, engine="manifold")
            except ImportError as exc:
                raise MeshBuildError("Composite shapes require manifold3d. Install requirements.txt and restart the backend.") from exc
            except Exception as exc:
                raise MeshBuildError(f"Boolean {step.shape_type} failed at step {index}; check overlapping parts and cutter placement") from exc
        else:
            mesh = _build_primitive(step)
        if mesh is None or mesh.is_empty:
            raise MeshBuildError(f"Step {index} produces an empty solid. Check the dimensions and placement of the parts.")
        results.append(_finish_mesh(_transform(mesh, step)))
    return _finish_mesh(_transform(results[-1].copy(), spec))


def _transform(mesh: trimesh.Trimesh, spec: SolidParameters) -> trimesh.Trimesh:
    mesh.apply_scale(spec.scale)
    matrix = trimesh.transformations.euler_matrix(*np.radians(spec.rotation), axes="sxyz")
    matrix[:3, 3] = spec.translation
    mesh.apply_transform(matrix)
    return mesh


def _build_primitive(spec: GeometrySpec | GeometryStep) -> trimesh.Trimesh:

    d = spec.dimensions
    bore = d.bore_radius or 0.0
    if spec.shape_type == "box":
        if bore:
            mesh = _box_with_bore(d.width, d.depth, d.height, bore)  # type: ignore[arg-type]
        else:
            mesh = trimesh.creation.box(extents=(d.width, d.depth, d.height))
    elif spec.shape_type == "cylinder":
        if bore:
            mesh = trimesh.creation.annulus(
                r_min=bore, r_max=d.radius, height=d.height, sections=128
            )
        else:
            mesh = trimesh.creation.cylinder(radius=d.radius, height=d.height, sections=128)
    elif spec.shape_type == "sphere":
        mesh = trimesh.creation.icosphere(subdivisions=4, radius=d.radius)
    elif spec.shape_type == "torus":
        mesh = trimesh.creation.torus(
            major_radius=(d.outer_radius + d.inner_radius) / 2,
            minor_radius=(d.outer_radius - d.inner_radius) / 2,
            major_sections=128, minor_sections=64,
        )
    elif spec.shape_type == "cone":
        mesh = trimesh.creation.cone(radius=d.radius, height=d.height, sections=128)
        mesh.apply_translation(-mesh.bounds.mean(axis=0))
    elif spec.shape_type == "ellipsoid":
        mesh = trimesh.creation.icosphere(subdivisions=4, radius=1)
        mesh.apply_scale([d.radius_x, d.radius_y, d.radius_z])
    elif spec.shape_type == "capsule":
        if np.isclose(d.height, 2 * d.radius):
            mesh = trimesh.creation.icosphere(subdivisions=4, radius=d.radius)
        else:
            mesh = trimesh.creation.capsule(radius=d.radius, height=d.height - 2 * d.radius, count=[48, 64])
            mesh.apply_translation(-mesh.bounds.mean(axis=0))
    elif spec.shape_type == "gear":
        # Two outer samples create a flat tooth tip; two root samples create the gap.
        profile = np.tile([d.root_radius, d.outer_radius, d.outer_radius, d.root_radius], d.teeth)
        mesh = _radial_extrusion(profile.astype(float), d.height, bore)  # type: ignore[arg-type]
    else:
        raise MeshBuildError(f"Unknown primitive: {spec.shape_type}")
    return mesh


def _finish_mesh(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    mesh.remove_unreferenced_vertices()
    # Inner shells of a hollow solid must keep their negative orientation.
    # fix_normals(multibody=True) would turn cavities into positive solids.
    if not mesh.is_winding_consistent:
        trimesh.repair.fix_winding(mesh)
    if not mesh.is_watertight:
        trimesh.repair.fill_holes(mesh)
    if not mesh.is_watertight:
        raise MeshBuildError("Generated mesh is not watertight after repair")
    if mesh.volume < 0:
        mesh.invert()
    if not mesh.is_volume or not np.isfinite(mesh.vertices).all() or mesh.volume <= 0:
        raise MeshBuildError("Generated mesh is not a finite, positively oriented solid")
    if len(mesh.faces) > 300000:
        raise MeshBuildError("The model is too detailed for interactive ray casting; simplify the construction")
    return mesh


def export_mesh(mesh: trimesh.Trimesh, destination: str | Path) -> Path:
    """Export a validated mesh as STL or OBJ and return its resolved path."""

    destination = Path(destination)
    if destination.suffix.lower() not in {".stl", ".obj"}:
        raise ValueError("Only STL and OBJ exports are supported")
    if not mesh.is_watertight:
        raise MeshBuildError("Refusing to export a non-watertight mesh")
    destination.parent.mkdir(parents=True, exist_ok=True)
    mesh.export(destination)
    return destination.resolve()

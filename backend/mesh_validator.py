"""Bounded STL/OBJ loading and explicit mesh-validation/repair reporting."""

import io
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import trimesh
from pydantic import BaseModel
from trimesh.grouping import group_rows

# Hard limits protecting memory and CPU during upload parsing and validation.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_FACES = 200000


class MeshValidationError(ValueError):
    pass


# Telemetry summarizing mesh topology, repair actions, and post-validation counts.
class ValidationReport(BaseModel):
    watertight_before: bool
    watertight_after: bool
    winding_consistent: bool
    repair_attempted: bool
    faces_added_by_repair: int
    orientation_flipped: bool
    scale: tuple[float, float, float]
    vertices: int
    faces: int
    warnings: list[str]


@dataclass(frozen=True)
class ValidatedMesh:
    mesh: trimesh.Trimesh
    report: ValidationReport


# Parse raw STL/OBJ bytes into single Trimesh object without loading external textures.
def load_uploaded_mesh(data: bytes, filename: str) -> trimesh.Trimesh:
    suffix = Path(filename).suffix.lower()
    if suffix not in {".stl", ".obj"}:
        raise MeshValidationError("Upload an STL or OBJ file")
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise MeshValidationError("Mesh file must be nonempty and at most 20 MiB")
    try:
        # No filesystem or remote material/texture resolution for untrusted OBJ.
        # Replace texture visuals with ColorVisuals before concatenating so
        # multi-material or UV-mapped OBJs do not require Pillow during copy().
        scene = trimesh.load_scene(io.BytesIO(data), file_type=suffix[1:],
                                   resolver={}, allow_remote=False, skip_materials=True,
                                   group_material=False)
        for geom in scene.geometry.values():
            if isinstance(geom, trimesh.Trimesh):
                geom.visual = trimesh.visual.ColorVisuals()
        loaded = scene.to_mesh()
    except Exception as exc:
        raise MeshValidationError("Could not parse the uploaded STL/OBJ mesh") from exc
    if not isinstance(loaded, trimesh.Trimesh) or loaded.is_empty:
        raise MeshValidationError("The file must contain triangular surface geometry")
    return loaded


def _close_component_holes(comp: trimesh.Trimesh) -> bool:
    """Repair open boundary loops using fan triangulation and directed loop stitching."""
    # Strip zero-area and duplicate triangles before inspecting boundary edges.
    comp.update_faces(comp.nondegenerate_faces())
    comp.update_faces(comp.unique_faces())
    comp.remove_unreferenced_vertices()
    if len(comp.faces) < 3:
        return False
    if comp.is_watertight:
        return True
    # Run Trimesh fan hole filler up to 3 passes for standard polygon gaps.
    for _ in range(3):
        trimesh.repair.fill_holes(comp, use_fan=True)
        comp.update_faces(comp.nondegenerate_faces())
        comp.update_faces(comp.unique_faces())
        if comp.is_watertight:
            return True

    # Fallback: trace directed open boundary loops and stitch non-coplanar/open edges
    for _ in range(3):
        if comp.is_watertight:
            return True
        # Find edges referenced by only one triangle (open boundary edges).
        bg = group_rows(comp.edges_sorted, require_count=1)
        if len(bg) < 3:
            break
        boundary = comp.edges[bg]
        # Build reversed directed adjacency list to walk open loops with matching winding.
        adj = defaultdict(list)
        for u, v in boundary:
            adj[int(v)].append(int(u))
        visited_edges = set()
        new_faces = []
        for start_u, start_v in boundary:
            u, v = int(start_v), int(start_u)
            if (u, v) in visited_edges:
                continue
            loop = [u]
            curr = v
            visited_edges.add((u, v))
            while curr != u and len(loop) <= len(boundary) + 1:
                loop.append(curr)
                candidates = [nxt for nxt in adj.get(curr, []) if (curr, nxt) not in visited_edges]
                if not candidates:
                    break
                nxt = candidates[0]
                visited_edges.add((curr, nxt))
                curr = nxt
            # Triangulate closed boundary loop as triangle fan anchored at first vertex.
            if curr == u and len(loop) >= 3:
                for i in range(1, len(loop) - 1):
                    new_faces.append([loop[0], loop[i], loop[i + 1]])
        if not new_faces:
            break
        comp.faces = np.vstack([comp.faces, np.asarray(new_faces, dtype=np.int64)])
        comp.update_faces(comp.nondegenerate_faces())
        comp.update_faces(comp.unique_faces())
    return bool(comp.is_watertight)


# Scale, clean, repair holes, orient normals outward, and verify positive enclosed volume.
def validate_mesh(mesh: trimesh.Trimesh, scale=(1.0, 1.0, 1.0)) -> ValidatedMesh:
    scale = np.asarray(scale, dtype=float)
    if scale.shape != (3,) or not np.isfinite(scale).all() or np.any(scale <= 0) or np.any(scale > 100):
        raise MeshValidationError("Scale must contain three finite positive factors no greater than 100")
    if not isinstance(mesh, trimesh.Trimesh) or mesh.is_empty:
        raise MeshValidationError("The mesh is empty")
    if len(mesh.faces) > MAX_FACES:
        raise MeshValidationError(f"Mesh exceeds the {MAX_FACES:,}-triangle limit; simplify it first")
    if not np.isfinite(mesh.vertices).all():
        raise MeshValidationError("Mesh vertices contain non-finite coordinates")
    # Copy mesh, apply axis scaling, weld coincident vertices, and drop degenerate faces.
    mesh = mesh.copy()
    mesh.apply_scale(scale)
    mesh.remove_unreferenced_vertices()
    mesh.merge_vertices()
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.update_faces(mesh.unique_faces())
    mesh.remove_unreferenced_vertices()
    if not np.isfinite(mesh.vertices).all() or np.any(mesh.extents <= 1e-8):
        raise MeshValidationError("The scaled mesh has invalid or degenerate bounds")
    watertight_before = bool(mesh.is_watertight)
    faces_before = len(mesh.faces)
    repair_attempted = not watertight_before
    if repair_attempted:
        trimesh.repair.fill_holes(mesh, use_fan=True)
        mesh.update_faces(mesh.nondegenerate_faces())
        mesh.update_faces(mesh.unique_faces())
        # Split multi-part meshes so each component can be closed and oriented independently.
        components = mesh.split(only_watertight=False)
        if not mesh.is_watertight or len(components) > 1:
            solid_components = []
            for comp in components:
                # Keep only components that form valid closed solids (minimum 4 faces for tetrahedron).
                if _close_component_holes(comp) and len(comp.faces) >= 4:
                    if not comp.is_winding_consistent:
                        trimesh.repair.fix_winding(comp)
                    if comp.volume < 0:
                        comp.invert()
                    if comp.is_volume and np.isfinite(comp.volume) and comp.volume > 1e-9:
                        solid_components.append(comp)
            if solid_components:
                if len(solid_components) > 1:
                    # Boolean-union overlapping sub-parts via Manifold engine to prevent double-counted volume.
                    try:
                        unioned = trimesh.boolean.union(solid_components, engine="manifold")
                        if isinstance(unioned, trimesh.Trimesh) and unioned.is_volume and unioned.volume > 0:
                            mesh = unioned
                        else:
                            mesh = trimesh.util.concatenate(solid_components)
                    except Exception:
                        mesh = trimesh.util.concatenate(solid_components)
                else:
                    mesh = solid_components[0]
    if not mesh.is_watertight:
        raise MeshValidationError("Mesh remains non-watertight after hole repair; repair large gaps or non-manifold edges before uploading")
    # Align face winding and invert normals if signed volume is negative.
    if not mesh.is_winding_consistent:
        trimesh.repair.fix_winding(mesh)
    flipped = bool(mesh.volume < 0)
    if flipped:
        mesh.invert()
    # Do not fix normals per component when already watertight: inner cavity shells must stay inward.
    if not mesh.is_volume or not np.isfinite([mesh.volume, mesh.area]).all() or mesh.volume <= 0:
        raise MeshValidationError("Mesh must enclose a finite, positive volume with consistent face winding")
    warnings = ["Watertightness does not certify absence of self-intersections or overlapping shells."]
    if repair_attempted:
        warnings.append("Holes were filled; reference metrics describe the repaired mesh.")
    return ValidatedMesh(mesh, ValidationReport(
        watertight_before=watertight_before, watertight_after=True,
        winding_consistent=bool(mesh.is_winding_consistent), repair_attempted=repair_attempted,
        faces_added_by_repair=len(mesh.faces) - faces_before, orientation_flipped=flipped,
        scale=tuple(float(value) for value in scale), vertices=len(mesh.vertices), faces=len(mesh.faces), warnings=warnings,
    ))

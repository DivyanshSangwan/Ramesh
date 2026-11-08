"""Numerically estimate mesh volume by integrating ray/solid intersections."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from time import perf_counter

import numpy as np
import trimesh
from pydantic import BaseModel, ConfigDict
from trimesh.ray.ray_triangle import RayMeshIntersector


# Result container for single-axis ray integration pass.
@dataclass(frozen=True)
class RayVolumeResult:
    volume_cm3: float
    grid_resolution: int
    rays_cast: int
    odd_intersection_rays: int
    ray_axis: str = "z"
    surface_area_contribution_cm2: float = 0.0


# Complete benchmark payload comparing ray-cast integration against mesh ground truth.
class BenchmarkMetrics(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    raycast_volume_cm3: float
    ground_truth_volume_cm3: float
    volume_absolute_error_cm3: float
    volume_error_percent: float
    raycast_surface_area_cm2: float
    ground_truth_surface_area_cm2: float
    surface_area_absolute_error_cm2: float
    surface_area_error_percent: float
    bounding_box_dimensions_cm: tuple[float, float, float]
    direction: Literal["X", "Y", "Z"]
    ray_density: int
    volume_ray_count: int
    surface_area_ray_count: int
    total_unique_ray_count: int
    surface_area_contributions_cm2: dict[str, float]
    elapsed_seconds: float
    reference_method: str = "Trimesh volume and area of the validated triangulated mesh"
    surface_area_method: str = "Three orthogonal ray grids, weighted by absolute face-normal components"


def calculate_ray_volume(
    mesh: trimesh.Trimesh,
    resolution: int = 160,
    batch_size: int = 512,
    axis: Literal["x", "y", "z"] = "z",
) -> RayVolumeResult:
    """Integrate in-solid lengths along an axis over its perpendicular plane."""

    if not mesh.is_watertight:
        raise ValueError("Ray volume integration requires a watertight mesh")
    if not isinstance(resolution, int) or isinstance(resolution, bool) or resolution < 8 or resolution > 1024:
        raise ValueError("resolution must be an integer between 8 and 1024")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if axis not in {"x", "y", "z"}:
        raise ValueError("axis must be x, y, or z")
    # Map chosen scan axis to index (0=X, 1=Y, 2=Z) and identify perpendicular grid plane axes.
    axis_index = {"x": 0, "y": 1, "z": 2}[axis]
    plane_axes = [index for index in range(3) if index != axis_index]

    bounds = np.asarray(mesh.bounds, dtype=float)
    extents = bounds[1] - bounds[0]
    if np.any(extents <= 0):
        raise ValueError("Mesh has degenerate bounds")

    # Divide perpendicular bounding-box face into N x N rectangular cells of size dx by dy.
    u, v = plane_axes
    dx, dy = extents[u] / resolution, extents[v] / resolution
    # A tiny deterministic subcell offset prevents whole ray rows from lying
    # exactly on a pyramid's silhouette edges (ambiguous one-point tangencies).
    # Every sample remains close to its cell midpoint and inside its cell.
    xs = bounds[0, u] + (np.arange(resolution) + 0.50013) * dx
    ys = bounds[0, v] + (np.arange(resolution) + 0.50037) * dy
    # Start rays slightly before minimum bounding-box coordinate along scan axis.
    origin_axis = bounds[0, axis_index] - max(extents[axis_index] * 0.01, 1e-6)
    intersector = RayMeshIntersector(mesh)

    total_length = 0.0
    area_weight = 0.0
    odd_rays = 0
    total_rays = resolution * resolution
    # Fire rays in batches to bound peak memory usage even at very high N.
    for start in range(0, total_rays, batch_size):
        indices = np.arange(start, min(start + batch_size, total_rays))
        rows, cols = np.divmod(indices, resolution)
        origins = np.zeros((len(indices), 3))
        origins[:, u] = xs[cols]
        origins[:, v] = ys[rows]
        origins[:, axis_index] = origin_axis
        directions = np.zeros_like(origins)
        directions[:, axis_index] = 1.0
        locations, ray_ids, triangle_ids = intersector.intersects_location(
            ray_origins=origins, ray_directions=directions, multiple_hits=True
        )
        if not len(locations):
            continue

        # Triangle edges can report the same boundary more than once. Group and
        # merge nearly identical axis coordinates before the odd/even rule.
        order = np.lexsort((locations[:, axis_index], ray_ids))
        sorted_rays = ray_ids[order]
        sorted_z = locations[order, axis_index]
        signed_normals = mesh.face_normals[triangle_ids[order], axis_index]
        split_at = np.flatnonzero(np.diff(sorted_rays)) + 1
        groups = np.split(sorted_z, split_at)
        sign_groups = np.split(signed_normals, split_at)
        tolerance = max(extents[axis_index] * 1e-7, 1e-8)
        for z_values, n_z in zip(groups, sign_groups):
            # Deduplicate hits closer than tolerance on shared edges or vertices.
            keep = np.r_[True, np.diff(z_values) > tolerance]
            unique_z = z_values[keep]
            unique_nz = n_z[keep]
            weights = np.abs(unique_nz)
            if len(unique_z) % 2 == 0:
                # Pair entry (even index) and exit (odd index) hits to sum interior chord lengths.
                if len(unique_z):
                    total_length += float(np.sum(unique_z[1::2] - unique_z[0::2]))
                    area_weight += float(np.sum(weights))
            else:
                # Grazing tangency or repaired fan self-overlap produced an odd hit count.
                # Resolve inside/outside intervals via face-normal orientation (enter: n_z < 0, exit: n_z > 0).
                depth = 0
                enter_z = 0.0
                for z_val, nz_val, w_val in zip(unique_z, unique_nz, weights):
                    area_weight += float(w_val)
                    if nz_val < 0:
                        if depth == 0:
                            enter_z = float(z_val)
                        depth += 1
                    elif nz_val > 0 and depth > 0:
                        depth -= 1
                        if depth == 0:
                            total_length += float(z_val) - enter_z

    # Multiply summed chord lengths and normal weights by cell area (dx * dy).
    return RayVolumeResult(
        volume_cm3=total_length * dx * dy,
        grid_resolution=resolution,
        rays_cast=resolution * resolution,
        odd_intersection_rays=odd_rays,
        ray_axis=axis,
        surface_area_contribution_cm2=area_weight * dx * dy,
    )


def benchmark_mesh(mesh: trimesh.Trimesh, ray_density: int = 128, direction: Literal["X", "Y", "Z"] = "Z") -> BenchmarkMetrics:
    """Numerically estimate volume and full surface area, then benchmark both.

    Density is samples per edge of each projection grid (N*N rays per axis).
    A patch dA projects to |n_i| dA on axis i's perpendicular plane. Weighting
    each hit by |n_i| integrates n_i**2 dA. Summing X/Y/Z gives dA because a
    unit normal satisfies n_x**2+n_y**2+n_z**2=1. This includes surfaces parallel
    to the chosen volume rays and avoids division by near-zero cosines.
    Reference area is used only for the error calculation, never the estimator.
    """
    if direction not in {"X", "Y", "Z"}:
        raise ValueError("direction must be X, Y, or Z")
    if not mesh.is_volume or mesh.volume <= 0 or mesh.area <= 0:
        raise ValueError("Benchmarking requires a closed, consistently oriented solid")
    start = perf_counter()
    # Cast orthogonal ray grids along X, Y, and Z; use selected direction for volume and all three for surface area.
    scans = {axis: calculate_ray_volume(mesh, resolution=ray_density, axis=axis.lower()) for axis in "XYZ"}
    volume = float(scans[direction].volume_cm3)
    contributions = {axis: float(scan.surface_area_contribution_cm2) for axis, scan in scans.items()}
    area = sum(contributions.values())
    # Compute mesh ground-truth volume and area via Trimesh to calculate percentage errors.
    reference_volume, reference_area = float(mesh.volume), float(mesh.area)
    volume_error, area_error = abs(volume - reference_volume), abs(area - reference_area)
    return BenchmarkMetrics(
        raycast_volume_cm3=volume, ground_truth_volume_cm3=reference_volume,
        volume_absolute_error_cm3=volume_error, volume_error_percent=100 * volume_error / reference_volume,
        raycast_surface_area_cm2=area, ground_truth_surface_area_cm2=reference_area,
        surface_area_absolute_error_cm2=area_error, surface_area_error_percent=100 * area_error / reference_area,
        bounding_box_dimensions_cm=tuple(float(value) for value in mesh.extents),
        direction=direction, ray_density=ray_density, volume_ray_count=ray_density**2,
        surface_area_ray_count=3 * ray_density**2, total_unique_ray_count=3 * ray_density**2,
        surface_area_contributions_cm2=contributions, elapsed_seconds=perf_counter() - start,
    )

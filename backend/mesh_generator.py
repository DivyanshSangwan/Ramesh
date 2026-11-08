"""Deterministic parametric solids; all lengths are centimeters."""

import math
from dataclasses import dataclass
from typing import Literal

import trimesh
from pydantic import BaseModel, ConfigDict, Field, model_validator


# Base schema rejecting unknown fields and non-finite numbers (NaN/Inf).
class Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


# Parameter schemas enforcing positive dimensions bounded to 10,000 cm.
class SphereParameters(Parameters):
    radius: float = Field(gt=0, le=10000)


class CuboidParameters(Parameters):
    length: float = Field(gt=0, le=10000)
    breadth: float = Field(gt=0, le=10000)
    height: float = Field(gt=0, le=10000)


class TorusParameters(Parameters):
    inner_radius: float = Field(gt=0, le=10000)
    outer_radius: float = Field(gt=0, le=10000)

    # Ensure inner hole radius stays strictly below outer ring radius.
    @model_validator(mode="after")
    def ordered_radii(self):
        if self.inner_radius >= self.outer_radius:
            raise ValueError("inner_radius must be smaller than outer_radius")
        return self


class CylinderParameters(SphereParameters):
    height: float = Field(gt=0, le=10000)


class PyramidConeParameters(Parameters):
    height: float = Field(gt=0, le=10000)
    base_sides: int = Field(ge=3, le=256)
    base_side_length: float = Field(gt=0, le=10000)


# Lookup table mapping shape identifier to corresponding Pydantic validator.
PARAMETER_MODELS = {
    "sphere": SphereParameters,
    "cuboid": CuboidParameters,
    "torus": TorusParameters,
    "cylinder": CylinderParameters,
    "pyramid_cone": PyramidConeParameters,
}


# Input specification pairing shape name with shape-specific dimension dictionary.
class PrimitiveSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    shape_type: Literal["sphere", "cuboid", "torus", "cylinder", "pyramid_cone"]
    parameters: dict[str, float | int]

    # Validate raw parameter dictionary against selected shape schema.
    @model_validator(mode="after")
    def shape_parameters(self):
        params = PARAMETER_MODELS[self.shape_type].model_validate(self.parameters)
        self.parameters = params.model_dump()
        return self


@dataclass(frozen=True)
class GeneratedPrimitive:
    mesh: trimesh.Trimesh


def generate_primitive(spec: PrimitiveSpec) -> GeneratedPrimitive:
    """Build a triangulated parametric solid mesh."""
    p = spec.parameters
    shape = spec.shape_type
    if shape == "sphere":
        # Subdivide icosahedron 5 times to approximate sphere surface.
        mesh = trimesh.creation.icosphere(subdivisions=5, radius=p["radius"])
    elif shape == "cuboid":
        # Construct axis-aligned rectangular box from X, Y, Z extents.
        mesh = trimesh.creation.box(extents=(p["length"], p["breadth"], p["height"]))
    elif shape == "torus":
        # Convert inner/outer ring radii into major ring radius and minor tube radius.
        major = (p["outer_radius"] + p["inner_radius"]) / 2
        minor = (p["outer_radius"] - p["inner_radius"]) / 2
        mesh = trimesh.creation.torus(major_radius=major, minor_radius=minor, major_sections=192, minor_sections=96)
    elif shape == "cylinder":
        # Extrude circular cross-section with 256 radial segments along Z axis.
        mesh = trimesh.creation.cylinder(radius=p["radius"], height=p["height"], sections=256)
    else:
        # Compute circumradius from regular polygon side length; switch to smooth cone at 32+ sides.
        h, n, side = p["height"], int(p["base_sides"]), p["base_side_length"]
        radius = side / (2 * math.sin(math.pi / n))
        smooth = n >= 32
        mesh = trimesh.creation.cone(radius=radius, height=h, sections=max(256, n) if smooth else n)
        # Center cone bounding box at origin.
        mesh.apply_translation(-mesh.bounds.mean(axis=0))
    return GeneratedPrimitive(mesh)

"""Validated solids and ordered constructive-solid-geometry plans."""

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PRIMITIVE_FIELDS = {
    "box": ("width", "depth", "height"),
    "cylinder": ("radius", "height"),
    "gear": ("outer_radius", "root_radius", "height", "teeth"),
    "sphere": ("radius",),
    "torus": ("inner_radius", "outer_radius"),
    "cone": ("radius", "height"),
    "ellipsoid": ("radius_x", "radius_y", "radius_z"),
    "capsule": ("radius", "height"),
}
BOOLEAN_OPERATIONS = ("union", "difference", "intersection")
Primitive = Literal["box", "cylinder", "gear", "sphere", "torus", "cone", "ellipsoid", "capsule"]
MAX_STEPS = 32


class Dimensions(BaseModel):
    """Lengths in centimeters; height means total axial height."""
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    width: float | None = None
    depth: float | None = None
    height: float | None = None
    thickness: float | None = None
    radius: float | None = None
    outer_radius: float | None = None
    inner_radius: float | None = None
    root_radius: float | None = None
    bore_radius: float | None = None
    radius_x: float | None = None
    radius_y: float | None = None
    radius_z: float | None = None
    teeth: int | None = None

    @field_validator("width", "depth", "height", "thickness", "radius", "outer_radius",
                     "inner_radius", "root_radius", "radius_x", "radius_y", "radius_z")
    @classmethod
    def positive_measurements(cls, value):
        if value is not None and not 0.01 <= value <= 10000:
            raise ValueError("measurement must be between 0.01 and 10000 cm")
        return value

    @field_validator("bore_radius")
    @classmethod
    def nonnegative_bore(cls, value):
        if value is not None and not 0 <= value <= 10000:
            raise ValueError("bore_radius must be between 0 and 10000 cm")
        return value

    @field_validator("teeth")
    @classmethod
    def valid_tooth_count(cls, value):
        if value is not None and not 3 <= value <= 256:
            raise ValueError("teeth must be between 3 and 256")
        return value


class SolidParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    dimensions: Dimensions = Field(default_factory=Dimensions)
    translation: tuple[float, float, float] = (0, 0, 0)
    rotation: tuple[float, float, float] = (0, 0, 0)
    scale: tuple[float, float, float] = (1, 1, 1)

    @field_validator("translation", "rotation", "scale")
    @classmethod
    def bounded_transform(cls, value, info):
        if info.field_name == "scale":
            if any(not 0.01 <= item <= 100 for item in value):
                raise ValueError("scale components must be between 0.01 and 100")
        elif any(abs(item) > 10000 for item in value):
            raise ValueError("translation or rotation component exceeds 10000")
        return value

    def validate_primitive(self, shape):
        d = self.dimensions
        if d.height is None and d.thickness is not None:
            d.height = d.thickness
        elif d.height is not None and d.thickness is not None and not math.isclose(d.height, d.thickness):
            raise ValueError("height and thickness must agree")
        required = PRIMITIVE_FIELDS[shape]
        missing = [name for name in required if getattr(d, name) is None]
        if missing:
            raise ValueError(f"{shape} requires: {', '.join(missing)}")
        allowed = set(required)
        if "height" in allowed:
            allowed.add("thickness")
        if shape in {"box", "cylinder", "gear"}:
            allowed.add("bore_radius")
        for name, value in d.model_dump().items():
            if name not in allowed and value is not None and not (name == "bore_radius" and value == 0):
                raise ValueError(f"{shape} does not support {name}; use a difference step for cuts")
        limit = {"box": min(d.width or 1, d.depth or 1) / 2,
                 "cylinder": d.radius, "gear": d.root_radius}.get(shape)
        if d.bore_radius and limit is not None and d.bore_radius >= limit:
            raise ValueError("bore_radius must be smaller than the solid's limiting radius")
        if shape == "gear" and d.root_radius >= d.outer_radius:
            raise ValueError("root_radius must be smaller than outer_radius")
        if shape == "torus" and d.inner_radius >= d.outer_radius:
            raise ValueError("torus inner_radius must be smaller than outer_radius")
        if shape == "capsule" and d.height < 2 * d.radius:
            raise ValueError("capsule total height must be at least twice its radius")


class GeometryStep(SolidParameters):
    """A primitive or boolean referencing earlier steps by zero-based index."""
    shape_type: Primitive | Literal["union", "difference", "intersection"]
    inputs: list[int] = Field(default_factory=list, max_length=MAX_STEPS)

    @model_validator(mode="after")
    def valid_step(self):
        if self.shape_type in BOOLEAN_OPERATIONS:
            if len(self.inputs) < 2 or len(set(self.inputs)) != len(self.inputs):
                raise ValueError("boolean steps require at least two distinct inputs")
            if any(value is not None for value in self.dimensions.model_dump().values()):
                raise ValueError("boolean steps take inputs, not dimensions")
        else:
            if self.inputs:
                raise ValueError("primitive steps cannot have inputs")
            self.validate_primitive(self.shape_type)
        return self


class GeometrySpec(SolidParameters):
    shape_type: Primitive | Literal["assembly"]
    steps: list[GeometryStep] = Field(default_factory=list, max_length=MAX_STEPS)

    @model_validator(mode="after")
    def validate_shape_dimensions(self):
        if self.shape_type != "assembly":
            if self.steps:
                raise ValueError("steps are only valid for an assembly")
            self.validate_primitive(self.shape_type)
            return self
        if not self.steps:
            raise ValueError("assembly requires steps; its last step is the output")
        if any(value is not None for value in self.dimensions.model_dump().values()):
            raise ValueError("assembly dimensions belong to its individual steps")
        ancestors = []
        for index, step in enumerate(self.steps):
            if any(ref < 0 or ref >= index for ref in step.inputs):
                raise ValueError(f"step {index} must only reference earlier steps")
            reached = {index}
            for ref in step.inputs:
                reached |= ancestors[ref]
            ancestors.append(reached)
        if len(ancestors[-1]) != len(self.steps):
            raise ValueError("every step must contribute to the final output")
        return self

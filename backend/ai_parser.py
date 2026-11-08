"""Translate natural-language shape descriptions into validated geometry specs."""

from __future__ import annotations

import json
import logging
import os

from google import genai
from google.genai import types
from pydantic import ValidationError

from .config import gemini_key
from .geometry_spec import BOOLEAN_OPERATIONS, MAX_STEPS, PRIMITIVE_FIELDS, Dimensions, GeometrySpec

logger = logging.getLogger(__name__)


SYSTEM_INSTRUCTION = """
Interpret the request as a solid or a constructive geometry plan. You can create
boxes, cylinders, spheres, toruses, cones, ellipsoids, capsules and simplified
spur gears, and combine them using union, difference and intersection.
Do not reject a composite object just because it is not a named primitive:
decompose it into primitives and boolean operations, e.g. drilled blocks,
hollow spheres, brackets, intersecting pipes, or a sphere with a cylindrical cut.

For a simple object return a primitive. For composites use shape_type assembly
with at most 32 ordered steps. Each step is a primitive or a boolean operation.
Boolean inputs are zero-based indices of EARLIER steps. Difference subtracts
all remaining inputs from its first input. The LAST step is the result; every
step must contribute to it. Union must merge overlaps, not sum their volumes.
Make cutters extend beyond surfaces for through-holes. Do not execute code.

All lengths and translations are in centimeters. Each primitive is centered at
the origin. Boxes use X width, Y depth, Z height. Cylinders, cones, gears and
capsules run along Z. Toruses lie in XY with symmetry axis Z.
Optional translation, rotation and scale are arrays [x,y,z]. Rotation is degrees
about fixed X, then Y, then Z axes. Apply scale, then rotation, then translation.
To make a Z cylinder run along X, rotate [0,90,0]; along Y rotate [90,0,0].
Transform a boolean step's result after its operation. Primitive default
translation/rotation is [0,0,0], scale [1,1,1]. An assembly can also be transformed.

For a torus, inner_radius means hole radius (center to innermost surface), and
outer_radius means center to outermost surface, with 0 < inner < outer.
If the user instead supplies major radius R and tube/minor radius r, convert to
inner_radius=R-r and outer_radius=R+r. Do not confuse hole radius with tube radius.
Ellipsoid radius_x/y/z are semi-axis lengths. Capsule height is TOTAL end-to-end
height, at least 2*radius. Cone height includes the tip. Thick/thickness maps to
height. A centered Z bore is available directly on boxes, cylinders and gears;
use difference for other cuts. An unqualified bore size means diameter; halve
it for bore_radius. Preserve explicit radius versus diameter wording.

Preserve all stated measurements. For a gear, infer root radius at 80% of outer
radius when absent. Infer positions from unambiguous relations such as centered,
flush or attached. For missing essential dimensions, ambiguous arrangements,
or objects that cannot be represented by this geometry language, return the
error schema asking for clarification. Do not silently substitute an unrelated
shape or invent dimensions. Return JSON only.
Omit dimensions that do not apply to a shape; do not fill them with zeros.
Omit optional transforms unless needed. Default scale is [1,1,1], never zeros.
""".strip()


class GeometryParseError(RuntimeError):
    """Raised when Gemini cannot produce a usable geometry specification."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def geometry_response_schema() -> dict:
    """Keep the provider grammar flat; validate conditional rules locally."""
    transforms = {name: {"type": "array", "items": {"type": "number"},
                         "description": description + "; exactly three numbers"}
                  for name, description in {
                      "translation": "X,Y,Z displacement in cm, applied last",
                      "rotation": "Euler angles in degrees around fixed X then Y then Z",
                      "scale": "Positive scale factors on local X,Y,Z, applied first",
                  }.items()}
    all_dimensions = {
        name: {"type": ["integer" if name == "teeth" else "number", "null"],
               "description": "Tooth count" if name == "teeth" else f"{name} in centimeters"}
        for required in PRIMITIVE_FIELDS.values() for name in required
    }
    all_dimensions["bore_radius"] = {"type": ["number", "null"], "description": "Through-bore radius in cm; 0 means solid"}
    dimensions = {
        "type": "object", "properties": all_dimensions, "additionalProperties": False,
        "description": "Required fields by shape: " + "; ".join(f"{name}: {', '.join(fields)}" for name, fields in PRIMITIVE_FIELDS.items()),
    }
    step_schema = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "shape_type": {"type": "string", "enum": list(PRIMITIVE_FIELDS) + list(BOOLEAN_OPERATIONS)},
            "dimensions": dimensions,
            "inputs": {"type": "array", "items": {"type": "integer"},
                       "description": "Required for boolean steps: at least two earlier zero-based step indices"},
            **transforms,
        }, "required": ["shape_type"],
    }
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "shape_type": {"type": "string", "enum": list(PRIMITIVE_FIELDS) + ["assembly", "error"]},
            "dimensions": dimensions,
            "steps": {"type": "array", "description": f"Assembly only: 1 to {MAX_STEPS} ordered construction steps", "items": step_schema},
            "error": {"type": "string", "description": "Only for shape_type error: explain missing information or unsupported geometry"},
            **transforms,
        }, "required": ["shape_type"],
    }


def _normalize_payload(payload):
    """Ignore only empty placeholders for fields inapplicable to this shape.

    Required zero dimensions still fail validation; nonzero unexpected fields
    also fail. Never infer or substitute missing dimensions from the prompt.
    """
    if not isinstance(payload, dict):
        return payload
    payload = {key: value for key, value in payload.items() if value is not None}
    if payload.get("error") == "":
        payload.pop("error")
    shape = payload.get("shape_type")
    allowed = set(PRIMITIVE_FIELDS.get(shape, ()))
    if shape in {"box", "cylinder", "gear"}:
        allowed.add("bore_radius")
    if "height" in allowed:
        allowed.add("thickness")
    if isinstance(payload.get("dimensions"), dict):
        payload["dimensions"] = {
            key: value for key, value in payload["dimensions"].items()
            if value is not None and (key in allowed or value != 0)
        }
    if isinstance(payload.get("steps"), list):
        payload["steps"] = [_normalize_payload(step) for step in payload["steps"]]
    return payload


def parse_geometry_prompt(prompt: str) -> GeometrySpec:
    """Parse *prompt* through Gemini's structured-output interface."""

    prompt = prompt.strip()
    if not prompt:
        raise ValueError("prompt must not be empty")

    api_key = gemini_key()
    if not api_key:
        raise GeometryParseError("Set GEMINI_API_KEY in .env and restart the backend, or use Manual dimensions.", 503)

    model_name = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    fallback_model = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.8-flash")
    model_names = list(dict.fromkeys(name.strip() for name in (model_name, fallback_model) if name.strip()))
    try:
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            # JSON Schema avoids the SDK's narrower OpenAPI Schema adapter,
            # which rejects valid Pydantic keywords in google-genai 3.x.
            response_json_schema=geometry_response_schema(),
        )
        response = None
        with genai.Client(api_key=api_key, http_options=types.HttpOptions(
            timeout=30000, retry_options=types.HttpRetryOptions(attempts=1),
        )) as client:
            for index, candidate_model in enumerate(model_names):
                try:
                    response = client.models.generate_content(model=candidate_model, contents=prompt, config=config)
                    break
                except Exception as exc:
                    code = getattr(exc, "code", None)
                    logger.warning("Gemini model %s failed (%s, status=%s)", candidate_model, type(exc).__name__, code)
                    if code not in {404, 408, 429, 500, 502, 503, 504} or index == len(model_names) - 1:
                        raise
        if response is None:
            raise GeometryParseError("Gemini returned no response")
        if isinstance(response.parsed, GeometrySpec):
            return response.parsed
        if response.parsed is not None:
            payload = response.parsed
        elif response.text:
            payload = json.loads(response.text)
        else:
            raise GeometryParseError("Gemini returned an empty response")
        if isinstance(payload, dict) and payload.get("error"):
            raise GeometryParseError(str(payload["error"]), 422)
        return GeometrySpec.model_validate(_normalize_payload(payload))
    except GeometryParseError:
        raise
    except ValidationError as exc:
        problems = "; ".join(error["msg"] for error in exc.errors(include_input=False))
        raise GeometryParseError(f"Gemini returned invalid geometry: {problems}. Try specifying every dimension, or use Manual dimensions.", 422) from exc
    except Exception as exc:
        code = getattr(exc, "code", None)
        if code in {401, 403}:
            message = "Gemini rejected the API key or its permissions. Check the backend .env key."
        elif code == 404:
            message = "The configured Gemini models are unavailable for this key. Check GEMINI_MODEL in .env."
        elif code == 429:
            message = "Gemini quota or rate limit reached. Check your API quota, retry later, or use Manual dimensions."
        elif code in {408, 500, 502, 503, 504} or "timeout" in type(exc).__name__.lower():
            message = "Gemini timed out or is temporarily unavailable. Retry or use Manual dimensions."
        elif code == 400:
            message = "Gemini rejected the request configuration. Check that the API key and configured model support structured output."
        else:
            message = "Could not complete the Gemini request. Check network connectivity and API configuration, or use Manual dimensions."
        logger.warning("Gemini parsing failed (%s, status=%s)", type(exc).__name__, code)
        raise GeometryParseError(message, 503 if code in {429, 500, 502, 503, 504} else 502) from exc

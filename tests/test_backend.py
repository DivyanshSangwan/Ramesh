"""Offline regression checks: no paid Gemini requests or persistent model files."""

import io
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import trimesh
import numpy as np
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend import ai_parser, main
from backend.ai_parser import Dimensions, GeometrySpec, GeometryParseError
from backend.mesh_builder import MeshBuildError, build_mesh
from backend.ray_volume import calculate_ray_volume


class GeometryTests(unittest.TestCase):
    def test_shapes_and_volume(self):
        cases = [
            ("box", dict(width=8, depth=6, height=3)),
            ("box", dict(width=8, depth=6, height=3, bore_radius=.6)),
            ("cylinder", dict(radius=3, height=8)),
            ("cylinder", dict(radius=3, height=8, bore_radius=1)),
            ("gear", dict(outer_radius=4, root_radius=3.1, height=1.2, teeth=18)),
            ("gear", dict(outer_radius=4, root_radius=3.1, thickness=1.2, teeth=18, bore_radius=.6)),
            ("sphere", dict(radius=5)),
            ("torus", dict(inner_radius=5, outer_radius=10)),
            ("cone", dict(radius=3, height=8)),
            ("ellipsoid", dict(radius_x=4, radius_y=3, radius_z=2)),
            ("capsule", dict(radius=2, height=8)),
        ]
        for shape, dimensions in cases:
            with self.subTest(shape=shape, dimensions=dimensions):
                mesh = build_mesh(GeometrySpec(shape_type=shape, dimensions=dimensions))
                self.assertTrue(mesh.is_volume)
                result = calculate_ray_volume(mesh, 80)
                self.assertLess(abs(result.volume_cm3 - mesh.volume) / mesh.volume, .02)
                self.assertEqual(result.odd_intersection_rays, 0)

    def test_bored_box_keeps_exact_corners(self):
        mesh = build_mesh(GeometrySpec(shape_type="box", dimensions=dict(width=8, depth=6, height=3, bore_radius=.5)))
        # Polygonal bore slightly overestimates analytic volume, never chamfers corners.
        expected = (8 * 6 - math.pi * .5**2) * 3
        self.assertGreaterEqual(mesh.volume, expected)
        self.assertLess(mesh.volume - expected, .002)

    def test_even_odd_handles_multiple_solids_on_one_ray(self):
        bottom = trimesh.creation.box(extents=(2, 3, 1))
        top = bottom.copy()
        top.apply_translation((0, 0, 4))
        mesh = trimesh.util.concatenate([bottom, top])
        self.assertAlmostEqual(calculate_ray_volume(mesh, 20).volume_cm3, 12)

    def test_invalid_dimensions_rejected(self):
        for radius in [-1, 0, float("nan"), float("inf")]:
            with self.subTest(radius=radius), self.assertRaises(ValidationError):
                Dimensions(radius=radius)
        with self.assertRaises(ValidationError):
            GeometrySpec(shape_type="cylinder", dimensions=dict(radius=1, height=2, bore_radius=1))
        with self.assertRaises(ValidationError):
            GeometrySpec(shape_type="gear", dimensions=dict(outer_radius=4, root_radius=3, height=1, thickness=2, teeth=18))
        with self.assertRaises(ValidationError):
            GeometrySpec(shape_type="sphere", dimensions=dict(radius=5, bore_radius=1))

    def test_torus_geometry_and_all_ray_axes(self):
        mesh = build_mesh(GeometrySpec(shape_type="torus", dimensions=dict(inner_radius=5, outer_radius=10)))
        analytic = 2 * math.pi**2 * 7.5 * 2.5**2
        self.assertLess(abs(mesh.volume / analytic - 1), .003)
        self.assertTrue(np.allclose(mesh.extents, [20, 20, 5]))
        for axis in "xyz":
            result = calculate_ray_volume(mesh, 80, axis=axis)
            self.assertEqual(result.ray_axis, axis)
            self.assertLess(abs(result.volume_cm3 / mesh.volume - 1), .01)
        for inner, outer in [(5, 5), (10, 5), (0, 10)]:
            with self.assertRaises(ValidationError):
                GeometrySpec(shape_type="torus", dimensions=dict(inner_radius=inner, outer_radius=outer))

    def test_rays_physically_follow_selected_axis(self):
        from trimesh.ray.ray_triangle import RayMeshIntersector
        mesh = trimesh.creation.box(extents=[2, 3, 7])
        mesh.apply_translation([5, -7, 11])
        original = RayMeshIntersector.intersects_location
        for index, axis in enumerate("xyz"):
            with patch.object(RayMeshIntersector, "intersects_location", autospec=True, side_effect=original) as cast:
                result = calculate_ray_volume(mesh, 20, axis=axis)
                self.assertAlmostEqual(result.volume_cm3, 42)
                directions = cast.call_args.kwargs["ray_directions"]
                origins = cast.call_args.kwargs["ray_origins"]
                self.assertTrue(np.allclose(directions, np.eye(3)[index]))
                self.assertTrue(np.all(origins[:, index] < mesh.bounds[0, index]))
        with self.assertRaises(ValueError):
            calculate_ray_volume(mesh, axis="bad")

    def test_boolean_volumes_and_transforms(self):
        boxes = [
            {"shape_type": "box", "dimensions": {"width": 2, "depth": 2, "height": 2}},
            {"shape_type": "box", "dimensions": {"width": 2, "depth": 2, "height": 2}, "translation": [1, 0, 0]},
        ]
        for operation, expected in [("union", 12), ("difference", 4), ("intersection", 4)]:
            spec = GeometrySpec(shape_type="assembly", steps=boxes + [{"shape_type": operation, "inputs": [0, 1]}],
                                scale=[1, 2, 3], rotation=[0, 0, 90], translation=[3, -2, 5])
            mesh = build_mesh(spec)
            self.assertTrue(mesh.is_volume)
            self.assertAlmostEqual(mesh.volume, expected * 6, places=4)
            for axis in "xyz":
                self.assertAlmostEqual(calculate_ray_volume(mesh, 20, axis=axis).volume_cm3, expected * 6, places=4)

    def test_enclosed_cavity_keeps_negative_inner_shell(self):
        outer = {"shape_type": "sphere", "dimensions": {"radius": 5}}
        inner = {"shape_type": "sphere", "dimensions": {"radius": 4}}
        spec = GeometrySpec(shape_type="assembly", steps=[outer, inner, {"shape_type": "difference", "inputs": [0, 1]}])
        mesh = build_mesh(spec)
        expected = build_mesh(GeometrySpec(**outer)).volume - build_mesh(GeometrySpec(**inner)).volume
        self.assertAlmostEqual(mesh.volume, expected, places=4)
        self.assertLess(abs(calculate_ray_volume(mesh, 80).volume_cm3 / expected - 1), .01)

    def test_assembly_rejects_invalid_and_empty_constructions(self):
        box = {"shape_type": "box", "dimensions": {"width": 2, "depth": 2, "height": 2}}
        for steps in [[], [box, box], [box, {"shape_type": "union", "inputs": [0, 2]}], [box] * 33]:
            with self.assertRaises(ValidationError):
                GeometrySpec(shape_type="assembly", steps=steps)
        with self.assertRaises(MeshBuildError):
            build_mesh(GeometrySpec(shape_type="assembly", steps=[box, box, {"shape_type": "difference", "inputs": [0, 1]}]))


class ParserTests(unittest.TestCase):
    def test_provider_schema_describes_primitives_and_boolean_steps(self):
        schema = ai_parser.geometry_response_schema()
        self.assertIn("torus", schema["properties"]["shape_type"]["enum"])
        self.assertIn("height", schema["properties"]["dimensions"]["properties"])
        self.assertIn("difference", schema["properties"]["steps"]["items"]["properties"]["shape_type"]["enum"])
        self.assertIn("gear: outer_radius, root_radius, height, teeth", schema["properties"]["dimensions"]["description"])

    def test_missing_key(self):
        with patch.object(ai_parser, "gemini_key", return_value=""), self.assertRaises(GeometryParseError) as context:
            ai_parser.parse_geometry_prompt("Sphere of radius 5 cm")
        self.assertEqual(context.exception.status_code, 503)

    def test_sdk_response_and_bounded_fallback(self):
        class CapacityError(Exception):
            code = 503

        with patch.object(ai_parser, "gemini_key", return_value="test-key"), patch.object(ai_parser.genai, "Client") as client:
            generate = client.return_value.__enter__.return_value.models.generate_content
            generate.side_effect = [CapacityError(), SimpleNamespace(parsed={"shape_type": "sphere", "dimensions": {"radius": 5}}, text=None)]
            spec = ai_parser.parse_geometry_prompt("Sphere of radius 5 cm")
            self.assertEqual(spec.dimensions.radius, 5)
            self.assertEqual(generate.call_count, 2)
            options = client.call_args.kwargs["http_options"]
            self.assertEqual(options.timeout, 30000)
            self.assertEqual(options.retry_options.attempts, 1)

    def test_model_omission_does_not_invent_a_dimension(self):
        with patch.object(ai_parser, "gemini_key", return_value="test-key"), patch.object(ai_parser.genai, "Client") as client:
            client.return_value.__enter__.return_value.models.generate_content.return_value = SimpleNamespace(
                parsed={"shape_type": "gear", "dimensions": {"outer_radius": 4, "root_radius": 3, "teeth": 18}}, text=None)
            with self.assertRaises(GeometryParseError) as context:
                ai_parser.parse_geometry_prompt("a gear")
            self.assertEqual(context.exception.status_code, 422)
            self.assertIn("height", str(context.exception))


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.models_patch = patch.object(main, "MODEL_DIR", Path(self.directory.name))
        self.models_patch.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        self.models_patch.stop()
        self.directory.cleanup()

    def test_manual_api_and_export_round_trip(self):
        response = self.client.post("/api/generate", json={"geometry": {"shape_type": "box", "dimensions": {"width": 8, "depth": 6, "height": 3}}, "ray_resolution": 40})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["source"], "manual")
        self.assertAlmostEqual(data["volume"]["actual_cm3"], 144)
        self.assertAlmostEqual(data["volume"]["ray_cast_liters"], .144)
        model = self.client.get(data["asset_path"])
        self.assertEqual(model.status_code, 200)
        self.assertIn("model/stl", model.headers["content-type"])
        loaded = trimesh.load(io.BytesIO(model.content), file_type="stl")
        self.assertTrue(loaded.is_volume)
        self.assertAlmostEqual(loaded.volume, 144)

    def test_prompt_api_runs_parser_then_real_mesh(self):
        with patch.object(ai_parser, "gemini_key", return_value="test-key"), patch.object(ai_parser.genai, "Client") as client:
            client.return_value.__enter__.return_value.models.generate_content.return_value = SimpleNamespace(
                parsed={"shape_type": "sphere", "dimensions": {"radius": 5}}, text=None)
            response = self.client.post("/api/generate", json={"prompt": "Sphere of radius 5 cm", "ray_resolution": 40})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["source"], "gemini")
        self.assertEqual(self.client.get(response.json()["asset_path"]).status_code, 200)

    def test_failure_status_and_input_limits(self):
        with patch.object(main, "parse_geometry_prompt", side_effect=GeometryParseError("Gemini timed out", 503)):
            response = self.client.post("/api/generate", json={"prompt": "Sphere of radius 5 cm"})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["detail"], "Gemini timed out")
        for body in [{}, {"prompt": "   "}, {"prompt": "box", "ray_resolution": 1000}, {"prompt": "box", "geometry": {"shape_type": "sphere", "dimensions": {"radius": 5}}}]:
            self.assertEqual(self.client.post("/api/generate", json=body).status_code, 422)

    def test_health_and_missing_asset(self):
        self.assertEqual(self.client.get("/api/health").json()["status"], "ok")
        self.assertEqual(self.client.get("/models/missing.stl").status_code, 404)
        with self.assertRaises(main.HTTPException):
            main.get_model("../.env")

    def test_axis_selection_and_recast_reuse_same_asset(self):
        response = self.client.post("/api/generate", json={
            "geometry": {"shape_type": "torus", "dimensions": {"inner_radius": 5, "outer_radius": 10}},
            "ray_axis": "x", "ray_resolution": 40,
        })
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["volume"]["ray_axis"], "x")
        files_before = list(Path(self.directory.name).iterdir())
        with patch.object(main, "parse_geometry_prompt", side_effect=AssertionError("Recast must not call Gemini")):
            for axis in ["y", "z"]:
                r = self.client.post("/api/volume", json={"filename": data["filename"], "ray_axis": axis, "ray_resolution": 40})
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(r.json()["ray_axis"], axis)
                self.assertAlmostEqual(r.json()["actual_cm3"], data["volume"]["actual_cm3"], places=3)
        self.assertEqual(list(Path(self.directory.name).iterdir()), files_before)
        self.assertEqual(self.client.post("/api/volume", json={"filename": data["filename"], "ray_axis": "q"}).status_code, 422)
        self.assertEqual(self.client.post("/api/volume", json={"filename": "../.env"}).status_code, 422)


if __name__ == "__main__":
    unittest.main()

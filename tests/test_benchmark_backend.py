"""New benchmark API and numerical correctness tests; no paid API calls."""

import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import trimesh
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend import ai_analyzer, main
from backend.ai_analyzer import EngineeringAnalysis
from backend.mesh_generator import PrimitiveSpec, generate_primitive
from backend.mesh_validator import MeshValidationError, load_uploaded_mesh, validate_mesh
from backend.ray_volume import benchmark_mesh


class PrimitiveAndBenchmarkTests(unittest.TestCase):
    def test_cuboid_exact_volume_and_full_surface_area_on_every_axis(self):
        mesh = generate_primitive(PrimitiveSpec(shape_type="cuboid", parameters={"length": 2, "breadth": 3, "height": 4})).mesh
        for axis in "XYZ":
            r = benchmark_mesh(mesh, 32, axis)
            self.assertAlmostEqual(r.raycast_volume_cm3, 24)
            self.assertAlmostEqual(r.raycast_surface_area_cm2, 52)
            self.assertAlmostEqual(r.volume_error_percent, 0)
            self.assertAlmostEqual(r.surface_area_error_percent, 0)
            self.assertEqual(r.total_unique_ray_count, 3 * 32**2)
            self.assertEqual(r.direction, axis)

    def test_curved_shapes_against_independent_analytic_references(self):
        cases = [
            ("sphere", {"radius": 2}, 4 * math.pi * 8 / 3, 16 * math.pi),
            ("cylinder", {"radius": 2, "height": 3}, 12 * math.pi, 20 * math.pi),
            ("torus", {"inner_radius": 5, "outer_radius": 10}, 2 * math.pi**2 * 7.5 * 2.5**2, 4 * math.pi**2 * 7.5 * 2.5),
        ]
        for name, parameters, volume, area in cases:
            with self.subTest(shape=name):
                g = generate_primitive(PrimitiveSpec(shape_type=name, parameters=parameters))
                self.assertTrue(validate_mesh(g.mesh).mesh.is_volume)
                r = benchmark_mesh(g.mesh, 64, "Y")
                self.assertLess(abs(r.raycast_volume_cm3 / volume - 1), .015)
                self.assertLess(abs(r.raycast_surface_area_cm2 / area - 1), .02)
                self.assertAlmostEqual(r.surface_area_error_percent, 100 * abs(r.raycast_surface_area_cm2-r.ground_truth_surface_area_cm2)/r.ground_truth_surface_area_cm2)

    def test_pyramid_cone_switch_and_edge_length_convention(self):
        for n in [3, 4, 31, 32, 64]:
            g = generate_primitive(PrimitiveSpec(shape_type="pyramid_cone", parameters={"height": 5, "base_sides": n, "base_side_length": 2}))
            self.assertTrue(g.mesh.is_volume)
            if n == 4:
                self.assertAlmostEqual(g.mesh.volume, 4 * 5 / 3)
            r = benchmark_mesh(g.mesh, 64)
            self.assertLess(r.volume_error_percent, 1)
            self.assertLess(r.surface_area_error_percent, 2)

    def test_invalid_parameters(self):
        for parameters in [{"radius": 0}, {"radius": float("nan")}, {"radius": float("inf")}, {"radius": 5, "width": 1}]:
            with self.assertRaises(ValidationError):
                PrimitiveSpec(shape_type="sphere", parameters=parameters)
        with self.assertRaises(ValidationError):
            PrimitiveSpec(shape_type="torus", parameters={"inner_radius": 10, "outer_radius": 5})
        with self.assertRaises(ValidationError):
            PrimitiveSpec(shape_type="pyramid_cone", parameters={"height": 5, "base_sides": 3.5, "base_side_length": 2})

    def test_rotated_box_surface_is_numerically_integrated(self):
        mesh = trimesh.creation.box(extents=[2, 3, 4])
        mesh.apply_transform(trimesh.transformations.euler_matrix(.31, .57, .19))
        r = benchmark_mesh(mesh, 80, "Z")
        self.assertLess(r.volume_error_percent, .5)
        self.assertLess(r.surface_area_error_percent, 1)


class ValidatorTests(unittest.TestCase):
    def test_repair_small_hole_and_report_changes(self):
        mesh = trimesh.creation.box()
        mesh.update_faces(np.arange(len(mesh.faces) - 1))
        self.assertFalse(mesh.is_watertight)
        result = validate_mesh(mesh)
        self.assertTrue(result.mesh.is_volume)
        self.assertTrue(result.report.repair_attempted)
        self.assertEqual(result.report.faces_added_by_repair, 1)
        self.assertFalse(mesh.is_watertight, "Validator should not mutate its caller's mesh")

    def test_unrepairable_mesh_rejected(self):
        mesh = trimesh.Trimesh(vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 1]], faces=[[0, 1, 2]])
        with self.assertRaises(MeshValidationError):
            validate_mesh(mesh)

    def test_scaling_stl_obj_and_orientation(self):
        source = trimesh.creation.box(extents=[2, 3, 4])
        for file_type in ["stl", "obj"]:
            data = source.export(file_type=file_type)
            if isinstance(data, str):
                data = data.encode()
            loaded = load_uploaded_mesh(data, "input." + file_type)
            loaded.invert()
            result = validate_mesh(loaded, [2, 3, 4])
            self.assertTrue(result.report.orientation_flipped)
            self.assertAlmostEqual(result.mesh.volume, 576)
            self.assertAlmostEqual(result.mesh.area, 488)
        with self.assertRaises(MeshValidationError):
            validate_mesh(source, [1, 0, 1])

    def test_obj_with_uvs_materials_and_groups_parses_without_pillow(self):
        obj_data = b"""mtllib missing.mtl
o TexturedBox
g BottomAndTop
v 0 0 0
v 2 0 0
v 2 3 0
v 0 3 0
v 0 0 4
v 2 0 4
v 2 3 4
v 0 3 4
vt 0.0 0.0
vt 1.0 0.0
vt 1.0 1.0
vt 0.0 1.0
vn 0.0 0.0 1.0
usemtl MatA
f 1/1/1 3/3/1 2/2/1
f 1/1/1 4/4/1 3/3/1
f 5/1/1 6/2/1 7/3/1
f 5/1/1 7/3/1 8/4/1
usemtl MatB
f 1/1/1 2/2/1 6/3/1
f 1/1/1 6/3/1 5/4/1
f 2/1/1 3/2/1 7/3/1
f 2/1/1 7/3/1 6/4/1
f 3/1/1 4/2/1 8/3/1
f 3/1/1 8/3/1 7/4/1
f 4/1/1 1/2/1 5/3/1
f 4/1/1 5/3/1 8/4/1
"""
        loaded = load_uploaded_mesh(obj_data, "textured.obj")
        validated = validate_mesh(loaded, [1, 1, 1])
        self.assertTrue(validated.mesh.is_volume)
        self.assertAlmostEqual(validated.mesh.volume, 24.0)
        self.assertAlmostEqual(validated.mesh.area, 52.0)

    def test_large_polygon_holes_and_multi_component_repair(self):
        # Create a cylinder and remove the entire top cap (16-edge polygon hole > 4 edges)
        cyl = trimesh.creation.cylinder(radius=3.0, height=6.0, sections=16)
        top_faces = cyl.triangles_center[:, 2] > 2.9
        open_cyl = cyl.copy()
        open_cyl.update_faces(~top_faces)
        self.assertFalse(open_cyl.is_watertight)
        # Also include a stray 2-face zero-volume sheet to verify component cleanup
        sheet = trimesh.Trimesh(
            vertices=[[10, 0, 0], [11, 0, 0], [11, 1, 0], [10, 1, 0]],
            faces=[[0, 1, 2], [0, 2, 3]],
        )
        combined = trimesh.util.concatenate([open_cyl, sheet])
        self.assertFalse(combined.is_watertight)
        repaired = validate_mesh(combined)
        self.assertTrue(repaired.mesh.is_watertight)
        self.assertTrue(repaired.mesh.is_volume)
        self.assertTrue(repaired.report.repair_attempted)
        self.assertAlmostEqual(repaired.mesh.volume, cyl.volume, places=2)


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.metrics = benchmark_mesh(trimesh.creation.box(), 8)

    def test_missing_key_and_upstream_failure_preserve_results(self):
        with patch.object(ai_analyzer, "gemini_key", return_value=""):
            self.assertEqual(ai_analyzer.analyze_telemetry(self.metrics).status, "unavailable")
        with patch.object(ai_analyzer, "gemini_key", return_value="test-key"), patch.object(ai_analyzer.genai, "Client", side_effect=RuntimeError("offline")):
            self.assertEqual(ai_analyzer.analyze_telemetry(self.metrics).status, "unavailable")
        self.assertEqual(ai_analyzer.analyze_telemetry(self.metrics, enabled=False).status, "disabled")

    def test_report_receives_numbers_and_returns_text(self):
        with patch.object(ai_analyzer, "gemini_key", return_value="test-key"), patch.object(ai_analyzer.genai, "Client") as client:
            generate = client.return_value.__enter__.return_value.models.generate_content
            generate.return_value = SimpleNamespace(text="Volume and surface area agree with the reference mesh.")
            result = ai_analyzer.analyze_telemetry(self.metrics)
            self.assertEqual(result.status, "complete")
            self.assertIn('"ground_truth_surface_area_cm2": 6.0', generate.call_args.kwargs["contents"])
            self.assertIn('"ray_density": 8', generate.call_args.kwargs["contents"])


class BenchmarkApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.models_patch = patch.object(main, "MODEL_DIR", Path(self.directory.name))
        self.models_patch.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        self.models_patch.stop()
        self.directory.cleanup()

    def test_generate_download_and_optional_ai(self):
        body = {"shape_type": "cuboid", "parameters": {"length": 2, "breadth": 3, "height": 4}, "direction": "X", "ray_density": 32, "include_ai": False}
        response = self.client.post("/api/generate-primitive", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["source"], "primitive")
        self.assertEqual(data["ai_analysis"]["status"], "disabled")
        self.assertAlmostEqual(data["telemetry"]["ground_truth_volume_cm3"], 24)
        self.assertAlmostEqual(data["telemetry"]["raycast_surface_area_cm2"], 52)
        self.assertEqual(self.client.get(data["asset_path"]).status_code, 200)
        body["include_ai"] = True
        with patch.object(main, "analyze_telemetry", return_value=EngineeringAnalysis(status="unavailable", message="offline")):
            response = self.client.post("/api/generate-primitive", json=body)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["ai_analysis"]["status"], "unavailable")

    def test_upload_stl_obj_scale_and_metrics(self):
        mesh = trimesh.creation.box(extents=[2, 3, 4])
        for extension in ["stl", "obj"]:
            data = mesh.export(file_type=extension)
            response = self.client.post("/api/upload-and-analyze", files={"file": ("part." + extension, data)},
                                        data={"scale_x": 2, "scale_y": 3, "scale_z": 4, "direction": "Y", "ray_density": 32, "include_ai": "false"})
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            self.assertEqual(result["source"], "upload")
            self.assertAlmostEqual(result["telemetry"]["raycast_volume_cm3"], 576)
            self.assertAlmostEqual(result["telemetry"]["raycast_surface_area_cm2"], 488)
            self.assertEqual(result["validation"]["scale"], [2, 3, 4])

    def test_rejects_bad_requests(self):
        self.assertEqual(self.client.post("/api/generate-primitive", json={"shape_type": "sphere", "parameters": {"radius": 0}}).status_code, 422)
        self.assertEqual(self.client.post("/api/upload-and-analyze", files={"file": ("part.txt", b"bad")}).status_code, 415)
        self.assertEqual(self.client.post("/api/upload-and-analyze", files={"file": ("part.stl", b"bad")}).status_code, 422)
        self.assertEqual(self.client.post("/api/upload-and-analyze", content=b"", headers={"content-length": str(22 * 1024 * 1024)}).status_code, 413)
        mesh = trimesh.creation.box().export(file_type="stl")
        self.assertEqual(self.client.post("/api/upload-and-analyze", files={"file": ("part.stl", mesh)}, data={"scale_x": 0}).status_code, 422)

    def test_chunked_upload_is_bounded_without_content_length(self):
        def chunks():
            yield b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="part.stl"\r\nContent-Type: application/octet-stream\r\n\r\n'
            for _ in range(22):
                yield b"x" * (1024 * 1024)
            yield b'\r\n--boundary--\r\n'
        response = self.client.post("/api/upload-and-analyze", content=chunks(), headers={"content-type": "multipart/form-data; boundary=boundary"})
        self.assertEqual(response.status_code, 413, response.text)


if __name__ == "__main__":
    unittest.main()

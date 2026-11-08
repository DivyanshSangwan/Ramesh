import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { OBJLoader } from "three/addons/loaders/OBJLoader.js";
import { STLLoader } from "three/addons/loaders/STLLoader.js";
import { mergeGeometries } from "three/addons/utils/BufferGeometryUtils.js";

// Parse value as finite positive number bounded by `max`, or return null.
function positiveNumber(val, max = 10000) {
  const num = Number(val);
  return Number.isFinite(num) && num > 0 && num <= max ? num : null;
}

// Build client-side Three.js BufferGeometry matching backend primitive dimensions for instant preview.
function buildPrimitiveGeometry(shape, parameters) {
  if (!parameters) return null;
  if (shape === "sphere") {
    const r = positiveNumber(parameters.radius);
    if (!r) return null;
    return new THREE.IcosahedronGeometry(r, 5);
  }
  if (shape === "cuboid") {
    const a = positiveNumber(parameters.length);
    const b = positiveNumber(parameters.breadth);
    const c = positiveNumber(parameters.height);
    if (!a || !b || !c) return null;
    return new THREE.BoxGeometry(a, b, c);
  }
  if (shape === "torus") {
    // Convert inner/outer ring radii into Three.js major radius and tube radius.
    const inner = positiveNumber(parameters.inner_radius);
    const outer = positiveNumber(parameters.outer_radius);
    if (!inner || !outer || inner >= outer) return null;
    const major = (outer + inner) / 2;
    const minor = (outer - inner) / 2;
    return new THREE.TorusGeometry(major, minor, 48, 96);
  }
  if (shape === "cylinder") {
    // Rotate Three.js Y-aligned cylinder by +90 deg around X so height aligns with Z axis.
    const r = positiveNumber(parameters.radius);
    const h = positiveNumber(parameters.height);
    if (!r || !h) return null;
    const geom = new THREE.CylinderGeometry(r, r, h, 96);
    geom.rotateX(Math.PI / 2);
    return geom;
  }
  if (shape === "pyramid_cone") {
    // Compute polygon circumradius from side length and rotate cone to align height with Z axis.
    const h = positiveNumber(parameters.height);
    const rawSides = Number(parameters.base_sides);
    const side = positiveNumber(parameters.base_side_length);
    if (!h || !side || !Number.isInteger(rawSides) || rawSides < 3 || rawSides > 256) return null;
    const radius = side / (2 * Math.sin(Math.PI / rawSides));
    const segments = rawSides >= 32 ? Math.max(96, rawSides) : rawSides;
    const geom = new THREE.ConeGeometry(radius, h, segments);
    geom.rotateX(Math.PI / 2);
    return geom;
  }
  return null;
}

export default function MeshViewport({
  assetPath,
  mode,
  shape,
  parameters,
  file,
  scale,
  loading,
  rayAxis,
  rayDensity,
  onBoundsChange,
}) {
  const mountRef = useRef(null);
  const sceneRef = useRef(null);
  const onBoundsChangeRef = useRef(onBoundsChange);
  onBoundsChangeRef.current = onBoundsChange;
  const [viewerError, setViewerError] = useState("");
  const [meshReady, setMeshReady] = useState(false);
  const [logicalKey, setLogicalKey] = useState("");
  const [uploadedBaseGeometry, setUploadedBaseGeometry] = useState(null);
  const lastFetchedAssetRef = useRef(null);

  // Representative rays keep rendering lightweight; numerical grids run on the server.
  const numericDensity = Math.max(8, Math.min(512, Number(rayDensity) || 8));
  const visualEdge = Math.min(20, Math.max(3, Math.round(Math.sqrt(numericDensity))));

  // Initialize WebGL renderer, camera, orbit controls, lighting, ground grid, and render loop.
  useEffect(() => {
    const mount = mountRef.current;
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x080b10);
    const camera = new THREE.PerspectiveCamera(40, 1, 0.001, 10000);
    camera.position.set(19, 15, 22);
    let renderer;
    try { renderer = new THREE.WebGLRenderer({ antialias: true }); }
    catch { setViewerError("WebGL is unavailable. Enable browser hardware acceleration to view the model. STL downloads still work."); return; }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    mount.appendChild(renderer.domElement);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.autoRotate = false;
    scene.add(new THREE.HemisphereLight(0xaecbff, 0x111827, 2.2));
    const key = new THREE.DirectionalLight(0x78e6d0, 4.5);
    key.position.set(5, 9, 7);
    scene.add(key);
    const rim = new THREE.DirectionalLight(0x6d7cff, 3);
    rim.position.set(-7, 2, -5);
    scene.add(rim);
    const grid = new THREE.GridHelper(30, 30, 0x263343, 0x17202b);
    grid.position.y = -5.2;
    scene.add(grid);
    const state = {
      scene,
      camera,
      controls,
      grid,
      mesh: null,
      logicalSize: null,
      rayAnimation: null,
      fit: null,
      // Replace active mesh geometry, compute bounding box, report live bounds, and fit camera distance.
      applyGeometry: (geometry) => {
        if (state.mesh) {
          state.scene.remove(state.mesh);
          state.mesh.geometry.dispose();
          state.mesh.material.dispose();
          state.mesh = null;
        }
        if (!geometry) {
          state.logicalSize = null;
          state.fit = null;
          setMeshReady(false);
          setLogicalKey("");
          onBoundsChangeRef.current?.(null);
          return false;
        }
        geometry.computeVertexNormals();
        geometry.center();
        geometry.computeBoundingBox();
        const logicalSize = geometry.boundingBox.getSize(new THREE.Vector3());
        if (![logicalSize.x, logicalSize.y, logicalSize.z].every((v) => Number.isFinite(v) && v > 0)) {
          geometry.dispose();
          onBoundsChangeRef.current?.(null);
          return false;
        }
        state.logicalSize = logicalSize;
        onBoundsChangeRef.current?.([logicalSize.x, logicalSize.y, logicalSize.z]);
        const mesh = new THREE.Mesh(
          geometry,
          new THREE.MeshPhysicalMaterial({ color: 0x65d7c4, metalness: 0.58, roughness: 0.28, clearcoat: 0.55 })
        );
        mesh.rotation.x = -Math.PI / 2; // STL Z is vertical; ray geometry uses the same rotation.
        state.scene.add(mesh);
        state.mesh = mesh;
        const box = new THREE.Box3().setFromObject(mesh);
        const size = box.getSize(new THREE.Vector3());
        const span = Math.max(size.x, size.y, size.z);
        state.fit = (resetCamera = false) => {
          const vertical = THREE.MathUtils.degToRad(state.camera.fov);
          const horizontal = 2 * Math.atan(Math.tan(vertical / 2) * state.camera.aspect);
          const distance = size.length() / 2 / Math.sin(Math.min(vertical, horizontal) / 2) * 1.25;
          if (resetCamera || !state.cameraDistance || distance > state.cameraDistance * 1.4 || distance < state.cameraDistance * 0.25) {
            state.cameraDistance = distance;
            state.camera.position.copy(new THREE.Vector3(1.35, 1.05, 1.55).normalize().multiplyScalar(distance));
            state.controls.target.set(0, 0, 0);
          }
          const activeDist = state.cameraDistance || distance;
          state.camera.near = Math.max(span / 1000, 0.000001);
          state.camera.far = Math.max(span * 100, activeDist * 5);
          state.camera.updateProjectionMatrix();
          state.controls.update();
        };
        state.fit(!state.cameraDistance);
        state.grid.position.y = box.min.y - span * 0.02;
        if (!state.gridScale || span > state.gridSpan * 2.5 || span < state.gridSpan * 0.25) {
          state.gridSpan = span;
          state.gridScale = span / 10;
          state.grid.scale.setScalar(state.gridScale);
        }
        setMeshReady(true);
        setLogicalKey(`${logicalSize.x.toFixed(5)},${logicalSize.y.toFixed(5)},${logicalSize.z.toFixed(5)}`);
        return true;
      },
    };
    sceneRef.current = state;
    const resize = () => {
      camera.aspect = mount.clientWidth / Math.max(mount.clientHeight, 1);
      camera.updateProjectionMatrix();
      renderer.setSize(mount.clientWidth, mount.clientHeight, false);
      state.fit?.();
    };
    const observer = new ResizeObserver(resize);
    observer.observe(mount);
    resize();
    let frame;
    const animate = (time = 0) => {
      state.rayAnimation?.(time);
      controls.update();
      renderer.render(scene, camera);
      frame = requestAnimationFrame(animate);
    };
    animate();
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      controls.dispose();
      if (state.mesh) {
        state.mesh.geometry.dispose();
        state.mesh.material.dispose();
      }
      grid.geometry.dispose();
      grid.material.dispose();
      renderer.dispose();
      renderer.domElement.remove();
      sceneRef.current = null;
    };
  }, []);

  // Parse local uploaded file (STL or OBJ) for immediate live preview & scaling
  useEffect(() => {
    let cancelled = false;
    if (!file) {
      setUploadedBaseGeometry((prev) => {
        prev?.dispose();
        return null;
      });
      return;
    }
    const isObj = /\.obj$/i.test(file.name);
    const reader = new FileReader();
    reader.onload = () => {
      if (cancelled) return;
      try {
        let geometry = null;
        if (isObj) {
          const text = typeof reader.result === "string" ? reader.result : new TextDecoder().decode(reader.result);
          const group = new OBJLoader().parse(text);
          const geometries = [];
          group.traverse((child) => {
            if (child.isMesh && child.geometry) {
              const g = child.geometry.clone();
              const pos = g.getAttribute("position");
              if (pos && pos.count >= 3) {
                const clean = new THREE.BufferGeometry();
                clean.setAttribute("position", pos);
                if (g.index) clean.setIndex(g.index);
                geometries.push(clean.index ? clean.toNonIndexed() : clean);
              }
              g.dispose();
            }
          });
          if (geometries.length === 1) {
            geometry = geometries[0];
          } else if (geometries.length > 1) {
            geometry = mergeGeometries(geometries, false);
            geometries.forEach((g) => g.dispose());
          }
        } else {
          geometry = new STLLoader().parse(reader.result);
        }
        if (geometry && geometry.getAttribute("position")?.count >= 3) {
          setViewerError("");
          setUploadedBaseGeometry((prev) => {
            prev?.dispose();
            return geometry;
          });
        } else {
          geometry?.dispose();
          setUploadedBaseGeometry((prev) => {
            prev?.dispose();
            return null;
          });
        }
      } catch {
        setUploadedBaseGeometry((prev) => {
          prev?.dispose();
          return null;
        });
      }
    };
    if (isObj) reader.readAsText(file);
    else reader.readAsArrayBuffer(file);
    return () => {
      cancelled = true;
    };
  }, [file]);

  // Live preview when primitive parameters or upload scaling change
  useEffect(() => {
    const state = sceneRef.current;
    if (!state) return;
    if (mode === "primitive") {
      const geom = buildPrimitiveGeometry(shape, parameters);
      if (geom) {
        setViewerError("");
        state.applyGeometry(geom);
      }
    } else if (mode === "upload") {
      if (!uploadedBaseGeometry) {
        if (!assetPath) state.applyGeometry(null);
        return;
      }
      const sx = positiveNumber(scale?.x, 100);
      const sy = positiveNumber(scale?.y, 100);
      const sz = positiveNumber(scale?.z, 100);
      if (!sx || !sy || !sz) return;
      const geom = uploadedBaseGeometry.clone();
      geom.scale(sx, sy, sz);
      setViewerError("");
      state.applyGeometry(geom);
    }
  }, [mode, shape, parameters, uploadedBaseGeometry, scale, assetPath]);

  // Load exported STL from backend when a benchmark completes
  useEffect(() => {
    const state = sceneRef.current;
    if (!assetPath || !state || assetPath === lastFetchedAssetRef.current) return;
    lastFetchedAssetRef.current = assetPath;
    setViewerError("");
    let cancelled = false;
    new STLLoader().load(
      assetPath,
      (geometry) => {
        if (cancelled) {
          geometry.dispose();
          return;
        }
        if (!state.applyGeometry(geometry)) {
          setViewerError("The downloaded STL contains no valid solid geometry.");
        }
      },
      undefined,
      () => {
        if (!cancelled) {
          setViewerError("Could not download the STL. Check that the backend is running, then run the benchmark again.");
        }
      }
    );
    return () => {
      cancelled = true;
    };
  }, [assetPath]);

  // Build 3D grid of ray lines along selected scan axis and animate pulse segments while benchmarking.
  useEffect(() => {
    const state = sceneRef.current;
    if (!state || (!meshReady && !loading)) return;
    const size = (state.logicalSize || new THREE.Vector3(10, 10, 10)).toArray();
    const axis = { X: 0, Y: 1, Z: 2 }[rayAxis];
    const plane = [0, 1, 2].filter((i) => i !== axis);
    const span = Math.max(...size);
    const start = -size[axis] / 2 - span * 0.25;
    const travel = size[axis] + span * 0.5;
    const positions = new Float32Array(visualEdge ** 2 * 6);
    for (let u = 0; u < visualEdge; u++) {
      for (let v = 0; v < visualEdge; v++) {
        const offset = (u * visualEdge + v) * 6;
        for (const endpoint of [0, 3]) {
          positions[offset + endpoint + plane[0]] = ((u + 0.5) / visualEdge - 0.5) * size[plane[0]];
          positions[offset + endpoint + plane[1]] = ((v + 0.5) / visualEdge - 0.5) * size[plane[1]];
          positions[offset + endpoint + axis] = start + (endpoint === 3 ? travel : 0);
        }
      }
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3).setUsage(THREE.DynamicDrawUsage));
    const material = new THREE.LineBasicMaterial({
      color: 0xf4c86a,
      transparent: true,
      opacity: loading ? 0.75 : 0.1,
      depthWrite: false,
    });
    const lines = new THREE.LineSegments(geometry, material);
    lines.rotation.x = -Math.PI / 2;
    lines.frustumCulled = false;
    state.scene.add(lines);
    if (loading) {
      state.rayAnimation = (time) => {
        for (let i = 0; i < visualEdge ** 2; i++) {
          const phase = (time / 1600 + (i % 7) / 7) % 1;
          positions[i * 6 + axis] = start + phase * travel;
          positions[i * 6 + 3 + axis] = start + Math.min(phase + 0.16, 1) * travel;
        }
        geometry.attributes.position.needsUpdate = true;
      };
    }
    return () => {
      state.rayAnimation = null;
      state.scene.remove(lines);
      geometry.dispose();
      material.dispose();
    };
  }, [assetPath, meshReady, logicalKey, loading, rayAxis, visualEdge]);

  return (
    <div
      className="viewport"
      data-testid="viewport"
      data-mesh-ready={meshReady}
      data-logical-bounds={logicalKey}
      data-ray-axis={rayAxis}
      data-scan-active={loading}
      data-visual-ray-count={visualEdge ** 2}
    >
      <div className="canvas-mount" ref={mountRef} />
      {viewerError && <div className="viewer-error" role="alert">{viewerError}</div>}
      {!meshReady && !loading && !viewerError && (
        <div className="empty-state">
          <span className="empty-icon">◇</span>
          <strong>Your geometry, measured.</strong>
          <small>Choose a primitive or upload a mesh to begin.</small>
        </div>
      )}
      {loading && (
        <div className="processing-badge" role="status">
          <span className="spinner" />
          Measuring mesh + preparing report
          {meshReady && <small>Current geometry remains in view</small>}
        </div>
      )}
      <div className="viewport-help">Drag to orbit · Scroll to zoom</div>
    </div>
  );
}

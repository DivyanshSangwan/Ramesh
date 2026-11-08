import { useEffect, useRef, useState } from "react";
import { apiRequest } from "./api";
import MeshViewport from "./MeshViewport";
import "./styles.css";

// Default parametric shape definitions and initial dimensions (cm).
const SHAPES = {
  sphere: { name: "Sphere", parameters: { radius: 5 } },
  cuboid: { name: "Cuboid", parameters: { length: 2, breadth: 3, height: 4 } },
  torus: { name: "Torus", parameters: { inner_radius: 5, outer_radius: 10 } },
  cylinder: { name: "Cylinder", parameters: { radius: 3, height: 8 } },
  pyramid_cone: { name: "Pyramid", parameters: { height: 6, base_sides: 4, base_side_length: 4 } },
};
// Format numeric value with up to `digits` decimals, or em-dash when null.
const number = (value, digits = 4) => value == null ? "—" : new Intl.NumberFormat("en-US", { maximumFractionDigits: digits }).format(value);

// Single telemetry readout card displaying metric label, formatted number, and unit.
function Metric({ label, value, unit, accent, id }) {
  return <article className={`metric-card ${accent ? "accent" : ""}`} data-testid={id}><span>{label}</span><div><strong>{number(value)}</strong><small>{unit}</small></div></article>;
}

export default function App() {
  // UI state for geometry source, shape parameters, custom upload, scan settings, and benchmark output.
  const [mode, setMode] = useState("primitive");
  const [shape, setShape] = useState("sphere");
  const [parameters, setParameters] = useState({ ...SHAPES.sphere.parameters });
  const [file, setFile] = useState(null);
  const [scale, setScale] = useState({ x: 1, y: 1, z: 1 });
  const [density, setDensity] = useState(128);
  const [direction, setDirection] = useState("Z");
  const [includeAI, setIncludeAI] = useState(true);
  const [dragging, setDragging] = useState(false);
  const [result, setResult] = useState(null);
  const [liveBounds, setLiveBounds] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [health, setHealth] = useState(null);
  const [elapsed, setElapsed] = useState(0);
  const [completedInput, setCompletedInput] = useState("");
  const requestRef = useRef(null);
  const fileRef = useRef(null);

  // Poll /api/health every 15s to update backend connection badge.
  useEffect(() => {
    const controller = new AbortController();
    const check = () => apiRequest("/api/health", { signal: controller.signal }).then(setHealth)
      .catch(() => { if (!controller.signal.aborted) setHealth({ status: "offline" }); });
    check();
    const interval = setInterval(check, 15000);
    return () => { clearInterval(interval); controller.abort(); requestRef.current?.abort(); };
  }, []);

  // Increment elapsed-seconds counter every second while benchmark request is running.
  useEffect(() => {
    if (!loading) return;
    const start = Date.now();
    const interval = setInterval(() => setElapsed(Math.floor((Date.now() - start) / 1000)), 1000);
    return () => clearInterval(interval);
  }, [loading]);

  // Validate dropped or selected mesh file extension (.stl/.obj) and 20 MiB size cap.
  const chooseFile = (files) => {
    if (loading) return;
    setError("");
    const next = files?.[0];
    if (!next) return;
    if (files.length !== 1 || !/\.(stl|obj)$/i.test(next.name) || next.size === 0 || next.size > 20 * 1024 * 1024) {
      setFile(null);
      if (fileRef.current) fileRef.current.value = "";
      setError("Choose one non-empty STL or OBJ file, up to 20 MiB.");
      return;
    }
    setFile(next);
  };

  // Validate inputs, send primitive JSON or multipart upload to backend, and store returned telemetry.
  const runBenchmark = async (event) => {
    event.preventDefault();
    if (loading) return;
    setError("");
    if (mode === "upload" && !file) { setError("Choose an STL or OBJ file first."); return; }
    if (mode === "primitive" && shape === "torus" && Number(parameters.inner_radius) >= Number(parameters.outer_radius)) {
      setError("The torus outer radius must be greater than its inner radius."); return;
    }
    const parsedDensity = Number(density);
    if (!Number.isInteger(parsedDensity) || parsedDensity < 8 || parsedDensity > 1024) {
      setError("Ray density N must be an integer between 8 and 1024."); return;
    }
    setLoading(true);
    setElapsed(0);
    const controller = new AbortController();
    requestRef.current = controller;
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, 600000);
    try {
      const settings = { ray_density: parsedDensity, direction, include_ai: includeAI };
      let body, headers;
      if (mode === "primitive") {
        body = JSON.stringify({ ...settings, shape_type: shape, parameters: Object.fromEntries(Object.entries(parameters).map(([key, value]) => [key, Number(value)])) });
        headers = { "Content-Type": "application/json" };
      } else {
        body = new FormData();
        body.append("file", file);
        for (const [key, value] of Object.entries(settings)) body.append(key, String(value));
        for (const [axis, value] of Object.entries(scale)) body.append(`scale_${axis}`, value);
      }
      const payload = await apiRequest(mode === "primitive" ? "/api/generate-primitive" : "/api/upload-and-analyze", {
        method: "POST", headers, body, signal: controller.signal,
      });
      if (!payload.asset_path || !payload.telemetry || !payload.validation || !payload.ai_analysis) throw new Error("The backend returned an incomplete benchmark result. Restart it with the latest code.");
      setResult(payload);
      setCompletedInput(mode === "primitive" ? SHAPES[shape].name : file.name);
    } catch (err) {
      setError(controller.signal.aborted
        ? timedOut ? "Benchmark exceeded 10 minutes. Try a lower ray density." : "Request cancelled. The backend may still finish its calculation."
        : err.message || "Could not reach the backend.");
    } finally {
      clearTimeout(timer);
      requestRef.current = null;
      setLoading(false);
    }
  };

  // Derive active telemetry, AI report, and perpendicular bounding-box plane axes for ray-spacing display.
  const t = result?.telemetry;
  const analysis = result?.ai_analysis;
  const validDensity = Number.isInteger(Number(density)) && Number(density) >= 8 && Number(density) <= 1024 ? Number(density) : null;
  const validScale = Object.values(scale).every((v) => v !== "" && Number.isFinite(Number(v)) && Number(v) > 0 && Number(v) <= 100);
  const planeIndices = { X: [1, 2], Y: [0, 2], Z: [0, 1] }[direction] || [0, 1];
  return <div className="app-shell">
    <header>
      <div className="brand-mark" aria-hidden="true">
        <svg viewBox="0 0 64 64" fill="none">
          <defs>
            <mask id="ramesh-prism-mask">
              <rect width="64" height="64" fill="white" />
              <g fill="black">
                <polygon points="19,32.5 62,19.5 62,24.8" />
                <polygon points="19,32.5 62,28.2 62,33.5" />
                <polygon points="19,32.5 62,37 62,42.3" />
                <polygon points="19,32.5 62,45.8 62,51.2" />
              </g>
            </mask>
          </defs>
          <path
            mask="url(#ramesh-prism-mask)"
            d="M29.2 8.5 C30.9 5.5 35.1 5.5 36.8 8.5 L57.5 49.5 C59 52.5 56.8 56 53.5 56 L12.5 56 C9.2 56 7 52.5 8.5 49.5 Z"
            fill="#71e2ce"
          />
        </svg>
      </div>
      <div className="brand-copy"><strong>RAMESH</strong><span>Ray-Cast Mesh Benchmarking</span></div>
      <div className={`system-status ${health?.status !== "ok" ? "offline" : ""}`}><span />{!health ? "CHECKING BACKEND" : health.status === "ok" ? "BACKEND CONNECTED" : "BACKEND OFFLINE"}</div>
    </header>
    <main>
      <section className="hero-copy"><h1>Cast rays. <em>Measure precisely.</em></h1><p>Build your mesh using the primitive tool or upload a custom mesh.</p></section>
      <div className="lab-layout">
        <form className="control-panel" onSubmit={runBenchmark}>
          <fieldset disabled={loading}>
            <legend className="sr-only">Benchmark configuration</legend>
            <div className="mode-control" role="group" aria-label="Geometry source"><button type="button" aria-pressed={mode === "primitive"} onClick={() => { setMode("primitive"); setError(""); }}>Parametric Primitives</button><button type="button" aria-pressed={mode === "upload"} onClick={() => { setMode("upload"); setError(""); }}>Custom Upload</button></div>
            {mode === "primitive" ? <>
              <label className="field" htmlFor="shape">Primitive shape<select id="shape" value={shape} onChange={(e) => { setShape(e.target.value); setParameters({ ...SHAPES[e.target.value].parameters }); setError(""); }}>{Object.entries(SHAPES).map(([key, value]) => <option key={key} value={key}>{value.name}</option>)}</select></label>
              <div className="dimension-fields">{Object.entries(parameters).map(([key, value]) => <label className="field" key={key}>{key.replaceAll("_", " ")} {key === "base_sides" ? "" : "(cm)"}<input type="number" required min={key === "base_sides" ? 3 : 0.000001} max={key === "base_sides" ? 256 : 10000} step={key === "base_sides" ? 1 : "any"} value={value} onChange={(e) => setParameters((current) => ({ ...current, [key]: e.target.value }))} /></label>)}</div>
            </> : <>
              <div className={`dropzone ${dragging ? "dragging" : ""}`} data-testid="dropzone" onDragOver={(e) => { e.preventDefault(); if (!loading) setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={(e) => { e.preventDefault(); setDragging(false); chooseFile(e.dataTransfer.files); }}>
                <span className="drop-icon" aria-hidden="true">↑</span><strong>{file ? file.name : "Drop your mesh here"}</strong><span>{file ? `${number(file.size / 1024, 1)} KiB · ready to analyze` : "STL or OBJ · up to 20 MiB"}</span><label className="file-button">Choose file<input ref={fileRef} type="file" accept=".stl,.obj" aria-label="Mesh file" onChange={(e) => chooseFile(e.target.files)} /></label>
              </div>
              <p className="control-heading"><span>SCALE FACTORS</span>{!validScale && <output>1-100</output>}</p><div className="dimension-fields scale-fields">{Object.entries(scale).map(([axis, value]) => <label className="field" key={axis}>Scale {axis.toUpperCase()}<input type="number" required min="0.000001" max="100" step="any" value={value} onChange={(e) => setScale((current) => ({ ...current, [axis]: e.target.value }))} /></label>)}</div>
              <p className="notice">Coordinates after scaling are in cm. For a mesh modeled in mm, use 0.1 on all axes. Up to 200,000 triangles; small holes are repaired when possible.</p>
            </>}
            <div className="live-bounds-stack" data-testid="live-bounds">
              <div className="live-bounds-card">
                <span>Bounds: {liveBounds ? `${number(liveBounds[0], 2)} cm x ${number(liveBounds[1], 2)} cm x ${number(liveBounds[2], 2)} cm` : "—"}</span>
              </div>
              <div className="live-bounds-card">
                <span>Ray spacing: {liveBounds && validDensity ? `${number(liveBounds[planeIndices[0]] / validDensity, 4)} cm x ${number(liveBounds[planeIndices[1]] / validDensity, 4)} cm` : "—"}</span>
              </div>
            </div>
            <div className="scan-controls">
              <label className="field" htmlFor="density">
                <span className="density-heading"><span>Ray density</span><output htmlFor="density">{validDensity ? `${validDensity} × ${validDensity}` : "8–1024"}</output></span>
                <input id="density" type="number" required min="8" max="1024" step="1" value={density} onChange={(e) => setDensity(e.target.value === "" ? "" : Number(e.target.value))} />
              </label>
              <label className="field" htmlFor="direction">Scan axis direction<select id="direction" value={direction} onChange={(e) => setDirection(e.target.value)}><option value="X">X</option><option value="Y">Y</option><option value="Z">Z</option></select></label>
              <label className="checkbox"><input type="checkbox" checked={includeAI} onChange={(e) => setIncludeAI(e.target.checked)} />AI Report</label>
            </div>
            <button className="primary-button" type="submit">{loading ? `BENCHMARKING · ${elapsed}s` : "RUN BENCHMARK"}<span aria-hidden="true">→</span></button>
          </fieldset>
          {loading && <button type="button" className="cancel-button" onClick={() => requestRef.current?.abort()}>Cancel request</button>}
          <div aria-live="polite">{error && <p className="error-message" role="alert">{error}</p>}</div>
        </form>
        <section className="results-panel" aria-label="Benchmark results">
          <MeshViewport assetPath={result?.asset_path} mode={mode} shape={shape} parameters={parameters} file={file} scale={scale} loading={loading} rayAxis={direction} rayDensity={validDensity || 8} onBoundsChange={setLiveBounds} />
          <div className="result-heading"><span>{result ? `LAST COMPLETED BENCHMARK · ${completedInput}` : "BENCHMARK TELEMETRY"}</span><span data-testid="measured-axis">{t ? `${t.direction} · ${t.ray_density} × ${t.ray_density} grid` : "Awaiting measurement"}</span></div>
          <div className="metrics-grid"><Metric label="Raycast volume" value={t?.raycast_volume_cm3} unit="cm³" accent id="ray-volume" /><Metric label="Ground-truth volume" value={t?.ground_truth_volume_cm3} unit="cm³" id="reference-volume" /><Metric label="Volume error" value={t?.volume_error_percent} unit="%" id="volume-error" /><Metric label="Raycast surface area" value={t?.raycast_surface_area_cm2} unit="cm²" accent id="ray-area" /><Metric label="Ground-truth surface area" value={t?.ground_truth_surface_area_cm2} unit="cm²" id="reference-area" /><Metric label="Surface area error" value={t?.surface_area_error_percent} unit="%" id="area-error" /></div>
          <ul className="notice method-list">
            <li>Ground truth volume calculated using signed tetrahedral divergence integration over the triangulated mesh.</li>
            <li>Ground truth surface area calculated using exact vector cross-product summation of all mesh triangles.</li>
            <li>Ray casted volume works as 1D Riemann chord integration of entry-to-exit intervals along the selected scan axis.</li>
            <li>Ray casted surface area works as Cauchy-Crofton projection integrating normal-weighted hits across X, Y, and Z ray grids.</li>
          </ul>
          {result && <div className="result-details"><dl><div><dt>Mesh Validation</dt><dd>{result.validation.watertight_after ? "Watertight" : "Open"} · {number(result.validation.faces, 0)} faces</dd></div></dl>
            {(result.validation.repair_attempted || result.validation.orientation_flipped) && <p className="notice">Mesh repair: {result.validation.faces_added_by_repair} faces added.{result.validation.orientation_flipped && " Orientation corrected."} References describe the repaired mesh.</p>}
            {result.validation.warnings.map((warning, i) => <p className="notice" key={i}>{warning}</p>)}
            <a className="download-link" href={result.asset_path} download={result.filename}>DOWNLOAD STL <span aria-hidden="true">↓</span></a>
          </div>}
        </section>
      </div>
      <section className="ai-panel" aria-label="Engineering interpretation" data-testid="ai-report"><div className="report-heading"><div><p className="eyebrow">AI INTERPRETATION</p><h2>Understand the measurement.</h2></div><span className={`report-status ${analysis?.status || "pending"}`}>{analysis?.status || "AWAITING BENCHMARK"}</span></div>{analysis?.report ? <div className="report-text">{analysis.report}</div> : <p className="notice">{analysis?.message || (analysis?.status === "disabled" ? "AI analysis was disabled for this run." : "Run a benchmark with ai report enabled to interpret accuracy, sampling and geometry.")}</p>}{analysis?.model && <p className="report-model">Model: {analysis.model}</p>}</section>
    </main><footer><span>PYTHON · FASTAPI · TRIMESH · NUMPY · SCIPY · MANIFOLD3D</span><span>REACT · THREE.JS · VITE · GOOGLE GEMINI</span></footer>
  </div>;
}

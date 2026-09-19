const $ = (id) => document.getElementById(id);

const map = new maplibregl.Map({
  container: "map",
  style: "https://tiles.openfreemap.org/styles/liberty",
  center: [-0.1276, 51.5034],
  zoom: 14.7,
  pitch: 0,
});
map.addControl(new maplibregl.NavigationControl(), "bottom-right");

let locationMarker = null;
let lastResult = null;
let activeScenario = "baseline";
let scenarioData = { baseline: null, live: null };
let scenarioRoads = { baseline: null, live: null };
let noiseEventsBound = false;
let activeRunId = null;

const PIPELINE_STEPS = [
  ["location", "Locate address"],
  ["weather", "Weather context"],
  ["osm", "OSM streets + buildings"],
  ["dft", "DfT traffic counts"],
  ["tfl", "TfL JamCam"],
  ["ai_camera", "Gemini traffic vision"],
  ["prepare", "Build acoustic scene"],
  ["noise_baseline", "CNOSSOS baseline"],
  ["noise_live", "CNOSSOS live scenario"],
  ["ai_explain", "Gemini explanation"],
];

const EMPTY_GEOJSON = { type: "FeatureCollection", features: [] };
const DEFAULT_VIEW = {
  center: [-0.1276, 51.5034],
  zoom: 14.7,
  pitch: 0,
  bearing: 0,
};

function setStatus(text, kind = "idle") {
  $("status-text").textContent = text;
  $("status-dot").className = `status-dot ${kind}`;
}

function fmtDb(value) {
  return Number.isFinite(Number(value)) ? `${Number(value).toFixed(1)} dB` : "—";
}

function fmtDelta(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  if (number !== 0 && Math.abs(number) < 0.01) {
    return `${number > 0 ? "+" : "−"}<0.01 dB`;
  }
  const sign = number > 0 ? "+" : "";
  return `${sign}${number.toFixed(2)} dB`;
}

function resetDemoState() {
  lastResult = null;
  activeRunId = null;
  activeScenario = "baseline";
  scenarioData = { baseline: null, live: null };
  scenarioRoads = { baseline: null, live: null };

  if (locationMarker) {
    locationMarker.remove();
    locationMarker = null;
  }

  document.querySelectorAll(".maplibregl-popup").forEach((node) => node.remove());

  const noiseSource = map.getSource("noise");
  if (noiseSource) noiseSource.setData(EMPTY_GEOJSON);
  const roadSource = map.getSource("roads");
  if (roadSource) roadSource.setData(EMPTY_GEOJSON);

  $("scenario-baseline").checked = true;
  $("scenario-live").checked = false;
  $("scenario-live").disabled = true;
  $("heat-toggle").checked = true;
  $("points-toggle").checked = true;
  $("roads-toggle").checked = true;

  for (const layerId of ["noise-heat", "noise-points", "model-roads"]) {
    if (map.getLayer(layerId)) {
      map.setLayoutProperty(layerId, "visibility", "visible");
    }
  }

  $("run-id").textContent = "";
  $("metric-center").textContent = "—";
  $("metric-max").textContent = "—";
  $("metric-p95").textContent = "—";
  $("metric-receivers").textContent = "—";
  $("metric-center-distance").textContent = "";
  $("badges").innerHTML = "";
  $("model-facts").innerHTML = "";
  $("explanation-headline").textContent = "What this means";
  $("explanation").textContent = "";
  $("caveat").textContent = "";
  $("errors").textContent = "";

  setVisible("metrics", false);
  setVisible("provenance", false);
  setVisible("explanation-panel", false);
  setVisible("errors-panel", false);
  setVisible("pipeline-panel", false);
  hideCameraFrame();

  map.stop();
  map.jumpTo(DEFAULT_VIEW);
}

function setVisible(id, visible = true) {
  $(id).classList.toggle("hidden", !visible);
}

function resetPipeline() {
  $("pipeline-list").innerHTML = PIPELINE_STEPS.map(([key, label]) => `
    <li class="pipeline-step" data-stage="${key}">
      <span class="pipeline-dot"></span>
      <span class="pipeline-copy">
        <span>${label}</span>
        <span class="pipeline-detail"></span>
      </span>
    </li>
  `).join("");
  $("pipeline-live").textContent = "starting";
  setVisible("pipeline-panel", true);
}

function progressDetail(event) {
  if (event.stage === "osm" && event.provider) {
    try { return new URL(event.provider).hostname; } catch (_) { return "geometry ready"; }
  }
  if (event.stage === "dft" && event.nearby_points != null) return `${event.nearby_points} nearby`;
  if (event.stage === "ai_camera" && event.confidence != null) return `${Math.round(Number(event.confidence) * 100)}% confidence`;
  if (event.stage === "prepare" && event.receivers != null) return `${event.receivers} receivers`;
  if (event.stage.startsWith("noise_") && event.center_db != null) return `${Number(event.center_db).toFixed(1)} dB`;
  if (event.status === "skipped") return "skipped";
  if (event.status === "failed") return "failed";
  return "";
}

function updatePipeline(event) {
  if (event.run_id) {
    activeRunId = event.run_id;
    $("run-id").textContent = `run: ${event.run_id}`;
  }
  if (event.stage === "pipeline") {
    $("pipeline-live").textContent = event.status === "running"
      ? "live"
      : (event.status === "complete" ? "complete" : "finished");
    return;
  }
  if (event.stage === "preflight") return;

  const row = document.querySelector(`.pipeline-step[data-stage="${event.stage}"]`);
  if (!row) return;
  row.classList.remove("running", "complete", "failed", "skipped");
  row.classList.add(event.status === "complete" ? "complete" : event.status);
  const detail = row.querySelector(".pipeline-detail");
  if (detail) detail.textContent = progressDetail(event);

  if (event.stage === "tfl" && event.status === "complete" && event.frame_available) {
    showCameraFrame(event.run_id, event.camera || null, event.frame_data_url || null);
  }
}

function hideCameraFrame() {
  $("camera-frame").removeAttribute("src");
  setVisible("camera-preview", false);
}

function showCameraFrame(runId, camera = null, frameDataUrl = null) {
  if (!runId && !frameDataUrl) return;
  $("camera-name").textContent = camera?.common_name || "TfL camera";
  $("camera-foot").textContent = camera?.view
    ? `${camera.view} · frame used for Gemini analysis`
    : "Frame used for Gemini traffic analysis";

  const frame = $("camera-frame");
  frame.onload = () => setVisible("camera-preview", true);
  frame.onerror = () => setVisible("camera-preview", false);
  frame.src = frameDataUrl
    || `/runs/${encodeURIComponent(runId)}/camera/frame?t=${Date.now()}`;
}

function setSourceData(name, data) {
  const source = map.getSource(name);
  if (source) source.setData(data);
  else map.addSource(name, { type: "geojson", data });
}

function ensureNoiseLayers() {
  if (!map.getLayer("noise-heat")) {
    map.addLayer({
      id: "noise-heat",
      type: "heatmap",
      source: "noise",
      maxzoom: 18,
      filter: ["==", ["get", "HAS_MODELLED_CONTRIBUTION"], true],
      paint: {
        "heatmap-weight": ["coalesce", ["get", "DISPLAY_WEIGHT"], 0],
        "heatmap-intensity": ["interpolate", ["linear"], ["zoom"], 12, 0.75, 17, 1.35],
        "heatmap-radius": ["interpolate", ["linear"], ["zoom"], 12, 16, 17, 42],
        "heatmap-opacity": 0.76,
        "heatmap-color": [
          "interpolate", ["linear"], ["heatmap-density"],
          0, "rgba(50,136,189,0)",
          0.15, "#3288bd",
          0.34, "#66c2a5",
          0.50, "#e6f598",
          0.64, "#fee08b",
          0.82, "#f46d43",
          1, "#9e0142",
        ],
      },
    });
  }
  if (!map.getLayer("noise-points")) {
    map.addLayer({
      id: "noise-points",
      type: "circle",
      source: "noise",
      minzoom: 14,
      filter: ["==", ["get", "HAS_MODELLED_CONTRIBUTION"], true],
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 14, 2, 17, 4],
        "circle-color": [
          "interpolate", ["linear"], ["get", "DISPLAY_DB"],
          35, "#3288bd", 50, "#66c2a5", 55, "#e6f598",
          60, "#fee08b", 65, "#fdae61", 75, "#d53e4f", 80, "#9e0142",
        ],
        "circle-opacity": 0.76,
        "circle-stroke-width": 0.4,
        "circle-stroke-color": "#ffffff",
      },
    });
  }
  bindNoiseEvents();
}

function bindNoiseEvents() {
  if (noiseEventsBound) return;
  noiseEventsBound = true;
  map.on("click", "noise-points", (event) => {
    const feature = event.features?.[0];
    if (!feature) return;
    const db = feature.properties?.NOISE_DB;
    const period = feature.properties?.PERIOD || currentSummary()?.period || "";
    new maplibregl.Popup({ offset: 10 })
      .setLngLat(feature.geometry.coordinates)
      .setHTML(`<strong>${fmtDb(db)}</strong><br/>${period} receiver`)
      .addTo(map);
  });
  map.on("mouseenter", "noise-points", () => { map.getCanvas().style.cursor = "pointer"; });
  map.on("mouseleave", "noise-points", () => { map.getCanvas().style.cursor = ""; });
}

function ensureRoadLayer() {
  if (map.getLayer("model-roads")) return;
  map.addLayer({
    id: "model-roads",
    type: "line",
    source: "roads",
    paint: {
      "line-color": [
        "match", ["get", "TRAF_SRC"],
        "dft+jamcam_ai", "#b692f6",
        "dft", "#53b1fd",
        "observed_run_average", "#fdb022",
        "#98a2b3",
      ],
      "line-width": ["interpolate", ["linear"], ["zoom"], 13, 1, 17, 3],
      "line-opacity": 0.68,
    },
  });
}

function fitGeojson(data) {
  const coords = [];
  for (const feature of data.features || []) {
    const g = feature.geometry;
    if (g?.type === "Point") coords.push(g.coordinates);
  }
  if (!coords.length) return;
  const bounds = coords.reduce(
    (b, c) => b.extend(c),
    new maplibregl.LngLatBounds(coords[0], coords[0]),
  );
  map.fitBounds(bounds, { padding: 55, duration: 900, maxZoom: 16.4 });
}

function currentSummary() {
  const noise = lastResult?.noise || {};
  return activeScenario === "live" ? noise.live : noise.baseline;
}

function currentTrafficMeta() {
  const prep = lastResult?.prepare || {};
  return activeScenario === "live" ? prep.traffic_live : prep.traffic_baseline;
}

function renderScenario(kind) {
  if (!lastResult) return;
  if (kind === "live" && !scenarioData.live) kind = "baseline";
  activeScenario = kind;
  $("scenario-baseline").checked = kind === "baseline";
  $("scenario-live").checked = kind === "live";

  const noise = currentSummary() || {};
  const traffic = currentTrafficMeta() || {};
  const delta = lastResult.noise?.delta || null;

  if (scenarioData[kind]) {
    setSourceData("noise", scenarioData[kind]);
    ensureNoiseLayers();
  }
  const roads = scenarioRoads[kind] || scenarioRoads.baseline;
  if (roads) {
    setSourceData("roads", roads);
    ensureRoadLayer();
  }

  const scenarioLabel = kind === "baseline" ? "Baseline DEN at selected location" : `Live ${noise.period || "current"} at selected location`;
  $("metric-scenario-label").textContent = scenarioLabel;
  $("metric-center").textContent = fmtDb(noise.center_db);
  if (noise.center_status === "no_modelled_road_contribution") {
    $("metric-center-distance").textContent = "no meaningful modelled road contribution at nearest receiver";
  } else {
    const parts = [];
    if (noise.center_receiver_distance_m != null) parts.push(`nearest receiver ${noise.center_receiver_distance_m} m away`);
    if (kind === "live" && delta?.center_db != null) {
      parts.push(`${fmtDelta(delta.center_db)} vs baseline ${delta.period}`);
      if (
        delta.address_inside_camera_influence === false
        && delta.camera_influence_radius_m != null
      ) {
        parts.push(`selected address outside ${delta.camera_influence_radius_m} m JamCam influence`);
      }
    }
    $("metric-center-distance").textContent = parts.join(" · ");
  }
  $("metric-max").textContent = fmtDb(noise.max_db);
  $("metric-p95").textContent = fmtDb(noise.p95_db);
  $("metric-receivers").textContent = noise.modelled_receiver_count != null
    ? `${noise.modelled_receiver_count}/${noise.receiver_count}`
    : (noise.receiver_count ?? "—");
  $("legend-period").textContent = noise.period || "noise";

  const facts = [
    ["Scenario", kind === "baseline" ? "Baseline DEN" : `Live ${noise.period || ""}`],
    ["Traffic policy", traffic?.missing_policy],
    ["Modelled roads", traffic?.simulation_roads],
    ["DfT matched", traffic?.roads_matched_to_dft],
    ["Imputed/skipped", traffic ? (traffic.missing_policy === "average"
      ? Math.max(0, (traffic.simulation_roads || 0) - (traffic.roads_matched_to_dft || 0))
      : traffic.roads_skipped) : null],
    ["AI adjusted period", traffic?.ai_adjusted_period],
    ["AI adjusted roads", kind === "live" ? traffic?.ai_adjusted_roads : null],
    ["JamCam distance", kind === "live" && delta?.camera_distance_m != null
      ? `${Math.round(delta.camera_distance_m)} m` : null],
    ["JamCam influence", kind === "live" && delta?.camera_influence_radius_m != null
      ? `${delta.camera_influence_radius_m} m` : null],
    ["Area mean Δ", kind === "live" && delta?.area_mean_db != null
      ? fmtDelta(delta.area_mean_db) : null],
    ["Stats floor", noise.stats_floor_db != null ? `${noise.stats_floor_db} dB` : null],
    ["Display range", noise.display_min_db != null ? `${noise.display_min_db}–${noise.display_max_db} dB` : null],
  ].filter(([, v]) => v !== undefined && v !== null);
  $("model-facts").innerHTML = facts.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
}

async function getJson(url) {
  const response = await fetch(`${url}${url.includes("?") ? "&" : "?"}t=${Date.now()}`);
  if (!response.ok) throw new Error(`${url} HTTP ${response.status}`);
  return response.json();
}

async function loadMapData(result) {
  const links = result.links || {};
  scenarioData = { baseline: null, live: null };
  scenarioRoads = { baseline: null, live: null };

  if (links.baseline_noise_geojson) scenarioData.baseline = await getJson(links.baseline_noise_geojson);
  if (links.live_noise_geojson) scenarioData.live = await getJson(links.live_noise_geojson);
  if (links.baseline_roads_geojson) scenarioRoads.baseline = await getJson(links.baseline_roads_geojson);
  if (links.live_roads_geojson) scenarioRoads.live = await getJson(links.live_roads_geojson);

  $("scenario-live").disabled = !scenarioData.live;
  if (scenarioData.baseline) {
    renderScenario("baseline");
    fitGeojson(scenarioData.baseline);
  }

  const loc = result.location;
  if (loc?.lon != null && loc?.lat != null) {
    if (locationMarker) locationMarker.remove();
    locationMarker = new maplibregl.Marker({ color: "#ffffff" })
      .setLngLat([loc.lon, loc.lat])
      .setPopup(new maplibregl.Popup({ offset: 18 }).setText(loc.formatted_address || "Selected location"))
      .addTo(map);
  }
}

function renderResult(result) {
  lastResult = result;
  activeRunId = result.run_id || activeRunId;
  const baseline = result.noise?.baseline;
  // In Cloud Run, run artifacts live on instance-local /tmp. If the frame already
  // arrived through the progress stream, keep it instead of issuing a second request
  // that may be routed to another instance.
  const currentFrameSrc = $("camera-frame").getAttribute("src") || "";
  if (
    result.links?.camera_frame
    && result.run_id
    && !currentFrameSrc.startsWith("data:image/")
  ) {
    showCameraFrame(result.run_id, result.jamcam || null);
  }
  $("run-id").textContent = result.run_id ? `run: ${result.run_id}` : "";
  setVisible("metrics", !!baseline);

  const badges = [
    ["OSM geometry", !!result.osm],
    ["DfT traffic", !!result.dft],
    ["TfL JamCam", !!result.jamcam],
    ["Gemini vision", !!result.camera_observation],
    ["CNOSSOS baseline", !!result.noise?.baseline],
    ["CNOSSOS live", !!result.noise?.live],
  ];
  $("badges").innerHTML = badges.map(([name, ok]) =>
    `<span class="badge ${ok ? "ok" : "off"}">${ok ? "✓" : "–"} ${name}</span>`
  ).join("");
  setVisible("provenance", true);

  const explanation = result.ai_explanation;
  setVisible("explanation-panel", !!explanation);
  if (explanation) {
    $("explanation-headline").textContent = explanation.headline;
    $("explanation").textContent = explanation.explanation;
    $("caveat").textContent = explanation.caveat;
  }

  const errors = result.errors || [];
  setVisible("errors-panel", errors.length > 0);
  $("errors").textContent = errors.length ? JSON.stringify(errors, null, 2) : "";
}

async function runSimulation(event) {
  event.preventDefault();
  const address = $("address").value.trim();
  if (!address) return;

  $("run-btn").disabled = true;
  resetDemoState();
  resetPipeline();
  setStatus("Running staged London noise simulation…", "running");

  try {
    const response = await fetch("/simulate/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        address,
        skip_weather: $("skip-weather").checked,
        skip_tfl: $("skip-tfl").checked,
        skip_ai: $("skip-ai").checked,
        skip_noise: false,
      }),
    });

    if (!response.ok) {
      let message = `HTTP ${response.status}`;
      try {
        const body = await response.json();
        message = body.detail || message;
      } catch (_) {}
      throw new Error(message);
    }
    if (!response.body) throw new Error("Streaming response body unavailable");

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finalResult = null;

    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });

      let newline;
      while ((newline = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        if (!line) continue;

        const eventData = JSON.parse(line);
        if (eventData.type === "progress") {
          updatePipeline(eventData);
        } else if (eventData.type === "result") {
          finalResult = eventData.result;
        } else if (eventData.type === "fatal") {
          throw new Error(eventData.error || "Simulation failed");
        }
      }

      if (done) break;
    }

    if (!finalResult) throw new Error("Simulation finished without a result");

    renderResult(finalResult);
    await loadMapData(finalResult);
    const errorCount = (finalResult.errors || []).length;
    setStatus(
      errorCount ? `Finished with ${errorCount} stage error(s)` : "Simulation complete",
      errorCount ? "error" : "ok"
    );
  } catch (err) {
    setStatus("Simulation failed", "error");
    $("pipeline-live").textContent = "failed";
    setVisible("errors-panel", true);
    $("errors").textContent = String(err?.stack || err);
  } finally {
    $("run-btn").disabled = false;
  }
}

map.on("load", () => {
  $("simulate-form").addEventListener("submit", runSimulation);
  $("scenario-baseline").addEventListener("change", e => { if (e.target.checked) renderScenario("baseline"); });
  $("scenario-live").addEventListener("change", e => { if (e.target.checked) renderScenario("live"); });
  $("heat-toggle").addEventListener("change", e => {
    if (map.getLayer("noise-heat")) map.setLayoutProperty("noise-heat", "visibility", e.target.checked ? "visible" : "none");
  });
  $("points-toggle").addEventListener("change", e => {
    if (map.getLayer("noise-points")) map.setLayoutProperty("noise-points", "visibility", e.target.checked ? "visible" : "none");
  });
  $("roads-toggle").addEventListener("change", e => {
    if (map.getLayer("model-roads")) map.setLayoutProperty("model-roads", "visibility", e.target.checked ? "visible" : "none");
  });
});


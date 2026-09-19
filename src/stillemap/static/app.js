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

function setStatus(text, kind = "idle") {
  $("status-text").textContent = text;
  $("status-dot").className = `status-dot ${kind}`;
}

function fmtDb(value) {
  return Number.isFinite(Number(value)) ? `${Number(value).toFixed(1)} dB` : "—";
}

function setVisible(id, visible = true) {
  $(id).classList.toggle("hidden", !visible);
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
      const sign = Number(delta.center_db) > 0 ? "+" : "";
      parts.push(`${sign}${Number(delta.center_db).toFixed(1)} dB vs baseline ${delta.period}`);
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
  const baseline = result.noise?.baseline;
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
  setStatus("Collecting London data and running CNOSSOS…", "running");
  setVisible("errors-panel", false);
  try {
    const response = await fetch("/simulate", {
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
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);
    renderResult(result);
    await loadMapData(result);
    const errorCount = (result.errors || []).length;
    setStatus(errorCount ? `Finished with ${errorCount} stage error(s)` : "Simulation complete", errorCount ? "error" : "ok");
  } catch (err) {
    setStatus("Simulation failed", "error");
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

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
          1, "#9e0142"
        ]
      }
    });
  }
  if (!map.getLayer("noise-points")) {
    map.addLayer({
      id: "noise-points",
      type: "circle",
      source: "noise",
      minzoom: 14,
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 14, 2, 17, 4],
        "circle-color": [
          "interpolate", ["linear"], ["get", "DISPLAY_DB"],
          35, "#3288bd", 50, "#66c2a5", 55, "#e6f598",
          60, "#fee08b", 65, "#fdae61", 75, "#d53e4f", 80, "#9e0142"
        ],
        "circle-opacity": 0.76,
        "circle-stroke-width": 0.4,
        "circle-stroke-color": "#ffffff"
      }
    });
  }
  bindNoiseEvents();
}

function bindNoiseEvents() {
  if (noiseEventsBound) return;
  noiseEventsBound = true;
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
        "#98a2b3"
      ],
      "line-width": ["interpolate", ["linear"], ["zoom"], 13, 1, 17, 3],
      "line-opacity": 0.68
    }
  });
}

function fitGeojson(data) {
  const coords = [];
  for (const feature of data.features || []) {
    const g = feature.geometry;
    if (!g) continue;
    if (g.type === "Point") coords.push(g.coordinates);
  }
  if (!coords.length) return;
  const bounds = coords.reduce(
    (b, c) => b.extend(c),
    new maplibregl.LngLatBounds(coords[0], coords[0])
  );
  map.fitBounds(bounds, { padding: 55, duration: 900, maxZoom: 16.4 });
}

async function loadMapData(result) {
  const links = result.links || {};
  if (!links.noise_geojson) return;
  const noise = await fetch(`${links.noise_geojson}?t=${Date.now()}`).then(r => {
    if (!r.ok) throw new Error(`noise map HTTP ${r.status}`);
    return r.json();
  });
  setSourceData("noise", noise);
  ensureNoiseLayers();
  fitGeojson(noise);

  if (links.roads_geojson) {
    const roads = await fetch(`${links.roads_geojson}?t=${Date.now()}`).then(r => r.json());
    setSourceData("roads", roads);
    ensureRoadLayer();
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
  const noise = result.noise || {};
  const prep = result.prepare || {};
  const traffic = prep.traffic || {};

  $("run-id").textContent = result.run_id ? `run: ${result.run_id}` : "";
  setVisible("metrics", !!result.noise);
  $("metric-center").textContent = fmtDb(noise.center_db);
  $("metric-center-distance").textContent = noise.center_receiver_distance_m != null
    ? `nearest receiver ${noise.center_receiver_distance_m} m away` : "";
  $("metric-max").textContent = fmtDb(noise.max_db);
  $("metric-p95").textContent = fmtDb(noise.p95_db);
  $("metric-receivers").textContent = noise.receiver_count ?? "—";
  $("legend-period").textContent = noise.period || "noise";

  const badges = [
    ["OSM geometry", !!result.osm],
    ["DfT traffic", !!result.dft],
    ["TfL JamCam", !!result.jamcam],
    ["Gemini vision", !!result.camera_observation],
    ["CNOSSOS physics", !!result.noise],
  ];
  $("badges").innerHTML = badges.map(([name, ok]) =>
    `<span class="badge ${ok ? "ok" : "off"}">${ok ? "✓" : "–"} ${name}</span>`
  ).join("");
  setVisible("provenance", true);

  const facts = [
    ["Traffic policy", traffic.missing_policy],
    ["Modelled roads", traffic.simulation_roads],
    ["DfT matched", traffic.roads_matched_to_dft],
    ["Imputed/skipped", traffic.missing_policy === "average" ? Math.max(0, (traffic.simulation_roads || 0) - (traffic.roads_matched_to_dft || 0)) : traffic.roads_skipped],
    ["Period", noise.period],
    ["Display range", noise.display_min_db != null ? `${noise.display_min_db}–${noise.display_max_db} dB` : null],
  ].filter(([, v]) => v !== undefined && v !== null);
  $("model-facts").innerHTML = facts.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");

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
      })
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

A deliberately small hackathon implementation for turning a London address into a staged acoustic-model run.

The pipeline is designed for **debugging under time pressure**:

- `.env` is loaded at process start.
- Every external source is a separate stage.
- Every raw response / image / GeoJSON is dumped under `runs/<run-id>/`.
- Missing data becomes `None`; there are **no hidden provider fallbacks**.
- Missing source data still remains `None`; no provider fallback is invented.
- The demo defaults `TRAFFIC_MISSING_POLICY=average` because a complete map surface is an application requirement. Missing road inputs are filled only from means observed in the current run and labelled `observed_run_average`. Set it back to `skip` for strict coverage-only runs.
- Gemini observes JamCam imagery and explains outputs; deterministic Python owns the acoustic inputs.
- NoiseModelling runs as a pinned CLI Docker image with H2GIS workspaces persisted in the run folder.

## Pipeline

```text
address or lat/lon
  |
  +-- 00 preflight
  +-- 01 Google Geocoding v4
  +-- 02 Google Weather API
  +-- 03 OSM / Overpass: buildings + drive network
  +-- 04 DfT: London AADF -> best nearby Counted/latest observations
  +-- 05 TfL: nearest JamCam + still image
  +-- 06 Gemini: structured visual traffic observation
  +-- 07 prepare: BUILDINGS / baseline ROADS / optional live ROADS / RECEIVERS
  +-- 08 NoiseModelling baseline: CNOSSOS -> DEN heatmap
  +-- 09 NoiseModelling live: CNOSSOS -> current D/E/N heatmap (when AI observation is usable)
  +-- 10 Gemini: grounded explanation of baseline + live scenario
```

Cloud Run deployment is intentionally two-step: `scripts/deploy_infra.sh` once, then `scripts/deploy_service.sh` for each build/redeploy.

## 1. Setup

```bash
cp .env.example .env

git clone https://github.com/akaliutau/stillemap.git
cd stillemap

conda create -n stillemap python=3.12 -y
conda activate stillemap

pip install -r requirements.txt
```
If credentials come from `gcloud auth application-default login` or the eventual GCP runtime, 
`GOOGLE_APPLICATION_CREDENTIALS` may be blank; ADC is then used.

```bash
gcloud auth login
gcloud auth application-default login
gcloud auth application-default set-quota-project lon-agentic26lon-9268
```

```bash
gcloud config set project lon-agentic26lon-9268
gcloud services enable \
  geocoding-backend.googleapis.com \
  weather.googleapis.com
```

Google Cloud Console
-> APIs & Services
-> Credentials
-> Create credentials
-> API key

Then restrict the key `GOOGLE_MAPS_API_KEY` to just:

```text
Geocoding API
Weather API
```


and copy the value to `GOOGLE_MAPS_API_KEY` in `.env` 

Populate `.env`. The intended hackathon setup is Vertex AI:

```dotenv
GOOGLE_GENAI_USE_VERTEXAI=true
GOOGLE_CLOUD_PROJECT=lon-agentic26lon-9268
GOOGLE_CLOUD_LOCATION=global
GOOGLE_APPLICATION_CREDENTIALS=/absolute/path/to/service-account.json
GOOGLE_MAPS_API_KEY=...
TFL_APP_KEY=...
```


### GCP deploy

The deploy path mirrors local behavior but embeds NoiseModelling in the Cloud Run image, 
because Cloud Run cannot launch the local `docker run` used by `NM_MODE=docker`. 
Runtime deployment sets `NM_MODE=local` and `NM_LOCAL_HOME=/opt/noisemodelling`.

```bash
gcloud auth login
gcloud auth application-default login

# One-time project/API/Artifact Registry/service-account/secrets setup.
scripts/deploy_infra.sh

# Cloud Build -> Artifact Registry -> Cloud Run service -> /health smoke check.
scripts/deploy_service.sh
```

Both scripts load `.env`. `deploy_infra.sh` copies `GOOGLE_MAPS_API_KEY` and `TFL_APP_KEY` into Secret Manager when present. 
If a value and its secret are both absent, then it is skipped. 
`GEMINI_API_KEY` is only synced when `GOOGLE_GENAI_USE_VERTEXAI=false`; 
the normal Vertex AI path uses the Cloud Run service account and ADC.

For the demo the defaults use `europe-west2`, 4 CPU, 8 GiB, concurrency 1, and one warm instance.

## 2. Build NoiseModelling

The image pins **NoiseModelling 6.0.0** and Java 21. NoiseModelling 6.0.0 documents Java >= 11.

```bash
make nm-build
```

If the upstream release asset URL changes, override it explicitly:

```bash
sudo docker build \
  --build-arg NM_ARCHIVE_URL='https://.../NoiseModelling_6.0.0.zip' \
  -t stillemap/noisemodelling:6.0.0 \
  -f docker/noisemodelling/Dockerfile \
  docker/noisemodelling
```

### Check that NoiseModelling itself works

Fast smoke check:

```bash
make nm-check
```

or:

```bash
sudo docker compose run --rm noisemodelling-check
```

Expected result: `ScriptRunner` prints its CLI/help text and exits successfully.

You can also inspect the installation:

```bash
sudo docker run --rm stillemap/noisemodelling:6.0.0 \
  sh -lc 'java -version && ls -la /opt/noisemodelling/bin && ls /opt/noisemodelling/scripts/NoiseModelling | head'
```

## 3. Preflight

No network calls are made:

```bash
PYTHONPATH=src python -m stillemap.cli preflight
```

This checks Python dependencies, configured credentials, Docker, and whether the NoiseModelling image exists.

## 4. No-run plan

Also makes **zero external API calls**:

```bash
PYTHONPATH=src python -m stillemap.cli run \
  --address '10 Downing Street, London' \
  --no-run
```

It still creates a run folder containing `00_preflight/report.json` and `plan.json`.

## 5. Staged data collection without physics

Useful while API credentials are being fixed:

```bash
PYTHONPATH=src python -m stillemap.cli run \
  --address '10 Downing Street, London' \
  --skip-noise
```

Or bypass geocoding completely while debugging downstream stages:

```bash
PYTHONPATH=src python -m stillemap.cli run \
  --lat 51.5034 --lon -0.1276 \
  --skip-noise
```

## 6. Full local run

```bash
PYTHONPATH=src python -m stillemap.cli run \
  --address '10 Downing Street, London'
```

The terminal intentionally prints detailed JSON debug events. The same events are saved to:

```text
runs/<timestamp>_<label>/debug.log
```

The final aggregate is:

```text
runs/<...>/result.json
```

## 7. Individual stage flags

```text
--skip-geocode
--skip-weather
--skip-osm
--skip-dft
--skip-tfl
--skip-ai
--skip-noise
--no-run
```

These are intentionally simple. There is no DAG engine and no task framework.

If a stage fails, its value in `result.json` remains `null` and an entry is added to `errors`. Later stages run only when their required inputs exist.

## 8. What gets dumped

Typical run:

```text
runs/20260918T..._10-downing-street-london/
├── debug.log
├── result.json
├── 00_preflight/
│   ├── report.json
│   └── plan.json
├── 01_geocode/
│   ├── request.json
│   ├── raw.json
│   └── location.json
├── 02_weather/
│   ├── raw.json
│   └── weather.json
├── 03_osm/
│   ├── buildings_raw.geojson
│   └── roads_raw.geojson
├── 04_dft/
│   ├── region.json
│   ├── selection.json
│   ├── raw_pages/
│   └── nearby.json
├── 05_tfl/
│   ├── raw.json
│   ├── camera.json
│   └── camera_frame.jpg
├── 06_ai_camera/
│   ├── raw_response.json
│   └── observation.json
├── 07_prepare/
│   ├── BUILDINGS.geojson
│   ├── ROADS_BASELINE.geojson
│   ├── ROADS_BASELINE_WGS84.geojson
│   ├── ROADS_LIVE.geojson              # optional
│   ├── ROADS_LIVE_WGS84.geojson        # optional
│   ├── RECEIVERS.geojson
│   └── manifest.json
├── 08_noisemodelling_baseline/
│   ├── workspace/
│   ├── import_*.log
│   ├── calculate.log
│   ├── export.log
│   ├── RECEIVERS_LEVEL.geojson
│   ├── RECEIVERS_DEN_WGS84.geojson
│   └── summary.json
├── 09_noisemodelling_live/             # optional
│   ├── workspace/
│   ├── RECEIVERS_LEVEL.geojson
│   └── RECEIVERS_<D|E|N>_WGS84.geojson
└── 10_ai_explain/
    ├── raw_response.json
    └── explanation.json
```

This is intentionally verbose on disk so a broken demo can be diagnosed by opening one folder.

## 9. Missing-data behaviour

There are no silent source substitutions.

### DfT traffic missing

Strict coverage-only mode:

```dotenv
TRAFFIC_MISSING_POLICY=skip
```

Roads without complete required traffic + speed inputs are removed from `ROADS.geojson`.

The demo default is the explicit complete-surface policy:

```dotenv
TRAFFIC_MISSING_POLICY=average
```

The pipeline uses the mean of valid values **from that run's matched DfT/OSM roads**. The manifest labels those values `observed_run_average`; it does not invent a London-wide constant.

### Building height missing

NoiseModelling requires building height. OSM `height` is used first, then `building:levels * 3m`. Remaining missing heights use the mean observed building height in the current area. If the area has no usable building heights at all, those buildings are omitted rather than receiving a hard-coded height.

### JamCam missing

`jamcam = null`, Gemini camera inspection is skipped, and DfT remains unchanged.

### Gemini missing/failing

`camera_observation = null`; no AI traffic adjustment is applied. The physics can still run on public data.

### Weather missing

`weather = null`. Weather is currently collected for provenance/UI but deliberately **not injected into the pinned NoiseModelling traffic command** until its exact CLI contract is verified against the built 6.0.0 image.

## 10. AI use

Gemini receives the raw TfL image and must return a Pydantic schema:

```json
{
  "cars_visible": 9,
  "vans_visible": 2,
  "buses_visible": 1,
  "hgvs_visible": null,
  "motorcycles_visible": 1,
  "congestion": "heavy",
  "apparent_speed": "slow",
  "visibility_quality": "usable",
  "confidence": 0.78,
  "notes": "..."
}
```

It is explicitly instructed **not** to invent vehicles/hour or dB.

If confidence passes `AI_MIN_CONFIDENCE`, deterministic code translates semantic state into a live scenario multiplier for the **current London D/E/N period only**. The baseline DEN road table never receives a camera adjustment. The exact multipliers are in `traffic.py` and recorded in the run manifest/debug output.

The second Gemini call explains the finished simulation using only the dumped pipeline facts.

## 11. DfT query detail

The Road Traffic Statistics API has filters but no true radius/bounding-box query. The implementation therefore:

1. resolves the `London` region ID,
2. pages through `DFT_YEAR` back to `DFT_YEAR - DFT_YEAR_LOOKBACK`,
3. computes distance locally,
4. keeps one best row per count point (preferring `Counted`, then newer years),
5. retains the nearest `DFT_MAX_POINTS` within `DFT_NEARBY_RADIUS_M`,
6. matches those points to OSM road geometry within `DFT_ROAD_MATCH_RADIUS_M`.

This is intentionally direct and easy to inspect. It can be cached later if needed.

## 12. Minimal API

For frontend work:

```bash
make api
```

Endpoints:

```text
GET  /health
GET  /preflight
POST /simulate
```

Example body:

```json
{
  "address": "10 Downing Street, London",
  "skip_noise": true
}
```

The HTTP API calls the exact same staged `Pipeline` class as the CLI; there is no second implementation.

## 13. Tests

```bash
make test
```

For the hackathon, tests focus only on transformations that are easy to accidentally change (speed parsing and AI scenario translation). External APIs are debugged through raw stage dumps rather than an elaborate mocking layer.



## 14. Full web app / rendered streets + noise heatmap

Start the same FastAPI process used by the deployed service:

```bash
make api
```

Open:

```text
http://localhost:8080/
```

The page is a no-build MapLibre app served directly by FastAPI. It uses the OpenFreeMap Liberty street basemap and therefore needs no extra browser map API key.

A successful `POST /simulate` creates two isolated acoustic scenarios when a usable JamCam observation exists:

```text
runs/<run-id>/
├── 07_prepare/
│   ├── ROADS_BASELINE.geojson
│   ├── ROADS_BASELINE_WGS84.geojson
│   ├── ROADS_LIVE.geojson                 # only when Gemini observation is usable
│   └── ROADS_LIVE_WGS84.geojson
├── 08_noisemodelling_baseline/
│   ├── RECEIVERS_LEVEL.geojson             # untouched raw NoiseModelling export
│   ├── RECEIVERS_DEN_WGS84.geojson         # default baseline heatmap
│   └── RECEIVERS_<D|E|N>_WGS84.geojson     # same-period baseline used for live delta
└── 09_noisemodelling_live/
    ├── RECEIVERS_LEVEL.geojson
    └── RECEIVERS_<D|E|N>_WGS84.geojson     # current London period live heatmap
```

The baseline map is always produced from DfT/OSM traffic without a camera multiplier and defaults to `DEN`. The live map is a separate NoiseModelling run; Gemini only influences the current London `D`, `E`, or `N` traffic period. This prevents a single camera still from changing the strategic-style DEN baseline.

The browser keeps the noise heatmap and adds a scenario switch:

- **Baseline DEN** — default heatmap;
- **Live D/E/N** — available only when the JamCam + Gemini observation passes confidence checks;
- receiver dots and road provenance switch with the selected scenario;
- numerical no-contribution values are preserved in raw artifacts but hidden from the heatmap and excluded from summary statistics.

Useful HTTP endpoints:

```text
GET  /
POST /simulate
GET  /runs
GET  /runs/<run-id>/result.json
GET  /runs/<run-id>/noise/baseline.geojson
GET  /runs/<run-id>/noise/live.geojson
GET  /runs/<run-id>/roads/baseline.geojson
GET  /runs/<run-id>/roads/live.geojson
```

`/runs/<run-id>/noise.geojson` and `/roads.geojson` remain compatibility aliases for the baseline.

The map uses `NOISE_DISPLAY_MIN_DB` / `NOISE_DISPLAY_MAX_DB` only for visualization. Raw acoustic values stay in `NOISE_DB`; values below `NOISE_STATS_FLOOR_DB` are marked as having no meaningful modelled road contribution rather than being presented as physical silence.

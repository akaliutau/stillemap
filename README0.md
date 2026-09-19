# London Noise Hack — staged NoiseModelling + Google AI pipeline

A deliberately small hackathon implementation for turning a London address into a staged acoustic-model run.

The pipeline is designed for **debugging under time pressure**:

- `.env` is loaded at process start.
- Every external source is a separate stage.
- Every raw response / image / GeoJSON is dumped under `runs/<run-id>/`.
- Missing data becomes `None`; there are **no hidden provider fallbacks**.
- Roads without required traffic inputs are skipped by default.
- If `TRAFFIC_MISSING_POLICY=average`, required missing road values are filled only from the mean of observations collected in the current run.
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
  +-- 04 DfT: London AADF -> local nearby observations
  +-- 05 TfL: nearest JamCam + still image
  +-- 06 Gemini: structured visual traffic observation
  +-- 07 prepare: BUILDINGS / ROADS / RECEIVERS GeoJSON
  +-- 08 NoiseModelling 6.0.0: H2GIS + CNOSSOS
  +-- 09 Gemini: grounded user-facing explanation
```

Cloud Run deployment is intentionally two-step: `scripts/deploy_infra.sh` once, then `scripts/deploy_service.sh` for each build/redeploy.

## 1. Setup

```bash
cp .env.example .env

pip install -r requirements.txt
```

Populate `.env`. The intended hackathon setup is Vertex AI:

```dotenv
GOOGLE_GENAI_USE_VERTEXAI=true
GOOGLE_CLOUD_PROJECT=your-project
GOOGLE_CLOUD_LOCATION=global
GOOGLE_APPLICATION_CREDENTIALS=/absolute/path/to/service-account.json
GOOGLE_MAPS_API_KEY=...
TFL_APP_KEY=...
```

If credentials come from `gcloud auth application-default login` or the eventual GCP runtime, 
`GOOGLE_APPLICATION_CREDENTIALS` may be blank; ADC is then used.

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
If a value and its secret are both absent, it is deliberately skipped rather than invented. 
`GEMINI_API_KEY` is only synced when `GOOGLE_GENAI_USE_VERTEXAI=false`; 
the normal Vertex AI path uses the Cloud Run service account and ADC.

For the demo the defaults use `europe-west2`, 4 CPU, 8 GiB, concurrency 1, and one warm instance. 
Set `CLOUD_RUN_MIN_INSTANCES=0` after the event if you do not want an idle warm instance.

## 2. Build NoiseModelling

The image pins **NoiseModelling 6.0.0** and Java 21. NoiseModelling 6.0.0 documents Java >= 11, so Java 21 is deliberately conservative.

```bash
sudo make nm-build
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
sudo make nm-check
```

or:

```bash
docker compose run --rm noisemodelling-check
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
│   ├── ROADS.geojson
│   ├── RECEIVERS.geojson
│   └── manifest.json
├── 08_noisemodelling/
│   ├── workspace/
│   ├── import_*.log
│   ├── calculate.log
│   ├── export.log
│   ├── RECEIVERS_LEVEL.geojson
│   └── summary.json
└── 09_ai_explain/
    ├── raw_response.json
    └── explanation.json
```

This is intentionally verbose on disk so a broken demo can be diagnosed by opening one folder.

## 9. Missing-data behaviour

There are no silent source substitutions.

### DfT traffic missing

Default:

```dotenv
TRAFFIC_MISSING_POLICY=skip
```

Roads without complete required traffic + speed inputs are removed from `ROADS.geojson`.

Optional explicit policy:

```dotenv
TRAFFIC_MISSING_POLICY=average
```

The pipeline uses the mean of valid values **from that run's matched DfT/OSM roads**. The manifest labels those values `observed_run_average`.

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

If confidence passes `AI_MIN_CONFIDENCE`, deterministic code translates semantic state into a live scenario multiplier. The exact multipliers are in `traffic.py` and recorded in the run manifest/debug output. This makes the AI influence visible and auditable.

The second Gemini call explains the finished simulation using only the dumped pipeline facts.

## 11. DfT query detail

The Road Traffic Statistics API has filters but no true radius/bounding-box query. The implementation therefore:

1. resolves the `London` region ID,
2. pages through AADF for `DFT_YEAR`,
3. computes distance locally,
4. retains the nearest `DFT_MAX_POINTS` within `DFT_NEARBY_RADIUS_M`,
5. matches those points to OSM road geometry within `DFT_ROAD_MATCH_RADIUS_M`.

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

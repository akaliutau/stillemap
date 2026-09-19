from __future__ import annotations

import json
import queue
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import Settings
from .preflight import run_preflight

# .env is loaded exactly once at process start.
settings = Settings.load(".env")
app = FastAPI(title="StilleMap", version="0.3.0")

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class SimulationRequest(BaseModel):
    address: str | None = None
    lat: float | None = None
    lon: float | None = None
    skip_weather: bool = False
    skip_tfl: bool = False
    skip_ai: bool = False
    skip_noise: bool = False


def _run_dir(run_id: str) -> Path:
    if not run_id or Path(run_id).name != run_id:
        raise HTTPException(status_code=400, detail="invalid run id")
    root = settings.runs_dir.resolve()
    candidate = (root / run_id).resolve()
    if candidate.parent != root or not candidate.is_dir():
        raise HTTPException(status_code=404, detail="run not found")
    return candidate


def _file(run_id: str, relative: str) -> Path:
    run = _run_dir(run_id)
    candidate = (run / relative).resolve()
    if run not in candidate.parents or not candidate.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    return candidate


def _recorded_artifact(run_id: str, recorded_path: str | None) -> Path:
    if not recorded_path:
        raise HTTPException(status_code=404, detail="artifact not available for this run")
    run = _run_dir(run_id)
    candidate = Path(recorded_path).resolve()
    if run not in candidate.parents or not candidate.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    return candidate


def _read_result(run_id: str) -> dict:
    return json.loads(_file(run_id, "result.json").read_text(encoding="utf-8"))


def _decorate_result(result: dict) -> dict:
    run_id = result.get("run_id")
    if not run_id:
        return result

    links: dict[str, str] = {"result": f"/runs/{run_id}/result.json"}
    noise = result.get("noise") or {}
    baseline = noise.get("baseline") or {}
    live = noise.get("live") or {}
    prepare = result.get("prepare") or {}

    if baseline.get("map_geojson_path"):
        links["baseline_noise_geojson"] = f"/runs/{run_id}/noise/baseline.geojson"
        # Backward-compatible alias: the primary map remains the DEN baseline heatmap.
        links["noise_geojson"] = links["baseline_noise_geojson"]
    if live.get("map_geojson_path"):
        links["live_noise_geojson"] = f"/runs/{run_id}/noise/live.geojson"
    if prepare.get("road_map_geojson_path"):
        links["baseline_roads_geojson"] = f"/runs/{run_id}/roads/baseline.geojson"
        links["roads_geojson"] = links["baseline_roads_geojson"]
    if prepare.get("live_road_map_geojson_path"):
        links["live_roads_geojson"] = f"/runs/{run_id}/roads/live.geojson"

    camera_dir = settings.runs_dir / run_id / "05_tfl"
    if any(camera_dir.glob("camera_frame.*")):
        links["camera_frame"] = f"/runs/{run_id}/camera/frame"

    return {**result, "links": links}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@app.get("/health")
def health() -> dict:
    return {"ok": True, "app": "stillemap"}


@app.get("/preflight")
def preflight() -> dict:
    return run_preflight(settings)


@app.get("/runs")
def list_runs(limit: int = 20) -> dict:
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    items = []
    for run in sorted((p for p in settings.runs_dir.iterdir() if p.is_dir()), reverse=True)[: max(1, min(limit, 100))]:
        result_path = run / "result.json"
        if result_path.exists():
            try:
                result = _decorate_result(json.loads(result_path.read_text(encoding="utf-8")))
                items.append({
                    "run_id": run.name,
                    "result_url": result.get("links", {}).get("result"),
                    "baseline_noise_url": result.get("links", {}).get("baseline_noise_geojson"),
                    "live_noise_url": result.get("links", {}).get("live_noise_geojson"),
                })
                continue
            except Exception:
                pass
        items.append({"run_id": run.name, "result_url": None, "baseline_noise_url": None, "live_noise_url": None})
    return {"runs": items}


@app.get("/runs/{run_id}/result.json")
def run_result(run_id: str) -> dict:
    return _decorate_result(_read_result(run_id))


@app.get("/runs/{run_id}/noise.geojson")
def run_noise_compat(run_id: str) -> FileResponse:
    return run_noise_baseline(run_id)


@app.get("/runs/{run_id}/noise/baseline.geojson")
def run_noise_baseline(run_id: str) -> FileResponse:
    result = _read_result(run_id)
    recorded = (((result.get("noise") or {}).get("baseline") or {}).get("map_geojson_path"))
    return FileResponse(_recorded_artifact(run_id, recorded), media_type="application/geo+json")


@app.get("/runs/{run_id}/noise/live.geojson")
def run_noise_live(run_id: str) -> FileResponse:
    result = _read_result(run_id)
    recorded = (((result.get("noise") or {}).get("live") or {}).get("map_geojson_path"))
    return FileResponse(_recorded_artifact(run_id, recorded), media_type="application/geo+json")


@app.get("/runs/{run_id}/roads.geojson")
def run_roads_compat(run_id: str) -> FileResponse:
    return run_roads_baseline(run_id)


@app.get("/runs/{run_id}/roads/baseline.geojson")
def run_roads_baseline(run_id: str) -> FileResponse:
    result = _read_result(run_id)
    recorded = ((result.get("prepare") or {}).get("road_map_geojson_path"))
    return FileResponse(_recorded_artifact(run_id, recorded), media_type="application/geo+json")


@app.get("/runs/{run_id}/roads/live.geojson")
def run_roads_live(run_id: str) -> FileResponse:
    result = _read_result(run_id)
    recorded = ((result.get("prepare") or {}).get("live_road_map_geojson_path"))
    return FileResponse(_recorded_artifact(run_id, recorded), media_type="application/geo+json")


@app.get("/runs/{run_id}/camera/frame")
def run_camera_frame(run_id: str) -> FileResponse:
    stage = _run_dir(run_id) / "05_tfl"
    candidates = sorted(stage.glob("camera_frame.*"))
    if not candidates:
        raise HTTPException(status_code=404, detail="camera frame not available")
    frame = candidates[0]
    media_type = "image/png" if frame.suffix.lower() == ".png" else "image/jpeg"
    return FileResponse(frame, media_type=media_type)


def _validate_simulation_request(req: SimulationRequest) -> None:
    if (req.lat is None) != (req.lon is None):
        raise HTTPException(status_code=400, detail="lat and lon must be provided together")
    if not req.address and req.lat is None:
        raise HTTPException(status_code=400, detail="provide address or lat/lon")


def _pipeline_flags(req: SimulationRequest):
    from .pipeline import PipelineFlags

    return PipelineFlags(
        skip_weather=req.skip_weather,
        skip_tfl=req.skip_tfl,
        skip_ai=req.skip_ai,
        skip_noise=req.skip_noise,
    )


@app.post("/simulate/stream")
def simulate_stream(req: SimulationRequest) -> StreamingResponse:
    """NDJSON progress stream for the browser demo; /simulate remains unchanged."""
    _validate_simulation_request(req)

    def generate():
        from .pipeline import Pipeline

        events: queue.Queue[dict | None] = queue.Queue()

        def emit(event: dict) -> None:
            events.put({"type": "progress", **event})

        def worker() -> None:
            try:
                result = Pipeline(settings).run(
                    address=req.address,
                    lat=req.lat,
                    lon=req.lon,
                    flags=_pipeline_flags(req),
                    progress=emit,
                )
                events.put({"type": "result", "result": _decorate_result(result)})
            except Exception as exc:
                events.put({"type": "fatal", "error": repr(exc)})
            finally:
                events.put(None)

        threading.Thread(target=worker, daemon=True).start()

        while True:
            item = events.get()
            if item is None:
                break
            yield json.dumps(item, default=str) + "\n"

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/simulate")
def simulate(req: SimulationRequest) -> dict:
    _validate_simulation_request(req)

    # Lazy import keeps /, /health and /preflight usable while fixing heavy/optional deps.
    from .pipeline import Pipeline

    result = Pipeline(settings).run(
        address=req.address,
        lat=req.lat,
        lon=req.lon,
        flags=_pipeline_flags(req),
    )
    return _decorate_result(result)


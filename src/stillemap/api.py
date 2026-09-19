from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import Settings
from .preflight import run_preflight

# .env is loaded exactly once at process start.
settings = Settings.load(".env")
app = FastAPI(title="StilleMap", version="0.2.0")

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


def _decorate_result(result: dict) -> dict:
    run_id = result.get("run_id")
    if not run_id:
        return result
    links: dict[str, str] = {
        "result": f"/runs/{run_id}/result.json",
    }
    noise = result.get("noise") or {}
    if noise.get("map_geojson_path"):
        links["noise_geojson"] = f"/runs/{run_id}/noise.geojson"
    prepare = result.get("prepare") or {}
    if prepare.get("road_map_geojson_path"):
        links["roads_geojson"] = f"/runs/{run_id}/roads.geojson"
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
        noise_path = run / "08_noisemodelling" / f"RECEIVERS_{settings.noise_map_period}_WGS84.geojson"
        items.append({
            "run_id": run.name,
            "result_url": f"/runs/{run.name}/result.json" if result_path.exists() else None,
            "noise_url": f"/runs/{run.name}/noise.geojson" if noise_path.exists() else None,
        })
    return {"runs": items}


@app.get("/runs/{run_id}/result.json")
def run_result(run_id: str) -> dict:
    path = _file(run_id, "result.json")
    return _decorate_result(json.loads(path.read_text(encoding="utf-8")))


@app.get("/runs/{run_id}/noise.geojson")
def run_noise(run_id: str) -> FileResponse:
    path = _file(run_id, f"08_noisemodelling/RECEIVERS_{settings.noise_map_period}_WGS84.geojson")
    return FileResponse(path, media_type="application/geo+json")


@app.get("/runs/{run_id}/roads.geojson")
def run_roads(run_id: str) -> FileResponse:
    path = _file(run_id, "07_prepare/ROADS_WGS84.geojson")
    return FileResponse(path, media_type="application/geo+json")


@app.post("/simulate")
def simulate(req: SimulationRequest) -> dict:
    if (req.lat is None) != (req.lon is None):
        raise HTTPException(status_code=400, detail="lat and lon must be provided together")
    if not req.address and req.lat is None:
        raise HTTPException(status_code=400, detail="provide address or lat/lon")

    # Lazy import keeps /, /health and /preflight usable while fixing heavy/optional deps.
    from .pipeline import Pipeline, PipelineFlags

    result = Pipeline(settings).run(
        address=req.address,
        lat=req.lat,
        lon=req.lon,
        flags=PipelineFlags(
            skip_weather=req.skip_weather,
            skip_tfl=req.skip_tfl,
            skip_ai=req.skip_ai,
            skip_noise=req.skip_noise,
        ),
    )
    return _decorate_result(result)

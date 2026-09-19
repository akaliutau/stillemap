from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

from .config import Settings


def _check(name: str, ok: bool, detail: str, required: bool = False) -> dict:
    return {"name": name, "ok": ok, "required": required, "detail": detail}


def run_preflight(settings: Settings) -> dict:
    checks: list[dict] = []

    checks.append(_check(".env", Path(".env").exists(), str(Path(".env").resolve()), required=False))
    checks.append(_check("GOOGLE_MAPS_API_KEY", bool(settings.google_maps_api_key), "needed for geocoding + Google Weather"))

    if settings.google_genai_use_vertexai:
        checks.append(_check("GOOGLE_CLOUD_PROJECT", bool(settings.google_cloud_project), settings.google_cloud_project or "missing"))
        cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if cred_path:
            checks.append(_check("GOOGLE_APPLICATION_CREDENTIALS", Path(cred_path).exists(), cred_path))
        else:
            checks.append(_check("GOOGLE_APPLICATION_CREDENTIALS", True, "not set; ADC may come from gcloud/workload identity"))
    else:
        checks.append(_check("GEMINI_API_KEY", bool(settings.gemini_api_key), "direct Gemini API mode"))

    checks.append(_check("TFL_APP_KEY", bool(settings.tfl_app_key), "TfL Unified API key; endpoint may reject anonymous calls"))
    checks.append(_check("DfT API", True, "no authentication required"))
    if settings.osm_source == "local_gpkg":
        osm_path = settings.osm_local_gpkg_path
        osm_exists = osm_path.is_file()
        detail = str(osm_path.resolve())
        if osm_exists:
            detail += f" ({osm_path.stat().st_size / (1024 * 1024):.1f} MiB)"
        else:
            detail += " (missing; run scripts/download_osm_cache.sh)"
        checks.append(_check("OSM local GeoPackage", osm_exists, detail, required=True))
    else:
        checks.append(
            _check(
                "OSM/Overpass providers",
                bool(settings.osm_overpass_urls),
                ", ".join(settings.osm_overpass_urls) or "not configured",
                required=True,
            )
        )

    required_modules = ["geopandas", "shapely", "google.genai"]
    if settings.osm_source == "overpass":
        required_modules.append("osmnx")

    for module in required_modules:
        try:
            found = importlib.util.find_spec(module) is not None
        except (ModuleNotFoundError, ValueError):
            found = False
        checks.append(_check(f"python:{module}", found, "installed" if found else "missing", required=True))

    if settings.nm_mode == "docker":
        docker = shutil.which("docker")
        sudo = shutil.which("sudo") if settings.nm_docker_sudo else None
        docker_ok = bool(docker) and (not settings.nm_docker_sudo or bool(sudo))
        detail = f"{'sudo ' if settings.nm_docker_sudo else ''}{docker or 'docker not found'}"
        checks.append(_check("docker", docker_ok, detail, required=True))
        image_ok = False
        detail = settings.nm_docker_image
        if docker_ok:
            docker_cmd = ([sudo, docker] if settings.nm_docker_sudo else [docker])
            proc = subprocess.run([*docker_cmd, "image", "inspect", settings.nm_docker_image], capture_output=True, text=True)
            image_ok = proc.returncode == 0
            if not image_ok:
                detail += " (not built/pulled; run `make nm-build`)"
        checks.append(_check("NoiseModelling image", image_ok, detail, required=True))
    else:
        home = Path(settings.nm_local_home or "")
        runner = home / "bin" / "ScriptRunner"
        checks.append(_check("NoiseModelling local ScriptRunner", runner.exists(), str(runner), required=True))

    return {
        "ok": all(c["ok"] for c in checks if c["required"]),
        "checks": checks,
        "settings": {
            "gemini_model": settings.gemini_model,
            "vertex_ai": settings.google_genai_use_vertexai,
            "target_epsg": settings.target_epsg,
            "dft_year": settings.dft_year,
            "dft_year_lookback": settings.dft_year_lookback,
            "osm_source": settings.osm_source,
            "osm_local_gpkg_path": str(settings.osm_local_gpkg_path),
            "osm_overpass_urls": list(settings.osm_overpass_urls),
            "osm_overpass_retries": settings.osm_overpass_retries,
            "osm_overpass_timeout_sec": settings.osm_overpass_timeout_sec,
            "osm_overpass_rate_limit": settings.osm_overpass_rate_limit,
            "traffic_missing_policy": settings.traffic_missing_policy,
            "noise_max_source_distance_m": settings.noise_max_source_distance_m,
            "noise_diff_horizontal": settings.noise_diff_horizontal,
            "noise_diff_vertical": settings.noise_diff_vertical,
            "noise_stats_floor_db": settings.noise_stats_floor_db,
            "nm_mode": settings.nm_mode,
        },
    }


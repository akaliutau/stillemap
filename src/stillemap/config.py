from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


@dataclass(slots=True)
class Settings:
    google_maps_api_key: str | None
    google_genai_use_vertexai: bool
    google_cloud_project: str | None
    google_cloud_location: str
    gemini_api_key: str | None
    gemini_model: str

    tfl_app_key: str | None
    tfl_camera_radius_m: int

    dft_year: int
    dft_year_lookback: int
    dft_region_name: str
    dft_page_size: int
    dft_nearby_radius_m: int
    dft_road_match_radius_m: int
    dft_max_points: int

    osm_radius_m: int
    sim_radius_m: int
    receiver_grid_m: int
    receiver_height_m: float
    target_epsg: int

    noise_max_source_distance_m: int
    noise_reflection_order: int
    noise_diff_horizontal: bool
    noise_diff_vertical: bool
    noise_map_period: str
    noise_stats_floor_db: float
    noise_display_min_db: float
    noise_display_max_db: float

    traffic_day_share: float
    traffic_evening_share: float
    traffic_night_share: float
    traffic_missing_policy: str

    ai_live_traffic_enabled: bool
    ai_min_confidence: float

    nm_mode: str
    nm_docker_image: str
    nm_docker_sudo: bool
    nm_local_home: str | None
    nm_timeout_sec: int

    runs_dir: Path
    http_timeout_sec: int
    debug: bool

    @classmethod
    def load(cls, env_file: str | Path = ".env") -> "Settings":
        # Explicit requirement: load .env on application start.
        load_dotenv(dotenv_path=env_file, override=True)
        settings = cls(
            google_maps_api_key=os.getenv("GOOGLE_MAPS_API_KEY") or None,
            google_genai_use_vertexai=_bool("GOOGLE_GENAI_USE_VERTEXAI", True),
            google_cloud_project=os.getenv("GOOGLE_CLOUD_PROJECT") or None,
            google_cloud_location=os.getenv("GOOGLE_CLOUD_LOCATION", "global"),
            gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
            tfl_app_key=os.getenv("TFL_APP_KEY") or None,
            tfl_camera_radius_m=_int("TFL_CAMERA_RADIUS_M", 1000),
            dft_year=_int("DFT_YEAR", 2025),
            dft_year_lookback=_int("DFT_YEAR_LOOKBACK", 3),
            dft_region_name=os.getenv("DFT_REGION_NAME", "London"),
            dft_page_size=_int("DFT_PAGE_SIZE", 1000),
            dft_nearby_radius_m=_int("DFT_NEARBY_RADIUS_M", 1500),
            dft_road_match_radius_m=_int("DFT_ROAD_MATCH_RADIUS_M", 60),
            dft_max_points=_int("DFT_MAX_POINTS", 40),
            osm_radius_m=_int("OSM_RADIUS_M", 450),
            sim_radius_m=_int("SIM_RADIUS_M", 400),
            receiver_grid_m=_int("RECEIVER_GRID_M", 25),
            receiver_height_m=_float("RECEIVER_HEIGHT_M", 4.0),
            target_epsg=_int("TARGET_EPSG", 27700),
            noise_max_source_distance_m=_int("NOISE_MAX_SOURCE_DISTANCE_M", 300),
            noise_reflection_order=_int("NOISE_REFLECTION_ORDER", 0),
            noise_diff_horizontal=_bool("NOISE_DIFF_HORIZONTAL", False),
            noise_diff_vertical=_bool("NOISE_DIFF_VERTICAL", False),
            noise_map_period=os.getenv("NOISE_MAP_PERIOD", "DEN").strip().upper(),
            noise_stats_floor_db=_float("NOISE_STATS_FLOOR_DB", 20.0),
            noise_display_min_db=_float("NOISE_DISPLAY_MIN_DB", 35.0),
            noise_display_max_db=_float("NOISE_DISPLAY_MAX_DB", 80.0),

            traffic_day_share=_float("TRAFFIC_DAY_SHARE", 0.70),
            traffic_evening_share=_float("TRAFFIC_EVENING_SHARE", 0.20),
            traffic_night_share=_float("TRAFFIC_NIGHT_SHARE", 0.10),
            # A complete surface is an explicit application requirement for the demo.
            traffic_missing_policy=os.getenv("TRAFFIC_MISSING_POLICY", "average").strip().lower(),
            ai_live_traffic_enabled=_bool("AI_LIVE_TRAFFIC_ENABLED", True),
            ai_min_confidence=_float("AI_MIN_CONFIDENCE", 0.55),
            nm_mode=os.getenv("NM_MODE", "docker").strip().lower(),
            nm_docker_image=os.getenv("NM_DOCKER_IMAGE", "stillemap/noisemodelling:6.0.0"),
            nm_docker_sudo=_bool("NM_DOCKER_SUDO", False),
            nm_local_home=os.getenv("NM_LOCAL_HOME") or None,
            nm_timeout_sec=_int("NM_TIMEOUT_SEC", 600),
            runs_dir=Path(os.getenv("RUNS_DIR", "./runs")).resolve(),
            http_timeout_sec=_int("HTTP_TIMEOUT_SEC", 30),
            debug=_bool("DEBUG", True),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        total = self.traffic_day_share + self.traffic_evening_share + self.traffic_night_share
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"TRAFFIC_*_SHARE must sum to 1.0, got {total}")
        if self.traffic_missing_policy not in {"skip", "average"}:
            raise ValueError("TRAFFIC_MISSING_POLICY must be 'skip' or 'average'")
        if self.nm_mode not in {"docker", "local"}:
            raise ValueError("NM_MODE must be 'docker' or 'local'")
        if self.noise_display_max_db <= self.noise_display_min_db:
            raise ValueError("NOISE_DISPLAY_MAX_DB must be greater than NOISE_DISPLAY_MIN_DB")

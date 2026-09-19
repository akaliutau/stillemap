from __future__ import annotations

import math
import re
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely import force_3d
from shapely.geometry import Point

from .config import Settings
from .models import CameraObservation




def first_tag(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return str(value[0]) if value else None
    return str(value)

REQUIRED_TRAFFIC_FIELDS = [
    "LV_D", "LV_E", "LV_N", "HGV_D", "HGV_E", "HGV_N",
    "LV_SPD_D", "LV_SPD_E", "LV_SPD_N", "HGV_SPD_D", "HGV_SPD_E", "HGV_SPD_N",
]


def _num(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_speed_kph(value: Any) -> float | None:
    value = first_tag(value)
    if not value:
        return None
    v = value.strip().lower()
    match = re.search(r"(\d+(?:\.\d+)?)", v)
    if not match:
        return None
    speed = float(match.group(1))
    if "mph" in v:
        speed *= 1.609344
    return speed


def _norm_road_name(value: Any) -> str | None:
    value = first_tag(value)
    if not value:
        return None
    return re.sub(r"[^A-Z0-9]", "", value.upper()) or None


def _road_name_matches(road: pd.Series, dft: dict) -> bool:
    dft_name = _norm_road_name(dft.get("road_name"))
    if not dft_name:
        return True
    candidates = {_norm_road_name(road.get("ref")), _norm_road_name(road.get("name"))}
    candidates.discard(None)
    if not candidates:
        return True
    return dft_name in candidates


def dft_to_hourly_fields(row: dict, settings: Settings) -> dict[str, float | None]:
    cars = _num(row.get("cars_and_taxis"))
    lgvs = _num(row.get("lgvs"))
    buses = _num(row.get("buses_and_coaches"))
    hgvs = _num(row.get("all_hgvs"))
    if None in (cars, lgvs, buses, hgvs):
        return {k: None for k in ["LV_D", "LV_E", "LV_N", "HGV_D", "HGV_E", "HGV_N"]}

    lv_daily = float(cars + lgvs)
    hgv_daily = float(buses + hgvs)
    return {
        "LV_D": lv_daily * settings.traffic_day_share / 12.0,
        "LV_E": lv_daily * settings.traffic_evening_share / 4.0,
        "LV_N": lv_daily * settings.traffic_night_share / 8.0,
        "HGV_D": hgv_daily * settings.traffic_day_share / 12.0,
        "HGV_E": hgv_daily * settings.traffic_evening_share / 4.0,
        "HGV_N": hgv_daily * settings.traffic_night_share / 8.0,
    }


def ai_adjustment(observation: CameraObservation | None, min_confidence: float) -> tuple[float, float] | None:
    """Return (flow multiplier, speed multiplier), or None.

    This is deliberately a tiny, explicit translation layer. Gemini provides semantic
    observations; deterministic code maps those observations to a conservative scenario.
    """
    if observation is None or observation.confidence < min_confidence:
        return None
    flow = {
        "free_flow": 0.85,
        "moderate": 1.00,
        "heavy": 1.15,
        "near_stopped": 1.00,
        "unknown": 1.00,
    }[observation.congestion]
    speed = {
        "fast": 1.10,
        "normal": 1.00,
        "slow": 0.60,
        "stopped": 0.20,
        "unknown": 1.00,
    }[observation.apparent_speed]
    return flow, speed


def assign_traffic(
    roads: gpd.GeoDataFrame,
    nearby_dft: list[dict],
    settings: Settings,
    camera_observation: CameraObservation | None = None,
) -> tuple[gpd.GeoDataFrame, dict]:
    out = roads.copy()
    for field in REQUIRED_TRAFFIC_FIELDS:
        out[field] = np.nan
    out["PVMT"] = "DEF"
    out["DFT_ID"] = None
    out["DFT_DIST"] = np.nan
    out["TRAF_SRC"] = None

    dft_points: list[tuple[dict, Point]] = []
    for row in nearby_dft:
        try:
            point = gpd.GeoSeries(
                [Point(float(row["longitude"]), float(row["latitude"]))], crs=4326
            ).to_crs(settings.target_epsg).iloc[0]
            dft_points.append((row, point))
        except Exception:
            continue

    ai_mult = ai_adjustment(camera_observation, settings.ai_min_confidence) if settings.ai_live_traffic_enabled else None

    matched = 0
    for idx, road in out.iterrows():
        best: tuple[dict, float] | None = None
        for dft, point in dft_points:
            dist = float(road.geometry.distance(point))
            if dist > settings.dft_road_match_radius_m:
                continue
            if not _road_name_matches(road, dft):
                continue
            if best is None or dist < best[1]:
                best = (dft, dist)
        if best is None:
            continue

        dft, dist = best
        flows = dft_to_hourly_fields(dft, settings)
        speed = parse_speed_kph(road.get("maxspeed"))
        values: dict[str, float | None] = {**flows}
        for period in ("D", "E", "N"):
            values[f"LV_SPD_{period}"] = speed
            values[f"HGV_SPD_{period}"] = speed

        if ai_mult is not None:
            flow_mult, speed_mult = ai_mult
            for field in ("LV_D", "LV_E", "LV_N", "HGV_D", "HGV_E", "HGV_N"):
                if values[field] is not None:
                    values[field] = float(values[field]) * flow_mult
            for field in ("LV_SPD_D", "LV_SPD_E", "LV_SPD_N", "HGV_SPD_D", "HGV_SPD_E", "HGV_SPD_N"):
                if values[field] is not None:
                    values[field] = float(values[field]) * speed_mult

        for field, value in values.items():
            out.at[idx, field] = value
        out.at[idx, "DFT_ID"] = str(dft.get("count_point_id")) if dft.get("count_point_id") is not None else None
        out.at[idx, "DFT_DIST"] = dist
        out.at[idx, "TRAF_SRC"] = "dft+jamcam_ai" if ai_mult is not None else "dft"
        matched += 1

    # Explicit missing-data policy. No hard-coded traffic class fallback.
    before_policy = len(out)
    if settings.traffic_missing_policy == "average":
        means = {f: pd.to_numeric(out[f], errors="coerce").mean() for f in REQUIRED_TRAFFIC_FIELDS}
        for field, mean in means.items():
            if not math.isnan(mean):
                out[field] = pd.to_numeric(out[field], errors="coerce").fillna(mean)
        averaged_mask = out["TRAF_SRC"].isna() & out[REQUIRED_TRAFFIC_FIELDS].notna().all(axis=1)
        out.loc[averaged_mask, "TRAF_SRC"] = "observed_run_average"

    valid_mask = out[REQUIRED_TRAFFIC_FIELDS].notna().all(axis=1)
    simulation_roads = out.loc[valid_mask].copy()
    simulation_roads.reset_index(drop=True, inplace=True)
    # NoiseModelling requires source geometries to carry X/Y/Z coordinates.
    # CNOSSOS road traffic emission height = 5 cm above ground.
    simulation_roads["geometry"] = simulation_roads.geometry.apply(
        lambda geom: force_3d(geom, z=0.05)
    )
    simulation_roads["PK"] = np.arange(1, len(simulation_roads) + 1, dtype=int)

    debug = {
        "input_roads": before_policy,
        "nearby_dft_points": len(nearby_dft),
        "roads_matched_to_dft": matched,
        "missing_policy": settings.traffic_missing_policy,
        "simulation_roads": len(simulation_roads),
        "roads_skipped": before_policy - len(simulation_roads),
        "ai_adjustment": None if ai_mult is None else {"flow_multiplier": ai_mult[0], "speed_multiplier": ai_mult[1]},
    }
    return simulation_roads, debug

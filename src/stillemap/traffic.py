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
from .models import CameraObservation, JamCam


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


def _road_group(road: pd.Series) -> str:
    highway = (first_tag(road.get("highway")) or "").lower()
    if highway in {
        "motorway", "motorway_link", "trunk", "trunk_link",
        "primary", "primary_link", "secondary", "secondary_link",
    }:
        return "major"
    if highway in {
        "tertiary", "tertiary_link", "residential", "living_street",
        "unclassified", "service",
    }:
        return "local"
    return "other"


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

    Gemini supplies only semantic observations. Deterministic code translates them
    into a deliberately small scenario adjustment.
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


def _fill_run_averages(out: gpd.GeoDataFrame) -> tuple[dict[str, int], int]:
    """Fill missing roads using same-class observations from this run when possible.

    A current-run global mean is retained only as the final complete-surface fallback
    when a road class has no directly observed DfT roads. No fixed class constants are used.
    """
    out["_ROAD_GROUP"] = out.apply(_road_group, axis=1)
    averaged_by_group: dict[str, int] = {}

    for group in ("major", "local", "other"):
        group_mask = out["_ROAD_GROUP"].eq(group)
        observed = out.loc[
            group_mask & out["TRAF_SRC"].eq("dft"),
            REQUIRED_TRAFFIC_FIELDS,
        ]
        means = {
            field: pd.to_numeric(observed[field], errors="coerce").mean()
            for field in REQUIRED_TRAFFIC_FIELDS
        }
        missing_mask = group_mask & out["TRAF_SRC"].isna()
        for field, mean in means.items():
            if not math.isnan(mean):
                values = pd.to_numeric(out[field], errors="coerce")
                out.loc[missing_mask, field] = values.loc[missing_mask].fillna(mean)

        filled = missing_mask & out[REQUIRED_TRAFFIC_FIELDS].notna().all(axis=1)
        out.loc[filled, "TRAF_SRC"] = "observed_run_average"
        averaged_by_group[group] = int(filled.sum())

    remaining = out["TRAF_SRC"].isna()
    averaged_global = 0
    if remaining.any():
        observed = out.loc[out["TRAF_SRC"].eq("dft"), REQUIRED_TRAFFIC_FIELDS]
        global_means = {
            field: pd.to_numeric(observed[field], errors="coerce").mean()
            for field in REQUIRED_TRAFFIC_FIELDS
        }
        for field, mean in global_means.items():
            if not math.isnan(mean):
                values = pd.to_numeric(out[field], errors="coerce")
                out.loc[remaining, field] = values.loc[remaining].fillna(mean)

        globally_filled = remaining & out[REQUIRED_TRAFFIC_FIELDS].notna().all(axis=1)
        out.loc[globally_filled, "TRAF_SRC"] = "observed_run_average"
        averaged_global = int(globally_filled.sum())

    return averaged_by_group, averaged_global


def assign_traffic(
    roads: gpd.GeoDataFrame,
    nearby_dft: list[dict],
    settings: Settings,
    camera_observation: CameraObservation | None = None,
    *,
    live_period: str | None = None,
    camera: JamCam | None = None,
) -> tuple[gpd.GeoDataFrame, dict]:
    """Attach CNOSSOS traffic fields to OSM roads.

    Baseline calls pass camera_observation=None. A live scenario passes a camera,
    observation and current D/E/N period. The live multiplier is applied only to
    directly DfT-supported roads within the configured camera influence radius.
    """
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

    period = live_period.upper() if live_period else None
    if period not in {None, "D", "E", "N"}:
        raise ValueError(f"live_period must be D/E/N, got {live_period!r}")

    ai_mult = (
        ai_adjustment(camera_observation, settings.ai_min_confidence)
        if settings.ai_live_traffic_enabled and camera_observation is not None and period is not None
        else None
    )

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
        for p in ("D", "E", "N"):
            values[f"LV_SPD_{p}"] = speed
            values[f"HGV_SPD_{p}"] = speed

        for field, value in values.items():
            out.at[idx, field] = value
        out.at[idx, "DFT_ID"] = str(dft.get("count_point_id")) if dft.get("count_point_id") is not None else None
        out.at[idx, "DFT_DIST"] = dist
        out.at[idx, "TRAF_SRC"] = "dft"
        matched += 1

    before_policy = len(out)
    averaged_by_group: dict[str, int] = {}
    averaged_global = 0
    if settings.traffic_missing_policy == "average":
        averaged_by_group, averaged_global = _fill_run_averages(out)

    valid_mask = out[REQUIRED_TRAFFIC_FIELDS].notna().all(axis=1)
    simulation_roads = out.loc[valid_mask].copy()
    simulation_roads.reset_index(drop=True, inplace=True)

    ai_adjusted_roads = 0
    if ai_mult is not None and period is not None and camera is not None:
        camera_point = gpd.GeoSeries(
            [Point(camera.lon, camera.lat)], crs=4326
        ).to_crs(settings.target_epsg).iloc[0]
        distance_to_camera = simulation_roads.geometry.distance(camera_point)
        ai_mask = (
            distance_to_camera.le(settings.ai_camera_influence_radius_m)
            & simulation_roads["TRAF_SRC"].eq("dft")
        )
        flow_mult, speed_mult = ai_mult
        for field in (f"LV_{period}", f"HGV_{period}"):
            simulation_roads.loc[ai_mask, field] *= flow_mult
        for field in (f"LV_SPD_{period}", f"HGV_SPD_{period}"):
            simulation_roads.loc[ai_mask, field] *= speed_mult
        simulation_roads.loc[ai_mask, "TRAF_SRC"] = "dft+jamcam_ai"
        ai_adjusted_roads = int(ai_mask.sum())

    # NoiseModelling requires XYZ source geometry. CNOSSOS road source height = 5 cm.
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
        "averaged_by_road_group": averaged_by_group,
        "averaged_global": averaged_global,
        "ai_adjusted_period": period if ai_mult is not None else None,
        "ai_adjustment": None if ai_mult is None else {
            "flow_multiplier": ai_mult[0],
            "speed_multiplier": ai_mult[1],
        },
        "ai_camera_influence_radius_m": (
            settings.ai_camera_influence_radius_m if ai_mult is not None else None
        ),
        "ai_adjusted_roads": ai_adjusted_roads,
    }
    return simulation_roads, debug

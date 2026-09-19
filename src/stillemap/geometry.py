from __future__ import annotations

import re
from typing import Any

import geopandas as gpd
import numpy as np
from shapely.geometry import Point
from shapely.ops import unary_union

from .config import Settings


def _parse_height(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).split(";")[0].strip())
    except ValueError:
        match = re.search(r"\d+(?:\.\d+)?", str(value))
        return float(match.group(0)) if match else None


def prepare_buildings(raw: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, dict]:
    out = raw.copy()
    heights: list[float | None] = []
    sources: list[str] = []
    for _, row in out.iterrows():
        height = _parse_height(row.get("height"))
        source = "osm_height"
        if height is None:
            levels = _parse_height(row.get("building:levels"))
            if levels is not None:
                height = levels * 3.0
                source = "osm_levels_x3m"
        if height is None:
            # NoiseModelling requires height. This explicit application-required average
            # is intentionally visible in metadata, not a source-data fallback.
            source = "required_average_pending"
        heights.append(height)
        sources.append(source)

    out["HEIGHT"] = heights
    observed = out["HEIGHT"].dropna()
    average_height = float(observed.mean()) if len(observed) else None
    if average_height is not None:
        missing = out["HEIGHT"].isna()
        out.loc[missing, "HEIGHT"] = average_height
        out.loc[missing, "HEIGHT_SRC"] = "observed_run_average"
    out["HEIGHT_SRC"] = out.get("HEIGHT_SRC", None)
    for i, source in enumerate(sources):
        if source != "required_average_pending":
            out.iat[i, out.columns.get_loc("HEIGHT_SRC")] = source
    out = out[out["HEIGHT"].notna()].copy()
    out.reset_index(drop=True, inplace=True)
    out["PK"] = np.arange(1, len(out) + 1, dtype=int)
    return out[["PK", "HEIGHT", "HEIGHT_SRC", "geometry"]], {
        "input_buildings": len(raw),
        "output_buildings": len(out),
        "observed_height_average_m": average_height,
    }


def build_receivers(
    center_lat: float,
    center_lon: float,
    buildings: gpd.GeoDataFrame,
    settings: Settings,
) -> gpd.GeoDataFrame:
    center = gpd.GeoSeries([Point(center_lon, center_lat)], crs=4326).to_crs(settings.target_epsg).iloc[0]
    buildings_union = unary_union(list(buildings.geometry)) if len(buildings) else None
    points = []
    r = settings.sim_radius_m
    step = settings.receiver_grid_m
    for x in np.arange(center.x - r, center.x + r + 0.01, step):
        for y in np.arange(center.y - r, center.y + r + 0.01, step):
            p2d = Point(float(x), float(y))
            if p2d.distance(center) > r:
                continue
            if buildings_union is not None and buildings_union.contains(p2d):
                continue
            points.append(Point(float(x), float(y), settings.receiver_height_m))
    gdf = gpd.GeoDataFrame({"PK": np.arange(1, len(points) + 1, dtype=int)}, geometry=points, crs=settings.target_epsg)
    return gdf

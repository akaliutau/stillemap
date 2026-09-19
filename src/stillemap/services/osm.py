from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd
from shapely.geometry import Point

from ..config import Settings


DRIVABLE_HIGHWAYS = {
    "motorway",
    "motorway_link",
    "trunk",
    "trunk_link",
    "primary",
    "primary_link",
    "secondary",
    "secondary_link",
    "tertiary",
    "tertiary_link",
    "residential",
    "living_street",
    "unclassified",
    "service",
}


def first_tag(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return str(value[0]) if value else None
    return str(value)


def _normalize_overpass_base(url: str) -> str:
    """Return the OSMnx base API URL, accepting either base or /interpreter form."""
    clean = url.strip().rstrip("/")
    suffix = "/interpreter"
    if clean.endswith(suffix):
        clean = clean[: -len(suffix)]
    return clean.rstrip("/")


class OSMService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _local_radius(
        self,
        lat: float,
        lon: float,
    ) -> tuple[object, tuple[float, float, float, float]]:
        center_wgs = gpd.GeoSeries([Point(lon, lat)], crs=4326)
        center_metric = center_wgs.to_crs(self.settings.target_epsg).iloc[0]
        area_metric = center_metric.buffer(self.settings.osm_radius_m)
        area_wgs = gpd.GeoSeries(
            [area_metric],
            crs=self.settings.target_epsg,
        ).to_crs(4326).iloc[0]
        return area_metric, tuple(float(value) for value in area_wgs.bounds)

    def _fetch_local(
        self,
        lat: float,
        lon: float,
    ) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, dict]:
        path = self.settings.osm_local_gpkg_path
        if not path.is_file():
            raise FileNotFoundError(
                f"Local OSM GeoPackage not found: {path}. "
                "Run scripts/download_osm_cache.sh before starting StilleMap."
            )

        area_metric, bbox_wgs = self._local_radius(lat, lon)

        buildings = gpd.read_file(path, layer="buildings", bbox=bbox_wgs)
        roads = gpd.read_file(path, layer="roads", bbox=bbox_wgs)

        if buildings.crs is None or roads.crs is None:
            raise RuntimeError(f"Local OSM cache has missing CRS metadata: {path}")

        buildings = buildings.to_crs(self.settings.target_epsg)
        roads = roads.to_crs(self.settings.target_epsg)

        buildings = buildings[
            buildings.geometry.geom_type.isin(["Polygon", "MultiPolygon"])
            & buildings.geometry.intersects(area_metric)
        ].copy()
        roads = roads[
            roads.geometry.geom_type.isin(["LineString", "MultiLineString"])
            & roads.geometry.intersects(area_metric)
        ].copy()

        if "highway" in roads.columns:
            roads = roads[
                roads["highway"].map(first_tag).isin(DRIVABLE_HIGHWAYS)
            ].copy()

        if "building_levels" in buildings.columns and "building:levels" not in buildings.columns:
            buildings = buildings.rename(columns={"building_levels": "building:levels"})

        buildings = buildings.reset_index(drop=True)
        roads = roads.reset_index(drop=True)

        if buildings.empty:
            raise RuntimeError(
                f"Local OSM cache contains no building polygons within "
                f"{self.settings.osm_radius_m} m of {lat},{lon}"
            )
        if roads.empty:
            raise RuntimeError(
                f"Local OSM cache contains no drivable roads within "
                f"{self.settings.osm_radius_m} m of {lat},{lon}"
            )

        stat = path.stat()
        return buildings, roads, {
            "source": "local_gpkg",
            "cache_path": str(path),
            "cache_size_bytes": stat.st_size,
            "cache_modified_utc": datetime.fromtimestamp(
                stat.st_mtime,
                tz=timezone.utc,
            ).isoformat(),
            "query_mode": "local_spatial_filter",
            "network_used": False,
        }

    def _split_features(
        self,
        features: gpd.GeoDataFrame,
    ) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
        if features.empty:
            raise RuntimeError("Overpass returned no OSM features")

        buildings_mask = features.geometry.geom_type.isin(["Polygon", "MultiPolygon"])
        if "building" in features.columns:
            buildings_mask &= features["building"].notna()
        else:
            buildings_mask &= False

        roads_mask = features.geometry.geom_type.isin(["LineString", "MultiLineString"])
        if "highway" in features.columns:
            road_class = features["highway"].map(first_tag)
            roads_mask &= road_class.isin(DRIVABLE_HIGHWAYS)
        else:
            roads_mask &= False

        buildings = features.loc[buildings_mask].reset_index(drop=False).copy()
        roads = features.loc[roads_mask].reset_index(drop=False).copy()

        if buildings.empty:
            raise RuntimeError("Overpass response contained no building polygons")
        if roads.empty:
            raise RuntimeError("Overpass response contained no drivable road geometries")

        buildings = buildings.set_crs(4326, allow_override=True).to_crs(
            self.settings.target_epsg
        )
        roads = roads.set_crs(4326, allow_override=True).to_crs(
            self.settings.target_epsg
        )
        return buildings, roads

    def _fetch_overpass(
        self,
        lat: float,
        lon: float,
    ) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, dict]:
        # Lazy import means local_gpkg mode does not depend on OSMnx/Overpass at runtime.
        import osmnx as ox

        failures: list[dict[str, Any]] = []

        for configured_provider in self.settings.osm_overpass_urls:
            provider = _normalize_overpass_base(configured_provider)
            ox.settings.overpass_url = provider
            ox.settings.requests_timeout = self.settings.osm_overpass_timeout_sec
            ox.settings.overpass_rate_limit = self.settings.osm_overpass_rate_limit

            for attempt in range(1, self.settings.osm_overpass_retries + 1):
                try:
                    features = ox.features_from_point(
                        (lat, lon),
                        tags={"building": True, "highway": True},
                        dist=self.settings.osm_radius_m,
                    )
                    buildings, roads = self._split_features(features)
                    return buildings, roads, {
                        "source": "overpass",
                        "provider": provider,
                        "attempt": attempt,
                        "providers_configured": [
                            _normalize_overpass_base(url)
                            for url in self.settings.osm_overpass_urls
                        ],
                        "provider_failures": failures,
                        "query_mode": "single_features_query",
                        "network_used": True,
                        "overpass_rate_limit": self.settings.osm_overpass_rate_limit,
                        "overpass_timeout_sec": self.settings.osm_overpass_timeout_sec,
                    }
                except Exception as exc:
                    failures.append(
                        {
                            "provider": provider,
                            "attempt": attempt,
                            "error": repr(exc),
                        }
                    )
                    if attempt < self.settings.osm_overpass_retries:
                        time.sleep(
                            self.settings.osm_overpass_retry_delay_sec * attempt
                        )

        details = "; ".join(
            f"{item['provider']} attempt {item['attempt']}: {item['error']}"
            for item in failures
        )
        raise RuntimeError(
            "All explicitly configured Overpass providers failed. " + details
        )

    def fetch(
        self,
        lat: float,
        lon: float,
    ) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, dict]:
        if self.settings.osm_source == "local_gpkg":
            return self._fetch_local(lat, lon)
        return self._fetch_overpass(lat, lon)

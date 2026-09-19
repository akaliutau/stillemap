from __future__ import annotations

import time
from typing import Any

import geopandas as gpd
import osmnx as ox

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

    def _configure_provider(self, provider: str) -> str:
        base = _normalize_overpass_base(provider)
        ox.settings.overpass_url = base
        ox.settings.requests_timeout = self.settings.osm_overpass_timeout_sec
        ox.settings.overpass_rate_limit = self.settings.osm_overpass_rate_limit
        return base

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

    def fetch(
        self,
        lat: float,
        lon: float,
    ) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, dict]:
        """Fetch buildings and roads with one Overpass query.

        Providers are explicitly configured in OSM_OVERPASS_URLS and tried in order.
        A successful provider is recorded in the returned metadata. No other provider
        or data source is invented.
        """
        failures: list[dict[str, Any]] = []

        for configured_provider in self.settings.osm_overpass_urls:
            provider = self._configure_provider(configured_provider)

            for attempt in range(1, self.settings.osm_overpass_retries + 1):
                try:
                    # One request supplies both geometry products needed downstream.
                    features = ox.features_from_point(
                        (lat, lon),
                        tags={"building": True, "highway": True},
                        dist=self.settings.osm_radius_m,
                    )
                    buildings, roads = self._split_features(features)
                    return buildings, roads, {
                        "provider": provider,
                        "attempt": attempt,
                        "providers_configured": [
                            _normalize_overpass_base(url)
                            for url in self.settings.osm_overpass_urls
                        ],
                        "provider_failures": failures,
                        "query_mode": "single_features_query",
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

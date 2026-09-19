from __future__ import annotations

from typing import Any

import geopandas as gpd
import osmnx as ox
from shapely.geometry import LineString, MultiLineString

from ..config import Settings


class OSMService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def fetch(self, lat: float, lon: float) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
        # Buildings as polygons.
        buildings = ox.features_from_point(
            (lat, lon), tags={"building": True}, dist=self.settings.osm_radius_m
        )
        buildings = buildings.reset_index(drop=False)
        buildings = buildings[buildings.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].copy()
        buildings = buildings.set_crs(4326, allow_override=True).to_crs(self.settings.target_epsg)

        # Drivable public road geometry. simplify=True gives one geometry per edge where possible.
        graph = ox.graph_from_point(
            (lat, lon), dist=self.settings.osm_radius_m, network_type="drive", simplify=True
        )
        _, edges = ox.graph_to_gdfs(graph, nodes=True, edges=True)
        roads = edges.reset_index(drop=False).copy()
        roads = roads[roads.geometry.geom_type.isin(["LineString", "MultiLineString"])].copy()
        roads = roads.set_crs(4326, allow_override=True).to_crs(self.settings.target_epsg)
        return buildings, roads


def first_tag(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return str(value[0]) if value else None
    return str(value)

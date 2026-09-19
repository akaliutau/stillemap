from __future__ import annotations

import requests

from ..config import Settings
from .tfl import haversine_m


class DfTService:
    BASE = "https://roadtraffic.dft.gov.uk/api"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = requests.Session()
        self.headers = {"Accept": "application/json", "Content-Type": "application/json"}

    def region_id(self) -> tuple[int | None, list | dict | None]:
        response = self.session.get(
            f"{self.BASE}/regions",
            params={"filter[name]": self.settings.dft_region_name},
            headers=self.headers,
            timeout=self.settings.http_timeout_sec,
        )
        response.raise_for_status()
        payload = response.json()
        items = payload.get("data", payload) if isinstance(payload, dict) else payload
        for item in items or []:
            if str(item.get("name", "")).lower() == self.settings.dft_region_name.lower():
                return int(item["id"]), payload
        return None, payload

    def london_aadf_pages(self, region_id: int) -> tuple[list[dict], list[dict]]:
        rows: list[dict] = []
        pages: list[dict] = []
        page_number = 1
        while True:
            response = self.session.get(
                f"{self.BASE}/average-annual-daily-flow",
                params={
                    "filter[region_id]": region_id,
                    "filter[year]": self.settings.dft_year,
                    "page[size]": self.settings.dft_page_size,
                    "page[number]": page_number,
                },
                headers=self.headers,
                timeout=self.settings.http_timeout_sec,
            )
            response.raise_for_status()
            payload = response.json()
            pages.append(payload)
            rows.extend(payload.get("data") or [])
            if not payload.get("next_page_url"):
                break
            page_number += 1
        return rows, pages

    def nearby_aadf(self, lat: float, lon: float) -> tuple[list[dict], dict]:
        region_id, region_payload = self.region_id()
        if region_id is None:
            return [], {"region": region_payload, "pages": []}
        rows, pages = self.london_aadf_pages(region_id)
        nearby: list[dict] = []
        for row in rows:
            if row.get("latitude") in (None, "") or row.get("longitude") in (None, ""):
                continue
            try:
                dist = haversine_m(lat, lon, float(row["latitude"]), float(row["longitude"]))
            except (TypeError, ValueError):
                continue
            if dist <= self.settings.dft_nearby_radius_m:
                item = dict(row)
                item["distance_m"] = round(dist, 2)
                nearby.append(item)
        nearby.sort(key=lambda x: x["distance_m"])
        nearby = nearby[: self.settings.dft_max_points]
        return nearby, {"region": region_payload, "pages": pages}

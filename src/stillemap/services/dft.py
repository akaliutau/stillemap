from __future__ import annotations

import requests

from ..config import Settings
from .tfl import haversine_m


def quality(row: dict) -> tuple[int, int]:
    """
    Higher is better:
    1. Counted > Estimated
    2. newer year > older year
    """
    counted = (
        1
        if str(row.get("estimation_method", "")).lower() == "counted"
        else 0
    )

    year = int(row.get("year") or 0)

    return counted, year



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

    def london_aadf_pages(
            self,
            region_id: int,
    ) -> tuple[list[dict], list[dict]]:
        rows: list[dict] = []
        pages: list[dict] = []

        min_year = self.settings.dft_year - self.settings.dft_year_lookback

        for year in range(self.settings.dft_year, min_year - 1, -1):
            page_number = 1

            while True:
                response = self.session.get(
                    f"{self.BASE}/average-annual-daily-flow",
                    params={
                        "filter[region_id]": region_id,
                        "filter[year]": year,
                        "page[size]": self.settings.dft_page_size,
                        "page[number]": page_number,
                    },
                    headers=self.headers,
                    timeout=self.settings.http_timeout_sec,
                )
                response.raise_for_status()

                payload = response.json()

                pages.append({
                    "year": year,
                    "payload": payload,
                })

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

        best_by_count_point: dict[str, dict] = {}

        for item in nearby:
            count_id = str(item.get("count_point_id"))
            current = best_by_count_point.get(count_id)
            if current is None or quality(item) > quality(current):
                best_by_count_point[count_id] = item

        nearby = list(best_by_count_point.values())
        return nearby, {"region": region_payload, "pages": pages}



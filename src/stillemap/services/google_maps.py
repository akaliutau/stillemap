from __future__ import annotations

from urllib.parse import quote_plus

import requests

from ..config import Settings
from ..models import LatLon, WeatherObservation


class GoogleMapsService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = requests.Session()

    def geocode(self, address: str) -> tuple[LatLon | None, dict | None]:
        key = self.settings.google_maps_api_key
        if not key:
            return None, None
        url = f"https://geocode.googleapis.com/v4/geocode/address/{quote_plus(address)}"
        response = self.session.get(
            url,
            headers={"X-Goog-Api-Key": key},
            timeout=self.settings.http_timeout_sec,
        )
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results") or []
        if not results:
            return None, payload
        first = results[0]
        loc = (first.get("location") or {})
        if loc.get("latitude") is None or loc.get("longitude") is None:
            return None, payload
        return (
            LatLon(
                lat=float(loc["latitude"]),
                lon=float(loc["longitude"]),
                formatted_address=first.get("formattedAddress"),
            ),
            payload,
        )

    def current_weather(self, lat: float, lon: float) -> tuple[WeatherObservation | None, dict | None]:
        key = self.settings.google_maps_api_key
        if not key:
            return None, None
        response = self.session.get(
            "https://weather.googleapis.com/v1/currentConditions:lookup",
            params={
                "key": key,
                "location.latitude": lat,
                "location.longitude": lon,
                "unitsSystem": "METRIC",
            },
            timeout=self.settings.http_timeout_sec,
        )
        response.raise_for_status()
        payload = response.json()
        temperature = (payload.get("temperature") or {}).get("degrees")
        pressure = (payload.get("airPressure") or {}).get("meanSeaLevelMillibars")
        wind_speed = ((payload.get("wind") or {}).get("speed") or {}).get("value")
        condition = (((payload.get("weatherCondition") or {}).get("description") or {}).get("text"))
        return (
            WeatherObservation(
                current_time=payload.get("currentTime"),
                temperature_c=float(temperature) if temperature is not None else None,
                relative_humidity_pct=(
                    float(payload["relativeHumidity"]) if payload.get("relativeHumidity") is not None else None
                ),
                pressure_mbar=float(pressure) if pressure is not None else None,
                wind_speed_kph=float(wind_speed) if wind_speed is not None else None,
                condition=condition,
            ),
            payload,
        )

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class LatLon(BaseModel):
    lat: float
    lon: float
    formatted_address: str | None = None


class WeatherObservation(BaseModel):
    current_time: str | None = None
    temperature_c: float | None = None
    relative_humidity_pct: float | None = None
    pressure_mbar: float | None = None
    wind_speed_kph: float | None = None
    condition: str | None = None


class JamCam(BaseModel):
    id: str | None = None
    common_name: str | None = None
    lat: float
    lon: float
    image_url: str | None = None
    video_url: str | None = None
    view: str | None = None
    distance_m: float | None = None


class CameraObservation(BaseModel):
    cars_visible: int | None = Field(default=None, ge=0)
    vans_visible: int | None = Field(default=None, ge=0)
    buses_visible: int | None = Field(default=None, ge=0)
    hgvs_visible: int | None = Field(default=None, ge=0)
    motorcycles_visible: int | None = Field(default=None, ge=0)
    congestion: Literal["free_flow", "moderate", "heavy", "near_stopped", "unknown"]
    apparent_speed: Literal["fast", "normal", "slow", "stopped", "unknown"]
    visibility_quality: Literal["good", "usable", "poor"]
    confidence: float = Field(ge=0, le=1)
    notes: str | None = None


class AIExplanation(BaseModel):
    headline: str
    explanation: str
    caveat: str

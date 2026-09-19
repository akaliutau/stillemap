from __future__ import annotations

import math
from typing import Any

import requests

from ..config import Settings
from ..models import JamCam


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _flatten_properties(item: dict[str, Any]) -> dict[str, Any]:
    props: dict[str, Any] = {}
    for prop in item.get("additionalProperties") or []:
        key = str(prop.get("key") or "").strip()
        if key:
            props[key] = prop.get("value")
    return props


def _first_http(props: dict[str, Any], needles: tuple[str, ...]) -> str | None:
    for key, value in props.items():
        k = key.lower().replace("_", "").replace("-", "")
        if any(n in k for n in needles) and isinstance(value, str):
            value = value.strip()
            if value.startswith("https://") or value.startswith("http://"):
                return value
    return None


def _jamcam_from_item(item: dict[str, Any], target_lat: float, target_lon: float) -> JamCam | None:
    if item.get("lat") is None or item.get("lon") is None:
        return None

    try:
        lat = float(item["lat"])
        lon = float(item["lon"])
    except (TypeError, ValueError):
        return None

    props = _flatten_properties(item)
    return JamCam(
        id=item.get("id"),
        common_name=item.get("commonName"),
        lat=lat,
        lon=lon,
        image_url=_first_http(props, ("imageurl", "image")),
        video_url=_first_http(props, ("videourl", "video")),
        view=next((str(v) for k, v in props.items() if "view" in k.lower() and v), None),
        distance_m=haversine_m(target_lat, target_lon, lat, lon),
    )


class TfLService:
    JAMCAM_URL = "https://api.tfl.gov.uk/Place/Type/JamCam"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = requests.Session()

    def nearest_jamcam(self, lat: float, lon: float) -> tuple[JamCam | None, list[dict] | None]:
        """Return the nearest usable TfL JamCam inside the configured radius.

        TfL exposes JamCams through /Place/Type/JamCam. We fetch that canonical feed
        and do the small geographic nearest-camera search locally. This is more robust
        than relying on a generic /Place geo query to return JamCam place types.

        Prefer a camera with a current still-image URL because stage 6 needs an image.
        If cameras exist in radius but none has an image URL, return the nearest camera
        metadata anyway; the pipeline will keep camera_observation=None rather than
        inventing footage.
        """
        params: dict[str, Any] = {}
        if self.settings.tfl_app_key:
            params["app_key"] = self.settings.tfl_app_key

        response = self.session.get(
            self.JAMCAM_URL,
            params=params,
            timeout=self.settings.http_timeout_sec,
        )
        response.raise_for_status()
        payload = response.json()
        items = payload if isinstance(payload, list) else []

        cameras: list[JamCam] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            camera = _jamcam_from_item(item, lat, lon)
            if camera is None or camera.distance_m is None:
                continue
            if camera.distance_m <= self.settings.tfl_camera_radius_m:
                cameras.append(camera)

        if not cameras:
            return None, items

        cameras.sort(key=lambda c: c.distance_m if c.distance_m is not None else 1e18)

        # Stage 6 needs a still image. Prefer the nearest camera that actually exposes one.
        image_cameras = [camera for camera in cameras if camera.image_url]
        return (image_cameras[0] if image_cameras else cameras[0]), items

    def download_frame(self, camera: JamCam) -> tuple[bytes | None, str | None]:
        if not camera.image_url:
            return None, None
        response = self.session.get(camera.image_url, timeout=self.settings.http_timeout_sec)
        response.raise_for_status()
        if not response.content:
            return None, response.headers.get("content-type")
        return response.content, response.headers.get("content-type")

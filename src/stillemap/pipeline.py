from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import geopandas as gpd

from .artifacts import RunContext
from .config import Settings
from .geometry import build_receivers, prepare_buildings
from .models import CameraObservation, JamCam, LatLon, WeatherObservation
from .noisemodelling import NoiseModellingRunner, load_noise_summary
from .preflight import run_preflight
from .services.dft import DfTService
from .services.google_maps import GoogleMapsService
from .services.osm import OSMService
from .services.tfl import TfLService
from .traffic import REQUIRED_TRAFFIC_FIELDS, assign_traffic


@dataclass(slots=True)
class PipelineFlags:
    skip_geocode: bool = False
    skip_weather: bool = False
    skip_osm: bool = False
    skip_dft: bool = False
    skip_tfl: bool = False
    skip_ai: bool = False
    skip_noise: bool = False
    no_run: bool = False


class Pipeline:
    def __init__(self, settings: Settings):
        self.settings = settings

    def run(
        self,
        *,
        address: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        flags: PipelineFlags | None = None,
    ) -> dict[str, Any]:
        flags = flags or PipelineFlags()
        label = address or (f"{lat},{lon}" if lat is not None and lon is not None else "pipeline")
        ctx = RunContext.create(self.settings.runs_dir, label, self.settings.debug)
        ctx.log.info("pipeline_start", address=address, lat=lat, lon=lon, flags=asdict(flags))

        result: dict[str, Any] = {
            "run_id": ctx.root.name,
            "run_dir": str(ctx.root),
            "address": address,
            "location": None,
            "weather": None,
            "osm": None,
            "dft": None,
            "jamcam": None,
            "camera_observation": None,
            "prepare": None,
            "noise": None,
            "ai_explanation": None,
            "errors": [],
        }

        # 00 preflight
        preflight_dir = ctx.stage_dir(0, "preflight")
        preflight = run_preflight(self.settings)
        ctx.dump_json(preflight_dir / "report.json", preflight)
        result["preflight"] = preflight

        plan = {
            "stages": ["geocode", "weather", "osm", "dft", "tfl", "ai_camera", "prepare", "noisemodelling", "ai_explain"],
            "flags": asdict(flags),
        }
        ctx.dump_json(preflight_dir / "plan.json", plan)
        if flags.no_run:
            ctx.log.info("no_run_requested", note="preflight + plan only; no external data collection")
            ctx.dump_json(ctx.root / "result.json", result)
            return result

        # 01 location
        location: LatLon | None = None
        geocode_dir = ctx.stage_dir(1, "geocode")
        if lat is not None and lon is not None:
            location = LatLon(lat=lat, lon=lon, formatted_address=address)
            ctx.log.info("location_from_cli", location=location.model_dump())
        elif address and not flags.skip_geocode:
            try:
                ctx.dump_json(geocode_dir / "request.json", {"address": address})
                location, raw = GoogleMapsService(self.settings).geocode(address)
                ctx.dump_json(geocode_dir / "raw.json", raw)
                if location:
                    ctx.dump_json(geocode_dir / "location.json", location.model_dump())
            except Exception as exc:
                self._error(ctx, result, "geocode", exc)
        result["location"] = location.model_dump() if location else None
        if location is None:
            ctx.log.warn("pipeline_stopped_no_location")
            ctx.dump_json(ctx.root / "result.json", result)
            return result

        # 02 weather
        weather: WeatherObservation | None = None
        if not flags.skip_weather:
            stage = ctx.stage_dir(2, "weather")
            try:
                weather, raw = GoogleMapsService(self.settings).current_weather(location.lat, location.lon)
                ctx.dump_json(stage / "raw.json", raw)
                if weather:
                    ctx.dump_json(stage / "weather.json", weather.model_dump())
            except Exception as exc:
                self._error(ctx, result, "weather", exc)
        result["weather"] = weather.model_dump() if weather else None

        # 03 OSM
        buildings_raw: gpd.GeoDataFrame | None = None
        roads_raw: gpd.GeoDataFrame | None = None
        if not flags.skip_osm:
            stage = ctx.stage_dir(3, "osm")
            try:
                buildings_raw, roads_raw = OSMService(self.settings).fetch(location.lat, location.lon)
                ctx.dump_text(stage / "buildings_raw.geojson", buildings_raw.to_json())
                ctx.dump_text(stage / "roads_raw.geojson", roads_raw.to_json())
                result["osm"] = {"buildings": len(buildings_raw), "roads": len(roads_raw)}
                ctx.log.info("osm_collected", **result["osm"])
            except Exception as exc:
                self._error(ctx, result, "osm", exc)

        # 04 DfT
        nearby_dft: list[dict] = []
        if not flags.skip_dft:
            stage = ctx.stage_dir(4, "dft")
            try:
                nearby_dft, raw = DfTService(self.settings).nearby_aadf(location.lat, location.lon)
                ctx.dump_json(stage / "region.json", raw.get("region"))
                pages_dir = stage / "raw_pages"
                pages_dir.mkdir(exist_ok=True)
                for i, page in enumerate(raw.get("pages") or [], start=1):
                    ctx.dump_json(pages_dir / f"page_{i:03d}.json", page)
                ctx.dump_json(stage / "nearby.json", nearby_dft)
                result["dft"] = {"nearby_points": len(nearby_dft), "year": self.settings.dft_year}
                ctx.log.info("dft_collected", **result["dft"])
            except Exception as exc:
                self._error(ctx, result, "dft", exc)

        # 05 TfL
        camera: JamCam | None = None
        frame_bytes: bytes | None = None
        frame_mime = "image/jpeg"
        if not flags.skip_tfl:
            stage = ctx.stage_dir(5, "tfl")
            try:
                tfl = TfLService(self.settings)
                camera, raw = tfl.nearest_jamcam(location.lat, location.lon)
                ctx.dump_json(stage / "raw.json", raw)
                if camera:
                    ctx.dump_json(stage / "camera.json", camera.model_dump())
                    frame_bytes, content_type = tfl.download_frame(camera)
                    if content_type:
                        frame_mime = content_type.split(";")[0]
                    if frame_bytes:
                        ext = ".png" if "png" in frame_mime else ".jpg"
                        (stage / f"camera_frame{ext}").write_bytes(frame_bytes)
                        ctx.log.info("artifact_written", path=str(stage / f"camera_frame{ext}"), bytes=len(frame_bytes))
            except Exception as exc:
                self._error(ctx, result, "tfl", exc)
        result["jamcam"] = camera.model_dump() if camera else None

        # 06 Gemini camera inspection
        camera_obs: CameraObservation | None = None
        gemini: Any | None = None
        if not flags.skip_ai and frame_bytes:
            stage = ctx.stage_dir(6, "ai_camera")
            try:
                from .services.gemini import GeminiService
                gemini = GeminiService(self.settings)
                camera_obs, raw_text = gemini.inspect_camera(frame_bytes, frame_mime)
                ctx.dump_text(stage / "raw_response.json", raw_text)
                ctx.dump_json(stage / "observation.json", camera_obs.model_dump())
            except Exception as exc:
                self._error(ctx, result, "ai_camera", exc)
        result["camera_observation"] = camera_obs.model_dump() if camera_obs else None

        # 07 prepare NoiseModelling input files
        prepare_dir = ctx.stage_dir(7, "prepare")
        prepared_buildings: gpd.GeoDataFrame | None = None
        sim_roads: gpd.GeoDataFrame | None = None
        receivers: gpd.GeoDataFrame | None = None
        if buildings_raw is not None and roads_raw is not None:
            try:
                prepared_buildings, bmeta = prepare_buildings(buildings_raw)
                sim_roads, tmeta = assign_traffic(roads_raw, nearby_dft, self.settings, camera_obs)
                receivers = build_receivers(location.lat, location.lon, prepared_buildings, self.settings)

                # Keep NoiseModelling files intentionally minimal.
                prepared_buildings[["PK", "HEIGHT", "geometry"]].to_file(prepare_dir / "BUILDINGS.geojson", driver="GeoJSON")
                road_cols = ["PK", *REQUIRED_TRAFFIC_FIELDS, "PVMT", "geometry"]
                sim_roads[road_cols].to_file(prepare_dir / "ROADS.geojson", driver="GeoJSON")
                receivers[["PK", "geometry"]].to_file(prepare_dir / "RECEIVERS.geojson", driver="GeoJSON")

                # Browser/debug overlay: keep provenance fields and convert sources to WGS84.
                road_map_path = prepare_dir / "ROADS_WGS84.geojson"
                road_map_cols = [c for c in ["PK", "TRAF_SRC", "DFT_ID", "DFT_DIST", "geometry"] if c in sim_roads.columns]
                sim_roads[road_map_cols].to_crs(4326).to_file(road_map_path, driver="GeoJSON")

                manifest = {
                    "buildings": bmeta,
                    "traffic": tmeta,
                    "receivers": len(receivers),
                    "crs": f"EPSG:{self.settings.target_epsg}",
                    "weather_collected_not_injected": weather.model_dump() if weather else None,
                    "road_map_geojson_path": str(road_map_path),
                    "assumptions": {
                        "receiver_height_m": self.settings.receiver_height_m,
                        "aadf_period_shares": {
                            "day": self.settings.traffic_day_share,
                            "evening": self.settings.traffic_evening_share,
                            "night": self.settings.traffic_night_share,
                        },
                    },
                }
                ctx.dump_json(prepare_dir / "manifest.json", manifest)
                result["prepare"] = manifest
            except Exception as exc:
                self._error(ctx, result, "prepare", exc)

        # 08 NoiseModelling
        noise_summary: dict | None = None
        if not flags.skip_noise:
            if prepared_buildings is None or sim_roads is None or receivers is None:
                ctx.log.warn("noise_skipped_missing_prepared_inputs")
            elif len(sim_roads) == 0:
                ctx.log.warn("noise_skipped_no_roads_with_complete_traffic")
            else:
                stage = ctx.stage_dir(8, "noisemodelling")
                try:
                    output = NoiseModellingRunner(self.settings, ctx.log).run(
                        ctx.root, prepare_dir, stage, weather.model_dump() if weather else None
                    )
                    noise_summary = load_noise_summary(
                        output,
                        location.lat,
                        location.lon,
                        target_epsg=self.settings.target_epsg,
                        period=self.settings.noise_map_period,
                        display_min_db=self.settings.noise_display_min_db,
                        display_max_db=self.settings.noise_display_max_db,
                    )
                    ctx.dump_json(stage / "summary.json", noise_summary)
                except Exception as exc:
                    self._error(ctx, result, "noisemodelling", exc)
        result["noise"] = noise_summary

        # 09 AI explanation, grounded in pipeline facts only.
        if not flags.skip_ai and noise_summary is not None:
            stage = ctx.stage_dir(9, "ai_explain")
            try:
                if gemini is None:
                    from .services.gemini import GeminiService
                    gemini = GeminiService(self.settings)
                facts = {
                    "location": result["location"],
                    "weather": result["weather"],
                    "dft": result["dft"],
                    "jamcam": result["jamcam"],
                    "camera_observation": result["camera_observation"],
                    "simulation": noise_summary,
                    "prepare": result["prepare"],
                }
                explanation, raw_text = gemini.explain_result(facts)
                ctx.dump_text(stage / "raw_response.json", raw_text)
                ctx.dump_json(stage / "explanation.json", explanation.model_dump())
                result["ai_explanation"] = explanation.model_dump()
            except Exception as exc:
                self._error(ctx, result, "ai_explain", exc)

        ctx.dump_json(ctx.root / "result.json", result)
        ctx.log.info("pipeline_done", run_dir=str(ctx.root), errors=len(result["errors"]))
        return result

    @staticmethod
    def _error(ctx: RunContext, result: dict, stage: str, exc: Exception) -> None:
        result["errors"].append({"stage": stage, "error": repr(exc)})
        ctx.log.error("stage_failed", exc=exc, stage=stage)


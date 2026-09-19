from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

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


def current_london_noise_period() -> str:
    """CNOSSOS D/E/N period for London right now."""
    hour = datetime.now(ZoneInfo("Europe/London")).hour
    if 7 <= hour < 19:
        return "D"
    if 19 <= hour < 23:
        return "E"
    return "N"


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
        progress: Callable[[dict[str, Any]], None] | None = None,
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
            "noise": {
                "baseline": None,
                "baseline_current_period": None,
                "live": None,
                "delta": None,
            },
            "ai_explanation": None,
            "errors": [],
        }

        def emit_progress(stage: str, status: str, **data: Any) -> None:
            if progress is None:
                return
            progress(
                {
                    "run_id": ctx.root.name,
                    "stage": stage,
                    "status": status,
                    **data,
                }
            )

        emit_progress("pipeline", "running", address=address)

        # 00 preflight
        preflight_dir = ctx.stage_dir(0, "preflight")
        preflight = run_preflight(self.settings)
        ctx.dump_json(preflight_dir / "report.json", preflight)
        result["preflight"] = preflight
        emit_progress("preflight", "complete", ok=preflight.get("ok", False))
        plan = {
            "stages": [
                "geocode",
                "weather",
                "osm",
                "dft",
                "tfl",
                "ai_camera",
                "prepare_baseline_and_live",
                "noisemodelling_baseline",
                "noisemodelling_live",
                "ai_explain",
            ],
            "flags": asdict(flags),
        }
        ctx.dump_json(preflight_dir / "plan.json", plan)
        if flags.no_run:
            ctx.log.info("no_run_requested", note="preflight + plan only; no external data collection")
            ctx.dump_json(ctx.root / "result.json", result)
            emit_progress("pipeline", "complete")
            return result

        # 01 location
        emit_progress("location", "running")
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
        if location is not None:
            emit_progress("location", "complete", location=location.model_dump())
        if location is None:
            ctx.log.warn("pipeline_stopped_no_location")
            emit_progress("location", "failed")
            ctx.dump_json(ctx.root / "result.json", result)
            emit_progress("pipeline", "failed")
            return result

        # 02 weather
        weather: WeatherObservation | None = None
        if flags.skip_weather:
            emit_progress("weather", "skipped")
        else:
            emit_progress("weather", "running")
        if not flags.skip_weather:
            stage = ctx.stage_dir(2, "weather")
            try:
                weather, raw = GoogleMapsService(self.settings).current_weather(location.lat, location.lon)
                ctx.dump_json(stage / "raw.json", raw)
                if weather:
                    ctx.dump_json(stage / "weather.json", weather.model_dump())
            except Exception as exc:
                self._error(ctx, result, "weather", exc)
                emit_progress("weather", "failed", error=repr(exc))
        result["weather"] = weather.model_dump() if weather else None
        if not flags.skip_weather and weather is not None:
            emit_progress("weather", "complete")

        # 03 OSM
        emit_progress("osm", "skipped" if flags.skip_osm else "running")
        buildings_raw: gpd.GeoDataFrame | None = None
        roads_raw: gpd.GeoDataFrame | None = None
        if not flags.skip_osm:
            stage = ctx.stage_dir(3, "osm")
            try:
                buildings_raw, roads_raw, osm_meta = OSMService(self.settings).fetch(
                    location.lat,
                    location.lon,
                )
                ctx.dump_text(stage / "buildings_raw.geojson", buildings_raw.to_json())
                ctx.dump_text(stage / "roads_raw.geojson", roads_raw.to_json())
                ctx.dump_json(stage / "provider.json", osm_meta)
                result["osm"] = {
                    "buildings": len(buildings_raw),
                    "roads": len(roads_raw),
                    **osm_meta,
                }
                ctx.log.info("osm_collected", **result["osm"])
                emit_progress(
                    "osm",
                    "complete",
                    buildings=len(buildings_raw),
                    roads=len(roads_raw),
                    provider=osm_meta.get("provider"),
                )
            except Exception as exc:
                self._error(ctx, result, "osm", exc)
                emit_progress("osm", "failed", error=repr(exc))

        # 04 DfT
        emit_progress("dft", "skipped" if flags.skip_dft else "running")
        nearby_dft: list[dict] = []
        if not flags.skip_dft:
            stage = ctx.stage_dir(4, "dft")
            try:
                nearby_dft, raw = DfTService(self.settings).nearby_aadf(location.lat, location.lon)
                ctx.dump_json(stage / "region.json", raw.get("region"))
                ctx.dump_json(stage / "selection.json", raw.get("selection"))
                pages_dir = stage / "raw_pages"
                pages_dir.mkdir(exist_ok=True)
                for i, page in enumerate(raw.get("pages") or [], start=1):
                    ctx.dump_json(pages_dir / f"page_{i:03d}.json", page)
                ctx.dump_json(stage / "nearby.json", nearby_dft)
                result["dft"] = {
                    "nearby_points": len(nearby_dft),
                    "requested_year": self.settings.dft_year,
                    "lookback_years": self.settings.dft_year_lookback,
                    "selection": raw.get("selection"),
                }
                ctx.log.info("dft_collected", nearby_points=len(nearby_dft), selection=raw.get("selection"))
                emit_progress("dft", "complete", nearby_points=len(nearby_dft))
            except Exception as exc:
                self._error(ctx, result, "dft", exc)
                emit_progress("dft", "failed", error=repr(exc))

        # 05 TfL
        emit_progress("tfl", "skipped" if flags.skip_tfl else "running")
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
                emit_progress("tfl", "failed", error=repr(exc))
        result["jamcam"] = camera.model_dump() if camera else None
        if not flags.skip_tfl and camera is not None:
            frame_data_url = (
                f"data:{frame_mime};base64,{base64.b64encode(frame_bytes).decode('ascii')}"
                if frame_bytes
                else None
            )
            emit_progress(
                "tfl",
                "complete",
                camera=camera.model_dump(),
                frame_available=bool(frame_bytes),
                frame_data_url=frame_data_url,
            )
        elif not flags.skip_tfl and camera is None:
            emit_progress("tfl", "complete", frame_available=False)

        # 06 Gemini camera inspection
        if flags.skip_ai:
            emit_progress("ai_camera", "skipped")
        elif not frame_bytes:
            emit_progress("ai_camera", "skipped", reason="no_camera_frame")
        else:
            emit_progress("ai_camera", "running")
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
                emit_progress("ai_camera", "failed", error=repr(exc))
        result["camera_observation"] = camera_obs.model_dump() if camera_obs else None
        if camera_obs is not None:
            emit_progress("ai_camera", "complete", confidence=camera_obs.confidence)

        # 07 Prepare baseline + optional live inputs.
        emit_progress("prepare", "running")
        prepare_dir = ctx.stage_dir(7, "prepare")
        prepared_buildings: gpd.GeoDataFrame | None = None
        baseline_roads: gpd.GeoDataFrame | None = None
        live_roads: gpd.GeoDataFrame | None = None
        receivers: gpd.GeoDataFrame | None = None
        live_period = current_london_noise_period()
        baseline_meta: dict | None = None
        live_meta: dict | None = None
        if buildings_raw is not None and roads_raw is not None:
            try:
                prepared_buildings, bmeta = prepare_buildings(buildings_raw)
                baseline_roads, baseline_meta = assign_traffic(
                    roads_raw,
                    nearby_dft,
                    self.settings,
                    camera_observation=None,
                )
                if camera_obs is not None:
                    candidate_live_roads, candidate_live_meta = assign_traffic(
                        roads_raw,
                        nearby_dft,
                        self.settings,
                        camera_observation=camera_obs,
                        live_period=live_period,
                        camera=camera,
                    )
                    if candidate_live_meta.get("ai_adjustment") is not None:
                        live_roads = candidate_live_roads
                        live_meta = candidate_live_meta

                receivers = build_receivers(location.lat, location.lon, prepared_buildings, self.settings)

                prepared_buildings[["PK", "HEIGHT", "geometry"]].to_file(
                    prepare_dir / "BUILDINGS.geojson", driver="GeoJSON"
                )
                road_cols = ["PK", *REQUIRED_TRAFFIC_FIELDS, "PVMT", "geometry"]
                baseline_roads[road_cols].to_file(prepare_dir / "ROADS_BASELINE.geojson", driver="GeoJSON")
                # Compatibility/debug alias: baseline is the authoritative non-live road table.
                baseline_roads[road_cols].to_file(prepare_dir / "ROADS.geojson", driver="GeoJSON")
                receivers[["PK", "geometry"]].to_file(prepare_dir / "RECEIVERS.geojson", driver="GeoJSON")

                baseline_road_map = prepare_dir / "ROADS_BASELINE_WGS84.geojson"
                road_map_cols = [
                    c for c in ["PK", "TRAF_SRC", "DFT_ID", "DFT_DIST", "geometry"]
                    if c in baseline_roads.columns
                ]
                baseline_roads[road_map_cols].to_crs(4326).to_file(baseline_road_map, driver="GeoJSON")

                live_road_map: str | None = None
                if live_roads is not None:
                    live_roads[road_cols].to_file(prepare_dir / "ROADS_LIVE.geojson", driver="GeoJSON")
                    live_map_path = prepare_dir / "ROADS_LIVE_WGS84.geojson"
                    live_roads[road_map_cols].to_crs(4326).to_file(live_map_path, driver="GeoJSON")
                    live_road_map = str(live_map_path)

                manifest = {
                    "buildings": bmeta,
                    "traffic_baseline": baseline_meta,
                    "traffic_live": live_meta,
                    "live_period": live_period,
                    "receivers": len(receivers),
                    "crs": f"EPSG:{self.settings.target_epsg}",
                    "weather_collected_not_injected": weather.model_dump() if weather else None,
                    "road_map_geojson_path": str(baseline_road_map),
                    "live_road_map_geojson_path": live_road_map,
                    "assumptions": {
                        "receiver_height_m": self.settings.receiver_height_m,
                        "aadf_period_shares": {
                            "day": self.settings.traffic_day_share,
                            "evening": self.settings.traffic_evening_share,
                            "night": self.settings.traffic_night_share,
                        },
                        "baseline_camera_adjustment": False,
                        "live_camera_adjustment_period": live_period if live_meta else None,
                    },
                }
                ctx.dump_json(prepare_dir / "manifest.json", manifest)
                result["prepare"] = manifest
                emit_progress(
                    "prepare",
                    "complete",
                    receivers=len(receivers),
                    baseline_roads=len(baseline_roads),
                    live_ready=live_roads is not None,
                )
            except Exception as exc:
                self._error(ctx, result, "prepare", exc)
                emit_progress("prepare", "failed", error=repr(exc))
        else:
            emit_progress("prepare", "skipped", reason="missing_osm_inputs")

        # 08 Baseline NoiseModelling. This is the strategic-style DEN heatmap.
        if flags.skip_noise:
            emit_progress("noise_baseline", "skipped")
        else:
            emit_progress("noise_baseline", "running")
        baseline_summary: dict | None = None
        baseline_period_summary: dict | None = None
        if not flags.skip_noise:
            if prepared_buildings is None or baseline_roads is None or receivers is None:
                ctx.log.warn("noise_baseline_skipped_missing_prepared_inputs")
            elif len(baseline_roads) == 0:
                ctx.log.warn("noise_baseline_skipped_no_roads_with_complete_traffic")
            else:
                stage = ctx.stage_dir(8, "noisemodelling_baseline")
                try:
                    output = NoiseModellingRunner(self.settings, ctx.log).run(
                        ctx.root,
                        prepare_dir,
                        stage,
                        roads_filename="ROADS_BASELINE.geojson",
                        weather=weather.model_dump() if weather else None,
                    )
                    baseline_summary = load_noise_summary(
                        output,
                        location.lat,
                        location.lon,
                        target_epsg=self.settings.target_epsg,
                        period=self.settings.noise_map_period,
                        stats_floor_db=self.settings.noise_stats_floor_db,
                        display_min_db=self.settings.noise_display_min_db,
                        display_max_db=self.settings.noise_display_max_db,
                    )
                    # Same baseline physics, current D/E/N period: used only for a fair
                    # live-vs-baseline delta, never as a replacement for the DEN heatmap.
                    baseline_period_summary = load_noise_summary(
                        output,
                        location.lat,
                        location.lon,
                        target_epsg=self.settings.target_epsg,
                        period=live_period,
                        stats_floor_db=self.settings.noise_stats_floor_db,
                        display_min_db=self.settings.noise_display_min_db,
                        display_max_db=self.settings.noise_display_max_db,
                    )
                    ctx.dump_json(stage / "summary.json", baseline_summary)
                    ctx.dump_json(stage / f"summary_{live_period}.json", baseline_period_summary)
                    emit_progress(
                        "noise_baseline",
                        "complete",
                        period=baseline_summary.get("period"),
                        center_db=baseline_summary.get("center_db"),
                    )
                except Exception as exc:
                    self._error(ctx, result, "noisemodelling_baseline", exc)
                    emit_progress("noise_baseline", "failed", error=repr(exc))
        elif not flags.skip_noise:
            emit_progress("noise_baseline", "skipped", reason="missing_prepared_inputs")

        # 09 Optional live NoiseModelling scenario. Same engine, only current D/E/N
        # traffic fields may be adjusted from Gemini's JamCam observation.
        live_summary: dict | None = None
        if flags.skip_noise:
            emit_progress("noise_live", "skipped")
        elif live_roads is None or len(live_roads) == 0:
            emit_progress("noise_live", "skipped", reason="no_live_scenario")
        else:
            emit_progress("noise_live", "running")
        if not flags.skip_noise and live_roads is not None and len(live_roads) > 0:
            stage = ctx.stage_dir(9, "noisemodelling_live")
            try:
                output = NoiseModellingRunner(self.settings, ctx.log).run(
                    ctx.root,
                    prepare_dir,
                    stage,
                    roads_filename="ROADS_LIVE.geojson",
                    weather=weather.model_dump() if weather else None,
                )
                live_summary = load_noise_summary(
                    output,
                    location.lat,
                    location.lon,
                    target_epsg=self.settings.target_epsg,
                    period=live_period,
                    stats_floor_db=self.settings.noise_stats_floor_db,
                    display_min_db=self.settings.noise_display_min_db,
                    display_max_db=self.settings.noise_display_max_db,
                )
                ctx.dump_json(stage / "summary.json", live_summary)
                emit_progress(
                    "noise_live",
                    "complete",
                    period=live_summary.get("period"),
                    center_db=live_summary.get("center_db"),
                )
            except Exception as exc:
                self._error(ctx, result, "noisemodelling_live", exc)
                emit_progress("noise_live", "failed", error=repr(exc))

        delta: dict | None = None
        if baseline_period_summary is not None and live_summary is not None:
            baseline_center = baseline_period_summary.get("center_db")
            live_center = live_summary.get("center_db")
            baseline_mean = baseline_period_summary.get("mean_db")
            live_mean = live_summary.get("mean_db")

            center_delta = (
                float(live_center) - float(baseline_center)
                if baseline_center is not None and live_center is not None
                else None
            )
            area_mean_delta = (
                float(live_mean) - float(baseline_mean)
                if baseline_mean is not None and live_mean is not None
                else None
            )

            camera_distance_m = (
                float(camera.distance_m)
                if camera is not None and camera.distance_m is not None
                else None
            )
            camera_radius_m = (
                self.settings.ai_camera_influence_radius_m
                if live_meta is not None
                else None
            )
            address_inside_camera_influence = (
                camera_distance_m <= float(camera_radius_m)
                if camera_distance_m is not None and camera_radius_m is not None
                else None
            )

            delta = {
                "period": live_period,
                # Preserve the actual acoustic delta. UI decides display precision.
                "center_db": center_delta,
                "area_mean_db": area_mean_delta,
                "baseline_center_db": baseline_center,
                "live_center_db": live_center,
                "camera_distance_m": camera_distance_m,
                "camera_influence_radius_m": camera_radius_m,
                "address_inside_camera_influence": address_inside_camera_influence,
            }

        result["noise"] = {
            "baseline": baseline_summary,
            "baseline_current_period": baseline_period_summary,
            "live": live_summary,
            "delta": delta,
        }

        # 10 AI explanation, grounded in pipeline facts only.
        if flags.skip_ai:
            emit_progress("ai_explain", "skipped")
        elif baseline_summary is None:
            emit_progress("ai_explain", "skipped", reason="no_baseline_result")
        else:
            emit_progress("ai_explain", "running")
        if not flags.skip_ai and baseline_summary is not None:
            stage = ctx.stage_dir(10, "ai_explain")
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
                    "simulation": result["noise"],
                    "prepare": result["prepare"],
                }
                explanation, raw_text = gemini.explain_result(facts)
                ctx.dump_text(stage / "raw_response.json", raw_text)
                ctx.dump_json(stage / "explanation.json", explanation.model_dump())
                result["ai_explanation"] = explanation.model_dump()
                emit_progress("ai_explain", "complete")
            except Exception as exc:
                self._error(ctx, result, "ai_explain", exc)
                emit_progress("ai_explain", "failed", error=repr(exc))

        ctx.dump_json(ctx.root / "result.json", result)
        ctx.log.info("pipeline_done", run_dir=str(ctx.root), errors=len(result["errors"]))
        emit_progress(
            "pipeline",
            "complete" if not result["errors"] else "complete_with_errors",
            errors=len(result["errors"]),
        )
        return result

    @staticmethod
    def _error(ctx: RunContext, result: dict, stage: str, exc: Exception) -> None:
        result["errors"].append({"stage": stage, "error": repr(exc)})
        ctx.log.error("stage_failed", exc=exc, stage=stage)


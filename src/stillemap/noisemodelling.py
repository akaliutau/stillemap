from __future__ import annotations

import subprocess
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely import force_2d
from shapely.geometry import Point

from .config import Settings
from .debug import DebugLog


class NoiseModellingRunner:
    def __init__(self, settings: Settings, log: DebugLog):
        self.settings = settings
        self.log = log

    def _wrap(self, run_root: Path, inner: list[str]) -> list[str]:
        if self.settings.nm_mode == "local":
            home = Path(self.settings.nm_local_home or "").resolve()
            translated = []
            for arg in inner:
                translated.append(arg.replace("/opt/noisemodelling", str(home)).replace("/work", str(run_root)))
            return translated
        docker = ["sudo", "docker"] if self.settings.nm_docker_sudo else ["docker"]
        return [
            *docker, "run", "--rm",
            "-v", f"{run_root}:/work",
            self.settings.nm_docker_image,
            *inner,
        ]

    def _run(self, run_root: Path, cmd: list[str], log_file: Path) -> None:
        full = self._wrap(run_root, cmd)
        self.log.info("noisemodelling_command", command=full)
        proc = subprocess.run(
            full,
            text=True,
            capture_output=True,
            timeout=self.settings.nm_timeout_sec,
        )
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text(
            "COMMAND:\n" + " ".join(full) + "\n\nSTDOUT:\n" + proc.stdout + "\n\nSTDERR:\n" + proc.stderr,
            encoding="utf-8",
        )
        print(proc.stdout, end="")
        if proc.stderr:
            print(proc.stderr, end="")
        if proc.returncode != 0:
            raise RuntimeError(f"NoiseModelling command failed rc={proc.returncode}; see {log_file}")

    def run(self, run_root: Path, prepare_dir: Path, nm_dir: Path, weather: dict | None = None) -> Path:
        workspace = nm_dir / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        scripts = "/opt/noisemodelling/scripts"
        runner = "/opt/noisemodelling/bin/ScriptRunner"
        work_workspace = "/work/08_noisemodelling/workspace"

        for table in ("BUILDINGS", "ROADS", "RECEIVERS"):
            input_file = f"/work/07_prepare/{table}.geojson"
            self._run(
                run_root,
                [runner, "-w", work_workspace, "-s", f"{scripts}/Import_and_Export/Import_File.groovy",
                 "--pathFile", input_file, "--tableName", table, "--ifTableExists", "Overwrite"],
                nm_dir / f"import_{table.lower()}.log",
            )

        calc = [
            runner, "-w", work_workspace,
            "-s", f"{scripts}/NoiseModelling/Noise_level_from_traffic.groovy",
            "--tableBuilding", "BUILDINGS",
            "--tableRoads", "ROADS",
            "--tableReceivers", "RECEIVERS",
            "--confMaxSrcDist", str(self.settings.noise_max_source_distance_m),
            "--confReflOrder", str(self.settings.noise_reflection_order),
            "--confDiffHorizontal", str(self.settings.noise_diff_horizontal).lower(),
            "--confDiffVertical", str(self.settings.noise_diff_vertical).lower(),
        ]
        # Weather is collected and reported, but not injected here unless the traffic script's
        # CLI contract is verified for the pinned image. This avoids guessing unsupported args.
        self._run(run_root, calc, nm_dir / "calculate.log")

        output = nm_dir / "RECEIVERS_LEVEL.geojson"
        self._run(
            run_root,
            [runner, "-w", work_workspace, "-s", f"{scripts}/Import_and_Export/Export_Table.groovy",
             "--exportPath", "/work/08_noisemodelling/RECEIVERS_LEVEL.geojson",
             "--tableToExport", "RECEIVERS_LEVEL"],
            nm_dir / "export.log",
        )
        if not output.exists():
            raise RuntimeError(f"NoiseModelling completed but output missing: {output}")
        return output


def _column(gdf: gpd.GeoDataFrame, names: tuple[str, ...]) -> str | None:
    by_upper = {str(c).upper(): str(c) for c in gdf.columns}
    for name in names:
        if name.upper() in by_upper:
            return by_upper[name.upper()]
    return None


def load_noise_summary(
    path: Path,
    center_lat: float,
    center_lon: float,
    *,
    target_epsg: int = 27700,
    period: str = "DEN",
    display_min_db: float = 35.0,
    display_max_db: float = 80.0,
) -> dict:
    """Build browser-ready noise output and period-correct summary.

    NoiseModelling's RECEIVERS_LEVEL contains one row per receiver *per period*.
    We keep the raw export untouched, select one requested period for the map,
    convert it to WGS84, and add a normalized DISPLAY_WEIGHT used only by the UI.
    """
    gdf = gpd.read_file(path)
    raw_feature_count = len(gdf)
    if gdf.crs is None:
        gdf = gdf.set_crs(target_epsg)

    period_col = _column(gdf, ("PERIOD",))
    selected = gdf
    selected_period: str | None = None
    if period_col is not None:
        wanted = period.upper()
        mask = gdf[period_col].astype(str).str.upper().eq(wanted)
        if not mask.any():
            available = sorted(set(gdf[period_col].dropna().astype(str)))
            raise RuntimeError(f"Noise period {wanted!r} not found; available={available}")
        selected = gdf.loc[mask].copy()
        selected_period = wanted

    level_field = _column(selected, ("LAEQ", "LEQ", "LDEN", "L_DEN", "DEN", "LAEQ_D"))
    if level_field is None:
        raise RuntimeError(f"No supported noise level field in {path}; columns={list(gdf.columns)}")

    selected = selected.copy()
    selected[level_field] = pd.to_numeric(selected[level_field], errors="coerce")
    selected = selected[np.isfinite(selected[level_field]) & selected.geometry.notna()].copy()
    if selected.empty:
        raise RuntimeError(f"No numeric {level_field} values after filtering period={period!r}")

    levels = selected[level_field].astype(float)
    selected["NOISE_DB"] = levels
    selected["DISPLAY_DB"] = levels.clip(lower=display_min_db, upper=display_max_db)
    span = display_max_db - display_min_db
    selected["DISPLAY_WEIGHT"] = ((selected["DISPLAY_DB"] - display_min_db) / span).clip(0.0, 1.0)

    center = gpd.GeoSeries([Point(center_lon, center_lat)], crs=4326).to_crs(selected.crs).iloc[0]
    nearest_idx = selected.geometry.distance(center).idxmin()
    nearest = selected.loc[nearest_idx]
    center_db = float(nearest[level_field])
    center_distance_m = float(nearest.geometry.distance(center))

    browser = selected.to_crs(4326).copy()
    browser["geometry"] = browser.geometry.apply(force_2d)

    receiver_id = _column(browser, ("IDRECEIVER", "RECEIVER_ID", "PK", "ID"))
    keep = [c for c in (receiver_id, period_col, level_field, "NOISE_DB", "DISPLAY_DB", "DISPLAY_WEIGHT") if c and c in browser.columns]
    # Preserve order while removing aliases that resolve to the same column.
    keep = list(dict.fromkeys(keep))
    browser = browser[[*keep, "geometry"]]

    map_path = path.parent / f"RECEIVERS_{selected_period or period.upper()}_WGS84.geojson"
    browser.to_file(map_path, driver="GeoJSON")

    values = levels.to_numpy(dtype=float)
    return {
        "raw_feature_count": raw_feature_count,
        "receiver_count": len(selected),
        "period": selected_period,
        "period_field": period_col,
        "level_field": level_field,
        "min_db": float(np.min(values)),
        "max_db": float(np.max(values)),
        "mean_db": float(np.mean(values)),
        "median_db": float(np.median(values)),
        "p95_db": float(np.percentile(values, 95)),
        "center_db": center_db,
        "center_receiver_distance_m": round(center_distance_m, 2),
        "below_display_floor": int((levels < display_min_db).sum()),
        "display_min_db": display_min_db,
        "display_max_db": display_max_db,
        "raw_geojson_path": str(path),
        "map_geojson_path": str(map_path),
        "map_crs": "EPSG:4326",
    }

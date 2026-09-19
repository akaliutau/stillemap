from __future__ import annotations

import argparse
import json

from .config import Settings
from .preflight import run_preflight


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="StilleMap pipeline")
    p.add_argument("--env-file", default=".env", help="dotenv file loaded before anything else")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("preflight", help="check config, Python deps and NoiseModelling runtime; no network calls")

    run = sub.add_parser("run", help="run staged collection + preparation + NoiseModelling")
    run.add_argument("--address")
    run.add_argument("--lat", type=float)
    run.add_argument("--lon", type=float)
    run.add_argument("--no-run", action="store_true", help="preflight + print plan only; zero external calls")
    run.add_argument("--skip-geocode", action="store_true")
    run.add_argument("--skip-weather", action="store_true")
    run.add_argument("--skip-osm", action="store_true")
    run.add_argument("--skip-dft", action="store_true")
    run.add_argument("--skip-tfl", action="store_true")
    run.add_argument("--skip-ai", action="store_true")
    run.add_argument("--skip-noise", action="store_true")
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    settings = Settings.load(args.env_file)

    if args.command == "preflight":
        print(json.dumps(run_preflight(settings), indent=2, default=str))
        return 0

    # Lazy import: `preflight` must still work before optional/heavy deps are installed.
    from .pipeline import Pipeline, PipelineFlags

    if args.lat is None and args.lon is not None or args.lat is not None and args.lon is None:
        parser.error("--lat and --lon must be supplied together")
    if not args.address and args.lat is None:
        parser.error("provide --address or --lat/--lon")

    flags = PipelineFlags(
        skip_geocode=args.skip_geocode,
        skip_weather=args.skip_weather,
        skip_osm=args.skip_osm,
        skip_dft=args.skip_dft,
        skip_tfl=args.skip_tfl,
        skip_ai=args.skip_ai,
        skip_noise=args.skip_noise,
        no_run=args.no_run,
    )
    result = Pipeline(settings).run(
        address=args.address,
        lat=args.lat,
        lon=args.lon,
        flags=flags,
    )
    print("\n=== PIPELINE RESULT ===")
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

PBF_URL="${OSM_PBF_URL:-https://download.geofabrik.de/europe/united-kingdom/england/greater-london-latest.osm.pbf}"
CACHE_DIR="${OSM_CACHE_DIR:-${ROOT_DIR}/src/stillemap/data}"
PBF_PATH="${OSM_PBF_PATH:-${CACHE_DIR}/greater-london-latest.osm.pbf}"
GPKG_PATH="${OSM_LOCAL_GPKG_PATH:-${CACHE_DIR}/greater-london.gpkg}"

for cmd in curl ogr2ogr ogrinfo; do
  command -v "$cmd" >/dev/null 2>&1 || {
    printf 'ERROR: %s is required. On Debian/Ubuntu: sudo apt-get install -y gdal-bin curl\n' "$cmd" >&2
    exit 1
  }
done

mkdir -p "$CACHE_DIR"

printf '[osm-cache] download: %s\n' "$PBF_URL"
curl -fL \
  --retry 3 \
  --retry-delay 2 \
  --connect-timeout 20 \
  "$PBF_URL" \
  -o "${PBF_PATH}.tmp"
mv "${PBF_PATH}.tmp" "$PBF_PATH"

rm -f "$GPKG_PATH"

# With the SQLite SQL dialect, geometry must be explicitly projected.
# Omitting it creates a valid-looking but non-spatial GeoPackage table.
BUILDING_SQL="SELECT geometry, osm_id, osm_way_id, building, hstore_get_value(other_tags, 'height') AS height, hstore_get_value(other_tags, 'building:levels') AS building_levels FROM multipolygons WHERE building IS NOT NULL"
ROAD_SQL="SELECT geometry, osm_id, highway, name, hstore_get_value(other_tags, 'ref') AS ref, hstore_get_value(other_tags, 'maxspeed') AS maxspeed FROM lines WHERE highway IN ('motorway','motorway_link','trunk','trunk_link','primary','primary_link','secondary','secondary_link','tertiary','tertiary_link','residential','living_street','unclassified','service')"

printf '[osm-cache] build buildings layer\n'
ogr2ogr \
  -f GPKG "$GPKG_PATH" "$PBF_PATH" \
  -oo TAGS_FORMAT=HSTORE \
  --config OSM_MAX_TMPFILE_SIZE 2048 \
  -nln buildings \
  -lco SPATIAL_INDEX=YES \
  -dialect SQLite \
  -sql "$BUILDING_SQL"

printf '[osm-cache] build roads layer\n'
ogr2ogr \
  -f GPKG -update "$GPKG_PATH" "$PBF_PATH" \
  -oo TAGS_FORMAT=HSTORE \
  --config OSM_MAX_TMPFILE_SIZE 2048 \
  -nln roads \
  -lco SPATIAL_INDEX=YES \
  -dialect SQLite \
  -sql "$ROAD_SQL"

ogrinfo -ro -so "$GPKG_PATH" buildings >/dev/null
ogrinfo -ro -so "$GPKG_PATH" roads >/dev/null

# Verify the layers are actually spatial, not merely tables with attributes.
# A world-sized spatial filter is intentional: we only care that GDAL can set one.
ogrinfo -ro "$GPKG_PATH" buildings -spat -180 -90 180 90 >/dev/null
ogrinfo -ro "$GPKG_PATH" roads -spat -180 -90 180 90 >/dev/null

{
  printf 'source_url=%s\n' "$PBF_URL"
  printf 'generated_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "${GPKG_PATH}.source.txt"

if [[ "${KEEP_OSM_PBF:-false}" != "true" ]]; then
  rm -f "$PBF_PATH"
fi

printf '[osm-cache] ready: %s\n' "$GPKG_PATH"
du -h "$GPKG_PATH"

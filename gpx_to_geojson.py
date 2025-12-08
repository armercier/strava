from pathlib import Path

import json

import gpxpy

"""
gpx_to_geojson.py

Convert GPX track files into a single GeoJSON FeatureCollection.

Description:
- Scans a directory of .gpx files, parses each file with gpxpy, and extracts track
    point coordinates to produce GeoJSON LineString features.
- Coordinates are emitted in GeoJSON order [longitude, latitude].
- Each feature includes a minimal properties object with:
        - "name": the GPX track name when available, otherwise the GPX file stem
        - "source_file": the original filename
- All features are written into a single FeatureCollection JSON file.

Constants (expected to be defined in the module):
- GPX_DIR: Path-like location containing .gpx files to process.
- OUT_GEOJSON: Path-like destination for the resulting GeoJSON file.

Primary functions:
- gpx_file_to_feature(path):
        Parse the GPX file at the given Path and return a GeoJSON Feature dict
        representing the track as a LineString, or None if the GPX contains no points.
        Returns:
                dict or None
        Notes:
                - Iterates tracks -> segments -> points to collect coordinates.
                - Preserves source filename in feature properties.
                - Propagates file I/O or gpxpy.parse exceptions to the caller.

- main():
        Iterate over sorted .gpx files in GPX_DIR, convert each to a feature (if any),
        assemble a FeatureCollection, and write it to OUT_GEOJSON using json.dumps.
        Prints a summary line with the count of written tracks and the resolved output path.

Usage:
- Run as a script (if __name__ == "__main__": main()) to produce the GeoJSON file.
- Ensure the gpxpy dependency is installed (e.g. pip install gpxpy).
- Adjust GPX_DIR and OUT_GEOJSON in the module as needed, or refactor into
    a reusable function if integrating into a larger application.

Behavioral notes:
- Empty or malformed GPX files that raise parsing errors will cause exceptions
    unless handled externally.
- The output JSON is written without pretty-printing (compact form).
- GeoJSON coordinate order follows the specification: [lon, lat].

Examples:
- Basic invocation: python gpx_to_geojson.py
- To change input directory in code: set GPX_DIR = Path("my_gpx_dir")
"""


GPX_DIR = Path("gpx_activities")
OUT_GEOJSON = Path("tracks.geojson")


def gpx_file_to_feature(path):
    """Convert a single GPX file to a GeoJSON Feature (LineString)."""
    with path.open("r") as f:
        gpx = gpxpy.parse(f)

    points = []
    for track in gpx.tracks:
        for segment in track.segments:
            for p in segment.points:
                # GeoJSON expects [lon, lat]
                points.append([p.longitude, p.latitude])

    if not points:
        return None

    feature = {
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": points,
        },
        "properties": {
            "name": gpx.tracks[0].name if gpx.tracks else path.stem,
            "source_file": str(path.name),
        },
    }
    return feature


def main():
    features = []
    for path in sorted(GPX_DIR.glob("*.gpx")):
        feat = gpx_file_to_feature(path)
        if feat is not None:
            features.append(feat)

    fc = {
        "type": "FeatureCollection",
        "features": features,
    }

    OUT_GEOJSON.write_text(json.dumps(fc))
    print(f"Wrote {len(features)} tracks to {OUT_GEOJSON.resolve()}")


if __name__ == "__main__":
    main()
from pathlib import Path

import webbrowser

import gpxpy
import folium


"""
plot_gpx_osm.py

Description:
    Small utility to read a GPX track and produce an interactive HTML map
    using folium with OpenTopoMap tiles. The script extracts track points
    (latitude, longitude) from a GPX file, draws the track as a PolyLine,
    fits the map to the track bounds, saves the result to an HTML file and
    optionally opens it in the default web browser.

Requirements:
    - Python 3.7+
    - gpxpy (for parsing GPX files)
    - folium (for creating the map)
    - A valid GPX file with track and segment points

Public functions and behavior:
    read_gpx_points(path: pathlib.Path) -> list[tuple[float, float]]
        Parse the GPX file at `path` and return a list of (latitude, longitude)
        tuples representing the track points in order.
        Raises RuntimeError if no track points are found.

    make_map(points: Sequence[tuple[float, float]], out_html: pathlib.Path) -> pathlib.Path
        Create a folium.Map centered at the middle point of `points`, add an
        OpenTopoMap TileLayer, draw the GPX track as a PolyLine, fit the map to
        the track bounds, save the map to `out_html` and return `out_html`.

Script usage (when run as __main__):
    - Configure GPX_PATH to point to your GPX file and OUT_HTML to the desired
      output HTML path (defaults are provided in the module).
    - The script verifies the GPX file exists, reads the points, generates the
      map, prints the saved HTML path, and opens it in the default browser.

Notes and caveats:
    - The code expects the GPX to contain at least one track with one segment
      and multiple points. Tracks with no points will cause a RuntimeError.
    - The tile URL uses OpenTopoMap; be mindful of tile usage policies and
      attribution requirements when distributing or deploying this tool.
    - The created map uses tiles=None for the base folium.Map and explicitly
      adds the OpenTopoMap TileLayer to ensure correct attribution and control.

Example:
    # set GPX_PATH to your file and run the script:
    # python plot_gpx_osm.py

"""

GPX_PATH = Path("gpx/activity_16673694374.gpx")  # <-- put your GPX file here
OUT_HTML = Path("gpx_map.html")


def read_gpx_points(path: Path):
    """Return a list of (lat, lon) points from a GPX track."""
    with path.open("r") as f:
        gpx = gpxpy.parse(f)

    points = []
    for track in gpx.tracks:
        for segment in track.segments:
            for p in segment.points:
                points.append((p.latitude, p.longitude))

    if not points:
        raise RuntimeError("No track points found in GPX file.")

    return points


def make_map(points, out_html: Path):
    """Create a small map with OpenTopoMap tiles and the GPX track."""

    # Center of the map = middle point of the track
    mid_idx = len(points) // 2
    center = points[mid_idx]

    # Base map (we'll overwrite tiles below)
    m = folium.Map(
        location=center,
        zoom_start=13,
        tiles=None,  # we'll add our own tiles
    )

    # --- OpenTopoMap tile layer ---
    folium.TileLayer(
        tiles="https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
        attr="Map data: &copy; OpenStreetMap contributors, SRTM | "
             "Map style: &copy; OpenTopoMap (CC-BY-SA)",
        name="OpenTopoMap",
        max_zoom=17,
    ).add_to(m)

    # Add the GPX track as a line
    folium.PolyLine(
        locations=points,
        weight=3,
        opacity=0.9,
    ).add_to(m)

    # Fit map to the bounds of the track
    lats = [lat for lat, lon in points]
    lons = [lon for lat, lon in points]
    bounds = [[min(lats), min(lons)], [max(lats), max(lons)]]
    m.fit_bounds(bounds)

    m.save(str(out_html))
    return out_html


if __name__ == "__main__":
    if not GPX_PATH.exists():
        raise SystemExit(f"GPX file not found: {GPX_PATH}")

    pts = read_gpx_points(GPX_PATH)
    html_path = make_map(pts, OUT_HTML)
    print("Map saved to:", html_path.resolve())

    # Open in your default browser
    webbrowser.open(html_path.resolve().as_uri())
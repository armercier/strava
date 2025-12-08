from pathlib import Path
import webbrowser
import gpxpy
import folium

"""
plot_gpx_swisstopo.py

Description:
    Utility script to read a GPX track and render it on an interactive Folium map
    using the swisstopo "pixelkarte-farbe" WMTS tiles. The resulting map is saved
    as an HTML file and (when run as a script) opened in the default web browser.

Key behavior:
    - Parses a GPX file and extracts track points (latitude, longitude).
    - Creates a Folium map without default tiles and adds the swisstopo tile layer.
    - Draws the GPX track as a PolyLine and fits the map bounds to the track extents.
    - Saves the map to an output HTML file.

Dependencies:
    - Python 3.7+
    - gpxpy
    - folium

Constants:
    - GPX_PATH (Path): Default input GPX file path (can be replaced by caller).
    - OUT_HTML (Path): Default output HTML map file name.

Functions:
    read_gpx_points(path: Path) -> list[tuple[float, float]]
        Read and parse GPX track points from the given Path.
        Returns:
            A list of (latitude, longitude) tuples in decimal degrees.
        Raises:
            RuntimeError: If no track points are found in the GPX file.
        Notes:
            - Only tracks -> segments -> points are considered; waypoints and routes
              are ignored.
            - The returned coordinate order is (lat, lon) to match Folium's expectations.

    make_map(points: Sequence[tuple[float, float]], out_html: Path) -> Path
        Create and save a Folium map showing the provided track points using the
        swisstopo WMTS tile layer.
        Parameters:
            points: Iterable of (latitude, longitude) pairs.
            out_html: Path where the generated HTML will be saved.
        Returns:
            The Path to the saved HTML file (out_html).
        Behavior:
            - Centers the initial map view on the midpoint of the track.
            - Adds the swisstopo "pixelkarte-farbe" WMTS tile layer (max_zoom=19).
            - Draws the track as a PolyLine (weight=3, opacity=0.9).
            - Fits the map bounds to the min/max lat/lon of the track.
        Notes:
            - The function calls m.save(str(out_html)) and returns out_html.

Script usage (if executed as __main__):
    - Verifies GPX_PATH exists; exits with SystemExit if not found.
    - Reads points via read_gpx_points and generates a map with make_map.
    - Prints the absolute path to the saved map and opens it in the default browser.

Examples:
    >>> pts = read_gpx_points(Path("activity.gpx"))
    >>> make_map(pts, Path("activity_map.html"))

Error handling:
    - Missing GPX file when run as a script -> SystemExit with an explanatory message.
    - GPX without track points -> RuntimeError from read_gpx_points.

Notes and limitations:
    - The script assumes GPX coordinates are in WGS84 (latitude/longitude).
    - Tile usage is subject to the swisstopo terms of use; attribution is added to the map.
    - If using other tile servers or custom styles, replace the TileLayer URL and attribution.
"""


GPX_PATH = Path("gpx/activity_16673694374.gpx")  # <-- put your GPX file here
OUT_HTML = Path("gpx_swisstopo_map.html")


def read_gpx_points(path: Path):
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
    # Center of the map = middle point of the track
    mid_idx = len(points) // 2
    center = points[mid_idx]

    # Create folium map WITHOUT default tiles
    m = folium.Map(
        location=center,
        zoom_start=13,
        tiles=None,   # important: we add our own tiles
    )

    # --- swisstopo pixelkarte-farbe (topographic) ---
    folium.TileLayer(
        tiles=(
            "https://wmts.geo.admin.ch/1.0.0/"
            "ch.swisstopo.pixelkarte-farbe/default/current/3857/{z}/{x}/{y}.jpeg"
        ),
        attr="&copy; swisstopo / geo.admin.ch",
        name="swisstopo",
        max_zoom=19,
    ).add_to(m)

    # Add the GPX track as a line
    folium.PolyLine(
        locations=points,
        weight=3,
        opacity=0.9,
    ).add_to(m)

    # Fit map to bounds of track
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
    webbrowser.open(html_path.resolve().as_uri())
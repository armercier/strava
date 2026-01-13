from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import List, Tuple

import folium
import gpxpy
import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st
from streamlit.components.v1 import html

from daily_sync import main as sync_latest
from fetch_gpx_range import GPX_DIR
from fetch_hr_stream import get_hr_stream_cached
from fetch_power_stream import get_power_stream_cached
from strava_tracks import export_gpx_for_activity

CSV_PATH = Path("activities_clean.csv")


# ---------- DATA HELPERS ----------

@st.cache_data
def load_activities() -> pd.DataFrame:
    df = pd.read_csv(CSV_PATH)

    if "date" in df.columns:
        df["activity_date"] = pd.to_datetime(df["date"]).dt.date
    elif "start_date_local" in df.columns:
        df["activity_date"] = pd.to_datetime(df["start_date_local"]).dt.date
    else:
        raise RuntimeError("No 'date' or 'start_date_local' column in CSV.")

    if "sport_type" in df.columns:
        df["sport"] = df["sport_type"]
    elif "sport" in df.columns:
        df["sport"] = df["sport"]
    elif "type" in df.columns:
        df["sport"] = df["type"]
    else:
        raise RuntimeError("No sport column found (sport_type/sport/type).")

    df["id"] = df["id"].astype(int)
    return df


def ensure_gpx(activity_id: int) -> Path:
    """Return path to GPX for this activity; export from Strava if missing."""
    GPX_DIR.mkdir(exist_ok=True)
    gpx_path = GPX_DIR / f"activity_{activity_id}.gpx"
    if not gpx_path.exists():
        gpx_path = export_gpx_for_activity(activity_id)
    return gpx_path


def read_gpx_points(path: Path) -> List[Tuple[float, float]]:
    with path.open("r") as f:
        gpx = gpxpy.parse(f)

    pts: List[Tuple[float, float]] = []
    for track in gpx.tracks:
        for segment in track.segments:
            for p in segment.points:
                pts.append((p.latitude, p.longitude))

    if not pts:
        raise RuntimeError(f"No points in GPX file: {path}")
    return pts


def read_gpx_elevation_trace(path: Path):
    """Return (time_s, elevation_m) if GPX has time/elevation, else (None, None)."""
    with path.open("r") as f:
        gpx = gpxpy.parse(f)

    times = []
    elevs = []
    start_time = None

    for track in gpx.tracks:
        for segment in track.segments:
            for p in segment.points:
                if p.elevation is None or p.time is None:
                    continue
                if start_time is None:
                    start_time = p.time
                times.append((p.time - start_time).total_seconds())
                elevs.append(p.elevation)

    if not times or not elevs:
        return None, None
    return times, elevs


def make_swisstopo_map(points: List[Tuple[float, float]]) -> str:
    mid_idx = len(points) // 2
    center = points[mid_idx]

    m = folium.Map(location=center, zoom_start=13, tiles=None)

    folium.TileLayer(
        tiles=(
            "https://wmts.geo.admin.ch/1.0.0/"
            "ch.swisstopo.pixelkarte-farbe/default/current/3857/{z}/{x}/{y}.jpeg"
        ),
        attr="&copy; swisstopo / geo.admin.ch",
        name="swisstopo",
        max_zoom=19,
    ).add_to(m)

    folium.PolyLine(
        locations=points,
        weight=3,
        opacity=0.9,
    ).add_to(m)

    lats = [lat for lat, lon in points]
    lons = [lon for lat, lon in points]
    bounds = [[min(lats), min(lons)], [max(lats), max(lons)]]
    m.fit_bounds(bounds)

    return m._repr_html_()


def make_swisstopo_map_mini(points: List[Tuple[float, float]]) -> str:
    """Small-height map for thumbnails."""
    mid_idx = len(points) // 2
    center = points[mid_idx]

    m = folium.Map(location=center, zoom_start=12, tiles=None, width="100%", height=200)
    folium.TileLayer(
        tiles=(
            "https://wmts.geo.admin.ch/1.0.0/"
            "ch.swisstopo.pixelkarte-farbe/default/current/3857/{z}/{x}/{y}.jpeg"
        ),
        attr="&copy; swisstopo / geo.admin.ch",
        name="swisstopo",
        max_zoom=19,
    ).add_to(m)
    folium.PolyLine(locations=points, weight=3, opacity=0.9).add_to(m)
    lats = [lat for lat, lon in points]
    lons = [lon for lat, lon in points]
    bounds = [[min(lats), min(lons)], [max(lats), max(lons)]]
    m.fit_bounds(bounds)
    return m._repr_html_()


def load_hr_stream(activity_id: int):
    try:
        t, hr, _ = get_hr_stream_cached(activity_id, use_cache=True)
        return t, hr
    except Exception:
        return None, None


def load_power_stream(activity_id: int):
    try:
        t, watts, _ = get_power_stream_cached(activity_id, use_cache=True)
        return t, watts
    except Exception:
        return None, None


# ---------- STREAMLIT APP ----------


def main():
    st.set_page_config(page_title="Training Calendar", layout="wide")
    st.title("Training Calendar")

    if "ran_sync" not in st.session_state:
        with st.spinner("Syncing latest activities (GPX/HR/Power)..."):
            try:
                sync_latest()
                st.session_state["ran_sync"] = True
            except Exception as e:
                st.error(f"Sync failed: {e}")
                st.session_state["ran_sync"] = False

    df = load_activities()
    all_dates = sorted(df["activity_date"].unique())
    last_activity_date = max(all_dates) if all_dates else date.today()
    if "selected_activity_id" not in st.session_state:
        st.session_state["selected_activity_id"] = None
    if "selected_date" not in st.session_state:
        st.session_state["selected_date"] = last_activity_date

    st.sidebar.header("Filters")
    default_date = last_activity_date

    sports = sorted(df["sport"].dropna().unique())
    selected_sports = st.sidebar.multiselect(
        "Sports",
        options=sports,
        default=sports,
    )

    week_offset = st.sidebar.number_input(
        "Week offset (relative to current)", value=0, step=1,
        help="0 = current week, -1 = previous week, 1 = next week, etc."
    )

    tab_week, tab_detail = st.tabs(["Week overview", "Activity detail"])

    with tab_week:
        ref_day = last_activity_date + timedelta(weeks=int(week_offset))
        week_start = ref_day - timedelta(days=ref_day.weekday())  # Monday
        week_dates = [week_start + timedelta(days=i) for i in range(7)]
        st.subheader(f"Week of {week_start.isoformat()}")
        cols = st.columns(7)
        for col, d in zip(cols, week_dates):
            day_acts = df[
                (df["activity_date"] == d) & (df["sport"].isin(selected_sports))
            ]
            with col:
                col.markdown(f"**{d.strftime('%a')}**\n{d.isoformat()}")
                if day_acts.empty:
                    col.caption("No activity")
                    continue
                # Limit to 2 thumbnails per day to keep layout small
                for _, row in day_acts.head(2).iterrows():
                    act_id = int(row["id"])
                    label = f"{row['sport']} – {row.get('name', '')}"
                    col.markdown(f"*{label}*")
                    try:
                        gpx_path = ensure_gpx(act_id)
                        pts = read_gpx_points(gpx_path)
                        mini_html = make_swisstopo_map_mini(pts)
                        html(mini_html, height=180)
                    except Exception:
                        col.caption("Map unavailable")
                    if col.button(
                        "Open",
                        key=f"open_{act_id}_{d}",
                        use_container_width=True,
                    ):
                        st.session_state["selected_activity_id"] = act_id
                        st.session_state["selected_date"] = d
                        try:
                            st.experimental_rerun()  # older Streamlit versions
                        except AttributeError:
                            st.rerun()  # newer Streamlit

    with tab_detail:
        initial_date = (
            st.session_state["selected_date"]
            if st.session_state["selected_date"] is not None
            else default_date
        )
        selected_date = st.sidebar.date_input(
            "Select a date",
            value=initial_date,
            min_value=min(all_dates) if all_dates else date.today(),
            max_value=max(all_dates) if all_dates else date.today(),
        )
        st.session_state["selected_date"] = selected_date

        day_df = df[
            (df["activity_date"] == selected_date)
            & (df["sport"].isin(selected_sports))
        ].copy()

        st.subheader(f"Activities on {selected_date}")

        if day_df.empty:
            st.info("No activities for this date with selected sports.")
            return

        options = []
        for _, row in day_df.iterrows():
            label = f"{int(row['id'])} – {row.get('name', '')} ({row['sport']})"
            options.append((label, int(row["id"])))

        labels = [o[0] for o in options]
        ids = {o[0]: o[1] for o in options}

        selected_label = None
        default_index = 0
        if st.session_state["selected_activity_id"] is not None:
            for idx, (_, act_id) in enumerate(options):
                if act_id == st.session_state["selected_activity_id"]:
                    default_index = idx
                    break

        selected_label = st.selectbox("Choose an activity", labels, index=default_index)
        activity_id = ids[selected_label]
        st.session_state["selected_activity_id"] = activity_id

        act_row = day_df.loc[day_df["id"] == activity_id].iloc[0]

        col_metrics = st.container()
        col1, col2 = st.columns(2)

        with col_metrics:
            st.markdown("### Summary")

            distance_km = act_row.get("distance_km") or (
                act_row.get("distance", 0) / 1000.0
            )
            moving_h = act_row.get("moving_time_h") or (
                act_row.get("moving_time", 0) / 3600.0
            )
            elev = act_row.get("total_elevation_gain", 0)

            avg_hr = act_row.get("average_heartrate", None)
            max_hr = act_row.get("max_heartrate", None)
            avg_watts = act_row.get("average_watts", None)
            kjs = act_row.get("kilojoules", None)

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Sport", act_row["sport"])
            m2.metric("Distance (km)", f"{distance_km:.1f}")
            m3.metric("Moving time (h)", f"{moving_h:.2f}")
            m4.metric("Elevation gain (m)", f"{elev:.0f}")

            m5, m6, m7, m8 = st.columns(4)
            if avg_hr is not None:
                m5.metric("Avg HR", f"{avg_hr:.0f} bpm")
            if max_hr is not None:
                m6.metric("Max HR", f"{max_hr:.0f} bpm")
            if avg_watts is not None:
                m7.metric("Avg Watts", f"{avg_watts:.0f}")
            if kjs is not None:
                m8.metric("Work (kJ)", f"{kjs:.0f}")

        with col1:
            st.markdown("### Map (swisstopo)")
            try:
                gpx_path = ensure_gpx(activity_id)
                pts = read_gpx_points(gpx_path)
                map_html = make_swisstopo_map(pts)
                html(map_html, height=500)
            except Exception as e:
                st.error(f"Could not build map for this activity: {e}")

        with col2:
            st.markdown("### Heart rate / Power")
            t_hr, hr = load_hr_stream(activity_id)
            t_w, watts = load_power_stream(activity_id)
            gpx_path = ensure_gpx(activity_id)
            elev_t, elev = read_gpx_elevation_trace(gpx_path)

            if (t_hr is None or hr is None) and (t_w is None or watts is None) and (
                elev_t is None or elev is None
            ):
                st.info("No HR, power, or elevation streams available for this activity.")
            else:
                fig, ax = plt.subplots(figsize=(6, 4))
                if t_hr is not None and hr is not None:
                    ax.plot(t_hr, hr, label="HR (bpm)", color="red")
                if t_w is not None and watts is not None:
                    ax.plot(t_w, watts, label="Power (W)", color="blue", alpha=0.6)
                ax.set_xlabel("Time (s)")
                ax.grid(True, alpha=0.3)

                if elev_t is not None and elev is not None:
                    ax2 = ax.twinx()
                    ax2.plot(elev_t, elev, label="Elevation (m)", color="green", alpha=0.5)
                    ax2.set_ylabel("Elevation (m)")
                    lines, labels = ax.get_legend_handles_labels()
                    lines2, labels2 = ax2.get_legend_handles_labels()
                    if lines or lines2:
                        ax2.legend(lines + lines2, labels + labels2, loc="upper right")
                else:
                    if ax.has_data():
                        ax.legend()
                st.pyplot(fig)


if __name__ == "__main__":
    main()

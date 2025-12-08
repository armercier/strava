# Strava

Strava, but better...No fuss, no paywal, CLI, only fun.


## Actual workflow

`strava_fetch.py` : Fetch **ALL** strava activities into a single .json files named `strava_activities.json`

`build_table.py` : Build a CSV table from the .json file

`weekly_stats.py` : Compile weekly stats from the csv file

`fetch_hr_stream.py` : Fetch for a singel activity the full HR time series from strava and sves it into a .csv file

`strava_tracks.py` : Fetch the geo data from strava for a given activity and stores it either as a geojson or a gpx for visualisation

`plot_gpx_osm.py` : plot a specific gpx file into a OpenStreetMap

`plot_gpx_swisstopo.py` : pot a specific gpx file into a Swisstopo map

`last_month_export_and_map.py` : Fetch and export all of the gpx data and create an html map for visualisation in browser

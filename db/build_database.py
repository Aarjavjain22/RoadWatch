"""Build the RoadWatch Buffalo database from public data sources.

Creates roadwatch_buffalo.db (SQLite, no server needed) with:
  sources                  data dictionary: where each table comes from and how good it is
  roads                    OpenStreetMap streets in Buffalo
  city_311_road_requests   City of Buffalo 311 road requests, May 2024 - now (potholes, paving, dumping, snow, salt)
  city_311_potholes_hist   City of Buffalo 311 pothole requests, 2019 - May 2024
  events_511ny             511NY traffic events in Buffalo (crashes, debris, construction, ...), 2010 - Jun 2024
  crashes_nys              NYS DMV crash records for the City of Buffalo, 2021 - 2024 (no coordinates)
  weather_hourly           Buffalo airport (KBUF) hourly weather, 2019 - now
  bridges_erie             NYSDOT bridge conditions, Erie County (no coordinates)
  data_quality_checks      automatic checks run on every build

Every row keeps its source id and the time it was loaded. Points are snapped to the nearest
OpenStreetMap street so every record can be joined by street name.

Run:  python3 build_database.py            (needs internet; about 2-4 minutes)
Query: sqlite3 roadwatch_buffalo.db "SELECT type, COUNT(*) FROM city_311_road_requests GROUP BY 1;"
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import os
import sqlite3
import ssl
import statistics
import time
import urllib.parse
import urllib.request

DB = "roadwatch_buffalo.db"
UA = {"User-Agent": "RoadWatch-Buffalo-student-project/1.0"}


def _ssl_context():
    """Use certifi if installed, else the macOS system bundle, so HTTPS works on any Python build."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    if os.path.exists("/etc/ssl/cert.pem"):
        return ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    return ssl.create_default_context()


SSL = _ssl_context()
LOADED_AT = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
BBOX = (42.826, -78.925, 42.967, -78.795)  # south, west, north, east (City of Buffalo)
ROAD_311_TYPES = ["Pothole Issue", "Other Hole in Road", "Street Paving Request", "Illegal Dumping Street",
                  "Street Snow Plowing Request", "Street Snow Plowing Issue", "Street Salting Request", "Street Salting Issue"]


def http_json(url, params=None, data=None, timeout=180):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = urllib.parse.urlencode(data).encode() if data else None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=UA), timeout=timeout, context=SSL) as r:
                return json.load(r)
        except Exception as exc:  # retry transient network errors
            if attempt == 2:
                raise
            print(f"  retrying after error: {exc}")
            time.sleep(3)


def http_text(url, timeout=240):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout, context=SSL) as r:
        return r.read().decode("utf-8", "replace")


def socrata(domain, dataset, where, select=None, page=50000):
    rows, offset = [], 0
    while True:
        p = {"$where": where, "$limit": page, "$offset": offset, "$order": ":id"}
        if select:
            p["$select"] = select
        batch = http_json(f"https://{domain}/resource/{dataset}.json", p)
        rows += batch
        if len(batch) < page:
            return rows
        offset += page


# ---------------------------------------------------------------- geometry helpers
M_LAT = 111_132.0
M_LON = 111_320.0 * math.cos(math.radians(42.9))


def xy(lat, lon):
    return ((lon - BBOX[1]) * M_LON, (lat - BBOX[0]) * M_LAT)


class StreetIndex:
    """Nearest-street lookup over OpenStreetMap segments (simple grid index)."""

    def __init__(self, ways, cell=60.0):
        self.cell, self.grid = cell, {}
        for w in ways:
            pts = [xy(lat, lon) for lat, lon in w["coords"]]
            for a, b in zip(pts, pts[1:]):
                x0, x1 = sorted((a[0], b[0]))
                y0, y1 = sorted((a[1], b[1]))
                for i in range(int(x0 // cell), int(x1 // cell) + 1):
                    for j in range(int(y0 // cell), int(y1 // cell) + 1):
                        self.grid.setdefault((i, j), []).append((a, b, w))

    def nearest(self, lat, lon, max_m=60.0):
        px, py = xy(lat, lon)
        ci, cj = int(px // self.cell), int(py // self.cell)
        best, bd = None, max_m
        for i in range(ci - 1, ci + 2):
            for j in range(cj - 1, cj + 2):
                for a, b, w in self.grid.get((i, j), ()):
                    bx, by = b[0] - a[0], b[1] - a[1]
                    L = bx * bx + by * by or 1.0
                    t = max(0.0, min(1.0, ((px - a[0]) * bx + (py - a[1]) * by) / L))
                    d = math.hypot(px - (a[0] + t * bx), py - (a[1] + t * by))
                    if d < bd:
                        bd, best = d, w
        return (best, round(bd, 1)) if best else (None, None)


def fix_latlon(a, b):
    """Buffalo 311 stores some rows with latitude/longitude swapped. Returns (lat, lon, was_swapped)."""
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return None, None, False
    return (a, b, False) if a > 0 else (b, a, True)


def in_bbox(lat, lon):
    return lat is not None and BBOX[0] <= lat <= BBOX[2] and BBOX[1] <= lon <= BBOX[3]


# ---------------------------------------------------------------- schema
SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE sources (
  source_id TEXT PRIMARY KEY, name TEXT, provider TEXT, url TEXT, license TEXT,
  table_name TEXT, rows INTEGER, date_from TEXT, date_to TEXT, location_accuracy TEXT,
  refresh TEXT, role_in_project TEXT, known_issues TEXT, loaded_at TEXT
);
CREATE TABLE roads (
  osm_way_id INTEGER PRIMARY KEY, name TEXT, highway_class TEXT, length_m REAL,
  geometry_geojson TEXT, source_id TEXT REFERENCES sources(source_id), loaded_at TEXT
);
CREATE TABLE city_311_road_requests (
  case_number TEXT PRIMARY KEY, type TEXT, created_at TEXT, closed_at TEXT, days_to_close REAL,
  status TEXT, status_description TEXT, outcome TEXT, reported_location TEXT, location_kind TEXT,
  street TEXT, council_district TEXT, neighborhood TEXT, city_flagged_duplicate INTEGER,
  lat REAL, lon REAL, coords_were_swapped INTEGER, shares_exact_coords INTEGER,
  osm_way_id INTEGER, osm_street TEXT, dist_to_street_m REAL,
  source_id TEXT REFERENCES sources(source_id), loaded_at TEXT
);
CREATE TABLE city_311_potholes_hist (
  case_reference TEXT PRIMARY KEY, created_at TEXT, closed_at TEXT, days_to_close REAL, status TEXT,
  address TEXT, council_district TEXT, lat REAL, lon REAL, coords_placeholder INTEGER,
  osm_way_id INTEGER, osm_street TEXT, dist_to_street_m REAL,
  source_id TEXT REFERENCES sources(source_id), loaded_at TEXT
);
CREATE TABLE events_511ny (
  event_id INTEGER PRIMARY KEY, event_type TEXT, category TEXT, organization TEXT, facility TEXT,
  created_at TEXT, closed_at TEXT, duration_min REAL, lat REAL, lon REAL,
  osm_way_id INTEGER, osm_street TEXT, dist_to_street_m REAL,
  source_id TEXT REFERENCES sources(source_id), loaded_at TEXT
);
CREATE TABLE crashes_nys (
  crash_id INTEGER PRIMARY KEY, year INTEGER, crash_date TEXT, crash_time TEXT, day_of_week TEXT,
  severity TEXT, collision_type TEXT, event TEXT, road_surface TEXT, weather TEXT, lighting TEXT,
  road_descriptor TEXT, traffic_control TEXT, vehicles INTEGER,
  source_id TEXT REFERENCES sources(source_id), loaded_at TEXT
);
CREATE TABLE weather_hourly (
  observed_at TEXT PRIMARY KEY, temp_f REAL, dewpoint_f REAL, precip_in REAL, visibility_mi REAL,
  wind_kt REAL, weather_codes TEXT, condition TEXT,
  source_id TEXT REFERENCES sources(source_id), loaded_at TEXT
);
CREATE TABLE bridges_erie (
  bin TEXT PRIMARY KEY, location TEXT, feature_carried TEXT, feature_crossed TEXT, owner TEXT,
  year_built_or_replaced INTEGER, last_inspection TEXT, poor_status TEXT,
  source_id TEXT REFERENCES sources(source_id), loaded_at TEXT
);
CREATE TABLE data_quality_checks (
  check_id INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT, check_name TEXT, value REAL,
  unit TEXT, result TEXT, note TEXT, checked_at TEXT
);
CREATE INDEX idx_311_type ON city_311_road_requests(type);
CREATE INDEX idx_311_created ON city_311_road_requests(created_at);
CREATE INDEX idx_311_street ON city_311_road_requests(osm_street);
CREATE INDEX idx_hist_created ON city_311_potholes_hist(created_at);
CREATE INDEX idx_511_cat ON events_511ny(category);
CREATE INDEX idx_crash_surface ON crashes_nys(road_surface);
"""


def main():
    if os.path.exists(DB):
        os.remove(DB)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    con.execute("PRAGMA defer_foreign_keys = ON")  # sources are registered after their row counts are known; checked at commit
    def add_source(row):
        """Register a source in the data dictionary (before its rows, so foreign keys hold)."""
        con.execute(f"INSERT INTO sources VALUES ({','.join('?' * 14)})", row)

    # ------------------------------------------------ roads (OpenStreetMap)
    print("1/7 OpenStreetMap streets ...")
    q = (f'[out:json][timeout:180];way["highway"~"^(motorway|trunk|primary|secondary|tertiary|residential|unclassified|'
         f'motorway_link|trunk_link|primary_link|secondary_link|living_street)$"]({BBOX[0]},{BBOX[1]},{BBOX[2]},{BBOX[3]});out geom tags;')
    osm = http_json("https://overpass-api.de/api/interpreter", data={"data": q}, timeout=300)["elements"]
    ways = []
    for e in osm:
        if "geometry" not in e:
            continue
        coords = [(g["lat"], g["lon"]) for g in e["geometry"]]
        length = sum(math.dist(xy(*a), xy(*b)) for a, b in zip(coords, coords[1:]))
        ways.append({"id": e["id"], "name": e["tags"].get("name"), "cls": e["tags"].get("highway"), "coords": coords, "len": length})
    con.executemany("INSERT INTO roads VALUES (?,?,?,?,?,?,?)", [
        (w["id"], w["name"], w["cls"], round(w["len"], 1),
         json.dumps({"type": "LineString", "coordinates": [[round(lon, 6), round(lat, 6)] for lat, lon in w["coords"]]}),
         "osm_roads", LOADED_AT) for w in ways])
    streets = StreetIndex([w for w in ways if w["name"]])
    add_source(("osm_roads", "Buffalo streets", "OpenStreetMap contributors", "https://www.openstreetmap.org", "ODbL 1.0",
                    "roads", len(ways), None, None, "Street centerlines, typically within a few meters",
                    "On every build", "Base map; every record is snapped to the nearest named street",
                    "Small residential streets may be missing names", LOADED_AT))

    # ------------------------------------------------ 311 road requests (May 2024 - now)
    print("2/7 City of Buffalo 311 road requests ...")
    types = ",".join(f"'{t}'" for t in ROAD_311_TYPES)
    raw = socrata("data.buffalony.gov", "3tj7-3tdz", f"type in({types})")
    coord_count = {}
    parsed = []
    for r in raw:
        lat, lon, swapped = fix_latlon(r.get("latitude"), r.get("longitude"))
        parsed.append((r, lat, lon, swapped))
        if lat is not None:
            k = (round(lat, 6), round(lon, 6))
            coord_count[k] = coord_count.get(k, 0) + 1
    rows = []
    for r, lat, lon, swapped in parsed:
        created, closed = r.get("createddate"), r.get("closeddate")
        days = None
        if created and closed:
            days = round((dt.datetime.fromisoformat(closed[:19]) - dt.datetime.fromisoformat(created[:19])).total_seconds() / 86400, 2)
        desc = r.get("statusdescription") or ""
        outcome = ("filled" if "has been filled" in desc else "responded, no detail" if "has responded" in desc
                   else "not needed / completed / referred" if "not in need of repair" in desc
                   else "open" if r.get("status") != "Closed" else "closed, other")
        loc = r.get("reportedlocation") or ""
        kind = "intersection" if ("&" in loc or " and " in loc.lower()) else "address" if loc[:1].isdigit() else "street or other"
        way, dist = streets.nearest(lat, lon) if in_bbox(lat, lon) else (None, None)
        rows.append((r.get("casenumber"), r.get("type"), created, closed, days, r.get("status"), desc, outcome, loc, kind,
                     r.get("street"), r.get("council_district"), r.get("planningneighborhood"), int(bool(r.get("duplicate_request"))),
                     lat, lon, int(swapped), int(coord_count.get((round(lat, 6), round(lon, 6)), 0) > 1) if lat is not None else None,
                     way["id"] if way else None, way["name"] if way else None, dist, "buffalo_311_2024", LOADED_AT))
    con.executemany(f"INSERT OR IGNORE INTO city_311_road_requests VALUES ({','.join('?' * 23)})", rows)
    dates = sorted(r[2] for r in rows if r[2])
    add_source(("buffalo_311_2024", "311 Service Requests (May 2024 - Present), road types", "City of Buffalo, Open Data Buffalo",
                    "https://data.buffalony.gov/d/3tj7-3tdz", "Open Data Buffalo terms", "city_311_road_requests", len(rows),
                    dates[0][:10], dates[-1][:10], "Address or intersection of the complaint, not the exact defect",
                    "Daily by the city", "Where residents report problems; route planning and comparison, not ground truth",
                    "Some rows have swapped lat/lon; 'Closed' often means 'responded', not 'fixed'", LOADED_AT))

    # ------------------------------------------------ 311 potholes 2019 - May 2024
    print("3/7 City of Buffalo 311 potholes 2019 - May 2024 ...")
    hist = socrata("data.buffalony.gov", "whkc-e5vr", "type='Pot Hole (Req_Serv)' AND open_date >= '2019-01-01'",
                   select="case_reference,open_date,closed_date,status,address_number,address_line_1,council_district,latitude,longitude")
    hrows = []
    for r in hist:
        lat, lon, _ = fix_latlon(r.get("latitude"), r.get("longitude"))
        placeholder = int(lat is not None and round(lat, 3) == 43.0 and round(lon, 3) == -79.0)
        if placeholder:  # the city used (43.0, -79.0) when a request had no real location
            lat = lon = None
        o, c = r.get("open_date"), r.get("closed_date")
        days = round((dt.datetime.fromisoformat(c[:19]) - dt.datetime.fromisoformat(o[:19])).total_seconds() / 86400, 2) if o and c else None
        way, dist = streets.nearest(lat, lon) if in_bbox(lat, lon) else (None, None)
        addr = " ".join(x for x in (r.get("address_number"), r.get("address_line_1")) if x)
        hrows.append((r.get("case_reference"), o, c, days, r.get("status"), addr, r.get("council_district"), lat, lon, placeholder,
                      way["id"] if way else None, way["name"] if way else None, dist, "buffalo_311_hist", LOADED_AT))
    con.executemany(f"INSERT OR IGNORE INTO city_311_potholes_hist VALUES ({','.join('?' * 15)})", hrows)
    hd = sorted(r[1] for r in hrows if r[1])
    add_source(("buffalo_311_hist", "311 Service Requests (July 2008 - May 2024), potholes since 2019", "City of Buffalo, Open Data Buffalo",
                    "https://data.buffalony.gov/d/whkc-e5vr", "Open Data Buffalo terms", "city_311_potholes_hist", len(hrows),
                    hd[0][:10], hd[-1][:10], "Address-level from 2024; before that most rows carry a placeholder point", "Frozen (replaced by the 2024 dataset)",
                    "Long history for seasonal patterns and repair-time baselines (dates are reliable, locations mostly are not)",
                    "88% of rows use the placeholder location (43.0, -79.0); only 2024 rows have real coordinates", LOADED_AT))

    # ------------------------------------------------ 511NY events
    print("4/7 511NY traffic events (Buffalo) ...")
    ev = socrata("data.ny.gov", "ah74-pg4w", "upper(city) like '%BUFFALO%'")

    def category(t):
        t = (t or "").lower()
        for key, cat in [("pothole", "pothole repair"), ("debris", "debris"), ("crash", "crash"), ("accident", "crash"),
                         ("disabled", "disabled vehicle"), ("fire", "vehicle fire"), ("snow", "weather / surface"),
                         ("wet pavement", "weather / surface"), ("weather", "weather / surface"), ("paving", "pavement work"),
                         ("milling", "pavement work"), ("crack", "pavement work"), ("bridge", "bridge work"),
                         ("construction", "construction / roadwork"), ("roadwork", "construction / roadwork"),
                         ("repair", "construction / roadwork"), ("delay", "congestion"), ("congestion", "congestion")]:
            if key in t:
                return cat
        return "other"

    erows = []
    for i, r in enumerate(ev, 1):
        lat, lon = (float(r["latitude"]), float(r["longitude"])) if r.get("latitude") and r.get("longitude") else (None, None)
        c, e = r.get("create_time"), r.get("close_time")
        dur = round((dt.datetime.fromisoformat(e[:19]) - dt.datetime.fromisoformat(c[:19])).total_seconds() / 60, 1) if c and e else None
        way, dist = streets.nearest(lat, lon, 120) if in_bbox(lat, lon) else (None, None)
        erows.append((i, r.get("event_type"), category(r.get("event_type")), r.get("organization_name"), r.get("facility_name"),
                      c, e, dur, lat, lon, way["id"] if way else None, way["name"] if way else None, dist, "nys_511_events", LOADED_AT))
    con.executemany(f"INSERT INTO events_511ny VALUES ({','.join('?' * 15)})", erows)
    ed = sorted(r[5] for r in erows if r[5])
    add_source(("nys_511_events", "511 NY Events: Beginning 2010 (Buffalo)", "NYS Department of Transportation via data.ny.gov",
                    "https://data.ny.gov/d/ah74-pg4w", "NY Open Data terms", "events_511ny", len(erows), ed[0][:10], ed[-1][:10],
                    "Point placed by traffic operators, usually on the highway or intersection",
                    "Archive ends mid-2024; live feed needs a free 511NY developer key",
                    "Real incidents for new hazard classes (debris, crashes, disabled vehicles) and road work",
                    "Mostly highways and major roads; event types changed names over the years", LOADED_AT))

    # ------------------------------------------------ NYS crashes (Buffalo)
    print("5/7 NYS crash records (City of Buffalo) ...")
    cr = socrata("data.ny.gov", "e8ky-4vqe", "municipality='BUFFALO'")
    crows = [(i, int(r["year"]) if r.get("year") else None, (r.get("date") or "")[:10], r.get("time"), r.get("day_of_week"),
              r.get("accident_descriptor"), r.get("collision_type_descriptor"), r.get("event_descriptor"), r.get("road_surface_conditions"),
              r.get("weather_conditions"), r.get("lighting_conditions"), r.get("road_descriptor"), r.get("traffic_control_device"),
              int(r["number_of_vehicles_involved"]) if (r.get("number_of_vehicles_involved") or "").isdigit() else None,
              "nys_crashes", LOADED_AT) for i, r in enumerate(cr, 1)]
    con.executemany(f"INSERT INTO crashes_nys VALUES ({','.join('?' * 16)})", crows)
    add_source(("nys_crashes", "Motor Vehicle Crashes - Case Information (Buffalo)", "NYS DMV via data.ny.gov",
                    "https://data.ny.gov/d/e8ky-4vqe", "NY Open Data terms", "crashes_nys", len(crows),
                    str(min(r[1] for r in crows)), str(max(r[1] for r in crows)), "No coordinates (city level only)",
                    "Yearly", "How road surface, weather and light relate to crashes (weather agent)",
                    "No location; police-reported crashes only", LOADED_AT))

    # ------------------------------------------------ weather (KBUF hourly, IEM ASOS archive)
    print("6/7 Buffalo airport hourly weather ...")
    today = dt.date.today()
    url = ("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station=BUF&data=tmpf&data=dwpf&data=p01i&data=vsby"
           f"&data=sknt&data=wxcodes&year1=2019&month1=1&day1=1&year2={today.year}&month2={today.month}&day2={today.day}"
           "&tz=Etc/UTC&format=onlycomma&latlon=no&missing=M&trace=0.0001&report_type=3")
    text = http_text(url, timeout=600)
    wrows = {}

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def cond(codes):
        c = codes or ""
        if "SN" in c or "PL" in c:
            return "snow / ice"
        if "FZ" in c:
            return "freezing"
        if "RA" in c or "DZ" in c or "TS" in c:
            return "rain"
        if "FG" in c or "BR" in c or "HZ" in c:
            return "fog / mist"
        return "dry"

    for r in csv.DictReader(io.StringIO(text)):
        ts = (r.get("valid") or "").replace(" ", "T") + ":00Z"
        codes = "" if r.get("wxcodes") in (None, "M") else r["wxcodes"]
        wrows[ts] = (ts, num(r.get("tmpf")), num(r.get("dwpf")), num(r.get("p01i")), num(r.get("vsby")), num(r.get("sknt")),
                     codes, cond(codes), "iem_kbuf", LOADED_AT)
    con.executemany(f"INSERT INTO weather_hourly VALUES ({','.join('?' * 10)})", list(wrows.values()))
    wd = sorted(wrows)
    add_source(("iem_kbuf", "Buffalo Niagara Intl Airport (KBUF) hourly observations", "NWS ASOS via Iowa Environmental Mesonet",
                    "https://mesonet.agron.iastate.edu/request/download.phtml", "Public domain (NWS)", "weather_hourly", len(wrows),
                    wd[0][:10] if wd else None, wd[-1][:10] if wd else None, "One station about 10 km east of downtown",
                    "Hourly", "Weather context for every frame and report; freeze-thaw counts",
                    "One point for the whole city; lake-effect snow can differ street to street", LOADED_AT))

    # ------------------------------------------------ bridges (Erie County)
    print("7/7 NYSDOT bridge conditions (Erie County) ...")
    br = socrata("data.ny.gov", "wpyb-cjy8", "county='Erie'")
    brows = [(r.get("bin"), r.get("location"), r.get("feature_carried"), r.get("feature_crossed"), r.get("owner"),
              int(r["year_built_or_replaced"]) if (r.get("year_built_or_replaced") or "").isdigit() else None,
              (r.get("date_of_last_inspection") or "")[:10], r.get("poor_status"), "nysdot_bridges", LOADED_AT) for r in br]
    con.executemany(f"INSERT OR IGNORE INTO bridges_erie VALUES ({','.join('?' * 10)})", brows)
    add_source(("nysdot_bridges", "Bridge Conditions, NYS Department of Transportation (Erie County)", "NYSDOT via data.ny.gov",
                    "https://data.ny.gov/d/wpyb-cjy8", "NY Open Data terms", "bridges_erie", len(brows), None, None,
                    "No coordinates (named location only)", "After each inspection", "Infrastructure condition context",
                    "County-wide, not just the city", LOADED_AT))


    # ------------------------------------------------ data quality checks
    def check(table, name, sql, unit, ok, note):
        v = con.execute(sql).fetchone()[0]
        v = round(v, 2) if isinstance(v, float) else v
        con.execute("INSERT INTO data_quality_checks (table_name, check_name, value, unit, result, note, checked_at) VALUES (?,?,?,?,?,?,?)",
                    (table, name, v, unit, ok(v), note, LOADED_AT))

    T = "city_311_road_requests"
    P = f"FROM {T} WHERE type IN ('Pothole Issue','Other Hole in Road')"
    pct = lambda cond: f"SELECT 100.0 * SUM(CASE WHEN {cond} THEN 1 ELSE 0 END) / COUNT(*) {P}"
    check(T, "Pothole rows missing coordinates", pct("lat IS NULL"), "%", lambda v: "pass" if v < 1 else "warn", "Rows we cannot map")
    check(T, "Pothole rows with lat/lon swapped (fixed on load)", pct("coords_were_swapped = 1"), "%", lambda v: "fixed", "Corrected automatically")
    check(T, "Pothole rows sharing exact coordinates with another report", pct("shares_exact_coords = 1"), "%",
          lambda v: "warn" if v > 20 else "pass", "Points are geocoded addresses or intersections, not the defect itself")
    check(T, "Pothole rows located at an intersection", pct("location_kind = 'intersection'"), "%", lambda v: "info", "Intersection points can sit tens of meters from the defect")
    check(T, "Median distance from 311 point to street centerline",
          f"SELECT dist_to_street_m {P} AND dist_to_street_m IS NOT NULL ORDER BY dist_to_street_m LIMIT 1 OFFSET (SELECT COUNT(*)/2 {P} AND dist_to_street_m IS NOT NULL)",
          "m", lambda v: "pass" if v < 10 else "warn", "Address points sit on the curb or parcel, not on the lane")
    check(T, "Pothole rows more than 30 m from any named street", pct("dist_to_street_m IS NULL OR dist_to_street_m > 30"), "%",
          lambda v: "pass" if v < 5 else "warn", "Could not be matched to a street")
    check(T, "Closed pothole cases that say 'filled'", f"SELECT 100.0 * SUM(outcome='filled') / SUM(status='Closed') {P}", "%",
          lambda v: "warn" if v < 60 else "pass", "'Closed' is not proof of a repair")
    check(T, "Pothole cases closed within 1 day", pct("days_to_close < 1"), "%", lambda v: "info", "Fast closures often mean 'responded'")
    check(T, "City-flagged duplicates", pct("city_flagged_duplicate = 1"), "%", lambda v: "info", "The city's own duplicate flag")
    check(T, "Days since newest record", f"SELECT julianday('now') - julianday(MAX(created_at)) FROM {T}", "days",
          lambda v: "pass" if v < 7 else "warn", "Freshness")
    check("city_311_potholes_hist", "Rows with the placeholder location (43.0, -79.0)",
          "SELECT 100.0 * SUM(coords_placeholder) / COUNT(*) FROM city_311_potholes_hist", "%",
          lambda v: "warn" if v > 10 else "pass", "Usable for dates and counts, not for maps")
    check("city_311_potholes_hist", "Rows with a real location inside Buffalo",
          "SELECT 100.0 * SUM(lat IS NOT NULL) / COUNT(*) FROM city_311_potholes_hist", "%",
          lambda v: "warn" if v < 50 else "pass", "Almost all from 2024")
    check("events_511ny", "Events with coordinates", "SELECT 100.0 * SUM(lat IS NOT NULL) / COUNT(*) FROM events_511ny", "%",
          lambda v: "pass" if v > 95 else "warn", "")
    check("events_511ny", "Events matched to a named street within 120 m", "SELECT 100.0 * SUM(osm_street IS NOT NULL) / COUNT(*) FROM events_511ny", "%",
          lambda v: "pass" if v > 80 else "warn", "Highway events often sit on unnamed ramps")
    check("events_511ny", "Days since newest record", "SELECT julianday('now') - julianday(MAX(created_at)) FROM events_511ny", "days",
          lambda v: "pass" if v < 30 else "warn", "Archive stopped in 2024; live data needs the 511NY key")
    check("weather_hourly", "Hours with a temperature reading", "SELECT 100.0 * SUM(temp_f IS NOT NULL) / COUNT(*) FROM weather_hourly", "%",
          lambda v: "pass" if v > 98 else "warn", "")
    check("weather_hourly", "Days since newest record", "SELECT julianday('now') - julianday(MAX(observed_at)) FROM weather_hourly", "days",
          lambda v: "pass" if v < 2 else "warn", "")
    check("crashes_nys", "Crashes with a road-surface value", "SELECT 100.0 * SUM(road_surface IS NOT NULL AND road_surface NOT IN ('Unknown','')) / COUNT(*) FROM crashes_nys",
          "%", lambda v: "pass" if v > 90 else "warn", "")
    con.commit()

    print("\nRows per table:")
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        print(f"  {t:26s} {con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]:>8,}")
    con.execute("VACUUM")
    con.close()
    print(f"\nDone: {DB}")


if __name__ == "__main__":
    main()

"""Import the public Buffalo SQLite snapshot into an initialized PostGIS database."""

import argparse
import sqlite3
from pathlib import Path

import psycopg
from psycopg import sql


TABLES = (
    ("sources", "source_id"),
    ("roads", "osm_way_id"),
    ("city_311_road_requests", "case_number"),
    ("city_311_potholes_hist", "case_reference"),
    ("events_511ny", "event_id"),
    ("crashes_nys", "crash_id"),
    ("weather_hourly", "observed_at"),
    ("bridges_erie", "bin"),
    ("data_quality_checks", "check_id"),
)


def load_table(source, target, table, key):
    columns = target.execute(
        """
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table,),
    ).fetchall()
    if not columns:
        raise ValueError(f"Missing target table {table}. Apply schema_postgis.sql first.")

    rows = source.execute(f'SELECT * FROM "{table}"')
    source_columns = {column[0] for column in rows.description}
    names = []
    expressions = []
    for name, datatype in columns:
        if name in source_columns:
            expression = sql.SQL("%s")
        elif name == "geom" and "geometry_geojson" in source_columns:
            expression = sql.SQL("ST_SetSRID(ST_GeomFromGeoJSON(%s::text), 4326)")
        elif name == "geom" and {"lon", "lat"} <= source_columns:
            expression = sql.SQL("ST_SetSRID(ST_MakePoint(%s, %s), 4326)")
        else:
            raise ValueError(f"No source mapping for {table}.{name}")
        names.append((name, datatype))
        expressions.append(expression)

    insert = sql.SQL("""
        INSERT INTO public.{} ({}) VALUES ({})
        ON CONFLICT ({}) DO NOTHING
    """).format(
        sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(name) for name, _ in names),
        sql.SQL(", ").join(expressions),
        sql.Identifier(key),
    )

    def parameters(row):
        values = []
        for name, datatype in names:
            if name in source_columns:
                value = row[name]
                if value is not None and datatype == "boolean":
                    value = bool(value)
                if table == "sources" and name in {"date_from", "date_to"} and value and len(value) == 4:
                    # Some source coverage is recorded only to the year.
                    value += "-01-01" if name == "date_from" else "-12-31"
                values.append(value)
            elif table == "roads":
                values.append(row["geometry_geojson"])
            else:
                values.extend((row["lon"], row["lat"]))
        return values

    processed = 0
    inserted = 0
    with target.cursor() as cursor:
        while batch := rows.fetchmany(1000):
            cursor.executemany(insert, map(parameters, batch))
            processed += len(batch)
            inserted += cursor.rowcount
    return processed, inserted


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    # libpq supplies the connection settings through PG* variables or .pgpass.
    with sqlite3.connect(args.snapshot.resolve().as_uri() + "?mode=ro", uri=True) as source:
        source.row_factory = sqlite3.Row
        with psycopg.connect() as target:
            # Buffalo and NYS source timestamps without offsets are local civil times.
            target.execute("SET LOCAL TIME ZONE 'America/New_York'")
            results = [(table, *load_table(source, target, table, key)) for table, key in TABLES]
            target.execute("""
                SELECT setval(pg_get_serial_sequence('public.data_quality_checks', 'check_id'),
                    COALESCE((SELECT MAX(check_id) FROM public.data_quality_checks), 1),
                    EXISTS (SELECT 1 FROM public.data_quality_checks))
            """)
    for table, processed, inserted in results:
        print(f"{table}: {processed:,} source rows, {inserted:,} inserted")


if __name__ == "__main__":
    main()

"""Track pickup availability windows and export them as a readable CSV."""

import csv
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apple_pickup_once import COLORS_BY_PART
from product_catalog import CATALOG

PACIFIC = ZoneInfo("America/Los_Angeles")
PART_BY_COLOR = {color: part for part, color in COLORS_BY_PART.items()}
STORE_LABEL = re.compile(r"(.+) \(([^()]+)\)$")

SCHEMA = """
    CREATE TABLE IF NOT EXISTS stock_periods (
        id INTEGER PRIMARY KEY,
        zip TEXT NOT NULL,
        store TEXT NOT NULL,
        part TEXT NOT NULL,
        store_name TEXT NOT NULL,
        first_seen REAL NOT NULL,
        last_seen REAL NOT NULL,
        gone REAL,
        gone_basis TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS stock_periods_open
        ON stock_periods(zip, store, part) WHERE gone IS NULL;
    CREATE INDEX IF NOT EXISTS stock_periods_latest ON stock_periods(last_seen DESC);
"""


def update_periods(connection, zip_code, checked_at, rows, gone_basis):
    changed = False
    for store, part, status, label in rows:
        if status == "available":
            name = label.split(" — ", 1)[0].removesuffix(f" ({store})")
            connection.execute(
                """INSERT INTO stock_periods (zip, store, part, store_name, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (zip, store, part) WHERE gone IS NULL DO UPDATE
                   SET last_seen = excluded.last_seen, store_name = excluded.store_name""",
                (zip_code, store, part, name, checked_at, checked_at),
            )
            changed = True
        elif status == "unavailable":
            updated = connection.execute(
                """UPDATE stock_periods SET gone = ?, gone_basis = ?
                   WHERE zip = ? AND store = ? AND part = ? AND gone IS NULL""",
                (checked_at, gone_basis, zip_code, store, part),
            )
            changed |= updated.rowcount > 0
    return changed


def backfill(connection):
    connection.execute("BEGIN IMMEDIATE")
    if connection.execute("PRAGMA user_version").fetchone()[0] >= 1:
        return

    for check in connection.execute(
        """SELECT checked_at, zip, unknown, available FROM checks
           WHERE error = '' AND stores > 0 ORDER BY id"""
    ):
        zip_code = check["zip"]
        available = set()
        rows = []
        labels = json.loads(check["available"])
        for label in labels:
            pieces = label.split(" — ", 2)
            if len(pieces) < 2:
                continue
            location, color = pieces[:2]
            match = STORE_LABEL.fullmatch(location)
            if match and color in PART_BY_COLOR:
                store, part = match[2], PART_BY_COLOR[color]
                available.add((store, part))
                rows.append((store, part, "available", label))

        if not check["unknown"] and len(available) == len(labels):
            current = connection.execute(
                "SELECT store, part FROM stock_periods WHERE zip = ? AND gone IS NULL", (zip_code,)
            )
            rows.extend(
                (store, part, "unavailable", "")
                for store, part in current
                if (store, part) not in available
            )
        update_periods(connection, zip_code, check["checked_at"], rows, "Inferred from saved check")
    connection.execute("PRAGMA user_version = 1")


def format_time(timestamp):
    if timestamp is None:
        return ""
    return datetime.fromtimestamp(timestamp, PACIFIC).isoformat(sep=" ", timespec="seconds")


def period_values(row):
    return (
        row["zip"], row["store_name"], row["store"], CATALOG.get(row["part"], {}).get("label", row["part"]),
        format_time(row["first_seen"]), format_time(row["last_seen"]), format_time(row["gone"]),
        row["gone_basis"] or "",
    )


def write_csv(path, rows):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
    try:
        with temporary.open("w", encoding="utf-8", newline="") as file:
            writer = csv.writer(file)
            writer.writerow([
                "ZIP code", "Store", "Store ID", "Product", "First seen in stock (Pacific)",
                "Last seen in stock (Pacific)", "First seen gone (Pacific)", "Gone status source",
            ])
            writer.writerows(period_values(row) for row in rows)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

"""Background stock checks and local history for the dashboard."""

import http.client
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, time as clock_time, timedelta

from apple_pickup_once import (
    ZIP_CODES,
    fetch_pickup,
    load_email_settings,
    pickup_statuses,
    send_stock_email,
)
from product_catalog import CATALOG, pickup_groups
from stock_history import SCHEMA, backfill, update_periods, write_csv
from monitor_paths import WORK_FOLDER

DATABASE = WORK_FOLDER / "monitor.sqlite3"
DEFAULT_INTERVAL = 60
RECOVERY_INTERVAL = 600
APPLE_BAG_URL = "https://www.apple.com/shop/bag"
INTERVAL = int(os.environ.get("APPLE_STOCK_INTERVAL", DEFAULT_INTERVAL))
START_SECOND = os.environ.get("APPLE_STOCK_START_SECOND", "")
HISTORY_LOCK = threading.Lock()
MONITORING_LISTS = os.environ.get("APPLE_STOCK_LISTS")


@contextmanager
def database(path=None):
    with closing(sqlite3.connect(path or DATABASE, timeout=5)) as connection, connection:
        connection.row_factory = sqlite3.Row
        yield connection


def initialize(path=None):
    path = path or DATABASE
    path.touch(mode=0o600, exist_ok=True)
    with database(path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS control (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER NOT NULL,
                heartbeat REAL NOT NULL
            );
            INSERT OR IGNORE INTO control VALUES (1, 1, 0);
            CREATE TABLE IF NOT EXISTS recovery (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER NOT NULL DEFAULT 0,
                review_at REAL NOT NULL DEFAULT 0,
                cooling_down INTEGER NOT NULL DEFAULT 0,
                after_id INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT ''
            );
            INSERT OR IGNORE INTO recovery (id) VALUES (1);
            CREATE TABLE IF NOT EXISTS desktop_alerts (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER NOT NULL DEFAULT 0
            );
            INSERT OR IGNORE INTO desktop_alerts (id) VALUES (1);
            CREATE TABLE IF NOT EXISTS stock (
                zip TEXT NOT NULL,
                store TEXT NOT NULL,
                part TEXT NOT NULL,
                status TEXT NOT NULL,
                PRIMARY KEY (zip, store, part)
            );
            CREATE TABLE IF NOT EXISTS pickup_stores (
                zip TEXT NOT NULL,
                id TEXT NOT NULL,
                name TEXT NOT NULL,
                PRIMARY KEY (zip, id)
            );
            CREATE TABLE IF NOT EXISTS checks (
                id INTEGER PRIMARY KEY,
                checked_at REAL NOT NULL,
                zip TEXT NOT NULL,
                stores INTEGER NOT NULL,
                unknown INTEGER NOT NULL DEFAULT 0,
                available TEXT NOT NULL DEFAULT '[]',
                restocks TEXT NOT NULL DEFAULT '[]',
                error TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS checks_zip ON checks(zip, id DESC);
            CREATE INDEX IF NOT EXISTS checks_pending ON checks(id) WHERE email = 'Pending';
        """)
        connection.executescript(SCHEMA)
        connection.execute("BEGIN IMMEDIATE")
        if "alert_routes" not in {row["name"] for row in connection.execute("PRAGMA table_info(checks)")}:
            connection.execute("ALTER TABLE checks ADD COLUMN alert_routes TEXT")


def reset_recovery(db, now):
    """Require newly recorded checks after a manual change or automatic resume."""
    db.execute(
        """UPDATE recovery SET cooling_down = 0, review_at = ?,
                  after_id = (SELECT COALESCE(MAX(id), 0) FROM checks), message = ''
           WHERE id = 1""", (now + RECOVERY_INTERVAL,),
    )


def review_recovery(db, zip_codes, now):
    row = db.execute("SELECT * FROM recovery WHERE id = 1").fetchone()
    if not row["enabled"] or now < row["review_at"]:
        return ""
    # Recheck after taking the write lock so a dashboard toggle cannot race this decision.
    db.execute("BEGIN IMMEDIATE")
    row = db.execute("SELECT * FROM recovery WHERE id = 1").fetchone()
    if not row["enabled"] or now < row["review_at"]:
        return ""
    message = ""
    enabled = db.execute("SELECT enabled FROM control WHERE id = 1").fetchone()[0]
    if row["cooling_down"]:
        reset_recovery(db, now)
        db.execute("UPDATE control SET enabled = 1 WHERE id = 1")
        message = "Automatic recovery resumed stock checks after a 10-minute pause."
    else:
        blocked = []
        if enabled:
            for code in sorted(zip_codes):
                errors = db.execute(
                    """SELECT error FROM checks WHERE zip = ? AND id > ?
                       ORDER BY id DESC LIMIT 3""", (code, row["after_id"]),
                ).fetchall()
                if len(errors) == 3 and all(error[0] == "ValueError: HTTP 541 Unknown" for error in errors):
                    blocked.append(code)
        db.execute("UPDATE recovery SET review_at = ? WHERE id = 1", (now + RECOVERY_INTERVAL,))
        if blocked:
            db.execute("UPDATE control SET enabled = 0 WHERE id = 1")
            db.execute("UPDATE recovery SET cooling_down = 1 WHERE id = 1")
            message = "Automatic recovery paused stock checks after three consecutive HTTP 541 errors for ZIP " + ", ".join(blocked) + "."
    if message:
        db.execute("UPDATE recovery SET message = ? WHERE id = 1", (message,))
    return message


def export_stock_history():
    with HISTORY_LOCK, database() as connection:
        rows = connection.execute("SELECT * FROM stock_periods ORDER BY last_seen DESC, id DESC")
        try:
            write_csv(DATABASE.with_name("stock_history.csv"), rows)
        except OSError as error:
            print(f"Could not update the stock history CSV: {type(error).__name__}", file=sys.stderr)


def show_duo_restock(zip_code, matches):
    """Open the bag in the default browser and display a native notification in the logged-in Mac session."""
    body = "\n".join(f"{color} · {name}" for color, name in sorted(matches))
    commands = (
        ("the default browser", ["/usr/bin/open", APPLE_BAG_URL], None),
        ("macOS notification", ["/usr/bin/osascript", "-", f"iPhone Duo in stock near {zip_code}", body],
         """on run argv
           display notification (item 2 of argv) with title (item 1 of argv)
         end run"""),
    )
    for action, command, source in commands:
        try:
            subprocess.run(command, input=source, text=True, check=True, timeout=5, stdout=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as error:
            print(f"Could not open {action}: {type(error).__name__}", file=sys.stderr, flush=True)


def record_check(zip_code, parts=None):
    checked_at = time.time()
    try:
        if parts is None:
            stores = fetch_pickup(zip_code)
            rows = list(pickup_statuses(stores))
        else:
            stores = fetch_pickup(zip_code, parts=parts)
            rows = list(pickup_statuses(stores, {part: CATALOG[part]["label"] for part in parts}))
        store_names = [(zip_code, store["storeNumber"], store["storeName"]) for store in stores]
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError, AttributeError) as error:
        with database() as connection:
            connection.execute(
                "INSERT INTO checks (checked_at, zip, stores, error) VALUES (?, ?, 0, ?)",
                (checked_at, zip_code, f"{type(error).__name__}: {error}"),
            )
        return

    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.executemany(
            """INSERT INTO pickup_stores VALUES (?, ?, ?)
               ON CONFLICT (zip, id) DO UPDATE SET name = excluded.name""", store_names,
        )
        if not any(status in ("available", "unavailable") for _, _, status, _ in rows):
            connection.execute(
                "INSERT INTO checks (checked_at, zip, stores, error) VALUES (?, ?, ?, ?)",
                (checked_at, zip_code, len(stores), "ValueError: Apple returned no usable pickup statuses."),
            )
            return
        previous = {
            (row["store"], row["part"]): row["status"]
            for row in connection.execute(
                "SELECT store, part, status FROM stock WHERE zip = ?", (zip_code,)
            )
        }
        available, restocks, updates = [], [], []
        alerts = {}
        duo_matches = set()
        desktop_enabled = sys.platform == "darwin" and connection.execute(
            "SELECT enabled FROM desktop_alerts WHERE id = 1"
        ).fetchone()[0]
        names_by_store = {store: name for _, store, name in store_names}
        unknown = 0
        for store, part, status, label in rows:
            if status not in ("available", "unavailable"):
                unknown += 1
                continue
            if status == "available":
                available.append(label)
                if previous.get((store, part)) != "available":
                    restocks.append(label)
                    if parts is not None:
                        routes = parts[part]
                        config = CATALOG[part]
                        if desktop_enabled and config["model"] == "iPhone Duo" and (None in routes or store in routes):
                            duo_matches.add((config["color"], names_by_store[store]))
                        recipients = routes.get(None, set()) | routes.get(store, set())
                        for recipient in recipients:
                            alerts.setdefault(part, {}).setdefault(recipient, []).append(label)
            if previous.get((store, part)) != status:
                updates.append((zip_code, store, part, status))
        connection.executemany(
            """INSERT INTO stock VALUES (?, ?, ?, ?)
               ON CONFLICT (zip, store, part) DO UPDATE SET status = excluded.status""",
            updates,
        )
        changed = update_periods(connection, zip_code, checked_at, rows, "Confirmed unavailable")
        batches = []
        for part, matches_by_recipient in alerts.items():
            recipients_by_matches = {}
            for recipient, matches in matches_by_recipient.items():
                recipients_by_matches.setdefault(tuple(matches), []).append(recipient)
            batches.extend({"part": part, "recipients": sorted(recipients), "stores": matches}
                           for matches, recipients in recipients_by_matches.items())
        routes = None if parts is None else json.dumps(batches)
        connection.execute(
            """INSERT INTO checks (checked_at, zip, stores, unknown, available, restocks, email, alert_routes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                checked_at, zip_code, len(stores), unknown, json.dumps(available),
                json.dumps(restocks), "Pending" if alerts or (parts is None and restocks) else "",
                routes,
            ),
        )
    if duo_matches:
        show_duo_restock(zip_code, duo_matches)
    if changed:
        export_stock_history()


def email_failure(error):
    status = getattr(error, "smtp_code", None)
    return type(error).__name__ if status is None else f"{type(error).__name__} {status}"


def deliver_pending_alerts(stop):
    while not stop.is_set():
        with database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, zip, restocks, alert_routes FROM checks WHERE email = 'Pending' ORDER BY id LIMIT 1"
            ).fetchone()
            if row:
                connection.execute("UPDATE checks SET email = 'Sending' WHERE id = ?", (row["id"],))
        if row is None:
            stop.wait(0.4)
            continue

        try:
            settings = load_email_settings()
            if row["alert_routes"] is not None:
                count = 0
                failures = set()
                for route in json.loads(row["alert_routes"]):
                    config = CATALOG[route["part"]]
                    route_settings = settings | {"email_to": ", ".join(route["recipients"])}
                    try:
                        count += send_stock_email(
                            route_settings, row["zip"], route["stores"], config["label"], config["url"],
                        )
                    except (OSError, ValueError) as error:
                        failures.add(email_failure(error))
                if failures:
                    result = f"Failed ({', '.join(sorted(failures))}) · {count} confirmed"
                else:
                    result = f"Sent {count}"
            elif settings["email_to"]:
                count = send_stock_email(settings, row["zip"], json.loads(row["restocks"]))
                result = f"Sent {count}"
            else:
                result = "No recipients"
        except (OSError, ValueError) as error:
            result = f"Failed ({email_failure(error)})"
        with database() as connection:
            connection.execute("UPDATE checks SET email = ?, alert_routes = NULL WHERE id = ?", (result, row["id"]))


def first_check_delay(timestamp, second):
    """Wait for the selected clock second, or start during that second."""
    if second is None:
        return 0
    elapsed = timestamp % 60
    if second <= elapsed < second + 1:
        return 0
    return (second - elapsed) % 60


def list_start_delay(timestamp, start_time, default_second):
    if not start_time:
        return first_check_delay(timestamp, default_second)
    local = datetime.fromtimestamp(timestamp)
    target = datetime.combine(local.date(), clock_time.fromisoformat(start_time))
    if target.timestamp() > timestamp - 1:
        return max(0, target.timestamp() - timestamp)
    tomorrow = datetime.combine(local.date() + timedelta(days=1), target.time())
    return tomorrow.timestamp() - timestamp


def monitoring_schedules():
    if MONITORING_LISTS is None:
        return [({code: None for code in ZIP_CODES}, INTERVAL, None)] if ZIP_CODES else []
    schedules = []
    for watch in json.loads(MONITORING_LISTS):
        interval = watch.get("interval")
        multiplier = 60 if watch.get("interval_unit") == "minutes" else 1
        schedules.append((pickup_groups([watch]), interval * multiplier if interval else INTERVAL, watch.get("start_time")))
    return schedules


def run(stop=None):
    schedules = monitoring_schedules()
    zip_codes = {code for groups, _, _ in schedules for code in groups}
    if not zip_codes:
        raise SystemExit("Enter at least one ZIP code in the dashboard before starting the monitor.")
    stop = stop or threading.Event()
    start_second = int(START_SECOND) if START_SECOND else None
    initialize()
    with database() as connection:
        backfill(connection)
        connection.execute("UPDATE checks SET email = 'Interrupted', alert_routes = NULL WHERE email = 'Sending'")
    export_stock_history()
    threading.Thread(target=deliver_pending_alerts, args=(stop,), daemon=True).start()

    with ThreadPoolExecutor(max_workers=min(4, len(zip_codes))) as workers:
        in_flight = {}
        deadlines = []
        pending = {}
        next_heartbeat = 0
        next_recovery_poll = 0
        while not stop.is_set():
            now = time.monotonic()
            if now >= next_recovery_poll:
                with database() as connection:
                    message = review_recovery(connection, zip_codes, time.time())
                if message:
                    print(message, flush=True)
                next_recovery_poll = now + 1
            with database() as connection:
                if now >= next_heartbeat:
                    connection.execute("UPDATE control SET heartbeat = ? WHERE id = 1", (time.time(),))
                    next_heartbeat = now + 2
                enabled = connection.execute("SELECT enabled FROM control WHERE id = 1").fetchone()[0]
            if not enabled:
                deadlines.clear()
                pending.clear()
                stop.wait(0.2)
                continue
            if not deadlines:
                timestamp = time.time()
                deadlines = [now + list_start_delay(timestamp, start, start_second)
                             for _, _, start in schedules]
            for index, (groups, interval, _) in enumerate(schedules):
                deadline = deadlines[index]
                if now < deadline:
                    continue
                for zip_code, parts in groups.items():
                    if parts is None:
                        pending[zip_code] = None
                    else:
                        combined = pending.setdefault(zip_code, {})
                        for part, routes in parts.items():
                            combined_routes = combined.setdefault(part, {})
                            for store, recipients in routes.items():
                                combined_routes.setdefault(store, set()).update(recipients)
                # Advance from the scheduled deadline and skip missed rounds.
                deadlines[index] += (int((now - deadline) // interval) + 1) * interval
            for zip_code in list(pending):
                previous = in_flight.get(zip_code)
                if previous is not None:
                    if not previous.done():
                        continue
                    previous.result()
                in_flight[zip_code] = workers.submit(record_check, zip_code, pending.pop(zip_code))
            stop.wait(min(0.2, max(0, min(deadlines) - time.monotonic())))



if __name__ == "__main__":
    run()

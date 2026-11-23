"""Run the stock monitor and its localhost dashboard."""

import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from apple_pickup_once import email_recipients, load_email_settings, save_email_settings
from apple_stock_monitor import DEFAULT_INTERVAL, database, initialize, reset_recovery, run
from monitor_paths import APP_HOME
from stock_history import period_values
from product_catalog import CATALOG, DUO_DEFAULTS, pickup_groups

SOURCE = Path(__file__).resolve().parent
PORT = int(os.environ.get("APPLE_STOCK_PORT", "8766"))
URL = f"http://localhost:{PORT}"
LOCAL_HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}"}
ZIP_CODE = re.compile(r"[0-9]{5}")
SMTP_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]*")
HISTORY_PAGE_SIZE = 50
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
}


def zip_codes(value):
    codes = list(dict.fromkeys(re.split(r"[\s,]+", value.strip()))) if value.strip() else []
    if not codes or any(not ZIP_CODE.fullmatch(code) for code in codes):
        raise ValueError("Enter at least one five-digit US ZIP code.")
    return codes


def recipient_addresses(value):
    return [recipient.addr_spec for recipient in email_recipients(re.sub(r"[\r\n]+", ",", value))]


def query_start_time(value):
    if value is None or value == "":
        return None
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9](?::[0-5][0-9])?", value):
        raise ValueError("Choose a valid first check time for each list.")
    return value


def validate_lists(watches):
    result = []
    for watch in watches:
        name = watch["name"].strip()
        if not name:
            raise ValueError("Give each monitoring list a name.")
        codes = zip_codes(watch["zips"])
        products = []
        seen = set()
        for item in watch["products"]:
            part = item["part"]
            if part not in CATALOG or part in seen:
                raise ValueError("Choose a different supported configuration for each product in a list.")
            seen.add(part)
            recipients = item["recipients"]
            products.append({"part": part, "recipients": None if recipients is None else recipient_addresses(recipients)})
        if not products:
            raise ValueError("Add at least one product to each monitoring list.")
        interval = watch.get("interval")
        unit = watch.get("interval_unit", "seconds")
        if unit not in ("seconds", "minutes"):
            raise ValueError("Choose seconds or minutes for the list interval.")
        if interval not in (None, ""):
            interval = query_interval(interval)
            query_interval(interval * (60 if unit == "minutes" else 1))
        else:
            interval = None
        alert_stores = {}
        for code, selected in watch.get("alert_stores", {}).items():
            if code not in codes or not isinstance(selected, list) or any(
                not isinstance(store, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", store)
                for store in selected
            ):
                raise ValueError("Choose valid alert stores for each ZIP code.")
            alert_stores[code] = list(dict.fromkeys(selected))
        result.append({
            "interval": interval, "interval_unit": unit, "start_time": query_start_time(watch.get("start_time")),
            "name": name, "zips": codes, "alert_stores": alert_stores,
            "recipients": recipient_addresses(watch["recipients"]), "products": products,
        })
    return result


def legacy_lists(codes, recipients):
    return [{
        "name": "iPhone Duo", "zips": codes, "recipients": recipients,
        "products": [{"part": part, "recipients": None} for part in DUO_DEFAULTS],
    }] if codes else []


def query_interval(value):
    try:
        interval = int(str(value).strip())
    except ValueError:
        interval = 0
    if not 1 <= interval <= (2**53 - 1):
        raise ValueError("Enter the check interval as a positive whole number of seconds.")
    return interval


def query_start_second(value):
    if value is None or value == "":
        return None
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,2}", text) or not 0 <= int(text) <= 59:
        raise ValueError("Enter a first check second from 0 to 59, or leave it blank.")
    return int(text)


def write_preferences(path, payload):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        json.dump(payload, file)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def browse_folders(value):
    path = Path(value or Path.home()).expanduser().resolve()
    if not path.is_dir():
        raise ValueError("That folder could not be found on this computer.")
    entries = []
    for child in sorted(path.iterdir(), key=lambda item: item.name.casefold()):
        if not child.name.startswith(".") and child.is_dir():
            entries.append({"name": child.name, "path": str(child)})
    return {"path": str(path), "parent": str(path.parent) if path.parent != path else "", "entries": entries}


class Monitor:
    def __init__(self):
        APP_HOME.mkdir(parents=True, exist_ok=True)
        self.preferences = APP_HOME / "dashboard_settings.json"
        self.guard = threading.RLock()
        self.stopped = threading.Event()
        self.worker = None
        self.start_after = 0
        self.token = secrets.token_urlsafe(32)
        if self.preferences.exists():
            config = json.loads(self.preferences.read_text(encoding="utf-8"))
        else:
            config = {"folder": str(APP_HOME), "zips": []}
        self.folder = Path(config["folder"])
        self.configured = self.preferences.exists()
        self.lists = config.get("lists")
        if self.lists is None:
            settings = load_email_settings(self.folder / "email_settings.ini")
            self.lists = legacy_lists(config.get("zips", []), recipient_addresses(settings["email_to"]))
        self.zips = list(pickup_groups(self.lists))
        self.interval = query_interval(config.get("interval", DEFAULT_INTERVAL))
        self.start_second = query_start_second(config.get("start_second"))

    def start(self):
        threading.Thread(target=self.supervise, daemon=True).start()

    def supervise(self):
        while not self.stopped.is_set():
            with self.guard:
                if self.zips and time.monotonic() >= self.start_after:
                    if self.worker is None or self.worker.poll() is not None:
                        self.start_after = time.monotonic() + 5
                        try:
                            self.start_worker()
                        except OSError as error:
                            print(f"Worker could not start: {type(error).__name__}", file=sys.stderr)
            self.stopped.wait(1)

    def start_worker(self):
        env = os.environ | {
            "APPLE_STOCK_WORK_FOLDER": str(self.folder),
            "APPLE_STOCK_LISTS": json.dumps(self.lists),
            "APPLE_STOCK_INTERVAL": str(self.interval),
            "APPLE_STOCK_START_SECOND": "" if self.start_second is None else str(self.start_second),
        }
        self.worker = subprocess.Popen(
            [sys.executable, "-B", "-u", str(SOURCE / "apple_stock_web.py"), "--worker"],
            stdin=subprocess.PIPE, env=env, cwd=self.folder,
        )

    def stop_worker(self):
        if self.worker is not None:
            self.worker.stdin.close()
            try:
                self.worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.worker.kill()
                self.worker.wait()
            self.worker = None

    @property
    def database_path(self):
        return self.folder / "monitor.sqlite3"

    def configure(self, payload):
        if not payload["folder"].strip():
            raise ValueError("Choose a working folder before continuing.")
        target = Path(payload["folder"]).expanduser().resolve()
        watches = validate_lists(payload["lists"])
        codes = list(pickup_groups(watches))
        if not self.configured and not codes:
            raise ValueError("Add a monitoring list before finishing setup.")
        interval = query_interval(payload.get("interval", self.interval))
        start_second = query_start_second(payload.get("start_second", self.start_second))
        settings_path = target / "email_settings.ini"
        old_email = load_email_settings(settings_path)
        has_recipients = any(recipients for parts in pickup_groups(watches).values()
                             for routes in parts.values() for recipients in routes.values())
        settings = {
            "smtp_host": payload["smtp_host"].strip(),
            "smtp_port": str(payload["smtp_port"]).strip(),
            "smtp_user": payload["smtp_user"].strip(),
            "smtp_password": payload["smtp_password"] or old_email["smtp_password"],
            "email_to": old_email["email_to"],
        }
        if has_recipients:
            if not SMTP_HOST.fullmatch(settings["smtp_host"]):
                raise ValueError("Enter a valid SMTP server name, such as smtp.gmail.com.")
            if not settings["smtp_port"].isdigit() or not 1 <= int(settings["smtp_port"]) <= 65535:
                raise ValueError("Enter an SMTP port between 1 and 65535.")
            if len(email_recipients(settings["smtp_user"])) != 1 or not settings["smtp_password"]:
                raise ValueError("Enter the sender email and its app password to turn on email alerts.")
        if any("\n" in value or "\r" in value for value in settings.values()):
            raise ValueError("Enter the email settings without line breaks.")

        target.mkdir(parents=True, exist_ok=True)
        initialize(target / "monitor.sqlite3")
        save_email_settings(settings, settings_path)
        write_preferences(self.preferences, {
            "folder": str(target), "lists": watches, "interval": interval, "start_second": start_second,
        })
        first_setup = not self.configured
        if (target, watches, interval, start_second) != (self.folder, self.lists, self.interval, self.start_second):
            self.stop_worker()
        self.folder, self.zips, self.interval, self.start_second = target, codes, interval, start_second
        self.lists, self.configured = watches, True
        if first_setup:
            self.toggle(True)
        if self.zips and self.worker is None:
            self.start_worker()

    def toggle(self, enabled):
        if not self.zips:
            raise ValueError("Complete setup to start monitoring.")
        with database(self.database_path) as db:
            db.execute("UPDATE control SET enabled = ? WHERE id = 1", (int(enabled),))
            reset_recovery(db, time.time())

    def toggle_recovery(self, enabled):
        if not self.zips:
            raise ValueError("Add a monitoring list before turning on automatic recovery.")
        with database(self.database_path) as db:
            db.execute("UPDATE recovery SET enabled = ? WHERE id = 1", (int(enabled),))
            reset_recovery(db, time.time())

    def toggle_desktop_alerts(self, enabled):
        if sys.platform != "darwin" or not self.zips:
            raise ValueError("Mac restock alerts require a monitoring list on macOS.")
        with database(self.database_path) as db:
            db.execute("UPDATE desktop_alerts SET enabled = ? WHERE id = 1", (int(enabled),))

    def state(self):
        settings = load_email_settings(self.folder / "email_settings.ini")
        status = {"enabled": False, "online": False, "counts": {}, "latest": [], "stores_by_zip": {}}
        status["desktop_alerts"] = {"supported": sys.platform == "darwin", "enabled": False}
        status["recovery"] = {"enabled": False, "cooling_down": False, "review_at": 0, "message": ""}
        if self.zips and self.database_path.is_file():
            with database(self.database_path) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'recovery'").fetchone():
                    status["recovery"] = dict(db.execute(
                        "SELECT enabled, cooling_down, review_at, message FROM recovery WHERE id = 1"
                    ).fetchone())
                if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'desktop_alerts'").fetchone():
                    status["desktop_alerts"]["enabled"] = bool(db.execute(
                        "SELECT enabled FROM desktop_alerts WHERE id = 1"
                    ).fetchone()[0])
                control = db.execute("SELECT enabled, heartbeat FROM control").fetchone()
                if control:
                    status.update(enabled=bool(control["enabled"]), online=time.time() - control["heartbeat"] < 12)
                status["counts"] = dict(db.execute(
                    """SELECT CASE WHEN error = '' THEN 'ok' ELSE 'errors' END, COUNT(*)
                       FROM checks WHERE checked_at >= ? GROUP BY 1""", (time.time() - 86400,)
                ))
                has_stores = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'pickup_stores'"
                ).fetchone()
                for code in self.zips:
                    if has_stores:
                        status["stores_by_zip"][code] = [dict(store) for store in db.execute(
                            "SELECT id, name FROM pickup_stores WHERE zip = ? ORDER BY name COLLATE NOCASE, id", (code,)
                        )]
                    row = db.execute("SELECT * FROM checks WHERE zip = ? ORDER BY id DESC LIMIT 1", (code,)).fetchone()
                    if row:
                        status["latest"].append(check_data(row))
        return {
            "configured": self.configured, "lists": self.lists, "catalog": CATALOG,
            "interval": self.interval, "zips": self.zips,
            "start_second": self.start_second,
            "folder": str(self.folder), "default_folder": str(APP_HOME), "home": str(Path.home()),
            "smtp_host": settings["smtp_host"] or "smtp.gmail.com",
            "smtp_port": settings["smtp_port"], "smtp_user": settings["smtp_user"],
            "has_password": bool(settings["smtp_password"]),
            **status,
        }

    def history(self, kind, zipcode, page=1, through=None):
        if not self.database_path.is_file():
            return {"rows": [], "page": 0, "pages": 0, "through": None}
        table = "checks" if kind == "checks" else "stock_periods"
        with database(self.database_path) as db:
            db.execute("BEGIN")
            if through is None:
                through = db.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0]
            where = "WHERE id <= ?"
            parameters = [through]
            if zipcode:
                where += " AND zip = ?"
                parameters.append(zipcode)
            total = db.execute(f"SELECT COUNT(*) FROM {table} {where}", parameters).fetchone()[0]
            pages = (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE
            if not pages:
                return {"rows": [], "page": 0, "pages": 0, "through": through}
            page = min(max(int(page), 1), pages)
            records = db.execute(
                f"SELECT * FROM {table} {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                (*parameters, HISTORY_PAGE_SIZE, (page - 1) * HISTORY_PAGE_SIZE),
            ).fetchall()
        rows = [check_data(row) if kind == "checks" else period_values(row) for row in records]
        return {"rows": rows, "page": page, "pages": pages, "through": through}


def check_data(row):
    check = dict(row)
    check["available"] = json.loads(row["available"])
    check.pop("restocks")
    check.pop("alert_routes", None)
    return check


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(12)

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def handle_request(self):
        host = self.headers.get("Host")
        if host not in LOCAL_HOSTS:
            return self.respond(403, {"error": "Open the dashboard using its localhost address."})
        route = urlsplit(self.path)
        monitor = self.server.monitor
        try:
            if self.command == "GET" and route.path in ASSETS:
                name, mime = ASSETS[route.path]
                content = (SOURCE / "web" / name).read_bytes()
                if route.path == "/":
                    content = content.replace(b"__MONITOR_TOKEN__", monitor.token.encode())
                return self.respond(200, content, mime)
            if route.path == "/health" and self.command == "GET":
                return self.respond(200, {"app": "apple-stock-monitor"})
            if self.headers.get("X-Monitor-Token") != monitor.token:
                return self.respond(403, {"error": "Reload the dashboard to reconnect."})
            if self.headers.get("Origin", f"http://{host}") != f"http://{host}":
                return self.respond(403, {"error": "This request did not come from the dashboard."})

            if self.command == "GET":
                fields = {key: values[0] for key, values in parse_qs(route.query).items()}
                return self.get_api(route.path, fields, monitor)
            if self.headers.get_content_type() != "application/json":
                return self.respond(415, {"error": "Expected dashboard settings."})
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 32768:
                return self.respond(400, {"error": "The submitted settings are empty or too large."})
            payload = json.loads(self.rfile.read(length))
            with monitor.guard:
                if route.path == "/api/setup":
                    monitor.configure(payload)
                elif route.path == "/api/toggle" and type(payload.get("enabled")) is bool:
                    monitor.toggle(payload["enabled"])
                elif route.path == "/api/recovery" and type(payload.get("enabled")) is bool:
                    monitor.toggle_recovery(payload["enabled"])
                elif route.path == "/api/desktop-alerts" and type(payload.get("enabled")) is bool:
                    monitor.toggle_desktop_alerts(payload["enabled"])
                else:
                    return self.respond(404, {"error": "That dashboard action was not found."})
                return self.respond(200, monitor.state())
        except PermissionError:
            return self.respond(400, {"error": "The monitor cannot access that folder. Choose the default folder or a folder you own."})
        except (OSError, sqlite3.Error):
            return self.respond(400, {"error": "The monitor could not read or save this folder. Try the default working folder."})
        except (ValueError, AttributeError, KeyError, TypeError) as error:
            message = str(error) if type(error) is ValueError and not self.command == "GET" else ""
            return self.respond(400, {"error": message or "Check the folder or settings and try again."})

    def get_api(self, path, fields, monitor):
        if path == "/api/folders":
            return self.respond(200, browse_folders(fields.get("path")))
        with monitor.guard:
            if path == "/api/state":
                return self.respond(200, monitor.state())
            if path in ("/api/checks", "/api/restocks"):
                through = int(fields["through"]) if "through" in fields else None
                return self.respond(200, monitor.history(
                    path.split("/")[-1], fields.get("zip"), fields.get("page", 1), through,
                ))
            if path == "/api/export":
                export = monitor.folder / "stock_history.csv"
                if export.is_file():
                    return self.respond(200, export.read_bytes(), "text/csv; charset=utf-8")
                return self.respond(404, {"error": "Run a stock check before downloading history."})
        return self.respond(404, {"error": "That dashboard page was not found."})

    def respond(self, status, data, mime="application/json"):
        if isinstance(data, dict):
            data = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


def serve(open_browser):
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as error:
        if open_browser:
            try:
                with urllib.request.urlopen(URL + "/health", timeout=2) as response:
                    if json.load(response).get("app") == "apple-stock-monitor":
                        webbrowser.open(URL)
                        return
            except (OSError, ValueError):
                pass
        raise SystemExit(f"Cannot open {URL}: {error.strerror}.")
    monitor = server.monitor = Monitor()
    monitor.start()
    print(f"Stock Monitor dashboard: {URL}", flush=True)
    if open_browser:
        webbrowser.open(URL)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stopped.set()
        with monitor.guard:
            monitor.stop_worker()
        server.server_close()


if __name__ == "__main__":
    if "--worker" in sys.argv:
        stop = threading.Event()

        def wait_for_parent():
            sys.stdin.buffer.read(1)
            stop.set()

        threading.Thread(target=wait_for_parent, daemon=True).start()
        run(stop)
    else:
        serve("--serve" not in sys.argv)

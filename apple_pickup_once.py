"""Check Apple Store pickup availability once per configured ZIP code."""

import http.client
import json
import os
import smtplib
import ssl
import tempfile
from configparser import ConfigParser, Error as ConfigError
from datetime import datetime
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
from monitor_paths import WORK_FOLDER

ZIP_CODES = tuple(code for value in os.environ.get("APPLE_STOCK_ZIPS", "").split(",") if (code := value.strip()))
PRODUCT = "iPhone Duo 256 GB"
PRODUCT_URL = "https://www.apple.com/us/shop/buy-iphone/iphone-duo"
COLORS_BY_PART = {"MK1F4LL/A": "Night Sky", "MK1E4LL/A": "Star White"}
EMAIL_SETTINGS = WORK_FOLDER / "email_settings.ini"
EMAILS_PER_RECIPIENT = 5


def load_email_settings(path=None):
    config = ConfigParser(interpolation=None)
    try:
        with (path or EMAIL_SETTINGS).open(encoding="utf-8") as file:
            config.read_file(file)
    except FileNotFoundError:
        pass
    except (ConfigError, UnicodeError):
        raise ValueError("Invalid email_settings.ini. Check the [email] section and key = value lines.") from None

    defaults = {
        "smtp_host": "",
        "smtp_port": "465",
        "smtp_user": "",
        "smtp_password": "",
        "email_to": "",
    }
    return {
        key: os.environ.get(
            f"APPLE_PICKUP_{key.upper()}",
            config.get("email", key, fallback=default),
        )
        for key, default in defaults.items()
    }


def email_recipients(value):
    return [
        Address(addr_spec=address)
        for address in dict.fromkeys(item.strip() for item in value.split(","))
        if address
    ]


def save_email_settings(settings, path=None):
    config = ConfigParser(interpolation=None)
    config["email"] = settings
    destination = (path or EMAIL_SETTINGS).resolve()
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent, delete=False) as file:
        temporary_path = Path(file.name)
    try:
        with temporary_path.open("w", encoding="utf-8") as file:
            config.write(file)
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def send_stock_email(settings, zip_code, available_stores, product=PRODUCT, url=PRODUCT_URL):
    missing = [key for key in ("smtp_host", "smtp_user", "smtp_password") if not settings[key]]
    if missing:
        raise ValueError(f"Missing email settings: {', '.join(missing)}")

    host = settings["smtp_host"]
    sender = settings["smtp_user"]
    password = settings["smtp_password"]
    port = int(settings["smtp_port"])
    recipients = email_recipients(settings["email_to"])
    if not recipients:
        raise ValueError("Enter at least one address in email_to.")

    checked_at = datetime.now(ZoneInfo("America/Los_Angeles")).isoformat(
        sep=" ", timespec="milliseconds"
    )
    body = "\n".join([
        f"Apple reports {product} available for pickup near {zip_code}:",
        "",
        *available_stores,
        "",
        url,
    ])

    context = ssl.create_default_context()
    connection = (smtplib.SMTP(host, port, timeout=15) if port == 587
                  else smtplib.SMTP_SSL(host, port, timeout=15, context=context))
    with connection as smtp:
        if port == 587:
            smtp.starttls(context=context)
        smtp.login(sender, password)
        for recipient in recipients:
            for copy_number in range(1, EMAILS_PER_RECIPIENT + 1):
                message = EmailMessage()
                message["Subject"] = (
                    f"Alert {copy_number} of {EMAILS_PER_RECIPIENT} | "
                    f"{zip_code} | {product} in stock | {checked_at}"
                )
                message["From"] = sender
                message["To"] = recipient
                message["Message-ID"] = make_msgid()
                message.set_content(body)
                smtp.send_message(message)
    return len(recipients) * EMAILS_PER_RECIPIENT


def fetch_pickup(zip_code, timeout=8, *, parts=COLORS_BY_PART):
    params = {"pl": "true", "location": zip_code}
    params.update({f"parts.{index}": part for index, part in enumerate(parts)})
    query = urlencode(params)
    connection = http.client.HTTPSConnection("www.apple.com", timeout=timeout)
    try:
        connection.request(
            "GET",
            f"/shop/retail/pickup-message?{query}",
            headers={"Accept": "application/json", "Connection": "close"},
        )
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(f"HTTP {response.status} {response.reason}")
        stores = json.load(response)["body"]["stores"]
        if not isinstance(stores, list):
            raise ValueError("Expected a list of stores.")
        return stores
    finally:
        connection.close()


def pickup_statuses(stores, labels=COLORS_BY_PART):
    for store in stores:
        availability = store.get("partsAvailability", {})
        for part, color in labels.items():
            pickup = availability.get(part, {})
            status = pickup.get("pickupDisplay") or "unknown"
            quote = pickup.get("pickupSearchQuote")
            label = f"{store['storeName']} ({store['storeNumber']}) — {color}"
            yield store["storeNumber"], part, status, f"{label} — {quote}" if quote else label


def check_pickup(zip_code):
    try:
        colors = ", ".join(COLORS_BY_PART.values())
        print(f"GET Apple pickup availability for {zip_code}, {PRODUCT}, {colors}", flush=True)
        stores = fetch_pickup(zip_code)
        print("HTTP 200 OK")
        print(f"{len(stores)} stores returned")
        available_stores = []
        for _, _, status, label in pickup_statuses(stores):
            print(f"{label}: {status}")
            if status == "available":
                available_stores.append(label)
        return available_stores
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError) as error:
        print(f"{zip_code}: {type(error).__name__}: {error}")
        return None


def main():
    if not ZIP_CODES:
        raise SystemExit("Set APPLE_STOCK_ZIPS to the ZIP codes you want to check, separated by commas.")
    exit_status = 0
    for zip_code in ZIP_CODES:
        matches = check_pickup(zip_code)
        if matches is None:
            exit_status = 1
        if not matches:
            continue

        try:
            settings = load_email_settings()
            if settings["email_to"]:
                sent = send_stock_email(settings, zip_code, matches)
                print(f"{zip_code}: stock emails sent: {sent}.", flush=True)
        except (OSError, ValueError) as error:
            print(f"{zip_code}: email failed ({type(error).__name__}). Check the SMTP settings.")
            if isinstance(error, ValueError):
                print(error)
            exit_status = 1
    return exit_status


if __name__ == "__main__":
    raise SystemExit(main())

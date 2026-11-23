"""US Apple Store configurations supported by the dashboard."""

DUO_URL = "https://www.apple.com/shop/buy-iphone/iphone-duo"
STUDIO_URL = "https://www.apple.com/shop/buy-mac/mac-studio"
DUO_DEFAULTS = ("MK1E4LL/A", "MK1F4LL/A")

CATALOG = {}
for storage, white, black in (
    ("256 GB", "MK1E4LL/A", "MK1F4LL/A"),
    ("512 GB", "MK1G4LL/A", "MK1H4LL/A"),
    ("1 TB", "MK1J4LL/A", "MK1L4LL/A"),
    ("2 TB", "MK1Q4LL/A", "MK1R4LL/A"),
):
    for part, color in ((white, "Star White"), (black, "Night Sky")):
        CATALOG[part] = dict(model="iPhone Duo", color=color, storage=storage, memory="",
                             label=f"iPhone Duo · {color} · {storage}", url=DUO_URL)

for part, storage in (("MHL74LL/A", "1 TB"), ("Z1U50009V", "2 TB")):
    slug = storage.replace(" ", "").lower()
    CATALOG[part] = dict(
        model="Mac Studio", color="Silver", chip="M5 Ultra (30-core CPU)", memory="96 GB", storage=storage,
        label=f"Mac Studio · Silver · M5 Ultra (30-core CPU) · 96 GB · {storage}",
        url=f"{STUDIO_URL}/m5-ultra-chip-30-core-cpu-64-core-gpu-96gb-memory-{slug}-storage",
        pickup_note=("Apple may omit pickup status for this configure-to-order Mac."
                     if storage == "2 TB" else ""),
    )

CATALOG["MHL64LL/A"] = dict(
    model="Mac Studio", color="Silver", chip="M5 Max (18-core CPU)", memory="36 GB", storage="512 GB",
    label="Mac Studio · Silver · M5 Max (18-core CPU) · 36 GB · 512 GB",
    url=f"{STUDIO_URL}/m5-max-chip-18-core-cpu-32-core-gpu-36gb-memory-512gb-storage",
)


def pickup_groups(lists):
    """Combine overlaps so that each ZIP is requested once per round."""
    groups = {}
    for watch in lists:
        for zip_code in watch["zips"]:
            parts = groups.setdefault(zip_code, {})
            selected = watch.get("alert_stores", {}).get(zip_code)
            store_keys = (None,) if selected is None else selected
            for product in watch["products"]:
                recipients = product["recipients"]
                if recipients is None:
                    recipients = watch["recipients"]
                routes = parts.setdefault(product["part"], {})
                for store in store_keys:
                    routes.setdefault(store, set()).update(recipients)
    return groups

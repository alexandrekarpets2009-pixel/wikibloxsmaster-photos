"""
WikibloxsMaster! photo uploader
================================
Runs on your PC. It watches the game's photo queue, downloads each card's
Wikipedia photo (free-licence images only), uploads it to Roblox through
Open Cloud so Roblox moderates it, and saves the resulting image ID back to
the game. Cards then show the real photo.

Setup: see README.md in this folder.
Run:   python wiki_photo_uploader.py          (keeps running, Ctrl+C to stop)
       python wiki_photo_uploader.py --once   (process the queue once, then exit)
"""

import json
import os
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

API = "https://apis.roblox.com"
QUEUE_STORE = "WikiImageQueue"
IMAGES_STORE = "WikiImages"
WIKI_USER_AGENT = "WikibloxsMasterPhotoUploader/1.0 (Roblox game card images)"
ALLOWED_TYPES = {"image/png": "png", "image/jpeg": "jpg"}
MAX_BYTES = 20 * 1024 * 1024


def load_config():
    if not os.path.exists(CONFIG_PATH):
        sys.exit("config.json not found. Copy config.example.json to config.json and fill it in (see README.md).")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    key = os.environ.get("ROBLOX_API_KEY", "").strip()
    if not key:
        sys.exit("The ROBLOX_API_KEY environment variable is not set (see README.md).")
    cfg["api_key"] = key
    for field in ("universe_id", "creator_type", "creator_id"):
        if not cfg.get(field):
            sys.exit(f"config.json is missing '{field}'.")
    if cfg["creator_type"] not in ("user", "group"):
        sys.exit("creator_type must be 'user' or 'group'.")
    return cfg


# ---------------------------------------------------------------- HTTP helpers
class HttpError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


def request(method, url, headers=None, body=None, timeout=60):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # keep resp.headers as-is: its lookups ignore upper/lower case
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        raise HttpError(e.code, e.read().decode("utf-8", "replace")) from None


def roblox(cfg, method, path, payload=None, raw_body=None, content_type=None, retries=5):
    headers = {"x-api-key": cfg["api_key"]}
    body = raw_body
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if content_type:
        headers["Content-Type"] = content_type
    delay = 1
    for attempt in range(retries):
        try:
            status, _, data = request(method, API + path, headers, body)
            return json.loads(data) if data else {}
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            # slow or dropped connection: wait a bit and try again
            if attempt < retries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            raise HttpError(0, f"network: {e}") from None
        except HttpError as e:
            if e.status in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            # say which call failed, so permission problems are easy to pin down
            step = "UPLOAD (Assets API)" if "/assets/" in path else "DATA STORE (" + method + ")"
            raise HttpError(e.status, f"[{step}] {e.body}") from None


SCOPE = "global"  # the scope the game's DataStore:GetAsync/SetAsync use by default


def ds_path(cfg, store, entry_id=None, scoped=True):
    p = f"/cloud/v2/universes/{cfg['universe_id']}/data-stores/{urllib.parse.quote(store, safe='')}"
    if scoped:
        p += f"/scopes/{SCOPE}"
    p += "/entries"
    if entry_id is not None:
        p += "/" + urllib.parse.quote(entry_id, safe="")
    return p


def strip_scope(entry_id):
    """Listed IDs look like 'global/fr:123'; the game's key is 'fr:123'."""
    if entry_id.startswith(SCOPE + "/"):
        return entry_id[len(SCOPE) + 1:]
    return entry_id


def decode_value(v):
    """Values can come back as a JSON object or a JSON string depending on who wrote them."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


# ---------------------------------------------------------------- data stores
def list_queue(cfg, limit=50):
    data = roblox(cfg, "GET", ds_path(cfg, QUEUE_STORE) + f"?maxPageSize={limit}")
    ids = []
    for entry in data.get("dataStoreEntries", []):
        entry_id = entry.get("id") or entry.get("path", "").split("/entries/", 1)[-1]
        if entry_id and entry.get("state", "ACTIVE") != "DELETED":
            ids.append(strip_scope(urllib.parse.unquote(entry_id)))
    return ids


MISSING = object()


def get_entry(cfg, store, entry_id):
    """Returns the entry's value, or MISSING if there is no such entry."""
    try:
        data = roblox(cfg, "GET", ds_path(cfg, store, entry_id))
    except HttpError as e:
        if e.status == 404:
            return MISSING
        raise
    return decode_value(data.get("value"))


def set_entry(cfg, store, entry_id, value):
    roblox(cfg, "PATCH", ds_path(cfg, store, entry_id) + "?allowMissing=true", payload={"value": value})


def delete_entry(cfg, store, entry_id):
    try:
        roblox(cfg, "DELETE", ds_path(cfg, store, entry_id))
    except HttpError as e:
        if e.status != 404:
            raise


# ---------------------------------------------------------------- images
def download_photo(url):
    status, headers, data = request("GET", url, {"User-Agent": WIKI_USER_AGENT}, timeout=60)
    ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if not ctype:
        # fall back to the file's magic bytes
        if data[:3] == b"\xff\xd8\xff":
            ctype = "image/jpeg"
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            ctype = "image/png"
    return ctype, data


def upload_photo(cfg, title, lang, ctype, data, asset_type):
    creator = {"userId": str(cfg["creator_id"])} if cfg["creator_type"] == "user" else {"groupId": str(cfg["creator_id"])}
    meta = {
        "assetType": asset_type,
        "displayName": f"Wiki {title}"[:50],
        "description": f"Lead image of the Wikipedia article \"{title}\" ({lang}.wikipedia.org). Free licence, from Wikimedia Commons.",
        "creationContext": {"creator": creator},
    }
    boundary = uuid.uuid4().hex
    ext = ALLOWED_TYPES[ctype]
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="request"\r\n',
        b"Content-Type: application/json\r\n\r\n",
        json.dumps(meta).encode("utf-8"), b"\r\n",
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="fileContent"; filename="photo.{ext}"\r\n'.encode(),
        f"Content-Type: {ctype}\r\n\r\n".encode(),
        data, b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    op = roblox(cfg, "POST", "/assets/v1/assets", raw_body=body, content_type=f"multipart/form-data; boundary={boundary}")
    op_path = op.get("path") or ("operations/" + str(op.get("operationId", "")))
    # Wait for Roblox to finish processing the upload
    for _ in range(40):
        if op.get("done"):
            break
        time.sleep(3)
        op = roblox(cfg, "GET", "/assets/v1/" + op_path)
    if not op.get("done"):
        return None, "timeout"
    if op.get("error"):
        return None, "error: " + json.dumps(op["error"])[:200]
    resp = op.get("response", {})
    state = (resp.get("moderationResult") or {}).get("moderationState", "")
    return resp.get("assetId"), state


class Uploader:
    def __init__(self, cfg):
        self.cfg = cfg
        # Try Roblox's "Image" asset type first; fall back to "Decal" if it's refused.
        self.asset_type = cfg.get("asset_type") or "Image"

    def process(self, entry_id):
        job = get_entry(self.cfg, QUEUE_STORE, entry_id)
        if job is MISSING:
            return "not found (already handled?)"
        if not isinstance(job, dict) or not job.get("u"):
            # Don't delete: an unexpected format means this script needs fixing, not that the job is bad
            return f"unreadable job, left in queue: {str(job)[:150]}"

        existing = get_entry(self.cfg, IMAGES_STORE, entry_id)
        if isinstance(existing, dict) and (existing.get("a") or existing.get("x")):
            delete_entry(self.cfg, QUEUE_STORE, entry_id)
            return "already done"

        title, lang = job.get("t", entry_id), job.get("l", "en")
        try:
            ctype, data = download_photo(job["u"])
        except Exception as e:  # noqa: BLE001 - keep the queue moving
            set_entry(self.cfg, IMAGES_STORE, entry_id, {"x": "download"})
            delete_entry(self.cfg, QUEUE_STORE, entry_id)
            return f"download failed ({e})"
        if ctype not in ALLOWED_TYPES or len(data) > MAX_BYTES:
            set_entry(self.cfg, IMAGES_STORE, entry_id, {"x": "format"})
            delete_entry(self.cfg, QUEUE_STORE, entry_id)
            return f"skipped (format {ctype})"

        try:
            asset_id, state = upload_photo(self.cfg, title, lang, ctype, data, self.asset_type)
        except HttpError as e:
            if e.status == 400 and self.asset_type == "Image":
                print("  'Image' asset type was refused, switching to 'Decal' for the rest of this run.")
                self.asset_type = "Decal"
                asset_id, state = upload_photo(self.cfg, title, lang, ctype, data, self.asset_type)
            else:
                raise

        if not asset_id:
            set_entry(self.cfg, IMAGES_STORE, entry_id, {"x": "upload"})
            delete_entry(self.cfg, QUEUE_STORE, entry_id)
            return f"upload failed ({state})"
        if "REJECTED" in state.upper():
            set_entry(self.cfg, IMAGES_STORE, entry_id, {"x": "rejected"})
            delete_entry(self.cfg, QUEUE_STORE, entry_id)
            return "REJECTED by Roblox moderation"

        set_entry(self.cfg, IMAGES_STORE, entry_id, {"a": int(asset_id), "t": self.asset_type})
        delete_entry(self.cfg, QUEUE_STORE, entry_id)
        return f"uploaded as {self.asset_type} {asset_id} ({state or 'state not reported'})"


def main():
    cfg = load_config()
    once = "--once" in sys.argv
    pause = float(cfg.get("seconds_between_uploads", 3))
    uploader = Uploader(cfg)
    print(f"Watching the photo queue for universe {cfg['universe_id']} (Ctrl+C to stop)...")
    while True:
        try:
            ids = list_queue(cfg)
        except HttpError as e:
            print(f"Couldn't read the queue: {e}")
            if e.status in (401, 403):
                sys.exit("Check the API key's permissions (see README.md).")
            ids = []
        for entry_id in ids:
            try:
                result = uploader.process(entry_id)
            except HttpError as e:
                result = f"error, will retry later ({e})"
                if e.status in (401, 403):
                    sys.exit(f"{entry_id}: {result}\nCheck the API key's permissions (see README.md).")
            except Exception as e:  # noqa: BLE001
                result = f"error, will retry later ({e})"
            print(f"{entry_id}: {result}")
            time.sleep(pause)
        if once:
            break
        if not ids:
            time.sleep(20)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")

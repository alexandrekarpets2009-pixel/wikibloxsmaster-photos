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


# ---------------------------------------------------------------- safety
# Roblox moderation is secret, so nothing can guarantee an image passes. These checks keep the risk
# very low and FAIL CLOSED: when something can't be verified, the photo is not uploaded.
#
# 1. Only "safe" subjects get photos (decided by the game from Wikidata, sent as job["k"]).
SAFE_SUBJECTS = {
    "settlement", "place", "water", "mountain", "island", "church", "school", "station", "airport",
    "stadium", "bridge", "castle", "museum", "road", "building", "species", "ship", "vehicle", "space",
}
# 2. Words that block an image when found in its file name, Wikimedia categories or description.
BLOCK_WORDS = [
    # nudity / sexual
    "nude", "nudity", "naked", "topless", "bare breast", "breast", "nipple", "genital", "penis", "vagina", "vulva", "buttock",
    " sex", "sexual", "sexy", "erotic", "porn", "lingerie", "underwear", "bikini", "swimsuit", "striptease", "prostitut",
    "fetish", "bdsm", " nu ", " nue ", " nus ", " nues ", "nudité", "érotique", "desnud", "nackt", "akt (", " nudo", "naakt",
    "голы", "обнаж", "ヌード", "裸",
    # art that often shows nudity (statues, sculptures, paintings)
    "statue", "sculpt", "venus", "aphrodite", "painting", "fresco", "artwork", "bather", " bath ", "classical antiquity",
    "skulptur", "statua", "estatua", "beeld", "peinture", "gemälde", "pintura", "dipinto",
    # death / gore / medical
    "corpse", "dead body", "dead bodies", "cadaver", "skeleton", "skull", "death", "killed", "murder", "execution", "hanged",
    "hanging", "lynch", "beheading", " gore", "blood", "wound", "injur", "surgery", "autopsy", "medical", "disease", "tumor",
    "tumour", "lesion", "symptom", "infection", "pathology", "anatomy", "dissection", "mummy", "mummies", " grave", "cemetery",
    "cadavre", " mort ", "leiche", "cadáver", "cadavere", "труп",
    # violence / war / weapons / crime
    " war ", " wars ", "warfare", "battle", "massacre", "genocide", "holocaust", "concentration camp", "atrocit", "terror",
    " bomb ", "bombing", "bomber", "explosion", "weapon", "firearm", "rifle", "pistol", "handgun", " gun ", " guns",
    "ammunition", "grenade", "missile", "military", "soldier", " army", "torture", "prison", " riot", "crime", "police brutality",
    # extremism / hate symbols
    "nazi", "swastika", "hakenkreuz", "hitler", "third reich", " ss ", "waffen", "fascis", "ku klux", "kkk", "isis",
    "confederate flag", "extremis", "propaganda",
    # drugs / alcohol / gambling
    "drug", "cannabis", "marijuana", "cocaine", "heroin", "opium", "narcotic", "alcohol", "beer", "wine", "vodka",
    "whisky", "liquor", "cigarette", "tobacco", "smoking", "casino", "gambling",
]
# 3. NudeNet (free nudity detector) classes that block an image.
BLOCK_NUDENET = {
    "FEMALE_BREAST_EXPOSED", "FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "BUTTOCKS_EXPOSED", "ANUS_EXPOSED",
    "FEMALE_BREAST_COVERED", "FEMALE_GENITALIA_COVERED", "BUTTOCKS_COVERED", "MALE_BREAST_EXPOSED", "BELLY_EXPOSED",
}
NUDENET_MIN_SCORE = 0.30


class SafetyUnavailable(Exception):
    """A check couldn't run (network, missing detector): keep the job and try again later."""


def wikimedia_file(url):
    """'https://upload.wikimedia.org/wikipedia/commons/thumb/f/f4/X.jpg/500px-X.jpg' -> ('commons.wikimedia.org', 'X.jpg')"""
    path = urllib.parse.urlparse(url).path
    parts = path.split("/wikipedia/", 1)
    if len(parts) != 2:
        return None, None
    segs = parts[1].split("/")
    project = segs[0]
    if len(segs) > 1 and segs[1] == "thumb":
        name = segs[4] if len(segs) > 4 else None
    else:
        name = segs[3] if len(segs) > 3 else None
    if not name:
        return None, None
    host = "commons.wikimedia.org" if project == "commons" else f"{project}.wikipedia.org"
    return host, urllib.parse.unquote(name)


def blocked_word(text):
    low = " " + text.lower().replace("_", " ") + " "
    for w in BLOCK_WORDS:
        if w in low:
            return w.strip()
    return None


def safety_precheck(job):
    """Returns a reason string when the photo must NOT be uploaded, None when it's fine."""
    subject = job.get("k")
    if subject not in SAFE_SUBJECTS:
        return f"subject '{subject}' not in the safe list"
    reason = blocked_word(f"{job.get('t', '')}")
    if reason:
        return f"title contains '{reason}'"
    host, name = wikimedia_file(job.get("u", ""))
    if not host:
        return "unknown image source"
    api = (f"https://{host}/w/api.php?action=query&format=json&formatversion=2&prop=categories%7Cimageinfo"
           f"&cllimit=max&iiprop=extmetadata&iiextmetadatafilter=ImageDescription%7CCategories%7CObjectName"
           f"&titles={urllib.parse.quote('File:' + name)}")
    try:
        _, _, raw = request("GET", api, {"User-Agent": WIKI_USER_AGENT}, timeout=30)
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        raise SafetyUnavailable(f"Wikimedia check failed: {e}") from None
    pages = (data.get("query") or {}).get("pages") or []
    if not pages:
        return "image not found on Wikimedia"
    page = pages[0]
    texts = [name]
    for c in page.get("categories") or []:
        texts.append(c.get("title", ""))
    for info in page.get("imageinfo") or []:
        for v in (info.get("extmetadata") or {}).values():
            if isinstance(v, dict) and isinstance(v.get("value"), str):
                texts.append(v["value"])
    reason = blocked_word(" | ".join(texts))
    if reason:
        return f"image description/categories contain '{reason}'"
    return None


_detector = None


def nudity_check(data):
    """Runs NudeNet on the image. Returns a reason when it must not be uploaded."""
    global _detector
    try:
        if _detector is None:
            from nudenet import NudeDetector  # installed by the GitHub workflow (pip install nudenet)
            _detector = NudeDetector()
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as f:
            f.write(data)
            path = f.name
        try:
            found = _detector.detect(path)
        finally:
            os.remove(path)
    except Exception as e:  # noqa: BLE001
        raise SafetyUnavailable(f"nudity detector unavailable: {e}") from None
    for d in found or []:
        if d.get("class") in BLOCK_NUDENET and float(d.get("score", 0)) >= NUDENET_MIN_SCORE:
            return f"nudity detector found {d.get('class')} ({float(d.get('score', 0)):.2f})"
    return None


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

        def skip_unsafe(reason):
            set_entry(self.cfg, IMAGES_STORE, entry_id, {"x": "unsafe"})
            delete_entry(self.cfg, QUEUE_STORE, entry_id)
            return f"SKIPPED for safety: {reason}"

        try:
            reason = safety_precheck(job)
        except SafetyUnavailable as e:
            return f"left in queue ({e})"
        if reason:
            return skip_unsafe(reason)

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
            reason = nudity_check(data)
        except SafetyUnavailable as e:
            return f"left in queue ({e})"
        if reason:
            return skip_unsafe(reason)

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
    # --once (GitHub Actions): keep going until the queue is empty, but stop after ~7.5 minutes
    # so the run finishes inside its 9-minute limit; the next run continues where this one stopped.
    started = time.time()
    budget = float(cfg.get("once_time_budget_seconds", 450))
    retried = set()
    print(f"Watching the photo queue for universe {cfg['universe_id']} (Ctrl+C to stop)...")
    while True:
        try:
            ids = list_queue(cfg)
        except HttpError as e:
            print(f"Couldn't read the queue: {e}")
            if e.status in (401, 403):
                sys.exit("Check the API key's permissions (see README.md).")
            ids = []
        if once:
            # don't spin on entries that already failed during this run
            ids = [i for i in ids if i not in retried]
        for entry_id in ids:
            if once and time.time() - started > budget:
                print("Time budget reached, the next run will continue.")
                return
            retried.add(entry_id)
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
            if not ids:
                print("Queue empty, done.")
                break
            continue
        if not ids:
            time.sleep(20)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")

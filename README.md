# WikibloxsMaster! photo uploader

Puts real Wikipedia photos on cards. When a player pulls a card, the game adds its photo to a queue. This program uploads queued photos to Roblox, Roblox moderates them, and the card shows the photo from then on, for everyone. Until a card's photo is ready, it keeps its emoji art.

## 1. Install Python

Download Python 3 from python.org and tick **"Add python.exe to PATH"** during setup. No extra packages are needed.

## 2. Create a Roblox API key

On create.roblox.com: **Dashboard → Open Cloud → API Keys → Create API Key**.

Add these permissions:

| API system | Experience / target | Operations |
|---|---|---|
| **Assets** | (account-wide) | Read, Write |
| **Data stores** (`universe-datastores`) | WikibloxsMaster! | List entries, Read entry, Create entry, Update entry, Delete entry |

- Leave **IP restrictions** empty, or add your own IP address.
- Copy the key when it's shown. Roblox only shows it once.

## 3. Save the key on your PC

Open a new terminal and run this, putting your key between the quotes:

```
setx ROBLOX_API_KEY "paste-your-key-here"
```

Close the terminal and open a new one so it picks up the key. Never put the key in a file or share it.

## 4. Make your config

Copy `config.example.json` to `config.json`. The values are already filled in for your game:

- `universe_id`: WikibloxsMaster!'s universe ID.
- `creator_type` / `creator_id`: who owns the uploaded photos. This is your user account by default (see "Account safety" below).
- `asset_type`: `"Image"` (tried first). If Roblox refuses it, the uploader switches to `"Decal"` automatically.
- `seconds_between_uploads`: how fast to upload. Raise it if you see rate-limit errors.

## 5. Run it

Open a terminal in this folder and run:

```
python wiki_photo_uploader.py
```

It checks the queue every 20 seconds and prints one line per photo. Press Ctrl+C to stop. To process the queue once and exit, run `python wiki_photo_uploader.py --once`.

In the game, approved photos appear on cards within about 30 seconds without rejoining.

## Account safety

Every photo is uploaded under the creator in `config.json`. If Roblox moderation rejects one (for example, a medical or art photo), that counts against that account. Mitigations already in place:

- Only free-licence Wikipedia lead images are used. No fair-use images.
- Articles whose text fails Roblox's text filter never become cards, so their photos are never uploaded.
- Anatomy and biology articles never get photos.

**Safer:** upload under a Roblox group made just for this game. Set `"creator_type": "group"` and the group ID. Caveat: if the uploads come back as **Decals** (the console says "switching to 'Decal'"), the game can only turn them into images when the photos and the game have the same owner. In that case either keep uploading as your user, or move the game into the same group.

## How it fits together

- `WikiImageQueue` (data store): cards waiting for a photo. The game writes to it; the uploader reads and deletes.
- `WikiImages` (data store): results, `{ "a": assetId, "t": "Image" }` or `{ "x": "rejected" }`. The uploader writes; the game reads.
- `ServerScriptService.WikiServer` attaches known photos when a pack is prepared, queues missing ones, and checks pending ones every 30 seconds.

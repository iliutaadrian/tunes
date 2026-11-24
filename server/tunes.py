#!/usr/bin/env python3
"""Tunes: listen to recommended tracks before they reach the music library.

A mood is a set of seed tracks: a library playlist, pasted YouTube links, and whatever you kept
in that mood before. Every YouTube video has an auto-generated Mix (`RD<id>`), YouTube's own radio
for that track. A round lists the Mix of each seed with yt-dlp and ranks what the seeds agree on.
You play the queue in the browser, then Keep (download into a playlist folder) or Skip (never again).

Stdlib only (http.server + sqlite3). Config: tunes.json next to this file, or the path in TUNES_CONFIG.

  data/state.db     moods, seeds, cached mixes, the queues, verdicts and downloads

  GET    /api/moods                         moods with queue size and round progress
  POST   /api/moods {name, playlist, folder, seeds}
  DELETE /api/moods/<id>
  GET    /api/moods/<id>                    one mood with its pasted seeds
  POST   /api/moods/<id>/seeds {seeds}      add pasted links or ids, one per line
  DELETE /api/moods/<id>/seeds/<vid>
  POST   /api/moods/<id>/generate           start a round in the background
  GET    /api/queue?mood=<id>               tracks waiting for a verdict, best first
  POST   /api/verdict {vid, verdict: keep|skip|restore, mood, folder}
  GET    /api/history?kind=kept|skipped&offset=
  GET    /api/library?playlist=             your own playlists and the tracks of one (* for all of them)
  GET    /art/<item id>?token=              the cover embedded in a library track
  GET    /media/<item id>?token=            the audio file of a library track (Range supported)
  GET    /api/status                        folders, downloads, library sync
  POST   /api/sync                          run the library import command (needs import_cmd)
  GET    /health                            liveness, no token

  tunes.py --import DIR                     one-off: load the old recommender's data folder

Every /api route needs the app token (Authorization: Bearer <token>). /media takes it as ?token=
too, because an <audio> element cannot send a header.
"""
import contextlib
from concurrent.futures import ThreadPoolExecutor, as_completed
import hmac
import json
import math
import os
import queue
import random
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import Counter, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".json": "application/manifest+json",
        ".png": "image/png", ".svg": "image/svg+xml"}
AUDIO = {".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".opus": "audio/ogg", ".ogg": "audio/ogg",
         ".flac": "audio/flac", ".wav": "audio/wav"}
PAGE = 60
VID = r"[A-Za-z0-9_-]{11}"
VID_RE = re.compile(VID)
URL_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/embed/|/shorts/)(" + VID + r")")
MIN_SECONDS = 60          # shorter is a teaser or a Short
BREADTH_FROM = 12         # the "too generic" filter needs this many seeds to mean anything
EMPTY_MIX_RETRY = 7 * 86400

SCHEMA = """
CREATE TABLE IF NOT EXISTS moods (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  playlist TEXT,                  -- library playlist folder whose tracks seed this mood
  folder TEXT NOT NULL,           -- where Keep downloads by default
  max_minutes REAL NOT NULL DEFAULT 12,
  added_at INTEGER NOT NULL, generated_at INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS mood_seeds (
  mood_id INTEGER NOT NULL REFERENCES moods(id) ON DELETE CASCADE,
  vid TEXT NOT NULL, title TEXT, PRIMARY KEY (mood_id, vid)
);
CREATE TABLE IF NOT EXISTS tracks (  -- every video ever offered or ruled on
  vid TEXT PRIMARY KEY, title TEXT, uploader TEXT, duration REAL,
  verdict TEXT,                   -- NULL (waiting), kept, skipped, owned
  mood_id INTEGER, folder TEXT, decided_at INTEGER
);
CREATE TABLE IF NOT EXISTS candidates (
  mood_id INTEGER NOT NULL REFERENCES moods(id) ON DELETE CASCADE,
  vid TEXT NOT NULL, score REAL NOT NULL, seed_count INTEGER NOT NULL, why TEXT NOT NULL,
  PRIMARY KEY (mood_id, vid)
);
CREATE TABLE IF NOT EXISTS mixes (
  seed TEXT PRIMARY KEY, entries TEXT NOT NULL, fetched_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS downloads (
  vid TEXT PRIMARY KEY, folder TEXT NOT NULL, status TEXT NOT NULL,  -- queued, running, done, failed
  error TEXT, path TEXT, queued_at INTEGER NOT NULL, finished_at INTEGER, synced INTEGER NOT NULL DEFAULT 0
);
"""


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def parse_seeds(text):
    """Pasted seeds, one per line: a YouTube link or a bare video id, then an optional tab and title."""
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        ident, _, title = line.partition("\t")
        m = URL_RE.search(ident)
        vid = m.group(1) if m else ident.strip()
        if VID_RE.fullmatch(vid):
            out.setdefault(vid, title.strip() or None)
    return [{"id": vid, "title": title} for vid, title in out.items()]


def same_song_key(title):
    """A title without its brackets and punctuation, so two uploads of one song compare equal."""
    return re.sub(r"[^a-z0-9]+", " ", re.sub(r"[(\[{].*?[)\]}]", " ", (title or "").lower())).strip()


def rank_candidates(seeds, mixes, owned=(), bad_channels=(), max_seconds=720, top=50,
                    max_breadth=0.25, min_breadth=2, reject_penalty=0.4):
    """Rank what the seeds' mixes agree on. `mixes` is {seed id: [{id, title, uploader, duration}]}.

    Raw co-occurrence just finds whatever is famous, so:
    - a candidate scores by its mean position across the mixes that contain it, not the sum;
    - agreement between seeds still counts, but damped: sqrt(n - 1), not linear;
    - one seed alone proves nothing: at least `min_breadth` seeds must agree;
    - a video in more than `max_breadth` of the mixes is YouTube filler, not a match;
    - a channel you have only ever skipped is demoted by `reject_penalty`;
    - another upload of a seed's own song (lyrics video, reupload) is not a discovery.
    """
    owned, bad_channels = set(owned), set(bad_channels)
    seed_songs = {same_song_key(s.get("title")) for s in seeds} - {""}
    scores, meta, who = defaultdict(float), {}, defaultdict(set)
    for s in seeds:
        for pos, v in enumerate(mixes.get(s["id"]) or []):
            if v["id"] in owned or v["id"] == s["id"] or same_song_key(v.get("title")) in seed_songs:
                continue
            if v.get("duration") and not MIN_SECONDS <= v["duration"] <= max_seconds:
                continue
            scores[v["id"]] += 1.0 / (1 + pos * 0.15)  # earlier in the mix is a closer match
            meta.setdefault(v["id"], v)
            who[v["id"]].add(s["id"])
    n_seeds = len(seeds)
    need = min(min_breadth, max(n_seeds, 1))
    titles = {s["id"]: s.get("title") or s["id"] for s in seeds}
    out = []
    for vid, total in scores.items():
        n = len(who[vid])
        if n < need or (n_seeds >= BREADTH_FROM and n / n_seeds > max_breadth):
            continue
        demoted = meta[vid].get("uploader") in bad_channels
        score = total / n * (1 + 0.25 * math.sqrt(n - 1)) * (reject_penalty if demoted else 1)
        out.append({"vid": vid, "title": meta[vid].get("title") or vid, "uploader": meta[vid].get("uploader"),
                    "duration": meta[vid].get("duration") or None, "score": round(score, 4), "seed_count": n,
                    "why": sorted(titles[s] for s in who[vid])[:4]})
    out.sort(key=lambda c: (-c["score"], c["vid"]))
    return out[:top]


def ytdlp_mix(cmd, vid):
    """List a video's YouTube Mix: [{id, title, uploader, duration}], in YouTube's order."""
    url = f"https://www.youtube.com/watch?v={vid}&list=RD{vid}"
    out = subprocess.run(list(cmd) + ["--flat-playlist", "--no-warnings", "--print",
                                      "%(id)s\t%(title)s\t%(uploader)s\t%(duration)s", "--", url],
                         capture_output=True, text=True, timeout=120)
    entries = []
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not VID_RE.fullmatch(parts[0]):
            continue
        try:
            seconds = int(float(parts[3]))
        except (ValueError, IndexError):
            seconds = 0
        entries.append({"id": parts[0], "title": parts[1], "uploader": parts[2], "duration": seconds})
    if out.returncode != 0 and not entries:
        raise RuntimeError((out.stderr or "yt-dlp failed").strip()[-300:])
    return entries


def ytdlp_audio(cmd, vid, dest):
    """Download one track the way the music pipeline expects it: m4a audio, metadata and cover
    embedded, named `Title [id].m4a`. Returns the final file path."""
    os.makedirs(dest, exist_ok=True)
    out = subprocess.run(list(cmd) + [
        "--no-playlist", "--no-progress", "--no-warnings", "--no-force-overwrites",
        "--extract-audio", "--audio-format", "best", "--format-sort", "+acodec:m4a", "--format", "bestaudio/best",
        "--embed-metadata", "--embed-thumbnail", "--convert-thumbnail", "jpg", "--windows-filenames",
        "--output", os.path.join(dest, "%(track,title)S [%(id)s].%(ext)s"),
        "--print", "after_move:filepath", "--", f"https://www.youtube.com/watch?v={vid}"],
        capture_output=True, text=True, timeout=900)
    lines = [line for line in out.stdout.splitlines() if line.strip()]
    if out.returncode != 0 or not lines:
        raise RuntimeError((out.stderr or "yt-dlp failed").strip()[-300:])
    return lines[-1]


def mp4_cover(path):
    """The cover picture embedded in an m4a file (moov > udta > meta > ilst > covr > data), or None."""
    with open(path, "rb") as f:
        end = os.path.getsize(path)
        for name in (b"moov", b"udta", b"meta", b"ilst", b"covr", b"data"):
            while True:  # walk the sibling atoms up to `end` until the one wanted
                start = f.tell()
                head = f.read(8) if start + 8 <= end else b""
                if len(head) < 8:
                    return None
                size, kind = struct.unpack(">I4s", head)
                if size == 1:
                    size = struct.unpack(">Q", f.read(8))[0]
                elif size == 0:
                    size = end - start
                if size < 8 or start + size > end:
                    return None
                if kind == name:
                    end = start + size
                    break
                f.seek(start + size)
            if name == b"meta":
                f.seek(4, os.SEEK_CUR)  # version and flags
        f.seek(8, os.SEEK_CUR)          # data type and locale
        return f.read(end - f.tell()) or None


def read_only(path):
    con = sqlite3.connect("file:" + urllib.parse.quote(path) + "?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    return con


# What the web app remembers between visits. Kept in data/settings.json so phone and laptop agree.
SETTINGS = {"mood": str, "playlist": str, "shuffle": bool}


class App:
    def __init__(self, cfg, fetch_mix=None, download=None):
        self.cfg = cfg
        self.state_path = os.path.join(cfg["data_dir"], "state.db")
        self.settings_path = os.path.join(cfg["data_dir"], "settings.json")
        self.settings_lock = threading.Lock()
        self.download_retry_waits = (10, 60)
        ytdlp = cfg.get("ytdlp") or ["yt-dlp"]
        self.ytdlp = [ytdlp] if isinstance(ytdlp, str) else ytdlp
        self.fetch_mix = fetch_mix or (lambda vid: ytdlp_mix(self.ytdlp, vid))
        self.music_dir = cfg.get("music_dir")
        self.folders = cfg.get("folders") or ["misc"]
        # yt-dlp may run inside a container (docker exec) that mounts the music folder elsewhere.
        self.ytdlp_music_dir = cfg.get("ytdlp_music_dir") or self.music_dir
        self.download = download or self.ytdlp_download
        # Where the library's files are on this machine, and the folder beets believes they are in.
        self.library_dir = cfg.get("library_dir")
        self.beets_dir = cfg.get("beets_dir") or "/music"
        self.download_wake = threading.Event()
        self.rounds = queue.Queue()
        self.progress = {}       # mood id -> {"done", "total"} while a round is queued or running
        self.progress_lock = threading.Lock()
        self.sync_state = {"running": False, "ok": None, "finished_at": None, "tail": ""}
        with self.connect() as con:
            con.executescript(SCHEMA)

    @contextlib.contextmanager
    def connect(self):
        con = sqlite3.connect("file:" + urllib.parse.quote(self.state_path), uri=True, timeout=30)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("PRAGMA foreign_keys = ON")
            with con:
                yield con
        finally:
            con.close()

    # ---- the music library (read only) ----

    def library(self):
        """(every video id already in the library, {playlist folder: [seed]}) from pinchflat and beets."""
        owned, by_playlist = set(), defaultdict(dict)
        path = self.cfg.get("pinchflat_db")
        if path and os.path.exists(path):
            with contextlib.closing(read_only(path)) as con:
                for r in con.execute("SELECT media_id, title, media_filepath FROM media_items WHERE media_id IS NOT NULL"):
                    owned.add(r["media_id"])
                    parts = (r["media_filepath"] or "").split("/")
                    if len(parts) > 2:  # only downloaded tracks make sensible seeds
                        for folder in parts[1:-1]:
                            by_playlist[folder].setdefault(r["media_id"], r["title"])
        path = self.cfg.get("beets_db")
        if path and os.path.exists(path):
            with contextlib.closing(read_only(path)) as con:
                for r in con.execute(
                        "SELECT i.artist, i.title, i.comments, a.value AS playlist FROM items i "
                        "LEFT JOIN item_attributes a ON a.entity_id = i.id AND a.key = 'playlist'"):
                    m = URL_RE.search(r["comments"] or "")
                    if m:
                        owned.add(m.group(1))
                        if r["playlist"]:
                            by_playlist[r["playlist"]].setdefault(m.group(1), f"{r['artist']} - {r['title']}")
        return owned, {k: [{"id": vid, "title": t} for vid, t in v.items()] for k, v in by_playlist.items()}

    def track_file(self, raw):
        """The file of a beets item on this machine, or None when it is not here."""
        if not self.library_dir or not raw:
            return None
        rel = os.fsdecode(raw)
        if os.path.isabs(rel):
            rel = os.path.relpath(rel, self.beets_dir)
        root = os.path.realpath(self.library_dir)
        full = os.path.realpath(os.path.join(root, rel))
        return full if full.startswith(root + os.sep) and os.path.isfile(full) else None

    def library_tracks(self, q):
        """Your own playlists, and the tracks of the chosen one (the first when none is chosen), newest first."""
        path = self.cfg.get("beets_db")
        if not path or not os.path.exists(path):
            return {"playlists": [], "playlist": None, "tracks": []}
        join = "FROM items i JOIN item_attributes a ON a.entity_id = i.id AND a.key = 'playlist' "
        with contextlib.closing(read_only(path)) as con:
            playlists = [{"name": r["name"], "tracks": r["n"]} for r in con.execute(
                "SELECT a.value AS name, COUNT(*) AS n " + join + "GROUP BY a.value ORDER BY a.value COLLATE NOCASE")]
            names = [p["name"] for p in playlists]
            chosen = q.get("playlist") if q.get("playlist") in names + ["*"] else (names[0] if names else None)
            rows = con.execute(
                "SELECT i.id, i.artist, i.title, i.length, i.path " + join +
                "WHERE a.value = ? OR ? = '*' ORDER BY i.added DESC, i.id DESC",
                (chosen, chosen)).fetchall()
        return {"playlists": playlists, "playlist": chosen,
                "tracks": [{"id": r["id"], "artist": r["artist"], "title": r["title"], "length": r["length"],
                            "playable": bool(self.track_file(r["path"]))} for r in rows]}

    def media_file(self, item_id):
        path = self.cfg.get("beets_db")
        if not item_id.isdigit() or not path or not os.path.exists(path):
            return None
        with contextlib.closing(read_only(path)) as con:
            row = con.execute("SELECT path FROM items WHERE id = ?", (int(item_id),)).fetchone()
        return self.track_file(row["path"]) if row else None

    def cover(self, item_id):
        """(bytes, content type) of a library track's embedded cover, or None."""
        full = self.media_file(item_id)
        try:
            data = mp4_cover(full) if full else None
        except (OSError, struct.error):
            data = None
        return (data, "image/png" if data.startswith(b"\x89PNG") else "image/jpeg") if data else None

    # ---- moods ----

    def moods(self):
        with self.progress_lock:
            progress = dict(self.progress)
        with self.connect() as con:
            rows = con.execute(
                "SELECT m.*, (SELECT COUNT(*) FROM mood_seeds s WHERE s.mood_id = m.id) AS seeds, "
                "(SELECT COUNT(*) FROM candidates c LEFT JOIN tracks t ON t.vid = c.vid "
                " WHERE c.mood_id = m.id AND t.verdict IS NULL) AS waiting, "
                "(SELECT COUNT(*) FROM tracks t WHERE t.mood_id = m.id AND t.verdict = 'kept') AS kept "
                "FROM moods m ORDER BY m.name COLLATE NOCASE").fetchall()
        return {"moods": [{**dict(r), "round": progress.get(r["id"])} for r in rows]}

    def mood(self, mood_id):
        with self.connect() as con:
            row = con.execute("SELECT * FROM moods WHERE id = ?", (mood_id,)).fetchone()
            if not row:
                raise HttpError(404, "Unknown mood")
            seeds = [dict(r) for r in con.execute(
                "SELECT vid, title FROM mood_seeds WHERE mood_id = ? ORDER BY rowid", (mood_id,))]
        return {**dict(row), "seeds": seeds}

    def add_mood(self, body):
        name = " ".join(body.get("name").split()) if isinstance(body.get("name"), str) else ""
        if not 2 <= len(name) <= 40:
            raise HttpError(400, "Name the mood in 2 to 40 characters")
        playlist = body.get("playlist") or None
        if playlist is not None and playlist not in self.folders:
            raise HttpError(400, "Unknown playlist")
        folder = body.get("folder") or playlist or self.folders[0]
        if folder not in self.folders:
            raise HttpError(400, "Unknown folder")
        seeds = parse_seeds(body.get("seeds"))
        if not playlist and not seeds:
            raise HttpError(400, "Give the mood a playlist or at least one YouTube link to start from")
        with self.connect() as con:
            if con.execute("SELECT 1 FROM moods WHERE name = ?", (name,)).fetchone():
                raise HttpError(400, "You already have that mood")
            mood_id = con.execute("INSERT INTO moods (name, playlist, folder, added_at) VALUES (?,?,?,?)",
                                  (name, playlist, folder, int(time.time()))).lastrowid
            con.executemany("INSERT OR IGNORE INTO mood_seeds VALUES (?,?,?)",
                            [(mood_id, s["id"], s["title"]) for s in seeds])
        return {"id": mood_id, "name": name}

    def remove_mood(self, mood_id):
        with self.connect() as con:
            if con.execute("DELETE FROM moods WHERE id = ?", (mood_id,)).rowcount == 0:
                raise HttpError(404, "Unknown mood")
        return {"ok": True}

    def add_seeds(self, mood_id, body):
        seeds = parse_seeds(body.get("seeds"))
        if not seeds:
            raise HttpError(400, "Paste YouTube links or video ids, one per line")
        with self.connect() as con:
            if not con.execute("SELECT 1 FROM moods WHERE id = ?", (mood_id,)).fetchone():
                raise HttpError(404, "Unknown mood")
            con.executemany("INSERT OR IGNORE INTO mood_seeds VALUES (?,?,?)",
                            [(mood_id, s["id"], s["title"]) for s in seeds])
        return self.mood(mood_id)

    def remove_seed(self, mood_id, vid):
        with self.connect() as con:
            con.execute("DELETE FROM mood_seeds WHERE mood_id = ? AND vid = ?", (mood_id, vid))
        return self.mood(mood_id)

    # ---- rounds ----

    def seeds_for(self, mood, library_seeds):
        """Pasted seeds, then what you kept in this mood, then the playlist in random order."""
        with self.connect() as con:
            pasted = [{"id": r["vid"], "title": r["title"]} for r in con.execute(
                "SELECT vid, title FROM mood_seeds WHERE mood_id = ? ORDER BY rowid", (mood["id"],))]
            kept = [{"id": r["vid"], "title": r["title"]} for r in con.execute(
                "SELECT vid, title FROM tracks WHERE verdict = 'kept' AND mood_id = ? ORDER BY decided_at DESC",
                (mood["id"],))]
        library_seeds = list(library_seeds)
        random.shuffle(library_seeds)  # a new round listens to a different corner of the playlist
        for group in (pasted, kept):
            random.shuffle(group)
        uniq = {}
        for s in pasted + kept + library_seeds:
            uniq.setdefault(s["id"], s)
        return list(uniq.values())[:int(self.cfg.get("seeds_per_round", 40))]

    def mix(self, vid):
        """A seed's Mix from the cache, or from YouTube. An empty answer is retried after a week."""
        with self.connect() as con:
            row = con.execute("SELECT entries, fetched_at FROM mixes WHERE seed = ?", (vid,)).fetchone()
        if row:
            entries = json.loads(row["entries"])
            if entries or time.time() - row["fetched_at"] < EMPTY_MIX_RETRY:
                return entries
        entries = self.fetch_mix(vid)
        with self.connect() as con:
            con.execute("INSERT OR REPLACE INTO mixes VALUES (?,?,?)", (vid, json.dumps(entries), int(time.time())))
        return entries

    def bad_channels(self, con):
        """Channels with skipped tracks and nothing ever kept."""
        seen = Counter()
        kept = set()
        for r in con.execute("SELECT uploader, verdict FROM tracks WHERE uploader IS NOT NULL "
                             "AND verdict IN ('kept', 'skipped')"):
            if r["verdict"] == "kept":
                kept.add(r["uploader"])
            else:
                seen[r["uploader"]] += 1
        return {c for c in seen if c not in kept}

    def generate(self, mood_id):
        """One round for a mood: list the seeds' mixes, rank, replace the mood's queue."""
        mood = self.mood(mood_id)
        owned, playlists = self.library()
        seeds = self.seeds_for(mood, playlists.get(mood["playlist"], []) if mood["playlist"] else [])
        error, mixes, failed = None, {}, 0
        if not seeds:
            error = "No seeds: this mood needs YouTube links, or a playlist with downloaded tracks"
        with self.progress_lock:
            self.progress[mood_id] = {"done": 0, "total": len(seeds)}
        # The wait is YouTube, not this machine, so a few mixes are read at once.
        with ThreadPoolExecutor(max_workers=int(self.cfg.get("mix_workers", 4))) as pool:
            jobs = {pool.submit(self.mix, s["id"]): s["id"] for s in seeds}
            for n, job in enumerate(as_completed(jobs), 1):
                try:
                    mixes[jobs[job]] = job.result()
                except Exception as e:
                    failed += 1
                    error = f"{failed} of {len(seeds)} seeds failed, last: {str(e)[-200:]}"
                with self.progress_lock:
                    self.progress[mood_id] = {"done": n, "total": len(seeds)}
        with self.connect() as con:
            owned |= {r["vid"] for r in con.execute(
                "SELECT vid FROM tracks WHERE verdict IS NOT NULL UNION SELECT vid FROM downloads")}
            ranked = rank_candidates(seeds, mixes, owned, self.bad_channels(con),
                                     max_seconds=mood["max_minutes"] * 60, top=int(self.cfg.get("queue_size", 50)))
            for s in seeds:  # a pasted link has no title until its own mix names it
                own = next((v for v in mixes.get(s["id"]) or [] if v["id"] == s["id"]), None)
                if own and not s["title"]:
                    con.execute("UPDATE mood_seeds SET title = ? WHERE vid = ? AND title IS NULL", (own["title"], s["id"]))
            if ranked or not error:
                con.execute("DELETE FROM candidates WHERE mood_id = ?", (mood_id,))
            for c in ranked:
                con.execute("INSERT INTO tracks (vid, title, uploader, duration) VALUES (?,?,?,?) "
                            "ON CONFLICT(vid) DO UPDATE SET title = excluded.title, uploader = excluded.uploader, "
                            "duration = excluded.duration", (c["vid"], c["title"], c["uploader"], c["duration"]))
                con.execute("INSERT INTO candidates VALUES (?,?,?,?,?)",
                            (mood_id, c["vid"], c["score"], c["seed_count"], json.dumps(c["why"])))
            con.execute("UPDATE moods SET generated_at = ?, error = ? WHERE id = ?", (int(time.time()), error, mood_id))
        return {"tracks": len(ranked), "error": error}

    def start_round(self, mood_id):
        self.mood(mood_id)
        with self.progress_lock:
            if mood_id not in self.progress:
                self.progress[mood_id] = {"done": 0, "total": 0}
                self.rounds.put(mood_id)
        return {"ok": True}

    def round_worker(self):
        """One round at a time: the homelab is an old machine."""
        while True:
            mood_id = self.rounds.get()
            try:
                self.generate(mood_id)
            except Exception as e:
                print(f"round for mood {mood_id} failed: {e!r}", file=sys.stderr, flush=True)
                with contextlib.suppress(Exception), self.connect() as con:
                    con.execute("UPDATE moods SET error = ? WHERE id = ?", (str(e)[-300:] or "Round failed", mood_id))
            with self.progress_lock:
                self.progress.pop(mood_id, None)

    # ---- queue and verdicts ----

    def queue(self, q):
        mood_id = int_arg(q, "mood")
        with self.connect() as con:
            rows = con.execute(
                "SELECT c.vid, t.title, t.uploader, t.duration, c.seed_count, c.why FROM candidates c "
                "JOIN tracks t ON t.vid = c.vid WHERE c.mood_id = ? AND t.verdict IS NULL "
                "ORDER BY c.score DESC, c.vid", (mood_id,)).fetchall()
        return {"tracks": [{**dict(r), "why": json.loads(r["why"])} for r in rows]}

    def verdict(self, body):
        vid, verdict = body.get("vid"), body.get("verdict")
        if not isinstance(vid, str) or not VID_RE.fullmatch(vid):
            raise HttpError(400, "vid required")
        if verdict not in ("keep", "skip", "restore"):
            raise HttpError(400, "verdict must be keep, skip or restore")
        now = int(time.time())
        with self.connect() as con:
            row = con.execute("SELECT verdict FROM tracks WHERE vid = ?", (vid,)).fetchone()
            if not row:
                raise HttpError(404, "Unknown track")
            if verdict == "restore":
                if row["verdict"] != "skipped":
                    raise HttpError(400, "Only a skipped track can go back to the queue")
                con.execute("UPDATE tracks SET verdict = NULL, decided_at = NULL WHERE vid = ?", (vid,))
            elif verdict == "skip":
                if row["verdict"] == "kept":
                    raise HttpError(400, "Already kept. Delete it from the library instead")
                con.execute("UPDATE tracks SET verdict = 'skipped', decided_at = ? WHERE vid = ?", (now, vid))
            else:
                folder = body.get("folder")
                if folder not in self.folders:
                    raise HttpError(400, "Pick a playlist folder")
                mood_id = body.get("mood") if isinstance(body.get("mood"), int) else None
                con.execute("UPDATE tracks SET verdict = 'kept', mood_id = ?, folder = ?, decided_at = ? WHERE vid = ?",
                            (mood_id, folder, now, vid))
                if self.music_dir:
                    con.execute("INSERT INTO downloads (vid, folder, status, queued_at) VALUES (?,?,'queued',?) "
                                "ON CONFLICT(vid) DO UPDATE SET folder = excluded.folder, status = 'queued', error = NULL, "
                                "queued_at = excluded.queued_at WHERE downloads.status = 'failed'", (vid, folder, now))
        self.download_wake.set()
        return {"ok": True}

    def history(self, q):
        kind, offset = q.get("kind"), int_arg(q, "offset")
        if kind not in ("kept", "skipped"):
            raise HttpError(400, "kind must be kept or skipped")
        with self.connect() as con:
            rows = [dict(r) for r in con.execute(
                "SELECT t.vid, t.title, t.uploader, t.duration, t.folder, t.decided_at, m.name AS mood, "
                "d.status AS download, d.error AS download_error FROM tracks t "
                "LEFT JOIN moods m ON m.id = t.mood_id LEFT JOIN downloads d ON d.vid = t.vid "
                "WHERE t.verdict = ? ORDER BY t.decided_at DESC, t.vid LIMIT ? OFFSET ?", (kind, PAGE + 1, offset))]
        return {"tracks": rows[:PAGE], "next": offset + PAGE if len(rows) > PAGE else None}

    # ---- downloads and the library import ----

    def ytdlp_download(self, vid, folder):
        """Download into the folder as yt-dlp sees it, and answer with the path as this process sees it."""
        for wait in (*self.download_retry_waits, None):  # YouTube refuses a download now and then (403): try again
            try:
                path = ytdlp_audio(self.ytdlp, vid, os.path.join(self.ytdlp_music_dir, folder))
                break
            except RuntimeError as e:
                if wait is None:
                    raise
                print(f"download {vid}: {str(e)[-120:]}, again in {wait} s", file=sys.stderr, flush=True)
                time.sleep(wait)
        if path.startswith(self.ytdlp_music_dir):
            path = self.music_dir + path[len(self.ytdlp_music_dir):]
        return path

    def run_next_download(self):
        """Download the oldest queued track. One at a time. False when idle."""
        with self.connect() as con:
            row = con.execute("SELECT vid, folder FROM downloads WHERE status = 'queued' ORDER BY queued_at LIMIT 1").fetchone()
            if not row:
                return False
            con.execute("UPDATE downloads SET status = 'running' WHERE vid = ?", (row["vid"],))
        path = error = None
        try:
            free_gb = shutil.disk_usage(self.music_dir).free / 1e9
            if free_gb < float(self.cfg.get("download_min_free_gb", 5)):
                raise RuntimeError(f"Only {free_gb:.0f} GB free, download skipped")
            path = self.download(row["vid"], row["folder"])
            owner = self.cfg.get("download_owner")  # the library wants its files owned by 1000:1000
            if owner and os.path.exists(path):
                os.chown(path, int(owner[0]), int(owner[1]))
        except Exception as e:
            error = str(e)[-300:] or "Download failed"
            print(f"download {row['vid']} failed: {error}", file=sys.stderr, flush=True)
        with self.connect() as con:
            con.execute("UPDATE downloads SET status = ?, error = ?, path = ?, finished_at = ? WHERE vid = ?",
                        ("failed" if error else "done", error, path, int(time.time()), row["vid"]))
        return True

    def downloader(self):
        with self.connect() as con:  # a restart interrupts the running download: start it again
            con.execute("UPDATE downloads SET status = 'queued' WHERE status = 'running'")
        while True:
            try:
                while self.run_next_download():
                    pass
                if self.auto_import_due():
                    self.start_sync()
            except Exception as e:
                print(f"downloader failed: {e}", file=sys.stderr, flush=True)
            self.download_wake.wait(60)
            self.download_wake.clear()

    def auto_import_due(self, now=None):
        """True once downloads have been quiet for `auto_import_after` seconds and some still wait for the
        library import. A failed import is not repeated until a newer download arrives."""
        quiet = self.cfg.get("auto_import_after")
        state = self.sync_state
        if quiet is None or not self.cfg.get("import_cmd") or state["running"]:
            return False
        with self.connect() as con:
            busy = con.execute("SELECT COUNT(*) FROM downloads WHERE status IN ('queued', 'running')").fetchone()[0]
            last = con.execute("SELECT MAX(finished_at) FROM downloads WHERE status = 'done' AND synced = 0").fetchone()[0]
        if busy or last is None or (now or time.time()) - last < quiet:
            return False
        return state["ok"] is not False or last > (state["finished_at"] or 0)

    def status(self):
        with self.connect() as con:
            counts = {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) AS n FROM downloads GROUP BY status")}
            unsynced = con.execute("SELECT COUNT(*) FROM downloads WHERE status = 'done' AND synced = 0").fetchone()[0]
            failed = [dict(r) for r in con.execute(
                "SELECT d.vid, t.title, d.error FROM downloads d LEFT JOIN tracks t ON t.vid = d.vid "
                "WHERE d.status = 'failed' ORDER BY d.finished_at DESC LIMIT 10")]
        return {"folders": self.folders, "downloads_enabled": bool(self.music_dir),
                "downloading": counts.get("queued", 0) + counts.get("running", 0), "failed": failed,
                "sync": {"enabled": bool(self.cfg.get("import_cmd")), "waiting": unsynced, **self.sync_state}}

    def start_sync(self):
        if not self.cfg.get("import_cmd"):
            raise HttpError(400, "No import_cmd in tunes.json")
        with self.progress_lock:
            if self.sync_state["running"]:
                return self.status()
            self.sync_state = {"running": True, "ok": None, "finished_at": None, "tail": ""}
        threading.Thread(target=self.run_sync, daemon=True).start()
        return self.status()

    def run_sync(self):
        started = int(time.time())
        try:
            out = subprocess.run(self.cfg["import_cmd"], capture_output=True, text=True,
                                 timeout=int(self.cfg.get("import_timeout", 3600)))
            ok, tail = out.returncode == 0, (out.stdout + out.stderr).strip()[-600:]
        except Exception as e:
            ok, tail = False, str(e)[-600:]
        if ok:
            with self.connect() as con:
                con.execute("UPDATE downloads SET synced = 1 WHERE status = 'done' AND finished_at <= ?", (started,))
        self.sync_state = {"running": False, "ok": ok, "finished_at": int(time.time()), "tail": tail}

    # ---- one-off import of the old recommender's data ----

    def import_legacy(self, folder):
        """Load data/ of the old youtube-recommend.py: verdicts, the download ledger, cached mixes,
        seed files and the last suggestion lists. Safe to run twice."""
        def load(name, default):
            path = os.path.join(folder, name)
            if not os.path.exists(path):
                return default
            with open(path, encoding="utf-8") as f:
                return json.load(f)

        now = int(time.time())
        verdicts = {"liked": "kept", "rejected": "skipped", "owned": "owned", "pending": "owned"}
        with self.connect() as con:
            for name in self.folders:  # one mood per library playlist
                con.execute("INSERT OR IGNORE INTO moods (name, playlist, folder, added_at) VALUES (?,?,?,?)",
                            (name, name, name, now))
            for name, seeds_file, suggestions in (("jazz", "jazz_seeds.txt", "youtube_suggestions-jazz.json"),
                                                  ("rnb", "rnb_seeds.txt", "youtube_suggestions-rnb.json"),
                                                  ("thoia", "thoia_all_seeds.txt", "youtube_suggestions-thoia2.json"),
                                                  ("bangers", "banger_seeds.txt", None)):
                path = os.path.join(folder, seeds_file)
                if not os.path.exists(path):
                    continue
                con.execute("INSERT OR IGNORE INTO moods (name, folder, max_minutes, added_at) VALUES (?,?,?,?)",
                            (name, "misc" if "misc" in self.folders else self.folders[0], 15 if name == "jazz" else 12, now))
                mood_id = con.execute("SELECT id FROM moods WHERE name = ?", (name,)).fetchone()["id"]
                with open(path, encoding="utf-8") as f:
                    con.executemany("INSERT OR IGNORE INTO mood_seeds VALUES (?,?,?)",
                                    [(mood_id, s["id"], s["title"]) for s in parse_seeds(f.read())])
                for rank, c in enumerate(load(suggestions, []) if suggestions else []):
                    con.execute("INSERT OR IGNORE INTO tracks (vid, title, uploader, duration) VALUES (?,?,?,?)",
                                (c["id"], c["title"], c.get("uploader"), c.get("duration") or None))
                    con.execute("INSERT OR IGNORE INTO candidates VALUES (?,?,?,?,?)",
                                (mood_id, c["id"], -rank, c.get("seed_count", 0), json.dumps(c.get("from_your_tracks", []))))
            mood_of = {r["playlist"]: r["id"] for r in con.execute("SELECT id, playlist FROM moods WHERE playlist IS NOT NULL")}
            for vid, r in load("feedback.json", {}).get("items", {}).items():
                verdict = verdicts.get(r.get("verdict"))
                if verdict and VID_RE.fullmatch(vid):
                    con.execute(
                        "INSERT INTO tracks (vid, title, uploader, verdict, mood_id, folder, decided_at) VALUES (?,?,?,?,?,?,?) "
                        "ON CONFLICT(vid) DO UPDATE SET verdict = excluded.verdict, mood_id = excluded.mood_id, "
                        "folder = excluded.folder, decided_at = excluded.decided_at WHERE tracks.verdict IS NULL",
                        (vid, r.get("title") or r.get("artist"), r.get("uploader"), verdict,
                         mood_of.get(r.get("folder")) if verdict == "kept" else None, r.get("folder"), now))
            for vid in load("youtube_downloaded.json", []):
                con.execute("INSERT INTO tracks (vid, verdict, decided_at) VALUES (?, 'owned', ?) "
                            "ON CONFLICT(vid) DO UPDATE SET verdict = 'owned' WHERE tracks.verdict IS NULL", (vid, now))
            con.executemany("INSERT OR IGNORE INTO mixes VALUES (?,?,?)",
                            [(seed, json.dumps(entries), now) for seed, entries in load("youtube_mixes.json", {}).items()])
            return {name: con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                    for name in ("moods", "mood_seeds", "tracks", "candidates", "mixes")}

    # ---- routing ----

    # ---- settings shared by every device (the last mood, the last playlist, shuffle) ----
    def settings(self):
        try:
            with open(self.settings_path, encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            saved = {}
        return {k: saved[k] for k in SETTINGS if isinstance(saved.get(k), SETTINGS[k])} if isinstance(saved, dict) else {}

    def save_settings(self, body):
        with self.settings_lock:
            merged = self.settings()
            for key, kind in SETTINGS.items():
                if key in body:
                    if not isinstance(body[key], kind) or len(str(body[key])) > 200:
                        raise HttpError(400, f"Bad value for {key}")
                    merged[key] = body[key]
            tmp = self.settings_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=2)
                f.write("\n")
            os.replace(tmp, self.settings_path)  # never leaves a half written file
        return merged

    def authorised(self, token):
        expected = self.cfg.get("token") or ""
        return bool(expected) and hmac.compare_digest((token or "").encode(), expected.encode())

    def api(self, method, path, q, body, token):
        """Returns (status, json-able). Raises nothing: errors become {"error": ...}."""
        try:
            if not self.authorised(token):
                raise HttpError(401, "Token required")
            parts = [p for p in path.split("/") if p][1:]
            route = (method, parts[0] if parts else "", len(parts))
            if parts[:1] == ["moods"] and len(parts) > 1:
                if not parts[1].isdigit():
                    raise HttpError(404, "Unknown mood")
                mood_id = int(parts[1])
                if route == ("GET", "moods", 2):
                    return 200, self.mood(mood_id)
                if route == ("DELETE", "moods", 2):
                    return 200, self.remove_mood(mood_id)
                if (method, len(parts), parts[2:3]) == ("POST", 3, ["seeds"]):
                    return 200, self.add_seeds(mood_id, body)
                if (method, len(parts), parts[2:3]) == ("DELETE", 4, ["seeds"]):
                    return 200, self.remove_seed(mood_id, parts[3])
                if (method, len(parts), parts[2:3]) == ("POST", 3, ["generate"]):
                    return 200, self.start_round(mood_id)
            if route == ("GET", "settings", 1):
                return 200, self.settings()
            if route == ("POST", "settings", 1):
                return 200, self.save_settings(body)
            if route == ("GET", "moods", 1):
                return 200, self.moods()
            if route == ("POST", "moods", 1):
                return 200, self.add_mood(body)
            if route == ("GET", "queue", 1):
                return 200, self.queue(q)
            if route == ("POST", "verdict", 1):
                return 200, self.verdict(body)
            if route == ("GET", "history", 1):
                return 200, self.history(q)
            if route == ("GET", "library", 1):
                return 200, self.library_tracks(q)
            if route == ("GET", "status", 1):
                return 200, self.status()
            if route == ("POST", "sync", 1):
                return 200, self.start_sync()
            raise HttpError(404, "Not found")
        except HttpError as e:
            return e.status, {"error": str(e)}


def int_arg(q, key):
    try:
        return max(0, int(q.get(key) or 0))
    except ValueError:
        return 0


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "tunes"

        def log_message(self, *args):
            pass

        def send(self, status, body, ctype, cache="no-store"):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
            self.end_headers()
            self.wfile.write(body)

        def send_file(self, full, save=False):
            """One audio file, whole or the byte range the player asked for (seeking needs ranges).
            With save, the browser stores it as "Artist - Title" (the library keeps files as artist/title)."""
            size = os.path.getsize(full)
            start, end, status = 0, size - 1, 200
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), end) if m.group(2) else end
                else:
                    start = max(0, size - int(m.group(2)))
                status = 206
            self.send_response(416 if start > end else status)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "private, max-age=86400")
            self.send_header("X-Content-Type-Options", "nosniff")
            if start > end:
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                return self.end_headers()
            self.send_header("Content-Type", AUDIO.get(os.path.splitext(full)[1].lower(), "application/octet-stream"))
            if save:
                name = os.path.basename(os.path.dirname(full)) + " - " + os.path.basename(full)
                plain = re.sub(r"[^A-Za-z0-9 ._()-]", "_", name)
                self.send_header("Content-Disposition",
                                 f"attachment; filename=\"{plain}\"; filename*=UTF-8''{urllib.parse.quote(name)}")
            self.send_header("Content-Length", str(end - start + 1))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with open(full, "rb") as f:
                f.seek(start)
                left = end - start + 1
                try:
                    while left > 0:
                        chunk = f.read(min(65536, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the player moved on to another track

        def dispatch(self, method):
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/health":
                return self.send(200, b'{"ok":true}', "application/json")
            if url.path.startswith(("/media/", "/art/")):
                token = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip() \
                    or urllib.parse.parse_qs(url.query).get("token", [""])[0]
                if method != "GET" or not app.authorised(token):
                    return self.send(401, b"Token required", "text/plain")
                kind, item_id = url.path.split("/", 2)[1:]
                found = app.media_file(item_id) if kind == "media" else app.cover(item_id)
                if not found:
                    return self.send(404, b"Not found", "text/plain")
                if kind == "media":
                    return self.send_file(found, save="save" in urllib.parse.parse_qs(url.query, keep_blank_values=True))
                return self.send(200, *found, cache="private, max-age=604800")
            if url.path.startswith("/api/"):
                body = {}
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    try:
                        body = json.loads(self.rfile.read(min(length, 262144)))
                    except ValueError:
                        body = None
                    if not isinstance(body, dict):
                        return self.send(400, b'{"error":"Bad JSON"}', "application/json")
                token = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
                q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
                try:
                    status, out = app.api(method, url.path, q, body, token)
                except Exception as e:
                    print(f"{method} {url.path}: {e!r}", file=sys.stderr, flush=True)
                    status, out = 500, {"error": "Server error"}
                return self.send(status, json.dumps(out).encode(), "application/json")
            if method != "GET":
                return self.send(405, b"", "text/plain")
            name = "index.html" if url.path == "/" else url.path.lstrip("/")
            full = os.path.realpath(os.path.join(WEB, name))
            if not full.startswith(os.path.realpath(WEB) + os.sep) or not os.path.isfile(full):
                return self.send(404, b"Not found", "text/plain")
            with open(full, "rb") as f:
                self.send(200, f.read(), MIME.get(os.path.splitext(full)[1], "application/octet-stream"))

        def do_GET(self):
            self.dispatch("GET")

        def do_POST(self):
            self.dispatch("POST")

        def do_DELETE(self):
            self.dispatch("DELETE")

    return Handler


def load_config():
    path = os.environ.get("TUNES_CONFIG") or os.path.join(HERE, "tunes.json")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    base = os.path.dirname(path)
    cfg["data_dir"] = os.path.normpath(os.path.join(base, cfg.get("data_dir", "../data")))
    for key in ("music_dir", "library_dir", "beets_db", "pinchflat_db"):
        if cfg.get(key):
            cfg[key] = os.path.normpath(os.path.join(base, cfg[key]))
    if not cfg.get("token"):
        raise SystemExit("tunes.json needs a token")
    return cfg


def main():
    cfg = load_config()
    os.makedirs(cfg["data_dir"], exist_ok=True)
    app = App(cfg)
    if len(sys.argv) == 3 and sys.argv[1] == "--import":
        print(json.dumps(app.import_legacy(sys.argv[2])))
        return
    threading.Thread(target=app.round_worker, daemon=True).start()
    if app.music_dir:
        if not os.path.isdir(app.music_dir):
            raise SystemExit(f"music_dir {app.music_dir} does not exist")
        threading.Thread(target=app.downloader, daemon=True).start()
    server = ThreadingHTTPServer((cfg.get("bind", "0.0.0.0"), int(cfg.get("port", 8792))), make_handler(app))
    print(f"tunes on :{server.server_address[1]}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

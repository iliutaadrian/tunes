import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import tunes  # noqa: E402

TOKEN = "t"


def vid(n):
    return f"v{n:010d}"


def entry(n, uploader="Chan", duration=200):
    return {"id": vid(n), "title": f"Track {n}", "uploader": uploader, "duration": duration}


class RankTest(unittest.TestCase):
    def seeds(self, n):
        return [{"id": vid(900 + i), "title": f"Seed {i}"} for i in range(n)]

    def test_needs_two_seeds_and_prefers_early_positions(self):
        seeds = self.seeds(3)
        mixes = {seeds[0]["id"]: [entry(1), entry(2), entry(3)],
                 seeds[1]["id"]: [entry(2), entry(1)],
                 seeds[2]["id"]: [entry(4)]}
        ranked = tunes.rank_candidates(seeds, mixes)
        self.assertEqual([c["vid"] for c in ranked], [vid(1), vid(2)])  # 3 and 4 have one recommender
        self.assertEqual(ranked[0]["seed_count"], 2)
        self.assertEqual(ranked[0]["why"], ["Seed 0", "Seed 1"])

    def test_drops_owned_seed_itself_and_wrong_lengths(self):
        seeds = self.seeds(2)
        mix = [entry(1), entry(2, duration=30), entry(3, duration=3600), entry(4), {**entry(0), "id": seeds[0]["id"]}]
        ranked = tunes.rank_candidates(seeds, {s["id"]: mix for s in seeds}, owned={vid(1)})
        self.assertEqual([c["vid"] for c in ranked], [vid(4)])

    def test_generic_filler_is_dropped_once_there_are_enough_seeds(self):
        seeds = self.seeds(12)
        mixes = {s["id"]: [entry(1)] + ([entry(2)] if i < 3 else []) for i, s in enumerate(seeds)}
        self.assertEqual([c["vid"] for c in tunes.rank_candidates(seeds, mixes)], [vid(2)])

    def test_channel_with_only_skips_is_demoted(self):
        seeds = self.seeds(2)
        mix = [entry(1, uploader="Bad"), entry(2, uploader="Fine")]
        ranked = tunes.rank_candidates(seeds, {s["id"]: mix for s in seeds}, bad_channels={"Bad"})
        self.assertEqual([c["vid"] for c in ranked], [vid(2), vid(1)])

    def test_another_upload_of_a_seed_is_not_a_discovery(self):
        seeds = [{"id": vid(901), "title": "Khia - My Neck, My Back (Official Video)"}, {"id": vid(902), "title": None}]
        mix = [{**entry(1), "title": "Khia - My Neck, My Back [Lyrics]"}, entry(2)]
        self.assertEqual([c["vid"] for c in tunes.rank_candidates(seeds, {s["id"]: mix for s in seeds})], [vid(2)])

    def test_parse_seeds(self):
        text = f"https://www.youtube.com/watch?v={vid(1)}&list=x\tA title\nhttps://youtu.be/{vid(2)}\n{vid(3)}\n# note\nnot a video\n{vid(1)}"
        self.assertEqual(tunes.parse_seeds(text), [{"id": vid(1), "title": "A title"}, {"id": vid(2), "title": None},
                                                   {"id": vid(3), "title": None}])


class AppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.music = os.path.join(self.tmp.name, "music")
        os.makedirs(self.music)
        self.mixes, self.fetched, self.downloaded = {}, [], []
        cfg = {"token": TOKEN, "data_dir": self.tmp.name, "music_dir": self.music, "folders": ["Electro", "misc"]}

        def fetch(seed):
            self.fetched.append(seed)
            return self.mixes.get(seed, [])

        def download(v, folder):
            if v == vid(13):
                raise RuntimeError("Video unavailable")
            self.downloaded.append((v, folder))
            return os.path.join(self.music, folder, v + ".m4a")

        self.app = tunes.App(cfg, fetch_mix=fetch, download=download)

    def call(self, method, path, body=None, q=None, token=TOKEN):
        return self.app.api(method, "/api/" + path, q or {}, body or {}, token)

    def test_settings_are_saved_to_a_json_file(self):
        self.assertEqual(self.call("GET", "settings"), (200, {}))
        self.assertEqual(self.call("POST", "settings", {"playlist": "Electro", "shuffle": False})[0], 200)
        self.assertEqual(self.call("POST", "settings", {"mood": "3", "ignored": "x"})[1],
                         {"playlist": "Electro", "shuffle": False, "mood": "3"})
        with open(os.path.join(self.tmp.name, "settings.json")) as f:
            self.assertEqual(json.load(f), {"playlist": "Electro", "shuffle": False, "mood": "3"})
        self.assertEqual(self.call("POST", "settings", {"shuffle": "yes"})[0], 400)
        self.assertEqual(self.call("GET", "settings", token="wrong")[0], 401)

    def mood(self, seeds=(1, 2)):
        status, out = self.call("POST", "moods", {"name": "jazz", "folder": "misc",
                                                  "seeds": "\n".join(vid(900 + s) for s in seeds)})
        self.assertEqual(status, 200, out)
        return out["id"]

    def queue(self, mood_id):
        return [t["vid"] for t in self.call("GET", "queue", q={"mood": str(mood_id)})[1]["tracks"]]

    def beets(self, tracks):
        """A beets library with (id, playlist, relative path) rows, and the files of those marked present."""
        db = os.path.join(self.tmp.name, "beets.blb")
        root = os.path.join(self.tmp.name, "library")
        con = sqlite3.connect(db)
        con.executescript("CREATE TABLE items (id INTEGER PRIMARY KEY, artist TEXT, title TEXT, length REAL, comments TEXT, path BLOB, added REAL);"
                          "CREATE TABLE item_attributes (entity_id INTEGER, key TEXT, value TEXT);")
        for item_id, playlist, rel, present in tracks:
            con.execute("INSERT INTO items VALUES (?, 'Artist', ?, 200.0, '', ?, ?)",
                        (item_id, f"Song {item_id}", rel.encode(), 1000.0 + item_id))
            con.execute("INSERT INTO item_attributes VALUES (?, 'playlist', ?)", (item_id, playlist))
            if present:
                full = os.path.join(root, os.path.relpath(rel, "/music") if rel.startswith("/") else rel)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "wb") as f:
                    f.write(bytes(range(100)))
        con.commit()
        con.close()
        self.app.cfg["beets_db"] = db
        self.app.library_dir = root

    def test_library_lists_playlists_and_marks_what_can_play(self):
        self.beets([(1, "Electro", "Electro/a/one.m4a", True), (2, "Electro", "/music/Electro/a/two.m4a", True),
                    (3, "Electro", "Electro/a/gone.m4a", False), (4, "misc", "misc/b/four.m4a", True)])
        out = self.call("GET", "library")[1]
        self.assertEqual(out["playlists"], [{"name": "Electro", "tracks": 3}, {"name": "misc", "tracks": 1}])
        self.assertEqual(out["playlist"], "Electro")
        self.assertEqual({t["id"]: t["playable"] for t in out["tracks"]}, {1: True, 2: True, 3: False})
        self.assertEqual([t["id"] for t in out["tracks"]], [3, 2, 1])  # newest first
        self.assertEqual([t["id"] for t in self.call("GET", "library", q={"playlist": "misc"})[1]["tracks"]], [4])
        everything = self.call("GET", "library", q={"playlist": "*"})[1]
        self.assertEqual((everything["playlist"], sorted(t["id"] for t in everything["tracks"])), ("*", [1, 2, 3, 4]))

    def test_cover_is_read_from_the_m4a(self):
        def atom(kind, body):
            return (8 + len(body)).to_bytes(4, "big") + kind + body
        picture = b"\xff\xd8\xffcover"
        ilst = atom(b"ilst", atom(b"\xa9nam", atom(b"data", bytes(8) + b"Song")) + atom(b"covr", atom(b"data", bytes(8) + picture)))
        m4a = atom(b"ftyp", b"M4A ") + atom(b"mdat", bytes(50)) + atom(b"moov", atom(b"mvhd", bytes(20)) + atom(
            b"udta", atom(b"meta", bytes(4) + atom(b"hdlr", bytes(10)) + ilst)))
        self.beets([(1, "Electro", "Electro/a/one.m4a", True), (2, "Electro", "Electro/a/two.m4a", True)])
        with open(os.path.join(self.app.library_dir, "Electro/a/one.m4a"), "wb") as f:
            f.write(m4a)
        self.assertEqual(self.app.cover("1"), (picture, "image/jpeg"))
        self.assertIsNone(self.app.cover("2"))   # not an mp4 at all
        self.assertIsNone(self.app.cover("99"))

    def test_media_file_stays_inside_the_library(self):
        self.beets([(1, "Electro", "Electro/a/one.m4a", True), (2, "Electro", "../state.db", False),
                    (3, "Electro", "/etc/hosts", False)])
        self.assertTrue(self.app.media_file("1").endswith("one.m4a"))
        for item_id in ("2", "3", "99", "x"):
            self.assertIsNone(self.app.media_file(item_id))

    def test_media_is_served_in_ranges_and_needs_the_token(self):
        self.beets([(1, "Electro", "Electro/a/one.m4a", True)])
        server = tunes.ThreadingHTTPServer(("127.0.0.1", 0), tunes.make_handler(self.app))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}/media/"

        def get(path, rng=None):
            req = urllib.request.Request(base + path, headers={"Range": rng} if rng else {})
            try:
                with urllib.request.urlopen(req) as res:
                    return res.status, res.read(), res.headers
            except urllib.error.HTTPError as e:
                return e.code, b"", e.headers

        self.assertEqual(get("1")[0], 401)
        self.assertEqual(get("1?token=nope")[0], 401)
        self.assertEqual(get("2?token=" + TOKEN)[0], 404)
        status, body, headers = get("1?token=" + TOKEN)
        self.assertEqual((status, body, headers["Content-Type"]), (200, bytes(range(100)), "audio/mp4"))
        status, body, headers = get("1?token=" + TOKEN, "bytes=10-19")
        self.assertEqual((status, body, headers["Content-Range"]), (206, bytes(range(10, 20)), "bytes 10-19/100"))
        self.assertEqual(get("1?token=" + TOKEN, "bytes=90-")[1], bytes(range(90, 100)))
        self.assertEqual(get("1?token=" + TOKEN, "bytes=-5")[1], bytes(range(95, 100)))
        self.assertEqual(get("1?token=" + TOKEN, "bytes=500-")[0], 416)
        self.assertIsNone(headers["Content-Disposition"])                 # playing is not saving
        self.assertEqual(get("1?save&token=" + TOKEN)[2]["Content-Disposition"],
                         "attachment; filename=\"a - one.m4a\"; filename*=UTF-8''a%20-%20one.m4a")

    def test_import_starts_by_itself_after_downloads_go_quiet(self):
        self.app.cfg.update(import_cmd=["true"], auto_import_after=600)
        self.assertFalse(self.app.auto_import_due())                      # nothing downloaded
        with self.app.connect() as con:
            con.execute("INSERT INTO downloads (vid, folder, status, queued_at, finished_at) VALUES (?, 'misc', 'done', 1, 1000)", (vid(1),))
        self.assertFalse(self.app.auto_import_due(now=1300))              # still inside the quiet time
        self.assertTrue(self.app.auto_import_due(now=1700))
        self.app.sync_state = {"running": False, "ok": False, "finished_at": 1800, "tail": ""}
        self.assertFalse(self.app.auto_import_due(now=5000))              # it failed: wait for a newer download
        with self.app.connect() as con:
            con.execute("INSERT INTO downloads (vid, folder, status, queued_at, finished_at) VALUES (?, 'misc', 'queued', 1, NULL)", (vid(2),))
        self.app.sync_state["ok"] = True
        self.assertFalse(self.app.auto_import_due(now=5000))              # another download is on its way
        del self.app.cfg["auto_import_after"]
        self.assertFalse(self.app.auto_import_due(now=5000))              # the feature is off without the key

    def test_token_is_required(self):
        self.assertEqual(self.call("GET", "moods", token="nope")[0], 401)
        self.assertEqual(self.call("GET", "moods", token="")[0], 401)

    def test_mood_needs_seeds_or_playlist(self):
        self.assertEqual(self.call("POST", "moods", {"name": "empty"})[0], 400)
        self.assertEqual(self.call("POST", "moods", {"name": "house", "playlist": "Electro"})[0], 200)
        self.assertEqual(self.call("POST", "moods", {"name": "House", "playlist": "Electro"})[0], 400)
        self.assertEqual(self.call("POST", "moods", {"name": "odd", "playlist": "nope"})[0], 400)

    def test_round_fills_queue_and_caches_mixes(self):
        mood_id = self.mood()
        self.mixes = {vid(901): [{**entry(0), "id": vid(901), "title": "Seed one"}, entry(10), entry(11)],
                      vid(902): [entry(11), entry(10), entry(12)]}
        self.assertEqual(self.app.generate(mood_id), {"tracks": 2, "error": None})
        self.assertEqual(sorted(self.queue(mood_id)), [vid(10), vid(11)])
        self.assertEqual(self.call("GET", f"moods/{mood_id}")[1]["seeds"][0]["title"], "Seed one")
        self.app.generate(mood_id)
        self.assertEqual(sorted(self.fetched), [vid(901), vid(902)])  # second round used the cache

    def test_skip_keep_and_restore(self):
        mood_id = self.mood()
        self.mixes = {vid(901): [entry(10), entry(11), entry(13)], vid(902): [entry(10), entry(11), entry(13)]}
        self.app.generate(mood_id)
        self.assertEqual(self.call("POST", "verdict", {"vid": vid(10), "verdict": "skip"})[0], 200)
        self.assertEqual(self.call("POST", "verdict", {"vid": vid(11), "verdict": "keep", "mood": mood_id, "folder": "nope"})[0], 400)
        self.assertEqual(self.call("POST", "verdict", {"vid": vid(11), "verdict": "keep", "mood": mood_id, "folder": "misc"})[0], 200)
        self.assertEqual(self.call("POST", "verdict", {"vid": vid(13), "verdict": "keep", "mood": mood_id, "folder": "misc"})[0], 200)
        self.assertEqual(self.call("POST", "verdict", {"vid": vid(99), "verdict": "skip"})[0], 404)
        self.assertEqual(self.queue(mood_id), [])

        while self.app.run_next_download():
            pass
        self.assertEqual(self.downloaded, [(vid(11), "misc")])
        kept = self.call("GET", "history", q={"kind": "kept"})[1]["tracks"]
        self.assertEqual({t["vid"]: t["download"] for t in kept}, {vid(11): "done", vid(13): "failed"})
        status = self.call("GET", "status")[1]
        self.assertEqual((status["downloading"], len(status["failed"]), status["sync"]["waiting"]), (0, 1, 1))

        self.app.generate(mood_id)  # judged tracks never come back, and the kept one is now a seed
        self.assertEqual(self.queue(mood_id), [])
        self.assertIn(vid(11), self.fetched)
        self.assertEqual(self.call("POST", "verdict", {"vid": vid(11), "verdict": "restore"})[0], 400)
        self.assertEqual(self.call("POST", "verdict", {"vid": vid(10), "verdict": "restore"})[0], 200)
        self.assertEqual(self.queue(mood_id), [])  # restored, but the new round already dropped it
        self.assertEqual(self.app.generate(mood_id)["tracks"], 1)
        self.assertEqual(self.queue(mood_id), [vid(10)])

    def test_failed_seeds_keep_the_old_queue(self):
        mood_id = self.mood()
        self.mixes = {vid(901): [entry(10)], vid(902): [entry(10)]}
        self.app.generate(mood_id)

        def broken(seed):
            raise RuntimeError("Sign in to confirm you are not a bot")
        with self.app.connect() as con:
            con.execute("DELETE FROM mixes")
        self.app.fetch_mix = broken
        out = self.app.generate(mood_id)
        self.assertIn("2 of 2 seeds failed", out["error"])
        self.assertEqual(self.queue(mood_id), [vid(10)])

    def test_library_seeds_and_owned(self):
        beets = os.path.join(self.tmp.name, "beets.db")
        con = sqlite3.connect(beets)
        con.executescript("CREATE TABLE items (id INTEGER PRIMARY KEY, artist TEXT, title TEXT, comments TEXT);"
                          "CREATE TABLE item_attributes (entity_id INTEGER, key TEXT, value TEXT);")
        con.execute("INSERT INTO items VALUES (1, 'A', 'One', ?)", (f"https://www.youtube.com/watch?v={vid(901)}",))
        con.execute("INSERT INTO items VALUES (2, 'B', 'Two', ?)", (f"https://www.youtube.com/watch?v={vid(10)}",))
        con.execute("INSERT INTO items VALUES (3, 'C', 'Three', NULL)")
        con.executemany("INSERT INTO item_attributes VALUES (?, 'playlist', ?)", [(1, "Electro"), (2, "misc"), (3, "Electro")])
        con.commit()
        con.close()
        pf = os.path.join(self.tmp.name, "pf.db")
        con = sqlite3.connect(pf)
        con.execute("CREATE TABLE media_items (media_id TEXT, title TEXT, media_filepath TEXT)")
        con.execute("INSERT INTO media_items VALUES (?, 'PF two', '/media/Music/Electro/x.m4a')", (vid(902),))
        con.execute("INSERT INTO media_items VALUES (?, 'Not downloaded', NULL)", (vid(11),))
        con.commit()
        con.close()
        self.app.cfg.update(beets_db=beets, pinchflat_db=pf)
        owned, playlists = self.app.library()
        self.assertEqual(owned, {vid(901), vid(902), vid(10), vid(11)})
        self.assertEqual({s["id"] for s in playlists["Electro"]}, {vid(901), vid(902)})

        mood_id = self.call("POST", "moods", {"name": "house", "playlist": "Electro"})[1]["id"]
        self.mixes = {vid(901): [entry(10), entry(11), entry(12)], vid(902): [entry(10), entry(11), entry(12)]}
        self.app.generate(mood_id)
        self.assertEqual(self.queue(mood_id), [vid(12)])  # 10 and 11 are already in the library

    def test_import_legacy(self):
        src = os.path.join(self.tmp.name, "legacy")
        os.makedirs(src)

        def write(name, obj):
            with open(os.path.join(src, name), "w", encoding="utf-8") as f:
                f.write(obj if isinstance(obj, str) else json.dumps(obj))

        write("feedback.json", {"items": {
            vid(1): {"verdict": "liked", "title": "Liked", "uploader": "Good", "folder": "Electro"},
            vid(2): {"verdict": "rejected", "title": "Bad", "uploader": "Spam"},
            vid(3): {"verdict": "pending", "title": "Waiting"}}})
        write("youtube_downloaded.json", [vid(3), vid(4)])
        write("youtube_mixes.json", {vid(1): [entry(20)]})
        write("jazz_seeds.txt", f"{vid(901)}\tRyo Fukui\n")
        write("youtube_suggestions-jazz.json", [{"id": vid(30), "title": "New", "uploader": "X", "duration": 200, "seed_count": 2},
                                                {"id": vid(2), "title": "Bad", "uploader": "Spam", "duration": 200}])
        first = self.app.import_legacy(src)
        self.assertEqual(self.app.import_legacy(src), first)  # safe to run twice
        self.assertEqual(first, {"moods": 3, "mood_seeds": 1, "tracks": 5, "candidates": 2, "mixes": 1})
        moods = {m["name"]: m for m in self.call("GET", "moods")[1]["moods"]}
        self.assertEqual((moods["Electro"]["kept"], moods["jazz"]["waiting"], moods["jazz"]["max_minutes"]), (1, 1, 15))
        self.assertEqual(self.queue(moods["jazz"]["id"]), [vid(30)])
        with self.app.connect() as con:
            self.assertEqual(self.app.bad_channels(con), {"Spam"})


if __name__ == "__main__":
    unittest.main()

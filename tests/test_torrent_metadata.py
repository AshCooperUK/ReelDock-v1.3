import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET
from pathlib import Path

from backend.app import create_app
from types import SimpleNamespace

from backend.downloaders.torrent.metadata import apply_match, choose, fetch, matched_save_as, preview, scan_lookup


class Client:
    def get(self, path, **params):
        if path == "search/movie":
            return {"results": [{"id": 7, "title": "Coyote vs Acme", "release_date": "2026-01-01"}]}
        if path == "movie/7":
            return {"id": 7, "title": "Coyote vs Acme", "overview": "A chase.", "release_date": "2026-07-01"}
        if path == "search/tv":
            return {"results": [{"id": 3, "name": "It's Always Sunny in Philadelphia", "first_air_date": "2005-08-04"}]}
        if path == "tv/3":
            return {"id": 3, "name": "It's Always Sunny in Philadelphia", "overview": "The gang."}
        if path == "tv/3/season/18":
            return {"episodes": [{"episode_number": 9, "name": "The Episode", "overview": "Tonight."}]}
        return {}


class TorrentMetadataTests(unittest.TestCase):
    def test_missing_credential_keeps_release_note_and_video_without_claiming_metadata_done(self):
        import threading
        from backend.downloaders.torrent import metadata
        with tempfile.TemporaryDirectory() as folder:
            app=create_app({'TESTING':True,'DATA_DIR':folder,'LOCAL_SETUP_URL':'http://127.0.0.1:8790'})
            store=app.extensions['store'];base=Path(folder)/'media'
            target=base/'TV Shows'/'Example Show'/'Season 28'/'Example Show s28e02.mkv'
            target.parent.mkdir(parents=True);target.write_bytes(b'kept video')
            note=b'Release information, not an XML metadata document.'
            target.with_suffix('.nfo').write_bytes(note)
            job=SimpleNamespace(id='fixture',metadata_choice={},metadata_done=set(),metadata_started={0},metadata_note='',_lock=threading.RLock(),engine=SimpleNamespace(data_dir=Path(folder)))
            with mock.patch.object(metadata,'fetch') as fetch:
                metadata._save_active(store,base,job,0,str(target.relative_to(base)),'series',{'name':'Example Show','season':28,'episode':2})
            fetch.assert_not_called()
            self.assertIn('Basic local metadata saved',job.metadata_note)
            self.assertEqual(job.metadata_done,set())
            self.assertEqual(job.metadata_started,set(),'saving a credential later can retry metadata')
            self.assertEqual(target.read_bytes(),b'kept video')
            self.assertEqual(ET.fromstring(target.with_suffix('.nfo').read_bytes()).findtext('title'),'Episode 2')
            backups=list(target.parent.glob('*.release-note-*.txt'));self.assertEqual(len(backups),1);self.assertEqual(backups[0].read_bytes(),note)
            self.assertEqual(ET.fromstring((target.parent.parent/'tvshow.nfo').read_bytes()).findtext('title'),'Example Show')
            with self.assertRaises(ET.ParseError):ET.fromstring(note)

    def test_year_picks_one_movie_and_apostrophes_match_a_show(self) -> None:
        movies = [
            {"id": 1, "title": "Coyote vs Acme", "release_date": "1999-01-01"},
            {"id": 7, "title": "Coyote vs Acme", "release_date": "2026-01-01"},
        ]
        self.assertEqual(choose(movies, "Coyote vs Acme", 2026)["id"], 7)
        self.assertIsNone(choose(movies, "Coyote vs Acme", 2010))
        shows = [{"id": 3, "name": "It's Always Sunny in Philadelphia"}]
        self.assertEqual(choose(shows, "Its Always Sunny in Philadelphia")["id"], 3)

    def test_imdb_identity_writes_the_matching_film(self) -> None:
        class Finder:
            def get(self, path, **params):
                if path == "find/tt0089560":
                    self.source = params.get("external_source")
                    return {"movie_results": [{"id": 13333, "title": "Mask", "release_date": "1985-03-08"}]}
                if path == "movie/13333":
                    return {"id": 13333, "title": "Mask", "imdb_id": "tt0089560", "overview": "A portrait.", "release_date": "1985-03-08", "poster_path": "/poster.jpg", "backdrop_path": "/fanart.jpg"}
                return {}

        finder = Finder()
        with tempfile.TemporaryDirectory() as folder:
            app = create_app({"TESTING": True, "DATA_DIR": folder, "LOCAL_SETUP_URL": "http://127.0.0.1:8790"})
            store = app.extensions["store"]
            base = Path(folder) / "media"
            movie = base / "Movies" / "Mask (1985)" / "Mask (1985) CD1.avi"
            movie.parent.mkdir(parents=True)
            movie.write_bytes(b"video")
            with mock.patch("backend.m3u_sidecars.artwork", return_value=True) as art:
                self.assertTrue(fetch(store, base, movie.relative_to(base).as_posix(), "movie", {"name": "Mask", "year": 1985, "provider": "imdb", "identity": "0089560"}, finder))
            self.assertEqual(finder.source, "imdb_id")
            nfo = ET.fromstring(movie.with_suffix(".nfo").read_bytes())
            self.assertEqual(nfo.findtext("title"), "Mask")
            self.assertEqual([item.get("type") for item in nfo.findall("uniqueid")], ["tmdb", "imdb"])
            self.assertEqual(nfo.findall("uniqueid")[1].text, "tt0089560")
            written = [call.args[2].name for call in art.call_args_list]
            self.assertIn("poster.jpg", written)
            self.assertIn("fanart.jpg", written)

    def test_successful_rename_saves_movie_and_episode_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            app = create_app({"TESTING": True, "DATA_DIR": folder, "LOCAL_SETUP_URL": "http://127.0.0.1:8790"})
            store = app.extensions["store"]
            base = Path(folder) / "media"
            movie = base / "Movies" / "Coyote vs Acme (2026)" / "Coyote vs Acme (2026).mkv"
            episode = base / "TV Shows" / "Its Always Sunny in Philadelphia" / "Season 18" / "Its Always Sunny in Philadelphia s18e09-CAKES.mkv"
            movie.parent.mkdir(parents=True)
            episode.parent.mkdir(parents=True)
            movie.write_bytes(b"movie")
            episode.write_bytes(b"episode")
            self.assertTrue(fetch(store, base, "Movies/Coyote vs Acme (2026)/Coyote vs Acme (2026).mkv", "movie", {"name": "Coyote vs Acme", "year": 2026}, Client()))
            self.assertTrue(fetch(store, base, str(episode.relative_to(base)), "series", {"name": "Its Always Sunny in Philadelphia", "season": 18, "episode": 9}, Client()))
            movie_nfo = ET.fromstring(movie.with_suffix(".nfo").read_bytes())
            self.assertEqual(movie_nfo.tag, "movie")
            self.assertEqual(movie_nfo.findtext("title"), "Coyote vs Acme")
            self.assertEqual(movie_nfo.findtext("uniqueid"), "7")
            show_nfo = ET.fromstring((episode.parent.parent / "tvshow.nfo").read_bytes())
            self.assertEqual(show_nfo.tag, "tvshow")
            self.assertEqual(show_nfo.findtext("title"), "It's Always Sunny in Philadelphia")
            episode_nfo = ET.fromstring(episode.with_suffix(".nfo").read_bytes())
            self.assertEqual(episode_nfo.tag, "episodedetails")
            self.assertEqual(episode_nfo.findtext("season"), "18")
            self.assertEqual(episode_nfo.findtext("episode"), "9")

    def test_release_text_nfo_is_replaced_and_an_emby_nfo_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            app = create_app({"TESTING": True, "DATA_DIR": folder, "LOCAL_SETUP_URL": "http://127.0.0.1:8790"})
            store = app.extensions["store"]
            base = Path(folder) / "media"
            movie = base / "Movies" / "Coyote vs Acme (2026)" / "Coyote vs Acme (2026).mkv"
            movie.parent.mkdir(parents=True)
            movie.write_bytes(b"movie")
            movie.with_suffix(".nfo").write_text("Title           : Coyote vs Acme\n")
            self.assertTrue(fetch(store, base, movie.relative_to(base).as_posix(), "movie", {"name": "Coyote vs Acme", "year": 2026}, Client()))
            replaced = ET.fromstring(movie.with_suffix(".nfo").read_bytes())
            self.assertEqual(replaced.tag, "movie")
            kept = movie.with_suffix(".nfo").read_bytes()
            self.assertFalse(fetch(store, base, movie.relative_to(base).as_posix(), "movie", {"name": "Coyote vs Acme", "year": 2026}, Client()))
            self.assertEqual(movie.with_suffix(".nfo").read_bytes(), kept)

    def test_republish_keeps_the_saved_file(self) -> None:
        from backend.downloaders.torrent.metainfo import parse_torrent
        from backend.downloaders.torrent.storage import Storage
        from tests.torrentutil import build_torrent

        payload = b"0123456789abcdef"
        meta = parse_torrent(build_torrent([("Film 1999.mkv", payload)], piece_length=16))
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            storage = Storage(base, meta, {0})
            storage.prepare()
            self.assertTrue(storage.write_piece(0, payload))
            layout = {"profile": "emby", "movies": "Movies", "series": "TV Shows"}
            created = storage.publish_ready(layout)
            saved = base / created[0][1]
            data = saved.read_bytes()
            saved.unlink()
            saved.write_bytes(data)
            storage.published.clear()
            storage.publish_ready(layout)
            names = sorted(path.name for path in saved.parent.iterdir())
            self.assertEqual(names, [saved.name])

    def test_hover_lookup_matches_a_show_title(self) -> None:
        class Show:
            def get(self, path, **params):
                if path == "search/tv":
                    return {"results": [{"id": 9, "name": "MobLand", "first_air_date": "2025-03-30"}]}
                if path == "tv/9":
                    return {"id": 9, "name": "MobLand", "overview": "Two families.", "first_air_date": "2025-03-30", "vote_average": 8.5, "genres": [{"name": "Crime"}, {"name": "Drama"}], "credits": {"cast": [{"name": "Tom Hardy"}]}, "poster_path": "/poster.jpg"}
                return {}

        job = SimpleNamespace(
            selected={0},
            categories={},
            metadata_choice={},
            meta=SimpleNamespace(files=[SimpleNamespace(index=0, path="MobLand.S02E03.Bonzo.mkv", length=20)], name="MobLand"),
        )
        card = preview(None, job, client=Show())
        self.assertTrue(card["matched"])
        self.assertEqual(card["title"], "MobLand")
        self.assertEqual(card["year"], "2025")
        self.assertEqual(card["genres"], ["Crime", "Drama"])
        self.assertEqual(card["cast"], ["Tom Hardy"])
        self.assertEqual(card["rating"], 8.5)

    def test_premiere_year_matches_the_show_not_a_movie(self) -> None:
        class Show:
            def get(self, path, **params):
                if path == "search/tv":
                    return {"results": [
                        {"id": 1, "name": "South Park", "first_air_date": "1997-08-13"},
                        {"id": 2, "name": "South Park", "first_air_date": "2011-01-01"},
                    ]}
                if path == "tv/1":
                    return {"id": 1, "name": "South Park", "overview": "Four boys.", "first_air_date": "1997-08-13", "vote_average": 8.4, "genres": [{"name": "Animation"}], "credits": {"cast": [{"name": "Trey Parker"}]}, "poster_path": "/p.jpg"}
                return {}

        job = SimpleNamespace(
            selected={0},
            categories={},
            metadata_choice={},
            save_as="South Park (1997)",
            meta=SimpleNamespace(files=[SimpleNamespace(index=0, path="South.Park.S28E01.mkv", length=20)], name="South Park"),
        )
        card = preview(None, job, client=Show())
        self.assertTrue(card["matched"])
        self.assertEqual(card["media_type"], "tv")
        self.assertEqual(card["title"], "South Park")
        self.assertEqual(card["year"], "1997")

    def test_scanned_title_lookup_uses_the_show(self) -> None:
        class Show:
            def get(self, path, **params):
                if path == "search/tv":
                    self.query = params.get("query")
                    return {"results": [{"id": 4, "name": "Rick and Morty", "first_air_date": "2013-12-02"}]}
                if path == "tv/4":
                    return {"id": 4, "name": "Rick and Morty", "overview": "Adventures.", "first_air_date": "2013-12-02", "vote_average": 8.7, "genres": [{"name": "Animation"}], "credits": {"cast": [{"name": "Justin Roiland"}]}, "poster_path": "/r.jpg"}
                return {}

        client = Show()
        card = scan_lookup(None, "Rick.and.Morty.S09E08.1080p.WEB.h264-EDITH.mkv", client=client)
        self.assertTrue(card["matched"])
        self.assertEqual(card["media_type"], "tv")
        self.assertEqual(card["title"], "Rick and Morty")
        self.assertEqual(client.query, "Rick and Morty")

    def test_found_show_suggests_title_and_premiere_year(self) -> None:
        class Show:
            def get(self, path, **params):
                if path == "search/tv":
                    return {"results": [{"id": 11, "name": "Cupertino", "first_air_date": "2026-01-01"}]}
                if path == "tv/11":
                    return {"id": 11, "name": "Cupertino", "overview": "A lawyer.", "first_air_date": "2026-03-01", "vote_average": 5.5, "genres": [{"name": "Drama"}], "credits": {"cast": [{"name": "Mike Colter"}]}, "poster_path": "/c.jpg"}
                return {}

        card = scan_lookup(None, "Cupertino.S01E01.1080p.HEVC.x265-MeGusta.mkv", client=Show())
        self.assertEqual(matched_save_as(card), "Cupertino (2026)")
        self.assertEqual(matched_save_as({"matched": False, "title": "Cupertino", "year": "2026"}), "")

    def test_a_corrected_title_renames_the_file_and_the_activity_name(self) -> None:
        class Store:
            def get(self, key):
                return "token" if key == "m3u_tmdb_token" else ""

        class Film:
            def get(self, path, **params):
                return {"id": 42, "title": "Jackass 3D", "release_date": "2010-10-15"}

        root = Path(tempfile.mkdtemp())
        folder = root / "Movies" / "Jackass 3 (2010)"
        folder.mkdir(parents=True)
        (folder / "Jackass 3 (2010).mkv").write_bytes(b"video")
        (folder / "Jackass 3 (2010).nfo").write_text("old", encoding="utf-8")
        (folder / "Jackass 3 (2010)-poster.jpg").write_bytes(b"art")
        (folder / "poster.jpg").write_bytes(b"jpg")
        saved = []
        job = SimpleNamespace(
            save_as="Jackass 3 (2010)",
            selected={0},
            categories={0: "movie"},
            metadata_choice={},
            metadata_done=set(),
            metadata_started=set(),
            meta=SimpleNamespace(name="release", files=[SimpleNamespace(index=0, path="Jackass.3.2010.mkv", length=5)]),
            storage=SimpleNamespace(published={0: "Movies/Jackass 3 (2010)/Jackass 3 (2010).mkv"}),
            engine=SimpleNamespace(
                media_base=root,
                library_layout=lambda: {"profile": "emby", "movies": "Movies", "series": "TV Shows"},
                _save_job=lambda item: saved.append(item.save_as),
            ),
        )
        with mock.patch("backend.downloaders.torrent.metadata.TMDB", return_value=Film()), mock.patch("backend.downloaders.torrent.metadata.fetch", return_value=False):
            apply_match(Store(), root, job, 0, "tmdb", {"media_type": "movie", "tmdb_id": 42})
        self.assertEqual(job.save_as, "Jackass 3D (2010)")
        renamed = root / "Movies" / "Jackass 3D (2010)"
        self.assertEqual((renamed / "Jackass 3D (2010).mkv").read_bytes(), b"video")
        self.assertTrue((renamed / "Jackass 3D (2010).nfo").is_file())
        self.assertEqual((renamed / "Jackass 3D (2010)-poster.jpg").read_bytes(), b"art")
        self.assertEqual((renamed / "poster.jpg").read_bytes(), b"jpg")
        self.assertFalse(folder.exists())
        self.assertEqual(job.storage.published[0], "Movies/Jackass 3D (2010)/Jackass 3D (2010).mkv")
        occupied = root / "Movies" / "Taken (2008)"
        occupied.mkdir()
        (occupied / "Taken (2008).mkv").write_bytes(b"other")
        job.storage.published[0] = "Movies/Jackass 3D (2010)/Jackass 3D (2010).mkv"
        job.categories[0] = "movie"
        with mock.patch("backend.downloaders.torrent.metadata.TMDB", return_value=type("Taken", (), {"get": staticmethod(lambda path, **params: {"id": 7, "title": "Taken", "release_date": "2008-01-01"})})()), self.assertRaises(ValueError):
            apply_match(Store(), root, job, 0, "tmdb", {"media_type": "movie", "tmdb_id": 7})
        self.assertTrue((renamed / "Jackass 3D (2010).mkv").is_file())
        self.assertEqual(job.save_as, "Jackass 3D (2010)")


if __name__ == "__main__":
    unittest.main()

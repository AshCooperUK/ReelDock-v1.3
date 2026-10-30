import unittest
from types import SimpleNamespace

from backend.downloaders.torrent.media import gate
from backend.downloaders.torrent.metainfo import parse_torrent
from backend.downloaders.torrent.rename import align_existing_folder, companion_path, library_path, named_info, parse_save_as, release_save_as, suggest_download, suggested_title
from backend.downloaders.torrent.webseed import file_urls

from tests.torrentutil import build_torrent


def _video(path: str):
    return SimpleNamespace(kind="video", selectable=True, size=10, path=path)


class MediaGateTests(unittest.TestCase):
    def test_manual_episode_separators_do_not_become_the_series_title(self):
        source='law.and.order.svu.s28e02.1080p.web.h264-cakes.mkv'
        for label in ('law and order svu - S28-E02','law and order svu - S28 E02','law and order svu S28E02','law and order svu - s28-e02'):
            with self.subTest(label=label):
                parsed=parse_save_as(label)
                self.assertEqual((parsed['name'],parsed['season'],parsed['episode']),('law and order svu',28,2))
                described=named_info(source,label)
                self.assertEqual((described['name'],described['season'],described['episode']),('law and order svu',28,2))
                relative=library_path('',source,'video','series',title=label)
                self.assertEqual(relative,'TV Shows/law and order svu/Season 28/law and order svu s28e02.mkv')
                roundtrip=parse_save_as(relative.rsplit('/',1)[-1][:-4])
                self.assertEqual((roundtrip['name'],roundtrip['season'],roundtrip['episode']),('law and order svu',28,2))
        self.assertEqual(parse_save_as('Show S01E02E03 - Two episodes')['extra'],'E03')
        self.assertEqual(parse_save_as('Show S01E02E03 - Two episodes')['episode_title'],'Two episodes')
        movie=parse_save_as('Spider-Man (2002)')
        self.assertEqual((movie['name'],movie['year'],movie['season'],movie['episode']),('Spider-Man',2002,None,None))
        unsafe=library_path('',source,'video','series',title='../../Show S28-E02 - ../Other/Title')
        self.assertTrue(unsafe.startswith('TV Shows/'))
        self.assertNotIn('..',unsafe.split('/'))

    def test_rejects_archives_executables_and_documents(self) -> None:
        files = [
            (0, "readme.pdf", 10),
            (1, "installer.exe", 10),
            (2, "disk.iso", 10),
            (3, "pack.zip", 10),
            (4, "notes.txt", 10),
            (5, "movie.mp4.exe", 10),
        ]
        result = gate(files)
        self.assertFalse(result.accepted)
        self.assertTrue(all(not item.selectable for item in result.files))
        self.assertIn("Nothing will be downloaded", result.reason)

    def test_subtitles_and_artwork_need_media(self) -> None:
        result = gate([(0, "only.srt", 4), (1, "poster.jpg", 4)])
        self.assertFalse(result.accepted)
        self.assertTrue(all(item.kind in {"subtitle", "artwork"} for item in result.files))

    def test_selects_media_and_blocks_the_rest(self) -> None:
        blob = build_torrent(
            [
                ("Big Buck Bunny.mp4", b"v" * 20),
                ("Big Buck Bunny.en.srt", b"sub"),
                ("poster.jpg", b"jpg"),
                ("movie.nfo", b"<movie/>"),
                ("note.exe", b"MZ"),
                ("extra/../escape.mp4", b"nope"),
            ],
            name="Big Buck Bunny",
        )
        meta = parse_torrent(blob)
        result = gate([(item.index, item.path, item.length) for item in meta.files])
        self.assertTrue(result.accepted)
        by_path = {item.path: item for item in result.files}
        self.assertTrue(by_path["Big Buck Bunny.mp4"].selected)
        self.assertEqual(by_path["Big Buck Bunny.mp4"].kind, "video")
        self.assertTrue(by_path["Big Buck Bunny.en.srt"].selectable)
        self.assertTrue(by_path["poster.jpg"].selectable)
        self.assertTrue(by_path["movie.nfo"].selectable)
        self.assertEqual(by_path["movie.nfo"].kind, "nfo")
        self.assertFalse(by_path["note.exe"].selectable)
        self.assertIn(".exe", by_path["note.exe"].reason)
        self.assertFalse(by_path["extra/../escape.mp4"].selectable)

    def test_rename_and_web_seed_url(self) -> None:
        self.assertEqual(
            library_path("Show Name", "Show.Name.S01E02.The.Pilot.mkv", "video"),
            "TV Shows/Show Name/Season 1/Show Name s01e02.mkv",
        )
        self.assertEqual(
            library_path("Big Buck Bunny", "Big Buck Bunny 2008.mp4", "video"),
            "Movies/Big Buck Bunny (2008)/Big Buck Bunny (2008).mp4",
        )
        self.assertIsNone(library_path("Big Buck Bunny", "Big Buck Bunny.mp4", "video"))
        self.assertEqual(library_path("Song", "track.mp3", "audio"), "Downloads/Torrents/Audio/track.mp3")
        self.assertEqual(release_save_as("Bad.Counselors.2026.1080p.WEBRip.10Bit.DDP5.1.x265-NeoNoir"), "Bad Counselors (2026)")
        self.assertEqual(release_save_as("Bad Counselors (2026)"), "Bad Counselors (2026)")
        self.assertEqual(
            library_path("discover", "7d71102530d5195f09f1a16d7609f8c9.mkv", "video", title="Bad Counselors (2026)"),
            "Movies/Bad Counselors (2026)/Bad Counselors (2026).mkv",
        )

    def test_save_as_renames_movie_companions_before_metadata(self) -> None:
        video = "Mutiny.2026.1080p.WEBRip.x264.AAC5.1-LAMA.mp4"
        self.assertEqual(suggested_title(video), "Mutiny (2026)")
        saved = library_path("Mutiny", video, "video", title="Mutiny (2026)")
        self.assertEqual(saved, "Movies/Mutiny (2026)/Mutiny (2026).mp4")
        self.assertEqual(
            companion_path(saved, "Mutiny.2026.1080p.WEBRip.x264.AAC5.1-LAMA.mp4.nfo"),
            "Movies/Mutiny (2026)/Mutiny (2026).nfo",
        )
        self.assertEqual(
            companion_path(saved, "Mutiny.2026.1080p.WEBRip.x264.AAC5.1-LAMA.srt"),
            "Movies/Mutiny (2026)/Mutiny (2026).srt",
        )
        info = named_info(video, "Mutiny (2026)")
        self.assertEqual(info["name"], "Mutiny")
        self.assertEqual(info["year"], 2026)
        edited = library_path("Mutiny", video, "video", title="Other Film (1999)")
        self.assertEqual(edited, "Movies/Other Film (1999)/Other Film (1999).mp4")

    def test_save_as_uses_show_name_and_season(self) -> None:
        video = "Show.Name.S01E02.The.Pilot.mkv"
        self.assertEqual(suggested_title(video), "Show Name Season 1")
        self.assertEqual(
            library_path("Show", video, "video", title="Show Name Season 1"),
            "TV Shows/Show Name/Season 1/Show Name s01e02.mkv",
        )
        info = named_info(video, "Show Name Season 1")
        self.assertEqual(info["name"], "Show Name")
        self.assertEqual(info["season"], 1)
        self.assertEqual(info["episode"], 2)

    def test_save_as_episode_code_keeps_an_obfuscated_file_as_tv(self) -> None:
        hashed = "2VyKNP2062U1QFOXzgerWXUPhioWQc9.mkv"
        title = "Judy Justice (2021) S04E110"
        info = named_info(hashed, title)
        self.assertEqual(info["name"], "Judy Justice")
        self.assertEqual(info["year"], 2021)
        self.assertEqual(info["kind"], "series")
        self.assertEqual((info["season"], info["episode"]), (4, 110))
        self.assertEqual(
            library_path("release", hashed, "video", title=title),
            "TV Shows/Judy Justice (2021)/Season 4/Judy Justice (2021) s04e110.mkv",
        )

    def test_premiere_year_keeps_a_show_as_tv(self) -> None:
        video = "South.Park.S28E01.mkv"
        info = named_info(video, "South Park (1997)")
        self.assertEqual(info["name"], "South Park")
        self.assertEqual(info["year"], 1997)
        self.assertEqual(info["kind"], "series")
        self.assertEqual(info["season"], 28)
        self.assertEqual(info["episode"], 1)
        self.assertEqual(
            library_path("South Park", video, "video", title="South Park (1997)"),
            "TV Shows/South Park (1997)/Season 28/South Park (1997) s28e01.mkv",
        )

    def test_suggestion_reuses_a_similar_library_folder(self) -> None:
        import tempfile
        from pathlib import Path

        video = "Futurama.S11E10.FiNAL.MULTi.1080p.WEB.H264-UKDTV.mkv"
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base / "TV Shows" / "Futurama (1999)" / "Season 11").mkdir(parents=True)
            (base / "TV Shows" / "Future Man").mkdir()
            (base / "Movies" / "Spider-Man Far from Home (2019)").mkdir(parents=True)
            (base / "Movies" / "Coyote vs Acme (2026)").mkdir()
            (base / "Movies" / "Coyote vs Acme (1999)").mkdir()
            show = suggest_download([_video(video)], base, {"series": "TV Shows", "movies": "Movies"})
            self.assertEqual(show["name"], "Futurama (1999)")
            self.assertEqual(show["folder"], "TV Shows/Futurama (1999)")
            self.assertEqual(
                library_path("Futurama", video, "video", title=show["name"]),
                "TV Shows/Futurama (1999)/Season 11/Futurama (1999) s11e10.mkv",
            )
            info = named_info(video, show["name"])
            self.assertEqual(info["name"], "Futurama")
            self.assertEqual(info["year"], 1999)
            self.assertEqual(info["season"], 11)
            movie = suggest_download([_video("Spider-Man.Far.from.Home.2019.1080p.mkv")], base, {"movies": "Movies"})
            self.assertEqual(movie["name"], "Spider-Man Far from Home (2019)")
            self.assertEqual(movie["folder"], "Movies/Spider-Man Far from Home (2019)")
            ambiguous = suggest_download([_video("Coyote.vs.Acme.2024.mkv")], base, {"movies": "Movies"})
            self.assertEqual(ambiguous["folder"], "")
            plain = align_existing_folder(base, "TV Shows/futurama/Season 11/futurama s11e10.mkv")
            self.assertEqual(plain, "TV Shows/Futurama (1999)/Season 11/Futurama (1999) s11e10.mkv")
        blob = build_torrent(
            [("Big Buck Bunny.mp4", b"v"), ("Big Buck Bunny.en.srt", b"s")],
            name="Big Buck Bunny",
            url_list=["https://webtorrent.io/torrents/"],
        )
        meta = parse_torrent(blob)
        urls = file_urls(meta, meta.files[0])
        self.assertEqual(
            urls,
            ["https://webtorrent.io/torrents/Big%20Buck%20Bunny/Big%20Buck%20Bunny.mp4"],
        )

    def test_split_movies_use_emby_cd_names(self) -> None:
        from backend.downloaders.torrent.rename import stack_split_movies

        stacked = stack_split_movies([
            (0, "ils-maskdc-cd2.avi", "Movies/Mask (1985)/Mask (1985).avi"),
            (1, "ils-maskdc-cd1.avi", "Movies/Mask (1985)/Mask (1985).avi"),
            (2, "Show s01e01.mkv", "TV Shows/Show/Season 1/Show s01e01.mkv"),
            (3, "Show s01e02.mkv", "TV Shows/Show/Season 1/Show s01e02.mkv"),
            (4, "Only.mkv", "Movies/Only (1999)/Only (1999).mkv"),
        ])
        self.assertEqual(stacked[1], "Movies/Mask (1985)/Mask (1985)-cd1.avi")
        self.assertEqual(stacked[0], "Movies/Mask (1985)/Mask (1985)-cd2.avi")
        self.assertNotIn(2, stacked)
        self.assertNotIn(3, stacked)
        self.assertNotIn(4, stacked)


if __name__ == "__main__":
    unittest.main()

"""Fix match reports real saved and original names without predicting paths."""
import unittest
from types import SimpleNamespace

from backend.downloaders.torrent.metadata import match_state


def job(path="Release.Name.2020.1080p/Movie.2020.mkv", published=None, storage=True, name="Release.Name.2020.1080p", save_as=""):
    return SimpleNamespace(
        meta=SimpleNamespace(name=name, files=[SimpleNamespace(index=0, path=path)]),
        storage=SimpleNamespace(published=published or {}) if storage else None,
        metadata_choice={}, categories={}, save_as=save_as,
    )


class Store:
    def get(self, key, default=None):
        return default


class MatchStateNameTests(unittest.TestCase):
    def test_original_only(self):
        state = match_state(Store(), job(), 0)
        self.assertEqual(state["filename"], "Movie.2020.mkv")
        self.assertEqual(state["original_filename"], "Movie.2020.mkv")
        self.assertEqual(state["source_name"], "Release.Name.2020.1080p")

    def test_renamed_published_mapping(self):
        state = match_state(Store(), job(published={0: "Movies/Right Title (2020)/Right Title (2020).mkv"}), 0)
        self.assertEqual(state["filename"], "Right Title (2020).mkv")
        self.assertEqual(state["original_filename"], "Movie.2020.mkv")
        self.assertNotIn("Movies", "".join(str(v) for k, v in state.items() if k.endswith("name")))

    def test_storage_none(self):
        state = match_state(Store(), job(storage=False), 0)
        self.assertEqual(state["filename"], "Movie.2020.mkv")

    def test_source_name_unchanged_by_save_as(self):
        state = match_state(Store(), job(save_as="Altered Name"), 0)
        self.assertEqual(state["source_name"], "Release.Name.2020.1080p")
        self.assertEqual(state["original_filename"], "Movie.2020.mkv")

    def test_internal_placeholder_source_names_are_hidden(self):
        for name in ('discover', 'Discover', 'discover.nzb', 'DISCOVER.NZB'):
            state = match_state(Store(), job(name=name), 0)
            self.assertEqual(state['source_name'], '')
            self.assertEqual(state['original_filename'], 'Movie.2020.mkv')
        self.assertEqual(match_state(Store(), job(name='discover.2020.1080p'), 0)['source_name'], 'discover.2020.1080p')

    def test_missing_source_name_is_empty(self):
        self.assertEqual(match_state(Store(), job(name=None), 0)["source_name"], "")


if __name__ == "__main__":
    unittest.main()

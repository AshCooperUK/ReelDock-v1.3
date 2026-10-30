"""Two completed episodes must safely share their generic show metadata."""
import os
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from backend.storage import Store
from backend import m3u_sidecars
from backend.downloaders.torrent.metadata import write_basic


class BasicMetadataConcurrencyTests(unittest.TestCase):
    def test_two_parallel_episode_writers_keep_both_nfos_and_one_shared_show(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root/'data')
            base = root/'media'
            season = base/'TV Shows'/'Example'/'Season 01'
            season.mkdir(parents=True)
            targets = [season/f'Example S01E{episode:02d}.mkv' for episode in (1, 2)]
            originals = [b'unchanged completed episode one', b'unchanged completed episode two']
            for target, raw in zip(targets, originals): target.write_bytes(raw)
            m3u_sidecars.initialise(store)
            first_link, another_link, second_attempt, release = (threading.Event() for _ in range(4))
            guard = threading.Lock()
            original_link = os.link
            shared_calls = 0
            errors, finished = [], []

            def held_link(source, target, *args, **kwargs):
                nonlocal shared_calls
                if Path(target).name == 'tvshow.nfo':
                    with guard:
                        shared_calls += 1
                        ordinal = shared_calls
                    (first_link if ordinal == 1 else another_link).set()
                    if not release.wait(3): raise AssertionError('The test did not release the shared-show publication')
                return original_link(source, target, *args, **kwargs)

            def writer(episode):
                if episode == 2: second_attempt.set()
                try:
                    finished.append(write_basic(store, base, str(targets[episode-1].relative_to(base)),
                                                'series', {'name': 'Example', 'season': 1, 'episode': episode}))
                except BaseException as error:
                    errors.append(error)

            threads = [threading.Thread(target=writer, args=(episode,), daemon=True) for episode in (1, 2)]
            with patch('os.link', side_effect=held_link):
                threads[0].start()
                try:
                    self.assertTrue(first_link.wait(2), 'the first real writer reaches shared-show publication')
                    threads[1].start()
                    self.assertTrue(second_attempt.wait(2))
                    # Before serialization both writers reach the absent file;
                    # after serialization the second waits outside this window.
                    another_link.wait(.2)
                finally:
                    release.set()
                    for thread in threads:
                        if thread.ident is not None: thread.join(4)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(errors, [], 'a shared-show creation race cannot abort either episode sidecar')
            self.assertEqual(finished, [True, True])
            show = ET.parse(season.parent/'tvshow.nfo').getroot()
            self.assertEqual(show.tag, 'tvshow')
            self.assertEqual(show.findtext('title'), 'Example')
            for episode, target, raw in zip((1, 2), targets, originals):
                data = ET.parse(target.with_suffix('.nfo')).getroot()
                self.assertEqual(data.tag, 'episodedetails')
                self.assertEqual(data.findtext('showtitle'), 'Example')
                self.assertEqual(data.findtext('season'), '1')
                self.assertEqual(data.findtext('episode'), str(episode))
                self.assertEqual(target.read_bytes(), raw, 'sidecar concurrency never changes published video bytes')
            self.assertFalse(list(base.rglob('.reeldock-*.tmp')))


if __name__ == '__main__': unittest.main()

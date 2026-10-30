"""Retry naming must not accumulate catalogue suffixes."""
import json
import unittest
from backend.discover_acquire import library_name
from backend.downloaders.torrent.rename import release_save_as, named_info, library_path, companion_path

class DiscoverTitleTests(unittest.TestCase):
    def test_movie_retry_title_is_idempotent(self):
        row={'kind':'movie','title':'Speed','body':json.dumps({'year':'1994'})}
        for _ in range(8):
            row['title']=library_name(row,'Speed.1994.1080p.BluRay.mkv')
            self.assertEqual(row['title'],'Speed (1994)')

    def test_existing_duplicate_titles_are_repaired_for_lookup(self):
        for title in ('Speed (1994) (1994)','Speed (1994) (1994) (1994)'):
            clean=release_save_as(title)
            self.assertEqual(clean,'Speed (1994)')
            info=named_info('obfuscated.mkv',clean)
            self.assertEqual(info['name'],'Speed')
            self.assertEqual(info['year'],1994)

    def test_numeric_titles_and_other_parentheses_are_preserved(self):
        for title in ('1917 (2019)','1984 (1984)','Film (Part One) (1994)'):
            self.assertEqual(release_save_as(title),title)
            self.assertEqual(library_name({'kind':'movie','title':title,'body':'{}'},''),title)

    def test_episode_retry_does_not_repeat_year_or_episode(self):
        row={'kind':'episode','title':'Example (2020) S01E02','body':json.dumps({'year':'2020','episode':{'season':1,'number':2}})}
        for _ in range(3):
            row['title']=library_name(row,'Example.2020.S01E02.1080p.mkv')
            self.assertEqual(row['title'],'Example (2020) S01E02')

    def test_episode_title_survives_retries_and_publication(self):
        title = "It's Always Sunny in Philadelphia"
        episode = "Frank's Team: A Dark Day for Baseball"
        row = {'kind': 'episode', 'title': title, 'body': json.dumps({'year': '2005', 'episode': {'season':18, 'number':9, 'name':episode}})}
        expected = title + ' (2005) S18E09 - ' + episode
        for _ in range(3):
            row['title'] = library_name(row, 'obfuscated.mkv')
            self.assertEqual(row['title'], expected)
            self.assertEqual(release_save_as(row['title']), expected)
        path = library_path('release', 'obfuscated.mkv', 'video', None, 'emby', {'series':'TV Shows', 'movies':'Movies'}, row['title'])
        self.assertEqual(path, "TV Shows/" + title + " (2005)/Season 18/" + title + " (2005) s18e09 - Frank's Team- A Dark Day for Baseball.mkv")
        self.assertEqual(companion_path(path, 'original.nfo'), path[:-4] + '.nfo')
        info = named_info('obfuscated.mkv', expected)
        self.assertEqual(info['name'], title)
        self.assertEqual(info['episode'], 9)

    def test_episode_title_cannot_create_extra_directories(self):
        path = library_path('release', 'video.mkv', 'video', None, 'emby', {'series':'TV Shows'}, 'Show (2005) S01E02 - ../Other/Title')
        self.assertEqual(len(path.split('/')), 4)
        self.assertNotIn('/../', path)

    def test_emby_discover_episode_directly_in_show_folder(self):
        title = "It's Always Sunny in Philadelphia (2005) S18E09 - Frank's Team- A Dark Day for Baseball"
        path = library_path('release', 'video.mkv', 'video', None, 'emby', {'series':'TV Shows'}, title, flat_episodes=True)
        self.assertEqual(path, "TV Shows/It's Always Sunny in Philadelphia (2005)/It's Always Sunny in Philadelphia (2005) - s18e09 - Frank's Team- A Dark Day for Baseball.mkv")
        self.assertEqual(companion_path(path, 'original.nfo'), path[:-4] + '.nfo')
        self.assertNotIn('/Season ', path)

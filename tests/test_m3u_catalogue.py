import json
import unittest
from unittest.mock import patch
from tests.test_m3u_player import PlayerTests,ORIGIN
from backend import m3u,m3u_catalogue as catalogue

class CatalogueTests(unittest.TestCase):
    setUp=PlayerTests.setUp
    tearDown=PlayerTests.tearDown
    get=PlayerTests.get
    def test_disappeared_tmdb_match_is_unmatched_instead_of_crashing(self):
        from unittest.mock import Mock
        client=Mock()
        client.entity.return_value={'id':123}
        client.get.return_value={}
        self.assertEqual(catalogue.resolve(client,{'kind':'movies','identity':'tt123','provider':'imdb'}),{})

    def test_release_rating_added_and_random_before_pagination(self):
        catalogue.index(self.store)
        with self.store.connect() as db:
            rows=db.execute('SELECT * FROM m3u_catalog_entries ORDER BY path').fetchall()
            db.execute('UPDATE m3u_catalog_entries SET release_date=?,added=? WHERE path=?',('2026-09-20',9999999999,rows[-1]['path']))
        self.assertEqual(self.get('entries',sort='release').json['items'][0]['path'],rows[-1]['path'])
        self.assertEqual(self.get('entries',sort='added').json['items'][0]['path'],rows[-1]['path'])
        first=self.get('entries',sort='random',kind='movies',seed='abc').json['items'];second=self.get('entries',sort='random',kind='movies',seed='abc',offset=50).json['items']
        self.assertEqual(len({r['path'] for r in first+second}),65)
        self.assertEqual(first,self.get('entries',sort='random',kind='movies',seed='abc').json['items'])
        self.assertNotEqual(first,self.get('entries',sort='random',kind='movies',seed='other').json['items'])

    def test_home_movies_by_release_and_shows_by_latest_added_episode(self):
        catalogue.index(self.store)
        with self.store.connect() as db:
            rows=db.execute('SELECT * FROM m3u_catalog_entries ORDER BY path LIMIT 15').fetchall()
            for i,row in enumerate(rows):
                key='test'+str(i)
                # Last entries are duplicate episodes of one show and a future release.
                movie=i<8;identity=i if i<13 else 12
                release='2099-01-01' if i in (0,14) else f'2025-01-{i+1:02d}'
                data=dict(tmdb_id=identity,media_type='movie' if movie else 'tv',title=str(i),release_date=release,rating=8,artwork='/api/m3u-artwork/a.jpg')
                db.execute('INSERT INTO m3u_catalog_titles(key,info,data) VALUES(?,?,?)',(key,'{}',json.dumps(data)))
                db.execute('UPDATE m3u_catalog_entries SET key=?,added=? WHERE source=? AND path=?',(key,2000 if i==14 else 999-i,row['source'],row['path']))
                db.execute('UPDATE m3u_files SET kind=? WHERE source=? AND path=?',('movies' if movie else 'series',row['source'],row['path']))
        shelves=catalogue.shelves(self.store)
        self.assertEqual(len(shelves['movies']),6)
        self.assertEqual([x['release_date'] for x in shelves['movies']],['2099-01-01','2025-01-08','2025-01-07','2025-01-06','2025-01-05','2025-01-04'])
        self.assertEqual(len(shelves['shows']),5)
        self.assertEqual([x['title'] for x in shelves['shows']],['14','8','9','10','11'])
        self.assertEqual(shelves['shows'][0]['release_date'],'2099-01-01')
        self.assertEqual(shelves['shows'][0]['path'],rows[14]['path'])
        self.assertEqual(self.app.test_client().get('/api/m3u-highlights',base_url=ORIGIN).status_code,401)

    def test_browsing_does_not_rebuild_an_up_to_date_catalogue(self):
        # Imports catalogue their files straight away (setUp ran one).
        with self.store.connect() as db:
            self.assertGreater(db.execute('SELECT count(*) FROM m3u_catalog_entries').fetchone()[0],0)
            db.execute('DELETE FROM m3u_catalog_entries')
        # Files the worker has not catalogued yet are indexed on first browse.
        with patch('backend.m3u_catalogue.index',wraps=catalogue.index) as index:
            self.assertEqual(self.get('entries').status_code,200)
            self.assertEqual(index.call_count,1)
            self.get('entries');self.get('entries',sort='random')
            self.assertEqual(index.call_count,1)
        self.assertFalse(catalogue.index(self.store))

    def test_home_shelves_are_cached_until_the_catalogue_changes(self):
        catalogue.index(self.store)
        client=self.client
        with patch('backend.m3u_catalogue.shelves',wraps=catalogue.shelves) as shelves:
            first=client.get('/api/m3u-highlights',base_url=ORIGIN).json
            client.get('/api/m3u-highlights',base_url=ORIGIN)
            self.assertEqual(shelves.call_count,1)
            with self.store.connect() as db:db.execute("UPDATE m3u_catalog_titles SET checked=1")
            # Pending count stays live even when shelves come from the cache.
            self.assertEqual(client.get('/api/m3u-highlights',base_url=ORIGIN).json['pending'],0)
            self.assertGreater(first['pending'],0)
            self.assertEqual(shelves.call_count,1)
            with self.store.connect() as db:db.execute("DELETE FROM m3u_catalog_entries WHERE rowid IN (SELECT rowid FROM m3u_catalog_entries LIMIT 1)")
            self.assertTrue(catalogue.index(self.store))
            client.get('/api/m3u-highlights',base_url=ORIGIN)
            self.assertEqual(shelves.call_count,2)

    def test_existing_catalogue_rebuilds_genre_groups_into_show_names(self):
        text = '#EXTM3U\n#EXTINF:0 tvg-type="series" group-title="Drama",The Show S01E01\nhttps://example.com/series/1\n'
        m3u.request_sync(self.store, self.source)
        m3u.tick(self.store, self.root, fetch=lambda url: text)
        self.store.set('m3u_show_identity', '')
        with patch('backend.m3u_naming.series_name', side_effect=lambda name, group: group or name):
            catalogue.index(self.store)
        with self.store.connect() as db:
            names = [json.loads(r['info']).get('name') for r in db.execute(
                "SELECT t.info FROM m3u_catalog_titles t JOIN m3u_catalog_entries c ON c.key=t.key JOIN m3u_files f ON f.source=c.source AND f.path=c.path WHERE f.kind='series'")]
        self.assertEqual(names, ['Drama'])
        self.store.set('m3u_show_identity', '')
        catalogue.index(self.store)
        with self.store.connect() as db:
            names = [json.loads(r['info']).get('name') for r in db.execute(
                "SELECT t.info FROM m3u_catalog_titles t JOIN m3u_catalog_entries c ON c.key=t.key JOIN m3u_files f ON f.source=c.source AND f.path=c.path WHERE f.kind='series'")]
        self.assertEqual(names, ['The Show'])

    def test_playlist_release_date_survives_import(self):
        options=dict(m3u.DEFAULTS,naming='emby')
        entries,_=m3u.parse('#EXTM3U\n#EXTINF:0 tvg-type="movie" release-date="2026-03-12",Film (2026)\nhttps://example.com/movie.mp4\n',options)
        self.assertEqual(entries[0]['attrs']['release-date'],'2026-03-12')
        self.assertEqual(catalogue.normal_date('2026-02-31'),'')

    def test_browser_playback_conversion_is_removed(self):
        url='/api/m3u/'+self.source+'/convert'
        self.assertEqual(self.client.post(url,base_url=ORIGIN,headers={'Origin':ORIGIN},json={}).status_code,404)
        self.assertEqual(self.get('playback',path=self.row['path']).status_code,404)

del PlayerTests

if __name__=='__main__':unittest.main()

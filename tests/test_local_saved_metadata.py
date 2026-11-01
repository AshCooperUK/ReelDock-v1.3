"""Completed exact ownership reuses saved facts without sidecars or HTTP."""
import json
import unittest
from unittest.mock import patch
import test_view_media as vm
from backend.view_media import local

class LocalSavedMetadataTests(unittest.TestCase):
    setUp=vm.ViewMediaTests.setUp
    tearDown=vm.ViewMediaTests.tearDown
    get=vm.ViewMediaTests.get

    def fixture(self, nfo=None, state='complete', body=None):
        path=self.root/'Movies'/'The Grudge (2004)'/'The Grudge (2004).mkv'
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'isolated video fixture')
        if nfo:path.with_suffix('.nfo').write_text(nfo)
        body=body or dict(externalId='00391198',year='2004',description='Saved indexer synopsis',rating='5.9',genre='Horror',image='https://api.nzbgeek.info/covers/movies/00391198-cover.jpg')
        with self.store.connect() as db:
            db.execute("INSERT INTO discover_requests(id,idempotency,kind,title,state,quality,fallback,body,episode_key,updated,created) VALUES('fixture','fixture','movie','The Grudge (2004)',?,'1080',0,?,'movie:00391198',100,100)",(state,json.dumps(body)))
            db.execute("INSERT INTO discover_owned VALUES('movie:00391198','fixture',?,'complete')",(str(path),))
        state={};local._scan(self.root,state);local._CACHE[str(self.root)]=state
        return path,state

    def test_missing_sidecars_reuse_saved_metadata_without_writes_or_network(self):
        path,state=self.fixture()
        self.store.set('m3u_share_metadata','false')
        before=path.stat();files=list(path.parent.iterdir())
        with self.store.connect() as db:sql_before=list(db.iterdump())
        with patch('requests.get',side_effect=AssertionError('no metadata HTTP')):
            result=local.library_dates(self.store,state,self.root)[0]
            response=self.get('/api/view-media?source=local&media=movies&sort=new')
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json['items'][0]['image'],result['image'])
        self.assertEqual(result['description'],'Saved indexer synopsis')
        self.assertEqual(result['rating'],'5.9');self.assertEqual(result['year'],'2004')
        self.assertIn('00391198-cover.jpg',result['image'])
        self.assertEqual(result['action'],state['items'][0]['action'])
        self.assertEqual(list(path.parent.iterdir()),files)
        self.assertEqual((path.stat().st_ino,path.stat().st_size,path.stat().st_mtime_ns),(before.st_ino,before.st_size,before.st_mtime_ns))
        with self.store.connect() as db:self.assertEqual(list(db.iterdump()),sql_before)

    def test_local_authored_fields_and_art_take_precedence(self):
        path,state=self.fixture('<movie><title>My film title</title><year>2004</year><plot>My plot</plot><rating>8</rating><imdbid>tt0391198</imdbid></movie>')
        (path.parent/'poster.jpg').write_bytes(b'local poster');local._scan(self.root,state)
        item=local.library_dates(self.store,state,self.root)[0]
        self.assertEqual(item['title'],'My film title');self.assertEqual(item['description'],'My plot');self.assertEqual(item['rating'],'8')
        self.assertTrue(item['image'].startswith('/api/view-media/local/'))

    def test_conflicting_local_identity_or_year_does_not_receive_saved_art(self):
        for xml in ('<movie><year>2006</year></movie>','<movie><imdbid>tt0123456</imdbid></movie>'):
            with self.subTest(xml=xml):
                if hasattr(self,'fixture_used'):
                    with self.store.connect() as db:db.execute('DELETE FROM discover_owned');db.execute('DELETE FROM discover_requests')
                path,state=self.fixture(xml);self.fixture_used=True
                self.assertFalse(local.library_dates(self.store,state,self.root)[0]['image'])

    def test_cancelled_request_or_wrong_external_identity_is_not_enriched(self):
        path,state=self.fixture(state='cancelled')
        self.assertFalse(local.library_dates(self.store,state,self.root)[0]['image'])
        with self.store.connect() as db:db.execute("UPDATE discover_requests SET state='complete',body=?",(json.dumps(dict(externalId='0123456',year='2004',image='https://api.nzbgeek.info/covers/other.jpg')),))
        self.assertFalse(local.library_dates(self.store,state,self.root)[0]['image'])

    def test_same_filename_without_exact_ownership_is_not_enriched(self):
        path,state=self.fixture()
        other=self.root/'Other'/'The Grudge (2004).mkv';other.parent.mkdir();other.write_bytes(b'unowned')
        local._scan(self.root,state)
        items=local.library_dates(self.store,state,self.root)
        self.assertEqual(sum(bool(i['image']) for i in items),1)

if __name__=='__main__':unittest.main()

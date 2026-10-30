import json
import tempfile
import time
import unittest

from backend.storage import Store
from backend.catalogue import initialise
from backend.channel_metadata_refresh import request
from backend.artwork import QUEUE, queue_times, cache_batch


class ViewedArtworkTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store=Store(self.temp.name)
        initialise(self.store)
        self.channel='UC'+'a'*22
        with self.store.connect() as db:
            db.execute('INSERT INTO channels VALUES(?,?,1)',(self.channel,json.dumps(dict(id=self.channel,banner='/artwork/'+'f'*64,avatar='/artwork/'+'0'*64))))
            for index in range(50):
                identity=f'{index:064x}'
                db.execute('INSERT INTO artwork(id,url) VALUES(?,?)',(identity,'https://yt3.googleusercontent.com/avatar/'+str(index)))
                if index:
                    db.execute('INSERT INTO channels VALUES(?,?,1)',('UC'+f'{index:022}',json.dumps({'avatar':'/artwork/'+identity})))
            db.execute('INSERT INTO artwork(id,url) VALUES(?,?)',('f'*64,'https://yt3.googleusercontent.com/selected-banner'))

    def test_viewed_banner_precedes_bulk_avatars_with_the_same_worker_limits(self):
        request(self.store,self.channel)
        fetched=[]
        cache_batch(self.store,fetch=lambda url:fetched.append(url) or b'\xff\xd8\xfffixture',limit=1,workers=1)
        self.assertEqual(fetched,['https://yt3.googleusercontent.com/selected-banner'])
        with self.store.connect() as db:
            pending=db.execute('SELECT count(*) FROM artwork WHERE checked=0').fetchone()[0]
        self.assertEqual(pending,50,'a priority banner changes ordering, not batch or worker caps')

    def test_priority_does_not_reset_failed_image_backoff_and_expires_normally(self):
        request(self.store,self.channel)
        with self.store.connect() as db:
            db.execute('UPDATE artwork SET checked=? WHERE id=?',(time.time()-6*86400,'f'*64))
            row=db.execute(QUEUE+'SELECT id FROM eligible WHERE checked<:stale ORDER BY priority,id LIMIT 1',queue_times()).fetchone()
            self.assertNotEqual(row['id'],'f'*64,'the existing failed-artwork retry deadline remains authoritative')
            db.execute('UPDATE channel_metadata_refresh_requests SET expires=0')
            selected=db.execute(QUEUE+'SELECT priority FROM eligible WHERE id=:identity',{**queue_times(),'identity':'f'*64}).fetchone()
            self.assertIsNone(selected,'an expired view does not keep banner priority alive')


if __name__=='__main__':unittest.main()

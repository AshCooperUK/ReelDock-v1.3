"""Crash handoff recovery retains the saved catalogue and metadata decisions.

All requests, native checkpoints, media, artwork and NFOs are disposable fixtures.
Recovery is exercised without provider fetches or constructing a new engine.
"""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from PIL import Image
import test_dashboard as dashboard
from backend import discover_acquire as acquire
from backend import discover_handoff as handoff
from backend import discover_publication as publication
from backend import discover_store as ds
from backend.discover_youtube import register_video
from backend.downloaders.nzb.engine import NzbEngine
from backend.media_library import reserve
from backend.storage import Store


RAW = b'<nzb><file subject="&quot;Catalogue.Show.S04E15.mkv&quot; yEnc (1/1)"><groups><group>fixture</group></groups><segments><segment bytes="3000" number="1">metadata-fixture@invalid</segment></segments></file></nzb>'
IMMUTABLE = ('id', 'idempotency', 'kind', 'title', 'quality', 'fallback', 'manual',
             'rule_id', 'episode_key', 'created', 'body')


class DiscoverHandoffMetadataTests(unittest.TestCase):
    def setUp(self):
        self.fixture = dashboard.DashboardTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.store = self.fixture.store
        self.app = self.fixture.app
        self.media = Path(self.fixture.tmp.name) / 'media'
        self.media.mkdir(exist_ok=True)
        self.store.set('storage_base', str(self.media))
        self.item = dict(id='tvmaze:show:71810', kind='show', title='Catalogue Show',
                         year='1999', externalId='440642',
                         image='https://static.tvmaze.com/uploads/images/fixture.jpg',
                         backdrop='https://static.tvmaze.com/uploads/images/backdrop.jpg',
                         description='Saved catalogue synopsis.', provider='tvmaze')
        self.body = dict(discoveryEngine=True, item=self.item, externalId='440642',
                         year='1999', episode=dict(season=4, number=15, name='Exact episode title'),
                         date='2003-10-13', edition=dict(title='Catalogue Show', year='1999',
                                                       country='GB', countryName='United Kingdom'),
                         catalogueEpisode=dict(id=8080, name='Exact episode title', season=4, number=15),
                         minimumMinutes=27, customSavedMetadata={'retain': ['raw', 12]})
        self.row, _ = ds.insert_request(self.store, dict(
            idempotency='metadata-handoff-fixture', kind='episode', title='Catalogue Show · S04E15',
            quality='2160', fallback=True, manual=True, state='searching',
            episode_key='show:440642:4:15', body=self.body))

    def current(self):
        with self.store.connect() as db:
            return dict(db.execute('SELECT * FROM discover_requests WHERE id=?', (self.row['id'],)).fetchone())

    def immutable(self, row):
        return {field: row[field] for field in IMMUTABLE}

    def fingerprints(self, paths):
        return {str(path.relative_to(self.media)): (path.stat().st_ino, path.read_bytes()) for path in paths}

    def native(self, *, completed=False, manual=False):
        spawn = patch.object(NzbEngine, '_spawn')
        spawn.start()
        self.addCleanup(spawn.stop)
        native = NzbEngine(self.media, self.store.directory / 'nzb-engine')
        self.addCleanup(native.shutdown)
        native.config.update(min_video_minutes=0, share_metadata=False)
        preview = native.preview_bytes(RAW, 'metadata-fixture.nzb', check_availability=False)
        generation = ds.claim_start(self.store, self.row['id'])
        receipt = handoff.prepare_nzb(self.store, self.row['id'], generation,
                                     native.preview_activity_key(preview['id'], None), 'fixture-release-id')
        provider, identity = acquire._catalogue_identity(self.current())
        with patch.object(native, '_servers', return_value=[{}]), patch.object(native, '_launch'):
            started = native.start(preview['id'], None, acquire.library_name(self.current(), ''),
                                   lookup_provider=provider, lookup_identity=identity,
                                   minimum_minutes=27, handoff_token=receipt['token'], job_id=receipt['job_id'])
        job = native._jobs[started['id']]
        job.metadata_choice = {0: dict(mode='manual', media_type='tv',
                                      data=dict(title='My exact override', tvdb_id=440642, year=1999,
                                                season=4, episode=15, overview='Keep selected metadata.'))
                               if manual else dict(mode='tmdb', media_type='tv', tmdb_id=7654,
                                                   data=dict(title='Matched catalogue show'))}
        job.metadata_done = {0}
        job.metadata_note = 'A saved manual note' if manual else 'Matched before restart'
        job.categories = {0: 'series'}
        job.user_paused = not completed
        paths = []
        if completed:
            video = native.work_dir / job.id / 'media' / 'Catalogue.Show.S04E15.mkv'
            video.parent.mkdir(parents=True)
            video.write_bytes(b'fixture-complete-video' * 200)
            # The bytes are a labelled metadata fixture, not an encoded video.
            # Duration admission is covered by the engine's duration tests.
            with patch.object(native, '_filter_videos', return_value=[video]):
                native._describe_media(job, [video])
            layout = native.library_layout()
            relative = native._primary_relative(job, layout, {'movies': layout['movies'], 'series': layout['series']})
            target = self.media / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(video.read_bytes())
            nfo = target.with_suffix('.nfo')
            nfo.write_bytes(b'<episodedetails><title>My custom episode</title><season>4</season><episode>15</episode><uniqueid type="tmdb">7654</uniqueid></episodedetails>')
            poster, fanart = target.parent / 'poster.jpg', target.parent / 'fanart.jpg'
            poster.write_bytes(b'custom poster kept exactly')
            fanart.write_bytes(b'custom fanart kept exactly')
            paths = [target, nfo, poster, fanart]
            job.storage.published[0] = relative
            job.done_parts = job.total_parts
            job.progress_stage = 'complete'
            job.phase = 'stopped'
        native._save_job(job)
        return native, job, paths

    def recover(self):
        # A new Store represents a process reopening only its durable state.
        reopened = Store(self.store.directory)
        with patch('backend.discover_sources.fetch', side_effect=AssertionError('Recovery fetched a provider')), \
             patch('backend.downloaders.torrent.metadata.schedule', side_effect=AssertionError('Recovery reapplied metadata')):
            handoff.recover(self.app, reopened)
        return self.current()

    def test_native_recovery_keeps_catalogue_identity_manual_choice_and_completed_sidecars(self):
        native, job, paths = self.native(completed=True, manual=True)
        checkpoint = native.state_dir / (job.id + '.json')
        saved = checkpoint.read_bytes()
        before = self.current()
        fingerprints = self.fingerprints(paths)
        planned = publication.planned_path(self.store, self.media, before)
        recovered = self.recover()
        self.assertEqual(self.immutable(recovered), self.immutable(before))
        self.assertEqual((recovered['engine'], recovered['engine_id']), ('nzb', job.id))
        self.assertEqual(checkpoint.read_bytes(), saved, 'adoption never rewrites a native checkpoint')
        self.assertEqual(self.fingerprints(paths), fingerprints, 'existing video, custom NFO and artwork stay byte/inode identical')
        self.assertEqual(publication.planned_path(self.store, self.media, recovered), planned)
        self.assertEqual(acquire._catalogue_identity(recovered), ('tvdb', '440642'))
        self.assertEqual(json.loads(recovered['body'])['episode'], self.body['episode'])
        self.assertIn('1999', planned)
        acquire.sync_jobs(self.app, self.store)
        self.assertEqual(self.current()['state'], 'complete')
        self.assertEqual(ds.owned(self.store, recovered['episode_key'])['path'], str(paths[0]))
        self.assertEqual(self.fingerprints(paths), fingerprints, 'ordinary completion bookkeeping retains sidecars and artwork')

    def test_active_native_recovery_keeps_tmdb_selection_done_markers_and_pause(self):
        native, job, _ = self.native()
        checkpoint = native.state_dir / (job.id + '.json')
        saved = checkpoint.read_bytes()
        before = self.current()
        recovered = self.recover()
        self.assertEqual(self.immutable(recovered), self.immutable(before))
        self.assertEqual((recovered['engine'], recovered['engine_id']), ('nzb', job.id))
        self.assertEqual(checkpoint.read_bytes(), saved)
        payload = json.loads(saved)
        self.assertEqual(payload['metadata_choice']['0']['mode'], 'tmdb')
        self.assertEqual(payload['metadata_choice']['0']['tmdb_id'], 7654)
        self.assertEqual(payload['metadata_done'], [0])
        self.assertEqual((payload['lookup_provider'], payload['lookup_identity']), ('tvdb', '440642'))
        self.assertEqual(payload['minimum_minutes'], 27)
        self.assertTrue(payload['user_paused'])

    def test_no_child_recovery_preserves_exact_manual_episode_selection_and_policy(self):
        manual = deepcopy(self.body)
        manual.update(manualTvmaze=71810, manualReleaseId='chosen-special-release', imdbId='tt1234567',
                      showTitle='Catalogue Show', releaseEpisode=dict(season=0, number=3),
                      catalogueEpisode=dict(id=99999, name='Hand selected special', season=None, number=None))
        with self.store.connect() as db:
            db.execute('UPDATE discover_requests SET body=? WHERE id=?',
                       (json.dumps(manual, indent=2), self.row['id']))
        ds.claim_start(self.store, self.row['id'])
        before = self.current()
        planned = acquire.library_name(before, '')
        recovered = self.recover()
        self.assertEqual(recovered['state'], 'searching')
        self.assertEqual(self.immutable(recovered), self.immutable(before))
        self.assertEqual(acquire.library_name(recovered, ''), planned)
        self.assertEqual(acquire._catalogue_identity(recovered), ('imdb', 'tt1234567'))

    def test_youtube_publication_after_atomic_attachment_uses_saved_edition_and_episode(self):
        from backend import discovery_engine as engine
        video = register_video(self.store, dict(videoId='abcdefghijk', title='Catalogue Show UK 1999 S04E15', published='2026-10-14'))
        public = dict(id=video['id'], snippet=dict(title=video['title'], channelId='UC' + 'z' * 22,
                                                channelTitle='Fixture official', publishedAt='2026-10-14'),
                      contentDetails=dict(duration='PT45M'))
        before = self.current()
        with patch('backend.youtube_discover._search', return_value={'items': [dict(videoId=video['id'], title=video['title'])]}), \
             patch('backend.catalogue.Importer') as importer:
            importer.return_value.api.return_value = {'items': [public]}
            self.assertTrue(engine.fallback_youtube(self.app, self.store, before, deepcopy(self.body)))
        attached = self.current()
        old_body = json.loads(before['body'])
        new_body = json.loads(attached['body'])
        self.assertEqual({key: new_body[key] for key in old_body}, old_body)
        self.assertEqual(new_body['videoId'], video['id'])
        self.assertEqual((attached['quality'], bool(attached['fallback'])), ('2160', True))
        with self.store.connect() as db:
            quality = db.execute('SELECT allow_lower FROM discover_quality_policy WHERE id=?', (video['id'],)).fetchone()
            self.assertEqual(quality[0], 1)
        record = reserve(self.store, self.media, video['id'])
        record['path'] = publication.claim(self.store, self.media, video['id'], attached)
        art = self.media / 'fixture-art.jpg'
        Image.new('RGB', (20, 20), 'purple').save(art)
        with patch('backend.m3u_player.cached_image_path', return_value=art):
            publication.write_metadata(self.store, self.media, record, attached)
        target = self.media / record['path']
        nfo = ET.parse(target.with_suffix('.nfo')).getroot()
        self.assertEqual((nfo.findtext('showtitle'), nfo.findtext('year'), nfo.findtext('season'), nfo.findtext('episode')),
                         ('Catalogue Show', '1999', '4', '15'))
        self.assertEqual(nfo.find("uniqueid[@type='tvdb']").text, '440642')
        self.assertEqual(nfo.find("uniqueid[@type='youtube']").text, video['id'])
        self.assertNotIn('2026', record['path'], 'the upload year cannot replace the saved show edition')
        self.assertTrue((target.parent / 'poster.jpg').is_file())
        self.assertTrue((target.parent / 'fanart.jpg').is_file())


if __name__ == '__main__':
    unittest.main()

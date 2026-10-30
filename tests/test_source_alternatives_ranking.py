"""Independent, offline ranking and public-release privacy contracts."""
import copy
import json
import unittest

from backend.source_alternatives import public_release, rank_candidates


ORIGINAL={'id':'rel:failed','title':'Scream.7.2026.1080p.PMTP.WEB-DL.DUAL.DDP5.1.H.264-FUZEER',
          'quality':'1080p','size':5_008_170_000,'source':'nzb'}


def release(identity,size,quality='1080p',title=None,**private):
    return dict(id='rel:'+identity,title=title or 'Scream.7.2026.'+quality+'.'+identity,
                size=size,quality=quality,source='nzb',provider='Configured indexer',**private)


class SourceAlternativesRankingTests(unittest.TestCase):
    def rank(self,rows,**overrides):
        options=dict(wanted='1080',allow_lower=False,original=ORIGINAL,failed_ids={'rel:failed'},
                     failed_titles=set(),minimum=1_000_000,maximum=20_000_000_000)
        options.update(overrides)
        return rank_candidates(rows,**options)

    def test_closest_size_within_selected_quality_precedes_larger_release(self):
        rows=[release('far-large',8_000_000_000),release('near-small',4_799_430_581),release('near-large',5_480_519_826)]
        result=self.rank(rows)
        self.assertEqual([row['id'] for row in result],['rel:near-small','rel:near-large','rel:far-large'])
        self.assertEqual(result[0]['differencePercent'],round((4_799_430_581-5_008_170_000)/5_008_170_000*100,2))
        self.assertEqual([row['id'] for row in result if row['recommended']],['rel:near-small'])
        self.assertTrue(all(row['availability']=='unchecked' for row in result))

    def test_quality_policy_is_authoritative_before_size_distance(self):
        rows=[release('same',7_000_000_000),release('lower',5_008_170_000,'720p'),release('higher',5_008_170_000,'2160p'),release('unknown',5_008_170_000,'Unknown')]
        self.assertEqual([row['id'] for row in self.rank(rows)],['rel:same'])
        self.assertEqual([row['id'] for row in self.rank(rows,allow_lower=True)],['rel:same','rel:lower'])

    def test_actual_failed_selection_quality_precedes_current_setting_when_allowed(self):
        original=dict(ORIGINAL,quality='720p')
        result=self.rank([release('higher',5_008_170_000,'1080p'),release('same',6_000_000_000,'720p')],
                         original=original,allow_lower=True)
        self.assertEqual([row['id'] for row in result],['rel:same','rel:higher'])
        self.assertTrue(result[0]['recommended'])

    def test_same_failed_release_on_another_indexer_is_not_a_new_source(self):
        duplicate=release('different-id',5_008_169_831,title='SCREAM 7 2026 1080P PMTP WEB DL DUAL DDP5 1 H264 FUZEER')
        duplicate['provider']='Other indexer'
        known_bad=release('known-bad',4_000_000_000,title='Broken.Release.2026.1080p.Group')
        renamed_bad=release('reposted-bad',4_000_000_001,title='broken release 2026 1080p group')
        result=self.rank([duplicate,known_bad,renamed_bad,release('allowed',5_400_000_000)],
                         failed_ids={'rel:failed','rel:known-bad'},failed_titles={'Broken.Release.2026.1080p.Group'})
        self.assertEqual([row['id'] for row in result],['rel:allowed'])

    def test_duplicate_alternative_names_are_collapsed_after_ranking(self):
        first=release('far',6_500_000_000,title='Scream.7.2026.1080p.PiRaTeS')
        nearer=release('close',5_100_000_000,title='scream 7 2026 1080p pirates')
        result=self.rank([first,nearer])
        self.assertEqual([row['id'] for row in result],['rel:close'])

    def test_samples_failed_ids_and_known_out_of_range_sizes_cannot_be_selected(self):
        result=self.rank([release('too-small',999_999),release('too-large',20_000_000_001),
            release('sample',5_000_000_000,title='Scream.7.2026.SAMPLE.1080p'),
            release('bad-id',5_000_000_000),release('minimum',1_000_000),release('maximum',20_000_000_000)],
            failed_ids={'rel:failed','rel:bad-id'})
        self.assertEqual({row['id'] for row in result},{'rel:minimum','rel:maximum'})

    def test_unknown_sizes_stay_honest_and_never_receive_recommendation(self):
        values=[None,0,-1,float('nan'),float('inf'),True,'5000000000']
        rows=[release('unknown'+str(index),value) for index,value in enumerate(values)]
        result=self.rank(rows)
        self.assertEqual(len(result),len(rows))
        for row in result:
            self.assertIsNone(row['size']);self.assertFalse(row['sizeKnown'])
            self.assertIsNone(row['differencePercent']);self.assertFalse(row['recommended'])
        known=self.rank(rows+[release('known',5_100_000_000)])
        self.assertEqual(known[0]['id'],'rel:known');self.assertTrue(known[0]['recommended'])

    def test_unknown_original_size_does_not_fabricate_closeness(self):
        result=self.rank([release('known',5_100_000_000)],original=dict(ORIGINAL,size=None))
        self.assertTrue(result[0]['sizeKnown']);self.assertIsNone(result[0]['differencePercent'])
        self.assertFalse(result[0]['recommended'])

    def test_public_release_and_ranked_rows_never_copy_provider_credentials(self):
        candidate=release('safe',5_100_000_000,grab='https://provider.invalid/get?apikey=PRIVATE',
            api_key='PRIVATE',headers={'Authorization':'Bearer PRIVATE'},cookies='PRIVATE',
            password='PRIVATE',raw={'url':'https://provider.invalid/private'},
            error='Provider-private diagnostics')
        public=public_release(candidate)
        self.assertEqual(set(public),{'id','title','quality','size','source'})
        ranked=self.rank([candidate]);self.assertEqual(len(ranked),1)
        self.assertEqual(set(ranked[0]),{'id','title','quality','size','source','sizeKnown','differencePercent','recommended','availability'})
        text=json.dumps([public,ranked]);self.assertNotIn('PRIVATE',text);self.assertNotIn('provider.invalid',text)
        self.assertNotIn('Authorization',text);self.assertNotIn('diagnostics',text)

    def test_public_visible_text_redacts_structured_secrets_and_provider_links(self):
        candidate=release('safe',5_100_000_000,title='Release https://private.invalid/file?token=secret')
        candidate['provider']='api_key=TOPSECRET'
        public=public_release(candidate);text=json.dumps(public)
        self.assertNotIn('private.invalid',text);self.assertNotIn('token=secret',text);self.assertNotIn('TOPSECRET',text)
        self.assertTrue(public['title']);self.assertEqual(public['source'],'NZB')

    def test_ranking_is_deterministic_and_does_not_mutate_private_candidates(self):
        rows=[release('z',5_100_000_000),release('a',5_100_000_000),release('unknown',None)]
        before=copy.deepcopy(rows);first=self.rank(rows);second=self.rank(list(reversed(rows)))
        self.assertEqual(first,second);self.assertEqual(rows,before)

    def test_malformed_candidate_ids_never_enter_public_selection_tokens(self):
        rows=[dict(release('safe',5_100_000_000),id=identity) for identity in (None,'https://private.invalid/?token=secret','../escape','x'*101)]
        rows.append(release('valid',5_200_000_000))
        self.assertEqual([row['id'] for row in self.rank(rows)],['rel:valid'])


if __name__=='__main__':unittest.main()

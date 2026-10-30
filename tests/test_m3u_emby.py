import json
import tempfile
import unittest
from unittest.mock import patch
from backend.storage import Store
from backend import m3u,emby,m3u_emby

class EmbyLibraryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.store=Store(self.tmp.name);m3u.initialise(self.store);emby.initialise(self.store)
  self.store.set('emby_url','http://emby:8096');self.store.set('emby_key','test');self.store.set('emby_root','/media/ReelDock/YouTube')
  self.source=m3u.save(self.store,dict(name='Test',url='https://example.com/list',naming='emby'))
  self.store.set('m3u_emby_auto','true');self.folders=[];self.calls=[]
 def fake(self,store,path,method='GET',params=None,body=None):
  self.calls.append((path,method,body))
  if method=='GET':return self.folders
  if path=='Library/VirtualFolders':self.folders.append(dict(Name=body['Name'],CollectionType=body['CollectionType'],ItemId=str(len(self.folders)+1),Locations=list(body['Paths']),LibraryOptions=body['LibraryOptions']))
  else:next(f for f in self.folders if f['ItemId']==body['Id'])['Locations'].append(body['Path'])
  return {}
 def test_create_once_then_add_new_playlist_without_removing_paths(self):
  with patch('backend.m3u_emby.api',side_effect=self.fake):
   m3u_emby.reconcile(self.store);m3u_emby.reconcile(self.store)
   self.assertEqual(len([c for c in self.calls if c[1]=='POST']),2)
   m3u.save(self.store,dict(name='Second',url='https://example.com/second',naming='emby'))
   m3u_emby.reconcile(self.store)
  self.assertEqual(len(self.folders),2)
  self.assertTrue(all(len(f['Locations'])==3 for f in self.folders))
  self.assertTrue(all(not t['MetadataFetchers'] and not t['ImageFetchers'] for f in self.folders for t in f['LibraryOptions']['TypeOptions']))
 def test_disabled_and_disconnected_do_not_write(self):
  with patch('backend.m3u_emby.api') as api:
   self.store.set('m3u_emby_auto','false');m3u_emby.reconcile(self.store)
   self.store.set('m3u_emby_auto','true');self.store.set('emby_key','');m3u_emby.reconcile(self.store)
   api.assert_not_called()
 def test_name_collision_does_not_modify_existing_library(self):
  self.folders=[dict(Name='ReelDock Movies',CollectionType='movies',ItemId='123',Locations=['/other'],LibraryOptions={})]
  with patch('backend.m3u_emby.api',side_effect=self.fake):
   with self.assertRaises(ValueError):m3u_emby.reconcile(self.store)
  self.assertFalse(any(c[1]=='POST' for c in self.calls))
 def test_path_mapping_cannot_overlap_or_traverse(self):
  for paths in [dict(streams='/media',movies='/media/Movies',series='/shows'),dict(streams='/links/../other',movies='/movies',series='/shows')]:
   with self.assertRaises(ValueError):m3u_emby.validate_paths(paths)
 def test_initial_local_only_libraries_can_be_adopted(self):
  with patch('backend.m3u_emby.api',side_effect=self.fake):
   m3u_emby.reconcile(self.store);self.store.set('m3u_emby_owned','{}');self.calls=[];m3u_emby.reconcile(self.store)
  self.assertFalse(any(c[1]=='POST' for c in self.calls))

"""Explicit Activity actions serialize cleanup and hold release, never polling."""
import os
import select
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend import media_holds
from backend.storage import Store


class ActivityActionLockTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=Store(self.root/'data')
        media_holds.initialise(self.store)

    def test_same_thread_and_same_directory_alias_reuse_one_private_lock_descriptor(self):
        alias=SimpleNamespace(directory=self.store.directory/'.')
        with patch.object(media_holds.os,'open',wraps=os.open) as opened,patch.object(media_holds.fcntl,'flock',wraps=media_holds.fcntl.flock) as flock:
            with media_holds.explicit_action(self.store):
                with media_holds.explicit_action(alias):
                    with media_holds.explicit_action(self.store):
                        self.assertEqual(opened.call_count,1);self.assertEqual(flock.call_count,1)
        path=self.store.directory/'activity-actions.lock'
        self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
        self.assertTrue(stat.S_ISREG(path.stat().st_mode))
        self.assertFalse(getattr(media_holds._ACTION_LOCAL,'depths',{}))

    def test_resume_waits_for_clear_cleanup_without_taking_a_database_writer(self):
        key='native:nzb-job:fixture'
        with self.store.connect() as db:media_holds.hold(db,[key],[('nzb','fixture')])
        entered=threading.Event();release=threading.Event();resume_started=threading.Event();resumed=threading.Event();errors=[];results=[]
        def cleanup():
            try:
                with media_holds.explicit_action(self.store):
                    entered.set();release.wait(3)
            except BaseException as exc:errors.append(exc)
        def resume():
            resume_started.set()
            try:results.append(media_holds.resume(self.store,[key]))
            except BaseException as exc:errors.append(exc)
            finally:resumed.set()
        first=threading.Thread(target=cleanup);second=threading.Thread(target=resume)
        first.start()
        try:
            self.assertTrue(entered.wait(1));second.start();self.assertTrue(resume_started.wait(1))
            self.assertFalse(resumed.wait(.05));self.assertTrue(media_holds.blocked(self.store,key))
            # Resume is waiting for Action, not holding SQL while Clear needs it.
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE');db.execute("INSERT INTO settings VALUES('unrelated-read-progress','1')")
        finally:
            release.set();first.join(2)
            if second.ident is not None:second.join(2)
        self.assertFalse(first.is_alive());self.assertFalse(second.is_alive());self.assertFalse(errors)
        self.assertEqual(results,[True]);self.assertFalse(media_holds.blocked(self.store,key))

    def test_nested_explicit_resume_is_reentrant_and_releases_only_selected_hold(self):
        with self.store.connect() as db:
            media_holds.hold(db,['selected'],[('youtube','selected')])
            media_holds.hold(db,['other'],[('youtube','other')])
        with media_holds.explicit_action(self.store):
            self.assertTrue(media_holds.resume(self.store,['selected']))
            self.assertFalse(media_holds.blocked(self.store,'selected'))
            self.assertTrue(media_holds.blocked(self.store,'other'))

    def test_exception_releases_thread_and_advisory_locks(self):
        with self.assertRaisesRegex(RuntimeError,'isolated failure'):
            with media_holds.explicit_action(self.store):
                with media_holds.explicit_action(self.store):raise RuntimeError('isolated failure')
        entered=threading.Event()
        def later():
            with media_holds.explicit_action(self.store):entered.set()
        thread=threading.Thread(target=later);thread.start();thread.join(1)
        self.assertFalse(thread.is_alive());self.assertTrue(entered.is_set())
        self.assertFalse(getattr(media_holds._ACTION_LOCAL,'depths',{}))

    def test_other_installation_does_not_wait_for_this_action(self):
        other=self.root/'other';other.mkdir();entered=threading.Event()
        def independent():
            with media_holds.explicit_action(SimpleNamespace(directory=other)):entered.set()
        with media_holds.explicit_action(self.store):
            thread=threading.Thread(target=independent);thread.start();thread.join(1)
            self.assertTrue(entered.is_set());self.assertFalse(thread.is_alive())

    def test_another_process_waits_for_same_advisory_lock(self):
        code="""import sys
from types import SimpleNamespace
from backend.media_holds import explicit_action
print('waiting',flush=True)
with explicit_action(SimpleNamespace(directory=sys.argv[1])):
 print('entered',flush=True)
"""
        child=None
        try:
            with media_holds.explicit_action(self.store):
                child=subprocess.Popen([sys.executable,'-c',code,str(self.store.directory)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                self.assertTrue(select.select([child.stdout],[],[],3)[0])
                self.assertEqual(child.stdout.readline().strip(),'waiting')
                self.assertFalse(select.select([child.stdout],[],[],.1)[0])
                self.assertIsNone(child.poll())
            self.assertTrue(select.select([child.stdout],[],[],3)[0])
            self.assertEqual(child.stdout.readline().strip(),'entered')
            _output,error=child.communicate(timeout=3)
            self.assertEqual(child.returncode,0,error)
        finally:
            if child is not None:
                if child.poll() is None:child.terminate()
                child.communicate(timeout=3)

    def test_symlink_and_hardlink_lock_are_rejected_without_touching_target(self):
        outside=self.root/'outside';outside.write_text('owner content');outside.chmod(0o640)
        lock=self.store.directory/'activity-actions.lock'
        for link in ('symlink','hardlink'):
            with self.subTest(link=link):
                if link=='symlink':lock.symlink_to(outside)
                else:os.link(outside,lock)
                try:
                    with self.assertRaises(OSError):
                        with media_holds.explicit_action(self.store):self.fail('Unsafe lock entered')
                    self.assertEqual(outside.read_text(),'owner content')
                    self.assertEqual(stat.S_IMODE(outside.stat().st_mode),0o640)
                finally:lock.unlink()
        with media_holds.explicit_action(self.store):pass

    def test_flock_failure_closes_descriptor_and_does_not_strand_depth(self):
        descriptors=[];original=os.open
        def opened(*args,**kwargs):
            descriptor=original(*args,**kwargs);descriptors.append(descriptor);return descriptor
        with patch.object(media_holds.os,'open',side_effect=opened),patch.object(media_holds.fcntl,'flock',side_effect=OSError('isolated lock failure')):
            with self.assertRaises(OSError):
                with media_holds.explicit_action(self.store):self.fail('Lock failure entered')
        self.assertEqual(len(descriptors),1)
        with self.assertRaises(OSError):os.fstat(descriptors[0])
        self.assertFalse(getattr(media_holds._ACTION_LOCAL,'depths',{}))
        with media_holds.explicit_action(self.store):pass

    def test_ordinary_hold_reads_and_native_polling_never_acquire_action_lock(self):
        with patch.object(media_holds,'explicit_action',side_effect=AssertionError('Read acquired explicit action')):
            self.assertFalse(media_holds.blocked(self.store,'unrelated'))
            self.assertFalse(media_holds.native_blocked(self.store.directory/'nzb-engine',['unrelated']))

    def test_nonblocking_same_thread_reentry_reuses_descriptor_and_preserves_outer_depth(self):
        with patch.object(media_holds.os,'open',wraps=os.open) as opened:
            with media_holds.explicit_action(self.store) as outer:
                self.assertTrue(outer)
                with media_holds.explicit_action(self.store,blocking=False) as nested:
                    self.assertTrue(nested)
                    self.assertEqual(opened.call_count,1)
                    self.assertEqual(media_holds._ACTION_LOCAL.depths[str(self.store.directory.resolve())],2)
                self.assertEqual(media_holds._ACTION_LOCAL.depths[str(self.store.directory.resolve())],1)
        self.assertFalse(media_holds._ACTION_LOCAL.depths)

    def test_nonblocking_thread_contention_does_not_open_file_or_enter_depth(self):
        entered=threading.Event();release=threading.Event();errors=[]
        def owner():
            try:
                with media_holds.explicit_action(self.store):
                    entered.set();release.wait(3)
            except BaseException as exc:errors.append(exc)
        thread=threading.Thread(target=owner);thread.start()
        try:
            self.assertTrue(entered.wait(1))
            started=time.monotonic()
            with patch.object(media_holds.os,'open',side_effect=AssertionError('Contended thread opened a descriptor')):
                with media_holds.explicit_action(self.store,blocking=False) as acquired:
                    self.assertFalse(acquired)
            self.assertLess(time.monotonic()-started,.5)
            self.assertNotIn(str(self.store.directory.resolve()),getattr(media_holds._ACTION_LOCAL,'depths',{}))
        finally:
            release.set();thread.join(2)
        self.assertFalse(thread.is_alive());self.assertFalse(errors)
        with media_holds.explicit_action(self.store,blocking=False) as acquired:self.assertTrue(acquired)

    def test_nonblocking_process_contention_closes_file_and_releases_mutex_before_false_body(self):
        lock=self.store.directory/'activity-actions.lock'
        code="import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX); print('held',flush=True); sys.stdin.read(1)"
        child=subprocess.Popen([sys.executable,'-c',code,str(lock)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        descriptors=[];original=os.open
        def opened(*args,**kwargs):
            descriptor=original(*args,**kwargs);descriptors.append(descriptor);return descriptor
        try:
            self.assertTrue(select.select([child.stdout],[],[],3)[0]);self.assertEqual(child.stdout.readline().strip(),'held')
            with patch.object(media_holds.os,'open',side_effect=opened):
                started=time.monotonic()
                with media_holds.explicit_action(self.store,blocking=False) as acquired:
                    self.assertFalse(acquired);self.assertLess(time.monotonic()-started,.5)
                    self.assertEqual(len(descriptors),1)
                    with self.assertRaises(OSError):os.fstat(descriptors[0])
                    self.assertNotIn(str(self.store.directory.resolve()),getattr(media_holds._ACTION_LOCAL,'depths',{}))
                    child.communicate('x',timeout=3);self.assertEqual(child.returncode,0)
                    # The failed context is still open. Another thread must
                    # already be able to own both locks after the process exits.
                    results=[];errors=[]
                    def following():
                        try:
                            with media_holds.explicit_action(self.store,blocking=False) as later:results.append(later)
                        except BaseException as exc:errors.append(exc)
                    thread=threading.Thread(target=following);thread.start();thread.join(1)
                    self.assertFalse(thread.is_alive());self.assertFalse(errors);self.assertEqual(results,[True])
        finally:
            if child.poll() is None:child.terminate()
            child.communicate(timeout=3)

    def test_nonblocking_real_errors_and_body_exceptions_still_release_all_state(self):
        with patch.object(media_holds.fcntl,'flock',side_effect=OSError('isolated non-contention failure')):
            with self.assertRaisesRegex(OSError,'non-contention'):
                with media_holds.explicit_action(self.store,blocking=False):self.fail('Unsafe lock entered')
        self.assertFalse(getattr(media_holds._ACTION_LOCAL,'depths',{}))
        with self.assertRaisesRegex(RuntimeError,'isolated body failure'):
            with media_holds.explicit_action(self.store,blocking=False) as acquired:
                self.assertTrue(acquired);raise RuntimeError('isolated body failure')
        self.assertFalse(media_holds._ACTION_LOCAL.depths)
        with media_holds.explicit_action(self.store,blocking=False) as acquired:self.assertTrue(acquired)

    def test_same_thread_resume_after_first_clear_commit_preserves_new_native_attempt(self):
        from test_activity_attempt_clear import ActivityAttemptClearTests
        from backend import activity_items
        from backend.activity_observation import capture
        fixture=ActivityAttemptClearTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        identity='same-native';fixture.native.add(identity,state='downloading',generation=7)
        observed=capture('nzb',fixture.native.items[identity]);original=fixture.native.activity_key;calls=[]
        def selected_retry(target):
            calls.append(target)
            if len(calls)==2:
                key='native:nzb-job:'+target
                self.assertTrue(media_holds.blocked(fixture.store,key))
                self.assertTrue(media_holds.resume(fixture.store,[key]))
                fixture.native.add(target,state='downloading',generation=8)
            return original(target)
        fixture.native.activity_key=selected_retry
        result=activity_items.clear_one(fixture.app,fixture.store,'nzb',identity,observed=observed)
        self.assertEqual(fixture.native.removed,[])
        self.assertEqual(fixture.native.items[identity]['activity_generation'],8)
        self.assertEqual(fixture.native.items[identity]['state'],'downloading')
        self.assertFalse(media_holds.blocked(fixture.store,'native:nzb-job:'+identity))
        self.assertEqual(len(fixture.archive()),1)
        self.assertEqual(result['cleared'],1)


if __name__=='__main__':unittest.main()

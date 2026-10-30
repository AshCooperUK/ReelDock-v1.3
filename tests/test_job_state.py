import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from backend.downloaders import job_state


class JobStateTests(unittest.TestCase):
    def test_concurrent_metadata_updates_keep_complete_json_and_all_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'job.json';job_state.save(path,dict(phase='stopped',done=[],padding='x'*100000))
            errors=[];stop=threading.Event()
            def reader():
                while not stop.is_set():
                    try:json.loads(path.read_text())
                    except Exception as exc:errors.append(exc)
            thread=threading.Thread(target=reader);thread.start()
            try:
                def write(index):job_state.update(path,lambda payload:payload['done'].append(index))
                with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(write,range(60)))
            finally:stop.set();thread.join()
            saved=json.loads(path.read_text());self.assertEqual(saved['phase'],'stopped');self.assertEqual(sorted(saved['done']),list(range(60)))
            self.assertEqual(errors,[]);self.assertEqual(list(Path(directory).glob('*.tmp')),[])
            self.assertEqual(path.stat().st_mode&0o777,0o600)

    def test_metadata_update_serializes_with_cancellation_and_keeps_stopped_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'job.json';job_state.save(path,dict(phase='download',metadata_note=''))
            entered=threading.Event();release=threading.Event()
            def change(payload):entered.set();release.wait(3);payload['metadata_note']='saved'
            with ThreadPoolExecutor(max_workers=2) as pool:
                update=pool.submit(job_state.update,path,change);self.assertTrue(entered.wait(3))
                cancel=pool.submit(job_state.save,path,dict(phase='stopped',metadata_note='saved'))
                release.set();update.result();cancel.result()
            job_state.update(path,lambda payload:payload.update(metadata_note='finished'))
            self.assertEqual(json.loads(path.read_text()),dict(phase='stopped',metadata_note='finished'))

    def test_invalid_or_missing_state_is_never_silently_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'job.json'
            with self.assertRaises(FileNotFoundError):job_state.update(path,lambda payload:None)
            path.write_text('{"phase":"stopped"}}')
            with self.assertRaises(json.JSONDecodeError):job_state.update(path,lambda payload:None)
            self.assertEqual(path.read_text(),'{"phase":"stopped"}}')

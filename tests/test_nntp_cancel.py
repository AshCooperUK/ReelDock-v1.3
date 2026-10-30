"""Cancellation must never recycle a descriptor while its I/O owner is active."""
import socket
import threading
import unittest
from backend.downloaders.nzb.nntp import NntpClient, NntpError, _Reader

class CancellationTests(unittest.TestCase):
    def test_abort_wakes_reader_but_owner_retains_descriptor(self):
        left,right=socket.socketpair()
        client=NntpClient('unused',119,False,'','')
        client.sock=left;client.reader=_Reader(left)
        entered=threading.Event();unwound=threading.Event();release=threading.Event()
        errors=[]
        def read():
            entered.set()
            try:client.reader.line()
            except (NntpError,OSError):pass
            except Exception as exc:errors.append(exc)
            finally:
                unwound.set();release.wait(3);client.close()
        thread=threading.Thread(target=read);thread.start()
        try:
            self.assertTrue(entered.wait(1));client.abort();client.abort()
            self.assertTrue(unwound.wait(1))
            self.assertGreaterEqual(left.fileno(),0,'abort must not free the descriptor')
            self.assertEqual(errors,[])
        finally:
            release.set();thread.join(3);right.close();client.close()
        self.assertEqual(left.fileno(),-1)
        with self.assertRaisesRegex(NntpError,'stopped'):client.connect()

    def test_engine_cancel_interrupts_before_join_and_cleanup(self):
        from unittest.mock import Mock
        from backend.downloaders.nzb.engine import NzbEngine
        engine=object.__new__(NzbEngine)
        client=Mock();job=Mock();job._clients=threading.Lock();job.clients=[client]
        job.worker.is_alive.side_effect=[True,False]
        engine._require=Mock(return_value=job);engine._drop_unfinished=Mock()
        engine.remove('job')
        client.abort.assert_called_once();client.close.assert_not_called()
        job.worker.join.assert_called_once_with(20)
        engine._drop_unfinished.assert_called_once_with(job)

    def test_tls_cancellation_preserves_open_database(self):
        import shutil,ssl,sqlite3,subprocess,tempfile
        from pathlib import Path
        from unittest.mock import patch
        if not shutil.which('openssl'):self.skipTest('OpenSSL CLI needed for ephemeral test certificate')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cert=root/'cert.pem';key=root/'key.pem'
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost','-days','1','-keyout',str(key),'-out',str(cert)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            server_context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);server_context.load_cert_chain(cert,key)
            trust=ssl.create_default_context(cafile=str(cert))
            listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(3)
            connected=threading.Event();unwound=threading.Event();release=threading.Event();errors=[]
            client=NntpClient('localhost',listener.getsockname()[1],True,'','',timeout=3)
            def server():
                try:
                    raw,_=listener.accept()
                    with server_context.wrap_socket(raw,server_side=True) as sock:
                        sock.sendall(b'200 Ready\r\n');sock.recv(1)
                except (OSError,ssl.SSLError):pass
            def owner():
                try:
                    with patch('backend.downloaders.nzb.nntp.ssl.create_default_context',return_value=trust):client.connect()
                    connected.set();client.reader.line()
                except (NntpError,OSError):pass
                except Exception as exc:errors.append(type(exc).__name__)
                finally:unwound.set();release.wait(3);client.close()
            service=threading.Thread(target=server);worker=threading.Thread(target=owner)
            service.start();worker.start()
            try:
                self.assertTrue(connected.wait(3));sock=client.sock;client.abort()
                self.assertTrue(unwound.wait(3));self.assertGreaterEqual(sock.fileno(),0)
                for _ in range(10):
                    db=sqlite3.connect(root/'guard.sqlite3');db.execute('CREATE TABLE IF NOT EXISTS guard(value)');db.commit()
                    self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok');db.close()
                self.assertEqual(errors,[])
            finally:
                release.set();worker.join(4);listener.close();service.join(4);client.close()

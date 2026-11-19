"""Native clients add delta=1 to the existing snapshot/patch stream."""
import tests
import http.client
import json
import threading
import unittest
from unittest import mock

from tests.test_web_stream_shared import StreamShared
from modules.web import server as web_server


class StreamQuery(StreamShared):
    def event(self, response):
        kind = None
        while True:
            line = response.fp.readline().decode()
            self.assertTrue(line, 'stream closed before an event')
            if line.startswith('event:'):
                kind = line[6:].strip()
            elif line.startswith('data:'):
                return kind, json.loads(line[5:])

    def test_query_stream_delivers_snapshot_and_patch(self):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        self.addCleanup(c.close)
        c.request('GET', '/api/stream?delta=1', headers={'Host': f'127.0.0.1:{self.port}'})
        response = c.getresponse()
        self.addCleanup(response.close)
        self.assertEqual(response.status, 200)
        self.assertTrue(response.getheader('Content-Type').startswith('text/event-stream'))
        kind, snapshot = self.event(response)
        self.assertEqual(kind, 'snapshot')
        self.assertIn('a', [s['name'] for s in snapshot['sessions']])
        self.j.append('session.requested', 'b',
                      {'cwd': '/w', 'session_id': 's-b', 'channel': 'cli', 'owner': True})
        self.web.tail.poll()
        for _ in range(20):   # a patch of another key (the clock) may come first
            kind, patch = self.event(response)
            if kind == 'patch' and 'sessions' in patch:
                break
        self.assertEqual(kind, 'patch')
        self.assertIn('b', [s['name'] for s in patch['sessions']])

    def test_query_does_not_bypass_host_check_or_match_a_different_path(self):
        for path, host, expected in [('/api/stream?delta=1', 'attacker.example', 403),
                                     ('/api/stream/other?delta=1', f'127.0.0.1:{self.port}', 404),
                                     ('/api/streamx?delta=1', f'127.0.0.1:{self.port}', 404),
                                     ('/api/stream;x', f'127.0.0.1:{self.port}', 404)]:
            with self.subTest(path=path, host=host):
                c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
                try:
                    c.request('GET', path, headers={'Host': host})
                    response = c.getresponse()
                    self.assertEqual(response.status, expected)
                    response.read()
                finally:
                    c.close()

    def test_query_stream_closes_when_remote_access_is_revoked(self):
        allowed = threading.Event()
        allowed.set()
        with mock.patch.object(web_server.access, 'local', return_value=False), \
                mock.patch.object(web_server.access, 'allowed', side_effect=lambda *a, **kw: allowed.is_set()) as check, \
                mock.patch.object(web_server, 'STREAM_RECHECK', 0), \
                mock.patch.object(web_server, 'STREAM_WAIT', 0.01), \
                mock.patch.object(web_server, 'STREAM_GAP', 0.01):
            c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
            try:
                c.request('GET', '/api/stream?delta=1', headers={'Host': f'127.0.0.1:{self.port}'})
                response = c.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(self.event(response)[0], 'snapshot')
                before = check.call_count
                allowed.clear()
                with self.web.tail.changed:
                    self.web.tail.changed.notify_all()
                response.read()  # must reach EOF, not leave a revoked stream open
                self.assertGreater(check.call_count, before)   # re-checked after the revocation
                self.assertTrue(response.isclosed())
            finally:
                c.close()


def load_tests(loader, standard, pattern):
    """Only our own: StreamShared already runs in test_web_stream_shared."""
    suite = unittest.TestSuite()
    suite.addTests(StreamQuery(n) for n in StreamQuery.__dict__ if n.startswith("test"))
    return suite

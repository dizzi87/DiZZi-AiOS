import json
from pathlib import Path
import tempfile
import unittest
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib import error, request
from unittest.mock import Mock

from big_dizzi import brain
from big_dizzi.server import make_handler


class BrainBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'approved').mkdir()
        (self.root / 'other').mkdir()
        (self.root / 'approved' / 'one.md').write_text('# One\nLink to [Two](two.md)\npassword: secret-value', encoding='utf-8')
        (self.root / 'approved' / 'two.md').write_text('# Two\nA safe note.', encoding='utf-8')
        (self.root / 'other' / 'private.md').write_text('PRIVATE-MARKER', encoding='utf-8')
        (self.root / 'approved' / 'escape.md').symlink_to(self.root / 'other' / 'private.md')
        self.config = {'core_root': str(self.root), 'brain': {'sources': [{'id': 'approved', 'label': 'Approved', 'paths': ['approved']}]}}

    def test_only_selected_notes_and_redacted_values_are_exposed(self):
        graph, records = brain.build(self.config)
        payload = json.dumps(graph)
        self.assertEqual(len(graph['nodes']), 2)
        self.assertEqual(len(graph['links']), 1)
        self.assertNotIn('PRIVATE-MARKER', payload)
        self.assertNotIn(str(self.root), payload)
        self.assertNotIn('secret-value', payload)
        for identity in records:
            self.assertNotIn('secret-value', json.dumps(brain.note(self.config, identity)))
        self.assertIsNone(brain.note(self.config, 'approved:does-not-exist'))

    def test_traversal_and_unapproved_absolute_paths_fail(self):
        for path in ('../other', str(self.root / 'other'), 'approved/../other'):
            self.config['brain']['sources'][0]['paths'] = [path]
            with self.assertRaises(ValueError):
                brain.build(self.config)

    def test_explicit_symlink_source_fails(self):
        (self.root / 'selected-link').symlink_to(self.root / 'other', target_is_directory=True)
        self.config['brain']['sources'][0]['paths'] = ['selected-link']
        with self.assertRaises(ValueError):
            brain.build(self.config)

    def test_brain_http_rejects_cross_origin_and_unknown_notes(self):
        service = Mock()
        service.config = self.config
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(service))
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'
        with request.urlopen(base + '/api/graph') as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(len(json.load(response)['nodes']), 2)
        with self.assertRaises(error.HTTPError) as blocked:
            request.urlopen(request.Request(base + '/api/graph', headers={'Origin': 'http://evil.example'}))
        self.assertEqual(blocked.exception.code, 403)
        with self.assertRaises(error.HTTPError) as missing:
            request.urlopen(base + '/api/node?id=approved%3Aunknown')
        self.assertEqual(missing.exception.code, 404)


if __name__ == '__main__':
    unittest.main()

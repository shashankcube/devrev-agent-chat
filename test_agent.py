"""Offline protocol tests. No DevRev credentials or network service required."""
import base64
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import agent

SECRET = b'0123456789abcdef0123456789abcdef'
ARTIFACT = 'don:core:dvrv-us-1:devo/test:artifact/output'


def callback(kind='message', request_id='request-1', event_id='event-1', **extra):
    response = {'agent_response': kind, 'agent': 'agent-1', 'session_object': 'session-1',
                'client_metadata': {'tool_request_id': request_id}, kind: 'Hello' if kind == 'message' else {}, **extra}
    return {'id': event_id, 'webhook_id': 'webhook-1',
            'payload': {'type': 'ai_agent_response', 'ai_agent_response': response}}


@contextlib.contextmanager
def running(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def post_event(url, event, valid=True):
    data = json.dumps(event).encode()
    signature = base64.b64encode(hmac.digest(SECRET, data, 'sha256')).decode()
    request = Request(url + '/webhook', data=data, headers={
        'Content-Type': 'application/json', 'X-DevRev-Signature': signature if valid else 'wrong'})
    with urlopen(request, timeout=3) as response:
        return json.load(response)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = self.root / 'events.jsonl'

    def tearDown(self):
        self.temp.cleanup()

    def test_signed_verification_both_envelopes(self):
        with running(agent.make_receiver('127.0.0.1', 0, self.log, SECRET)) as url:
            event = {'type': 'verify', 'verify': {'challenge': 'opaque-base64-challenge'}}
            for envelope in (event, {'payload': event}):
                self.assertEqual(post_event(url, envelope), {'challenge': 'opaque-base64-challenge'})
        self.assertEqual(self.log.read_text(), '')

    def test_invalid_signature_and_stale_timestamp_rejected(self):
        with running(agent.make_receiver('127.0.0.1', 0, self.log, SECRET)) as url:
            with self.assertRaises(HTTPError) as err:
                post_event(url, callback(), valid=False)
            self.assertEqual(err.exception.code, 401)
            old = callback()
            old['timestamp'] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            with self.assertRaises(HTTPError) as err:
                post_event(url, old)
            self.assertEqual(err.exception.code, 401)
        self.assertEqual(self.log.read_text(), '')

    def test_duplicate_callbacks_persist_once_even_after_restart(self):
        with contextlib.redirect_stdout(io.StringIO()):
            for restart in range(2):
                with running(agent.make_receiver('127.0.0.1', 0, self.log, SECRET)) as url:
                    post_event(url, callback())
                    self.assertTrue(post_event(url, callback())['duplicate'])
        self.assertEqual(len(self.log.read_text().splitlines()), 1)

    def test_watcher_correlates_and_replays_early_callback(self):
        events = [callback(request_id='different', event_id='ignored'),
                  callback('progress', event_id='progress'), callback()]
        self.log.write_text(''.join(json.dumps(e) + '\n' for e in events))
        result = self.root / 'result.json'
        with contextlib.redirect_stdout(io.StringIO()) as output:
            status = agent.watch(self.log, 'request-1', 'session-1', 'agent-1', 1, result=result)
        self.assertEqual(status, 0)
        self.assertIn('[progress]', output.getvalue())
        self.assertEqual(json.loads(result.read_text()), events[-1])

    def test_watcher_error_and_timeout_exit_codes(self):
        self.log.write_text(json.dumps(callback('error', error={'error': 'execution failed'})) + '\n')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(agent.watch(self.log, 'request-1', timeout=0.2), 2)
            self.assertEqual(agent.watch(self.log, 'not-this-request', timeout=0.05), 124)

    def test_watcher_waits_for_partial_json_line(self):
        data = json.dumps(callback())
        self.log.write_text(data[:20])
        def finish():
            with self.log.open('a') as out:
                out.write(data[20:] + '\n')
        timer = threading.Timer(0.1, finish)
        timer.start()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(agent.watch(self.log, 'request-1', timeout=2), 0)
        timer.join()

    def test_payload_dry_run_no_token_or_network(self):
        with patch.dict('os.environ', {}, clear=True), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(agent.main(['invoke', '--agent', 'agent-1', '--webhook', 'hook-1',
                                         '--session', 'session-1', '--request-id', 'request-1',
                                         '--message', 'Read this', '--artifact', ARTIFACT, '--dry-run']), 0)
        body = json.loads(out.getvalue())
        self.assertEqual(body['message_context'], {'artifacts': [ARTIFACT]})
        self.assertEqual(body['agent_options'], {'runtime': 'container'})
        self.assertNotIn('stream_agent_response', body)
        self.assertNotIn('request_id', body)
        self.assertEqual(body['client_metadata']['tool_request_id'], 'request-1')

    def test_new_deployment_request_id_is_explicit_opt_in(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            agent.main(['invoke', '--agent', 'a', '--webhook', 'w', '--message', 'm',
                        '--request-id', 'turn-1', '--api-request-id', '--dry-run'])
        self.assertEqual(json.loads(out.getvalue())['request_id'], 'turn-1')

    def test_artifact_limit_fails_before_upload(self):
        argv = ['invoke', '--agent', 'a', '--webhook', 'w', '--message', 'm']
        for n in range(11):
            argv += ['--artifact', str(n)]
        with patch.object(agent.Client, 'upload') as upload, self.assertRaises(agent.ToolError):
            agent.main(argv)
        upload.assert_not_called()

    def test_untrusted_file_paths_are_flat_and_unpublished_files_skipped(self):
        class FakeClient:
            def download(_, aid, path):
                self.assertEqual(Path(path).parent, self.root)
                return str(path)
        response = {'file_references': [
            {'path': '../../escape.txt', 'artifact': {'id': ARTIFACT}},
            {'path': '/runtime/file.txt'},
            {'path': 'C:\\evil\\escape.txt', 'artifact': {'id': ARTIFACT + '2'}}]}
        with contextlib.redirect_stderr(io.StringIO()):
            files = agent.download_references(FakeClient(), response, self.root)
        self.assertEqual(len(files), 2)
        self.assertNotEqual(files[0]['path'], files[1]['path'])

    def test_upload_download_and_full_invoke_with_local_api(self):
        requests = []
        file_bytes = b'\x00binary\r\ndata\xff'
        source = self.root / 'sample.bin'
        source.write_bytes(file_bytes)
        receiver = agent.make_receiver('127.0.0.1', 0, self.log, SECRET)
        with contextlib.redirect_stdout(io.StringIO()), running(receiver) as receiver_url:
            class API(BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass

                def respond(self, body, code=200):
                    data = json.dumps(body).encode()
                    self.send_response(code)
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

                def do_GET(self):
                    requests.append(('GET', self.path, dict(self.headers), b''))
                    base = f'http://127.0.0.1:{self.server.server_port}'
                    if self.path.startswith('/internal/dev-users.self'):
                        self.respond({'dev_user': {'id': 'don:identity:test:devo/test:devu/1'}})
                    elif self.path.startswith('/internal/chats.get'):
                        self.respond({'chat': {'type': 'dm', 'id': 'don:core:test:devo/test:dm/1'}})
                    elif self.path.startswith('/internal/webhooks.get'):
                        self.respond({'webhook': {'id': 'webhook-1', 'status': 'active', 'event_types': ['ai_agent_response']}})
                    elif self.path.startswith('/internal/artifacts.locate'):
                        self.respond({'url': base + '/storage/download'})
                    elif self.path == '/storage/download':
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(file_bytes)
                    else:
                        self.respond({'error': 'unknown'}, 404)

                def do_POST(self):
                    body = self.rfile.read(int(self.headers['Content-Length']))
                    requests.append(('POST', self.path, dict(self.headers), body))
                    base = f'http://127.0.0.1:{self.server.server_port}'
                    if self.path == '/internal/chats.create':
                        self.respond({'chat': {'type': 'dm', 'id': 'don:core:test:devo/test:dm/1'}}, 201)
                    elif self.path == '/internal/artifacts.prepare':
                        self.respond({'id': ARTIFACT, 'url': base + '/storage/upload',
                                      'form_data': [{'key': 'key', 'value': 'signed/key'}, {'key': 'policy', 'value': 'signed-policy'}]})
                    elif self.path == '/storage/upload':
                        self.respond({}, 204)
                    elif self.path == '/internal/ai-agents.events.execute-async':
                        request = json.loads(body)
                        event = callback(request_id=request['client_metadata']['tool_request_id'], file_references=[
                            {'path': '../../result.bin', 'status': 'created', 'artifact': {'id': ARTIFACT}}])
                        event['payload']['ai_agent_response']['session_object'] = request['session_object']
                        # Callback arrives before the execute-async acknowledgement.
                        post_event(receiver_url, callback('progress', event_id='progress'))
                        post_event(receiver_url, event)
                        self.respond({'session': {'id': 'agent-session-1'}})
                    else:
                        self.respond({'error': 'unknown'}, 404)

            with running(ThreadingHTTPServer(('127.0.0.1', 0), API)) as api_url:
                receipt = self.root / 'receipt.json'
                result = self.root / 'result.json'
                with patch.dict('os.environ', {'DEVREV_TOKEN': 'test-token'}), contextlib.redirect_stderr(io.StringIO()):
                    status = agent.main(['--base-url', api_url + '/internal', 'invoke',
                        '--agent', 'agent-1', '--webhook', 'webhook-1', '--session', 'session-1',
                        '--request-id', 'request-1', '--message', 'Read and write a file',
                        '--file', str(source), '--wait', '--events', str(self.log),
                        '--receipt', str(receipt), '--result', str(result),
                        '--state-dir', str(self.root / 'state'),
                        '--download-dir', str(self.root / 'downloads'), '--timeout', '3'])
                self.assertEqual(status, 0)
                self.assertEqual(json.loads(receipt.read_text())['status'], 'accepted')
                downloaded = list((self.root / 'downloads').iterdir())
                self.assertEqual(len(downloaded), 1)
                self.assertEqual(downloaded[0].read_bytes(), file_bytes)
                client = agent.Client(api_url + '/internal', 'test-token')
                with self.assertRaises(agent.ToolError):
                    client.download(ARTIFACT, downloaded[0])
        for method, path, headers, body in requests:
            if path.startswith('/storage/'):
                self.assertNotIn('Authorization', headers)
            else:
                self.assertEqual(headers['Authorization'], 'Bearer test-token')
        upload = next(r for r in requests if r[1] == '/storage/upload')
        self.assertIn(file_bytes, upload[3])
        self.assertLess(upload[3].index(b'name="policy"'), upload[3].index(b'name="file"'))
        invoke = next(r for r in requests if r[1] == '/internal/ai-agents.events.execute-async')
        sent = json.loads(invoke[3])
        self.assertEqual(sent['message_context']['artifacts'], [ARTIFACT])
        self.assertEqual(sent['parent'], 'don:core:test:devo/test:dm/1')
        self.assertEqual(sent['session_object'], sent['parent'])
        self.assertNotIn('request_id', sent)
        created = next(r for r in requests if r[1] == '/internal/chats.create')
        self.assertFalse(json.loads(created[3])['is_default'])

    def test_dm_is_created_once_and_reused_for_same_label(self):
        class FakeClient:
            base = 'https://api.devrev.ai/internal'
            creates = 0
            def create_dm(self, agent_id, user_id, title):
                self.creates += 1
                return {'type': 'dm', 'id': 'don:core:test:devo/test:dm/1'}
            def call(self, endpoint, body, method):
                return {'chat': {'type': 'dm', 'id': body['id']}}
        client = FakeClient()
        first = agent.ensure_dm(client, 'agent-1', 'user-1', 'conversation', self.root)
        second = agent.ensure_dm(client, 'agent-1', 'user-1', 'conversation', self.root)
        self.assertEqual(first, second)
        self.assertEqual(client.creates, 1)
        agent.ensure_dm(client, 'agent-1', 'user-1', 'new-conversation', self.root)
        self.assertEqual(client.creates, 2)

    def test_non_dm_parent_rejected(self):
        from unittest.mock import Mock
        client = Mock()
        client.call.return_value = {'chat': {'type': 'channel', 'id': 'channel-1'}}
        with self.assertRaises(agent.ToolError):
            agent.ensure_dm(client, 'agent-1', 'user-1', 'label', self.root, parent='channel-1')
        client.create_dm.assert_not_called()

    def test_short_agent_id_uses_authenticated_user_org(self):
        self.assertEqual(agent.canonical_agent('ai_agent/48', 'don:identity:us:devo/org:devu/1'),
                         'don:core:us:devo/org:ai_agent/48')

    def test_api_redirect_does_not_leak_token(self):
        touched = []
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                self.send_response(302)
                self.send_header('Location', f'http://127.0.0.1:{self.server.server_port}/leak')
                self.end_headers()
            def do_GET(self):
                touched.append(self.path)
                self.send_response(200)
                self.end_headers()
        with running(ThreadingHTTPServer(('127.0.0.1', 0), Redirect)) as url:
            with self.assertRaises(agent.ToolError):
                agent.Client(url, 'private-token').call('test', {})
        self.assertEqual(touched, [])


if __name__ == '__main__':
    unittest.main()

import base64
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import agent
import chat
from test_agent import post_event

USER = 'don:identity:us:devo/org:devu/1'
AGENT = 'don:core:us:devo/org:ai_agent/48'


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_agent_prompt_format_and_remembered_selection(self):
        client = Mock(base='https://api.devrev.ai/internal')
        answers = iter(['48', 'ai_agent/48', 'ai_agent-48'])
        with contextlib.redirect_stdout(io.StringIO()):
            selected = chat.choose_agent(client, USER, self.state, read=lambda _: next(answers))
        self.assertEqual(selected, AGENT)
        prompts = []
        self.assertEqual(chat.choose_agent(client, USER, self.state,
                          read=lambda p: prompts.append(p) or ''), AGENT)
        self.assertIn('[ai_agent-48]', prompts[0])

    def test_inaccessible_agent_reprompts_before_creating_resources(self):
        client = Mock(base='base')
        client.call.side_effect = [agent.ToolError('HTTP 404'), {}]
        answers = iter(['ai_agent-4', 'ai_agent-48'])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(chat.choose_agent(client, USER, self.state, read=lambda _: next(answers)), AGENT)

    def test_keychain_write_uses_stdin_not_process_arguments(self):
        credential = chat.Credentials('https://api.devrev.ai/internal')
        secret = 'fake-sensitive-token'
        with patch.object(chat.sys, 'platform', 'darwin'), patch.object(credential, 'run') as run, patch.object(credential, 'get', return_value=secret):
            run.return_value = SimpleNamespace(returncode=0, stderr='')
            self.assertTrue(credential.put(secret))
        args, stdin = run.call_args.args
        self.assertNotIn(secret, str(args))
        self.assertIn(secret, stdin)

    def test_saved_token_is_reused_without_prompt(self):
        with patch.object(chat, 'Credentials') as store, patch.object(chat.agent, 'Client') as client, \
             patch('builtins.input') as prompt, patch.dict('os.environ', {}, clear=True):
            store.return_value.get.return_value = 'stored-token'
            client.return_value.current_user.return_value = USER
            chat.login('https://api.devrev.ai/internal')
            prompt.assert_not_called()
            client.assert_called_once_with('https://api.devrev.ai/internal', token='stored-token')

    def test_first_login_validates_then_saves_token(self):
        with patch.object(chat, 'Credentials') as store, patch.object(chat.agent, 'Client') as client, \
             patch('builtins.input', return_value='new-token'), patch.dict('os.environ', {}, clear=True), \
             contextlib.redirect_stdout(io.StringIO()):
            store.return_value.get.return_value = None
            client.return_value.current_user.return_value = USER
            chat.login('https://api.devrev.ai/internal')
            store.return_value.put.assert_called_once_with('new-token')

    def test_setup_creates_devrev_webhook_and_cleans_up(self):
        calls = []
        runtime = None
        class Client:
            base = 'https://api.devrev.ai/internal'
            def call(self, endpoint, body, method='POST'):
                calls.append((endpoint, body))
                if endpoint == 'webhooks.create':
                    # Verify the real local receiver using the secret sent to DevRev.
                    import hmac
                    from urllib.request import Request, urlopen
                    data = json.dumps({'type': 'verify', 'verify': {'challenge': 'challenge'}}).encode()
                    sig = base64.b64encode(hmac.digest(base64.b64decode(body['secret']), data, 'sha256')).decode()
                    req = Request(f'http://127.0.0.1:{runtime.server.server_port}/webhook', data=data,
                                  headers={'X-DevRev-Signature': sig})
                    with urlopen(req) as response:
                        if json.load(response) != {'challenge': 'challenge'}:
                            raise AssertionError('Verification failed')
                    return {'webhook': {'id': 'hook-1', 'status': 'active'}}
                return {}
        def spawn(args, stdout, **kwargs):
            stdout.write('https://local-test.trycloudflare.com\n')
            stdout.flush()
            return Mock(poll=Mock(return_value=None))
        runtime = chat.Runtime(Client(), USER, self.state)
        with patch.object(chat, 'tunnel_binary', return_value=Path('/fake/connector')), \
             patch.object(chat.subprocess, 'Popen', side_effect=spawn), contextlib.redirect_stdout(io.StringIO()):
            try:
                runtime.start()
                self.assertEqual(runtime.webhook, 'hook-1')
                self.assertTrue(runtime.pending.exists())
            finally:
                runtime.close()
        self.assertEqual(calls[0][0], 'webhooks.create')
        self.assertEqual(calls[0][1]['event_types'], ['ai_agent_response'])
        self.assertEqual(calls[-1], ('webhooks.delete', {'id': 'hook-1'}))
        self.assertFalse(runtime.pending.exists())
        runtime.tunnel.terminate.assert_called_once()

    def test_failed_webhook_cleanup_is_retried_on_next_launch(self):
        client = Mock(base='base')
        runtime = chat.Runtime(client, USER, self.state)
        runtime.webhook = 'hook-1'
        agent.save_json(runtime.pending, {'webhook': 'hook-1', 'user': USER, 'base': 'base'})
        client.call.side_effect = agent.ToolError('offline')
        with contextlib.redirect_stderr(io.StringIO()):
            runtime.close()
        self.assertTrue(runtime.pending.exists())
        client.call.side_effect = None
        # Stop after cleanup; assert the previous webhook was deleted first.
        with patch.object(chat, 'tunnel_binary', side_effect=agent.ToolError('stop after cleanup')):
            with self.assertRaises(agent.ToolError):
                chat.Runtime(client, USER, self.state).start()
        client.call.assert_called_with('webhooks.delete', {'id': 'hook-1'})
        self.assertFalse(runtime.pending.exists())

    def test_connector_checksum_failure_never_executes_download(self):
        fake = io.BytesIO(b'untrusted bytes')
        with patch.object(chat.platform, 'system', return_value='Linux'), \
             patch.object(chat.platform, 'machine', return_value='x86_64'), \
             patch.object(chat, 'urlopen', return_value=fake), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(agent.ToolError, 'checksum mismatch'):
                chat.tunnel_binary(self.state)
        self.assertFalse((self.state / 'bin' / f'cloudflared-{chat.VERSION}').exists())

    def test_chat_upload_followup_new_dm_and_auto_download(self):
        events = self.state / 'events.jsonl'
        file = self.state / 'input with spaces.csv'
        file.write_text('value\n28\n')
        requests = []
        class Client:
            base = 'https://api.devrev.ai/internal'
            creates = 0
            def create_dm(self, *args):
                self.creates += 1
                return {'type': 'dm', 'id': f'dm-{self.creates}'}
            def upload(self, path):
                return 'artifact-input'
            def download(self, aid, path):
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                Path(path).write_text('28')
                return str(path)
            def call(self, endpoint, body, method='POST'):
                if endpoint == 'chats.get':
                    return {'chat': {'type': 'dm', 'id': body['id']}}
                requests.append(body)
                event = {'type': 'ai_agent_response', 'ai_agent_response': {
                    'agent': AGENT, 'session_object': body['parent'],
                    'client_metadata': body['client_metadata'], 'agent_response': 'message',
                    'message': 'Total is 28', 'file_references': [
                        {'path': '/report.txt', 'artifact': {'id': 'artifact-output'}}]}}
                with events.open('a') as out:
                    out.write(json.dumps(event) + '\n')
                return {'session': {'id': 'session-1'}}
        client = Client()
        commands = iter([f'/attach {file}', 'Analyze file', 'Recall total', '/new', 'Hello', '/quit'])
        runtime = SimpleNamespace(webhook='hook-1', events=events)
        with contextlib.redirect_stdout(io.StringIO()):
            chat.interactive(client, runtime, AGENT, USER, self.state, 'default', 1,
                             read=lambda _: next(commands))
        self.assertEqual(client.creates, 2)
        self.assertEqual(requests[0]['message_context']['artifacts'], ['artifact-input'])
        self.assertNotIn('message_context', requests[1])
        self.assertEqual(requests[0]['parent'], requests[1]['parent'])
        self.assertNotEqual(requests[1]['parent'], requests[2]['parent'])
        self.assertTrue(all(r['parent'] == r['session_object'] for r in requests))
        self.assertEqual(len(list((self.state / 'downloads').rglob('*.txt'))), 3)


if __name__ == '__main__':
    unittest.main()

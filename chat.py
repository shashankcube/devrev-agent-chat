#!/usr/bin/env python3
"""Start chatting: python3 chat.py. Paste your DevRev token on first launch."""
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shlex
import signal
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from types import SimpleNamespace
from urllib.request import urlopen
import uuid

import agent

VERSION = '2026.10.4'
ASSETS = {
    ('Darwin', 'arm64'): ('cloudflared-darwin-arm64.tgz', '68b6bc4080ac1330234db2a9002d07f98b000537a1bd531a687b660d4160fa7f'),
    ('Darwin', 'x86_64'): ('cloudflared-darwin-amd64.tgz', 'f11fbef4207d715892331917dafd437f032cf4b7137ed64dea3008338c1c613d'),
    ('Linux', 'x86_64'): ('cloudflared-linux-amd64', 'ae5e8add7884b0531d66bbca4570d1de491ff6da51e4ebe7eb160cdf241d9e42'),
    ('Linux', 'aarch64'): ('cloudflared-linux-arm64', '2b7203b4fb0ba28a0c1e12f8855ce2c25f76d266a8d5edafac5540b6812acff6'),
}


class Credentials:
    """OS secret store only. No token is written to config or command arguments."""
    def __init__(self, base):
        self.service = 'devrev-agent-chat-' + hashlib.sha256(base.encode()).hexdigest()[:16]

    def run(self, args, data=None):
        return subprocess.run(args, input=data, text=True, capture_output=True, timeout=30)

    def get(self):
        if sys.platform == 'darwin':
            result = self.run(['security', 'find-generic-password', '-s', self.service, '-a', 'token', '-w'])
        elif shutil.which('secret-tool'):
            result = self.run(['secret-tool', 'lookup', 'service', self.service])
        else:
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    def put(self, token):
        if '\n' in token or '\r' in token:
            raise agent.ToolError('Token must be a single line')
        if sys.platform == 'darwin':
            # Interactive command mode keeps the secret out of process argv.
            command = 'add-generic-password -U -s {} -a token -w {}\n'.format(
                shlex.quote(self.service), shlex.quote(token))
            result = self.run(['security', '-i'], command)
            # security -i can itself exit successfully after a command failure.
            return result.returncode == 0 and 'error' not in result.stderr.lower() and self.get() == token
        if shutil.which('secret-tool'):
            return self.run(['secret-tool', 'store', '--label=DevRev agent chat',
                             'service', self.service], token).returncode == 0
        return False

    def delete(self):
        if sys.platform == 'darwin':
            self.run(['security', 'delete-generic-password', '-s', self.service, '-a', 'token'])
        elif shutil.which('secret-tool'):
            self.run(['secret-tool', 'clear', 'service', self.service])


def login(base, replace=False, save=True):
    store = Credentials(base)
    token = None if replace else os.getenv('DEVREV_TOKEN') or store.get()
    prompted = not token
    if prompted:
        token = input('Paste your DevRev token: ').strip()
    client = agent.Client(base, token=token)
    try:
        user = client.current_user()
    except agent.ToolError as exc:
        if not prompted and 'HTTP 401' in str(exc):
            print('Saved token expired or was rejected. Enter a replacement.')
            return login(base, replace=True, save=save)
        raise
    if prompted and save:
        try:
            stored = store.put(token)
        except (OSError, subprocess.TimeoutExpired):
            stored = False
        print('Token saved in your OS keychain.' if stored else
              'Using token for this session; OS keychain was unavailable.')
    return client, user


def tunnel_binary(state):
    name, expected = ASSETS.get((platform.system(), platform.machine()), (None, None))
    if not name:
        raise agent.ToolError('Automatic tunnel supports macOS/Linux on Intel or ARM64')
    binary = state / 'bin' / f'cloudflared-{VERSION}'
    checksum = binary.with_suffix('.sha256')
    if binary.exists() and checksum.exists() and hashlib.sha256(binary.read_bytes()).hexdigest() == checksum.read_text().strip():
        return binary
    print('Installing the HTTPS connector (first launch only)…')
    binary.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    url = f'https://github.com/cloudflare/cloudflared/releases/download/{VERSION}/{name}'
    with tempfile.TemporaryDirectory(dir=binary.parent) as scratch:
        archive = Path(scratch) / name
        with urlopen(url, timeout=60) as response, archive.open('wb') as out:
            shutil.copyfileobj(response, out)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
            raise agent.ToolError('Connector checksum mismatch; download rejected')
        candidate = Path(scratch) / 'connector'
        if name.endswith('.tgz'):
            with tarfile.open(archive) as package:
                member = next((m for m in package.getmembers() if Path(m.name).name == 'cloudflared' and m.isfile()), None)
                if member is None:
                    raise agent.ToolError('Connector missing from verified archive')
                with package.extractfile(member) as src, candidate.open('wb') as out:
                    shutil.copyfileobj(src, out)
        else:
            shutil.copyfile(archive, candidate)
        candidate.chmod(0o700)
        candidate.replace(binary)
    checksum.write_text(hashlib.sha256(binary.read_bytes()).hexdigest())
    return binary


class Runtime:
    def __init__(self, client, user, state):
        self.client, self.user, self.state = client, user, state
        self.server = self.tunnel = self.log_handle = None
        self.receiver_thread = None
        self.webhook = None
        self.pending = state / 'pending-webhook.json'
        self.events = state / 'events.jsonl'

    def close(self):
        if self.webhook:
            try:
                self.client.call('webhooks.delete', {'id': self.webhook})
                self.pending.unlink(missing_ok=True)
            except Exception:
                print('Could not delete temporary webhook; cleanup will be retried on next launch.', file=sys.stderr)
        if self.tunnel:
            self.tunnel.terminate()
            try:
                self.tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.tunnel.kill()
                self.tunnel.wait(timeout=5)
        if self.server:
            if self.receiver_thread and self.receiver_thread.is_alive():
                self.server.shutdown()
            self.server.server_close()
        if self.log_handle:
            self.log_handle.close()

    def start(self):
        if self.pending.exists():
            previous = json.loads(self.pending.read_text())
            if previous['user'] != self.user or previous['base'] != self.client.base:
                raise agent.ToolError('Another account has an unfinished webhook here; use --state-dir for this account')
            try:
                self.client.call('webhooks.delete', {'id': previous['webhook']})
            except agent.ToolError as exc:
                if 'HTTP 404' not in str(exc):
                    raise
            self.pending.unlink()
        binary = tunnel_binary(self.state)
        secret = secrets.token_bytes(32)
        self.server = agent.make_receiver('127.0.0.1', 0, self.events, secret, quiet=True)
        self.receiver_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.receiver_thread.start()
        log = self.state / 'connector.log'
        self.log_handle = log.open('w')
        print('Connecting HTTPS receiver through Cloudflare…')
        self.tunnel = subprocess.Popen([str(binary), 'tunnel', '--url',
            f'http://127.0.0.1:{self.server.server_port}', '--no-autoupdate'],
            stdout=self.log_handle, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 60
        url = None
        while time.monotonic() < deadline:
            if self.tunnel.poll() is not None:
                raise agent.ToolError(f'HTTPS connector exited; details: {log}')
            match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', log.read_text())
            if match:
                url = match.group(0) + '/webhook'
                break
            time.sleep(0.25)
        if not url:
            raise agent.ToolError(f'HTTPS connector timed out; details: {log}')
        print('Registering and verifying webhook in DevRev…')
        hook = self.client.call('webhooks.create', {'url': url, 'event_types': ['ai_agent_response'],
            'secret': base64.b64encode(secret).decode()})['webhook']
        self.webhook = hook['id']
        agent.save_json(self.pending, {'user': self.user, 'base': self.client.base, 'webhook': self.webhook})
        deadline = time.monotonic() + 90
        while hook.get('status') != 'active':
            if time.monotonic() > deadline:
                raise agent.ToolError('Webhook verification timed out')
            time.sleep(1)
            hook = self.client.call('webhooks.get', {'id': self.webhook}, 'GET')['webhook']
        return self


def show_event(event):
    response = agent.agent_response(event)
    if not response:
        return
    kind = response.get('agent_response')
    if kind == 'message':
        print(response.get('message', ''), flush=True)
    elif kind == 'progress':
        progress = response.get('progress', {})
        if progress.get('progress_state') == 'skill_triggered':
            skill = progress.get('skill_triggered', {})
            print('  ' + (skill.get('thought') or skill.get('skill_name', 'Working…')), flush=True)
    elif kind == 'error':
        error = response.get('error', {})
        print('Agent error: ' + (error.get('error', str(error)) if isinstance(error, dict) else str(error)), flush=True)


def interactive(client, runtime, agent_id, user, state, label, timeout, read=input):
    dm = agent.ensure_dm(client, agent_id, user, label, state / 'sessions')
    attachments = []
    pending = None
    print(f'\nReady. ai_agent-{agent_id.rsplit("/", 1)[-1]} · chat {label}')
    print('Type a message. /attach PATH adds a file. /new starts a new chat. /quit exits.')

    def wait_turn(turn):
        result = state / 'runs' / (turn['id'] + '-result.json')
        print('\nAgent:', flush=True)
        status = agent.watch(runtime.events, turn['id'], turn['dm'], agent_id, timeout=timeout,
                             result=result, render=show_event)
        if status == 0:
            response = agent.agent_response(json.loads(result.read_text()))
            directory = state / 'downloads' / turn['id']
            try:
                for file in agent.download_references(client, response, directory):
                    print('Downloaded: ' + str(Path(file['path']).resolve()))
            except (agent.ToolError, OSError) as exc:
                print(f'Download incomplete: {exc}\nSaved response: {result}')
        if status == 124:
            print('Use /wait to keep waiting. The server may still be running.')
        return status

    while True:
        try:
            message = read('\nYou> ').strip()
        except EOFError:
            return
        if not message:
            continue
        if message == '/quit':
            if pending:
                print('Closing callbacks; the pending server run may continue. Its receipt is saved.')
            return
        if message == '/help':
            print('/attach PATH · /files · /clear · /new · /wait · /quit')
            continue
        if message.startswith('/attach '):
            value = message[len('/attach '):].strip()
            if len(value) > 1 and value[0] == value[-1] and value[0] in '\'"':
                value = value[1:-1]
            path = Path(value).expanduser()
            if not path.is_file():
                print('File not found: ' + str(path))
            elif len(attachments) >= 10:
                print('Maximum 10 files per message.')
            else:
                attachments.append(str(path.resolve()))
                print('Attached for next message: ' + path.name)
            continue
        if message == '/files':
            print('\n'.join(attachments) or 'No files queued.')
            continue
        if message == '/clear':
            attachments.clear()
            print('Queued files cleared.')
            continue
        if message == '/wait':
            if pending:
                if wait_turn(pending) != 124:
                    pending = None
            else:
                print('No pending response.')
            continue
        if message == '/new':
            if pending:
                print('Use /wait before starting another chat, or /quit to stop listening.')
                continue
            label = 'chat-' + uuid.uuid4().hex[:8]
            dm = agent.ensure_dm(client, agent_id, user, label, state / 'sessions')
            attachments.clear()
            print('Started ' + label)
            continue
        if message.startswith('/'):
            print('Unknown command. Use /help.')
            continue
        if pending:
            print('Previous request is unresolved. Use /wait to avoid submitting it twice.')
            continue
        request_id = str(uuid.uuid4())
        receipt = state / 'runs' / (request_id + '.json')
        try:
            artifacts = []
            for filename in attachments:
                print('Uploading ' + Path(filename).name + '…')
                artifacts.append(client.upload(filename))
            args = SimpleNamespace(agent=agent_id, session=dm, parent=dm, message=message,
                webhook=runtime.webhook, request_id=request_id, api_request_id=False, agent_version=None)
            body = agent.invoke_body(args, artifacts)
            agent.save_json(receipt, {'status': 'submitting', 'request_id': request_id, 'request': body})
            pending = {'id': request_id, 'dm': dm}
            # Never automatically retry an invocation with an ambiguous outcome.
            accepted = client.call('ai-agents.events.execute-async', body)
            agent.save_json(receipt, {'status': 'accepted', 'request_id': request_id,
                                     'request': body, 'response': accepted})
            attachments.clear()
        except (agent.ToolError, OSError) as exc:
            print(f'Unable to submit: {exc}')
            if re.search(r'HTTP (400|401|403|404|409|413|422|429):', str(exc)):
                pending = None
            if pending:
                print(f'Request may be unresolved. Receipt: {receipt}. Use /wait.')
            continue
        if wait_turn(pending) != 124:
            pending = None


def choose_agent(client, user, state, supplied=None, read=input):
    key = hashlib.sha256((client.base + '\0' + user).encode()).hexdigest()[:16]
    prefs = state / ('agent-' + key + '.json')
    previous = json.loads(prefs.read_text()).get('agent') if prefs.exists() else None
    while True:
        value = supplied
        if value is None:
            prompt = f'Agent [{previous}]: ' if previous else 'Agent (for example ai_agent-48): '
            value = read(prompt).strip() or previous
        if not value:
            print('Enter an agent ID such as ai_agent-48.')
            continue
        if not (re.fullmatch(r'ai_agent-[0-9]+', value) or
                supplied and (value.startswith('don:') or re.fullmatch(r'ai_agent/[0-9]+', value))):
            if supplied:
                raise agent.ToolError('Use an agent ID such as ai_agent-48')
            print('Use ai_agent- followed by its number, for example ai_agent-48.')
            continue
        selected = agent.canonical_agent(value, user)
        try:
            client.call('ai-agents.get', {'id': selected}, 'GET')
        except agent.ToolError as exc:
            if supplied or not any(code in str(exc) for code in ('HTTP 400', 'HTTP 403', 'HTTP 404')):
                raise
            print('Agent unavailable to this token; choose another ID.')
            continue
        agent.save_json(prefs, {'agent': 'ai_agent-' + selected.rsplit('/', 1)[-1]})
        return selected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent', help='Skip the prompt, e.g. ai_agent-48')
    parser.add_argument('--base-url', default=os.getenv('DEVREV_BASE_URL', 'https://api.devrev.ai/internal'))
    parser.add_argument('--state-dir', type=Path, default=Path(__file__).resolve().parent / '.agent-state' / 'chat')
    parser.add_argument('--session', default='default', help='Resume this local conversation label')
    parser.add_argument('--login', action='store_true', help='Replace the saved token')
    parser.add_argument('--forget-token', action='store_true')
    parser.add_argument('--no-save-token', action='store_true')
    parser.add_argument('--timeout', type=float, default=600)
    args = parser.parse_args(argv)
    def stop(signum, frame):
        raise KeyboardInterrupt
    if threading.current_thread() is threading.main_thread():
        for name in ('SIGTERM', 'SIGHUP'):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), stop)
    if args.forget_token:
        Credentials(args.base_url.rstrip('/')).delete()
        print('Saved token removed.')
        return 0
    args.state_dir = args.state_dir.resolve()
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.umask(0o077)
    with (args.state_dir / 'chat.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise agent.ToolError('Chat is already running in this folder') from None
        print('DevRev Agent Chat')
        client, user = login(args.base_url.rstrip('/'), args.login, not args.no_save_token)
        selected = choose_agent(client, user, args.state_dir, args.agent)
        runtime = Runtime(client, user, args.state_dir)
        try:
            runtime.start()
            interactive(client, runtime, selected, user, args.state_dir, args.session, args.timeout)
        finally:
            print('\nClosing temporary webhook and connection…')
            runtime.close()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print('\nChat closed.')
        sys.exit(130)
    except (agent.ToolError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(1)

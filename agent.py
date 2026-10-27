#!/usr/bin/env python3
"""Small stdlib-only client for DevRev's async agent API. See README.md."""
import argparse
import base64
import fcntl
import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid


class ToolError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check_url(url):
    u = urlsplit(url)
    if u.username or u.password or not u.hostname:
        raise ToolError('Invalid URL')
    if u.scheme != 'https' and not (u.scheme == 'http' and u.hostname in ('127.0.0.1', 'localhost', '::1')):
        raise ToolError('HTTPS is required (HTTP is allowed only for local tests)')


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as out:
        os.chmod(path, 0o600)
        json.dump(value, out, indent=2, ensure_ascii=False)
        out.write('\n')


class Client:
    def __init__(self, base=None, token=None, timeout=60):
        self.base = (base or os.getenv('DEVREV_BASE_URL', 'https://api.devrev.ai/internal')).rstrip('/')
        self.token = token or os.getenv('DEVREV_TOKEN', '')
        self.timeout = timeout
        check_url(self.base)
        if not self.token:
            raise ToolError('Set DEVREV_TOKEN to your API token')
        # Never forward the bearer token through redirects or to storage hosts.
        self.api_opener = build_opener(NoRedirect())
        self.storage_opener = build_opener(NoRedirect())

    def call(self, endpoint, body, method='POST'):
        url = self.base + '/' + endpoint
        data = None
        if method == 'GET':
            url += '?' + urlencode(body, doseq=True)
        else:
            data = json.dumps(body).encode()
        req = Request(url, data=data, method=method, headers={
            'Authorization': 'Bearer ' + self.token.removeprefix('Bearer '),
            'Content-Type': 'application/json', 'Accept': 'application/json'})
        try:
            with self.api_opener.open(req, timeout=self.timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            detail = exc.read(4096).decode(errors='replace').replace(self.token, '[redacted]')
            raise ToolError(f'{endpoint}: HTTP {exc.code}: {detail}') from None
        except (URLError, TimeoutError) as exc:
            raise ToolError(f'{endpoint}: connection failed; request outcome may be unknown. '
                            'Do not blindly resubmit an invocation.') from exc

    def current_user(self):
        return self.call('dev-users.self', {}, 'GET')['dev_user']['id']

    def create_dm(self, agent_id, user_id, title='Async agent API session'):
        return self.call('chats.create', {
            'type': 'dm', 'title': title, 'users': [user_id], 'is_default': False,
            'agent_members': [{'agent': agent_id}]})['chat']

    def upload(self, filename):
        path = Path(filename)
        if not path.is_file():
            raise ToolError(f'File not found: {path}')
        mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        prepared = self.call('artifacts.prepare', {'file_name': path.name, 'file_type': mime})
        check_url(prepared['url'])
        boundary = 'devrev-' + secrets.token_hex(16)
        # Spool multipart body to disk, so large input files are not read into RAM.
        with tempfile.TemporaryFile() as body:
            for field in prepared['form_data']:
                key = field['key']
                if any(c in key for c in '\r\n"'):
                    raise ToolError('Invalid upload form key')
                body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
                body.write(str(field['value']).encode() + b'\r\n')
            safe_name = re.sub(r'[\r\n"\\]', '_', path.name)
            body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{safe_name}"\r\nContent-Type: {mime}\r\n\r\n'.encode())
            with path.open('rb') as source:
                shutil.copyfileobj(source, body)
            body.write(f'\r\n--{boundary}--\r\n'.encode())
            size = body.tell()
            body.seek(0)
            req = Request(prepared['url'], data=body, method='POST', headers={
                'Content-Type': 'multipart/form-data; boundary=' + boundary,
                'Content-Length': str(size)})
            try:
                with self.storage_opener.open(req, timeout=self.timeout) as response:
                    response.read()
            except (HTTPError, URLError, TimeoutError):
                raise ToolError(f'Upload failed for prepared artifact {prepared["id"]}; it was not attached to an agent') from None
        return prepared['id']

    def download(self, artifact, destination):
        destination = Path(destination)
        if destination.exists():
            raise ToolError(f'Refusing to overwrite {destination}')
        located = self.call('artifacts.locate', {'id': artifact}, 'GET')
        check_url(located['url'])
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with self.storage_opener.open(located['url'], timeout=self.timeout) as response:
                with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as out:
                    temp_path = Path(out.name)
                    shutil.copyfileobj(response, out)
            # Exclusive link avoids overwriting even if another process created the target.
            os.link(temp_path, destination)
        except (HTTPError, URLError, TimeoutError):
            raise ToolError(f'Download failed for {artifact}') from None
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        return str(destination)


def webhook_secret():
    try:
        secret = base64.b64decode(os.environ.get('DEVREV_WEBHOOK_SECRET', ''), validate=True)
    except ValueError:
        raise ToolError('DEVREV_WEBHOOK_SECRET must be base64') from None
    if not 8 <= len(secret) <= 32:
        raise ToolError('Set DEVREV_WEBHOOK_SECRET to base64-encoded 8–32 random bytes (see secret command)')
    return secret


def payload(event):
    # Guide examples wrap the event in payload; raw WebhookEventRequest is flat.
    return event.get('payload', event)


def agent_response(event):
    return payload(event).get('ai_agent_response')


def describe(event):
    response = agent_response(event)
    if not response:
        return
    kind = response.get('agent_response', 'unknown')
    if kind == 'message':
        print(response.get('message', ''), flush=True)
        for ref in response.get('file_references', []):
            print(f'[file] {ref.get("status", "")} {ref.get("path", "")} {artifact_id(ref) or "(no artifact published)"}', flush=True)
    elif kind == 'error':
        print('[error] ' + json.dumps(response.get('error', {})), flush=True)
    else:
        print(f'[{kind}] ' + json.dumps(response.get(kind, {}), ensure_ascii=False), flush=True)


def artifact_id(ref):
    # Gateway emits artifact: {id: DON}, not Synapse's internal artifact_id field.
    artifact = ref.get('artifact')
    return artifact.get('id') if isinstance(artifact, dict) else artifact if isinstance(artifact, str) else None


def download_references(client, response, directory):
    downloaded = []
    for ref in response.get('file_references', []):
        aid = artifact_id(ref)
        if not aid:
            print(f'Skipping unpublished artifact: {ref.get("path", "")}', file=sys.stderr)
            continue
        # Treat runtime paths as labels, never as local filesystem instructions.
        name = re.sub(r'[^\w.\-]', '_', str(ref.get('path', 'artifact')).replace('\\', '/').rsplit('/', 1)[-1])
        name = name[:160] if name not in ('', '.', '..') else 'artifact'
        suffix = hashlib.sha256((aid + '\0' + ref.get('path', '')).encode()).hexdigest()[:12]
        dest = Path(directory) / f'{suffix}-{name}'
        downloaded.append({'artifact': aid, 'path': client.download(aid, dest)})
    return downloaded


def make_receiver(host, port, events, secret, raw=False, quiet=False):
    events = Path(events)
    events.parent.mkdir(parents=True, exist_ok=True)
    events.touch(mode=0o600, exist_ok=True)
    os.chmod(events, 0o600)
    lock = threading.Lock()
    seen = set()
    with events.open(encoding='utf-8') as source:
        for line in source:
            try:
                old = json.loads(line)
                if old.get('id'):
                    seen.add((old.get('webhook_id'), old['id']))
            except (ValueError, AttributeError):
                pass

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.path != '/webhook':
                return self.reply(404, {'error': 'Use /webhook'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                return self.reply(400, {'error': 'Invalid Content-Length'})
            if not 0 < size <= 8 * 1024 * 1024:
                return self.reply(413, {'error': 'Body must be 1 byte to 8 MiB'})
            self.connection.settimeout(10)
            try:
                body = self.rfile.read(size)
            except TimeoutError:
                return self.reply(408, {'error': 'Body read timed out'})
            signature = base64.b64encode(hmac.digest(secret, body, 'sha256')).decode()
            if not hmac.compare_digest(signature, self.headers.get('X-DevRev-Signature', '')):
                return self.reply(401, {'error': 'Invalid webhook signature'})
            try:
                event = json.loads(body)
                if not isinstance(event, dict) or not isinstance(payload(event), dict):
                    raise ValueError('Expected an object')
                stamp = event.get('timestamp')
                if stamp:
                    sent = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                    if abs((datetime.now(timezone.utc) - sent).total_seconds()) > 300:
                        return self.reply(401, {'error': 'Stale event timestamp'})
                content = payload(event)
                if content.get('type') == 'verify':
                    challenge = content['verify']['challenge']
                    if not isinstance(challenge, str):
                        raise ValueError('Invalid challenge')
                    return self.reply(200, {'challenge': challenge})
            except (ValueError, KeyError, TypeError, AttributeError):
                return self.reply(400, {'error': 'Invalid event JSON'})
            key = (event.get('webhook_id'), event.get('id'))
            with lock:
                if key[1] and key in seen:
                    return self.reply(200, {'ok': True, 'duplicate': True})
                try:
                    with events.open('a', encoding='utf-8') as out:
                        out.write(json.dumps(event, ensure_ascii=False) + '\n')
                        out.flush()
                        os.fsync(out.fileno())
                except OSError:
                    return self.reply(500, {'error': 'Could not persist event'})
                if key[1]:
                    seen.add(key)
            # Persist before acknowledgement. Downloads happen in the watcher.
            self.reply(200, {'ok': True})
            if not quiet:
                if raw:
                    emit(event)
                else:
                    describe(event)

    return ThreadingHTTPServer((host, port), Handler)


def watch(events, request_id, session=None, agent=None, timeout=600, raw=False,
          result=None, client=None, download_dir=None, render=None):
    deadline = time.monotonic() + timeout
    path = Path(events)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    seen = set()
    with path.open(encoding='utf-8') as source:
        while time.monotonic() < deadline:
            offset = source.tell()
            line = source.readline()
            if not line or not line.endswith('\n'):
                source.seek(offset)
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
                continue
            event = json.loads(line)
            response = agent_response(event)
            if not response or response.get('client_metadata', {}).get('tool_request_id') != request_id:
                continue
            if session and response.get('session_object') != session:
                continue
            if agent and response.get('agent') != agent:
                continue
            key = (event.get('webhook_id'), event.get('id'))
            if key[1] and key in seen:
                continue
            if key[1]:
                seen.add(key)
            if render:
                render(event)
            else:
                emit(event) if raw else describe(event)
            kind = response.get('agent_response')
            if kind in ('message', 'error'):
                if result:
                    save_json(result, event)
                if kind == 'error':
                    return 2
                if download_dir:
                    emit({'downloads': download_references(client, response, download_dir)})
                return 0
    print('Timed out waiting for a terminal callback. Execution may still be running; resume with watch.', file=sys.stderr)
    return 124


def canonical_agent(value, user_id):
    if re.fullmatch(r'ai_agent-[A-Za-z0-9._-]+', value):
        value = value.replace('ai_agent-', 'ai_agent/', 1)
    if re.fullmatch(r'ai_agent/[A-Za-z0-9._-]+', value):
        match = re.match(r'don:identity:([^:]+):devo/([^:]+):', user_id)
        if not match:
            raise ToolError('Cannot expand short agent ID; provide its full DON')
        return f'don:core:{match[1]}:devo/{match[2]}:{value}'
    return value


def ensure_dm(client, agent_id, user_id, label, state_dir, parent=None, title=None):
    """Create one private DM per local conversation label, or reuse a given DM."""
    def validate(chat):
        if chat.get('type') != 'dm' or not chat.get('id'):
            raise ToolError('Session parent must be a DM')
        return chat['id']

    if parent:
        return validate(client.call('chats.get', {'id': parent}, 'GET')['chat'])
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(json.dumps([client.base, agent_id, user_id, label]).encode()).hexdigest()
    record = directory / (key + '.json')
    # Serialize concurrent calls for the same label to avoid duplicate DMs.
    with (directory / (key + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if record.exists():
            saved = json.loads(record.read_text())
            return validate(client.call('chats.get', {'id': saved['parent']}, 'GET')['chat'])
        parent_id = validate(client.create_dm(agent_id, user_id, title or f'Agent API: {label}'))
        save_json(record, {'parent': parent_id, 'session_label': label, 'agent': agent_id, 'user': user_id})
        return parent_id


def invoke_body(args, artifacts):
    if len(artifacts) > 10:
        raise ToolError('message_context.artifacts allows at most 10 artifacts')
    body = {'agent': args.agent, 'session_object': args.session,
            'event': {'input_message': {'message': args.message}},
            'webhook_target': {'webhook': args.webhook},
            'agent_options': {'runtime': 'container'},
            'client_metadata': {'tool_request_id': args.request_id}}
    if args.api_request_id:
        body['request_id'] = args.request_id
    if not args.parent:
        raise ToolError('A DM parent is required')
    body['parent'] = args.parent
    if args.agent_version:
        body['agent_version'] = args.agent_version
    if artifacts:
        body['message_context'] = {'artifacts': artifacts}
    return body


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base-url', default=os.getenv('DEVREV_BASE_URL', 'https://api.devrev.ai/internal'))
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('secret', help='Generate a base64 webhook signing secret')
    srv = sub.add_parser('serve', help='Receive signed callbacks and display live progress')
    srv.add_argument('--host', default='127.0.0.1')
    srv.add_argument('--port', type=int, default=8787)
    srv.add_argument('--events', default='events.jsonl')
    srv.add_argument('--json', action='store_true')
    create = sub.add_parser('webhook-create', help='Register a running public HTTPS receiver')
    create.add_argument('--url', required=True)
    create.add_argument('--wait', type=float, default=60, help='Seconds to wait for active status')
    parent = sub.add_parser('parent-create', help='Create a private non-default agent DM as a session parent')
    parent.add_argument('--agent', default=os.getenv('DEVREV_AGENT'))
    parent.add_argument('--user', help='Defaults to dev-users.self')
    parent.add_argument('--title', default='Async agent API session')
    get = sub.add_parser('webhook-get')
    get.add_argument('id')
    upload = sub.add_parser('upload')
    upload.add_argument('files', nargs='+')
    dl = sub.add_parser('download')
    dl.add_argument('artifact')
    dl.add_argument('--output', required=True)
    files = sub.add_parser('download-files', help='Download file_references from a saved final callback')
    files.add_argument('event')
    files.add_argument('--directory', default='downloads')
    inv = sub.add_parser('invoke', help='Submit to the async API with runtime=container')
    inv.add_argument('--agent', default=os.getenv('DEVREV_AGENT'))
    inv.add_argument('--webhook', default=os.getenv('DEVREV_WEBHOOK'))
    inv.add_argument('--parent', help='Reuse an existing DM; otherwise a private DM is created automatically')
    inv.add_argument('--user', help='Defaults to dev-users.self')
    inv.add_argument('--state-dir', default='.agent-state', help='Local label-to-DM records (no token stored)')
    inv.add_argument('--title', help='Title for a newly created DM')
    inv.add_argument('--agent-version')
    inv.add_argument('--session', default=None, help='Local conversation label; reuse it to reuse the same DM')
    inv.add_argument('--request-id', default=None, help='Local callback correlation ID; generated if omitted')
    inv.add_argument('--api-request-id', action='store_true', help='Also send request_id to the API (newer deployments only)')
    inv.add_argument('--message', required=True)
    inv.add_argument('--file', action='append', default=[])
    inv.add_argument('--artifact', action='append', default=[])
    inv.add_argument('--wait', action='store_true')
    inv.add_argument('--dry-run', action='store_true', help='Print payload without any API calls; use --artifact for file IDs')
    inv.add_argument('--receipt', default=None)
    watcher = sub.add_parser('watch', help='Replay/tail receiver log for one invocation')
    watcher.add_argument('--request-id', required=True)
    watcher.add_argument('--session')
    watcher.add_argument('--agent')
    for cmd in (inv, watcher):
        cmd.add_argument('--events', default='events.jsonl')
        cmd.add_argument('--timeout', type=float, default=600)
        cmd.add_argument('--json', action='store_true')
        cmd.add_argument('--result', default=None)
        cmd.add_argument('--download-dir')
    args = p.parse_args(argv)
    if args.command == 'secret':
        print(base64.b64encode(secrets.token_bytes(32)).decode())
        return 0
    if args.command == 'serve':
        server = make_receiver(args.host, args.port, args.events, webhook_secret(), args.json)
        print(f'Listening on http://{args.host}:{server.server_port}/webhook; log: {Path(args.events).resolve()}', file=sys.stderr)
        try:
            server.serve_forever()
        finally:
            server.server_close()
        return 0
    if args.command == 'invoke':
        if not args.agent or not args.webhook:
            raise ToolError('Set --agent/DEVREV_AGENT and --webhook/DEVREV_WEBHOOK')
        if len(args.file) + len(args.artifact) > 10:
            raise ToolError('At most 10 input files/artifacts per invocation')
        for filename in args.file:
            if not Path(filename).is_file():
                raise ToolError(f'File not found: {filename}')
        args.session = args.session or str(uuid.uuid4())
        args.session_label = args.session
        args.request_id = args.request_id or str(uuid.uuid4())
        if args.download_dir and not args.wait:
            raise ToolError('--download-dir requires --wait on invoke')
        if args.dry_run:
            if args.file:
                raise ToolError('--dry-run cannot upload --file; use existing --artifact IDs')
            args.parent = args.parent or '<DM_CREATED_OR_REUSED_ON_INVOKE>'
            args.session = args.parent
            emit(invoke_body(args, args.artifact))
            return 0
    client = None if args.command == 'watch' and not args.download_dir else Client(args.base_url)
    if args.command == 'webhook-create':
        check_url(args.url)
        if urlsplit(args.url).scheme != 'https':
            raise ToolError('Webhook registration requires a publicly reachable HTTPS URL')
        secret = webhook_secret()
        response = client.call('webhooks.create', {'url': args.url, 'event_types': ['ai_agent_response'],
                                                  'secret': base64.b64encode(secret).decode()})
        hook = response['webhook']
        emit({'webhook': hook['id'], 'status': hook.get('status')})
        deadline = time.monotonic() + args.wait
        while hook.get('status') != 'active' and time.monotonic() < deadline:
            time.sleep(min(1, max(0, deadline - time.monotonic())))
            hook = client.call('webhooks.get', {'id': hook['id']}, 'GET')['webhook']
        emit({'webhook': hook['id'], 'status': hook.get('status')})
        if hook.get('status') != 'active':
            raise ToolError('Webhook created but not active yet. Check receiver/tunnel and use webhook-get; do not create a duplicate.')
    elif args.command == 'parent-create':
        if not args.agent:
            raise ToolError('Set --agent or DEVREV_AGENT')
        user_id = args.user or client.current_user()
        chat = client.create_dm(canonical_agent(args.agent, user_id), user_id, args.title)
        emit({'parent': chat['id'], 'session_object': chat['id'], 'title': chat.get('title'),
              'agents': [{'agent': entry.get('agent_id', {}).get('id'),
                          'options': entry.get('options')} for entry in chat.get('agents', [])]})
    elif args.command == 'webhook-get':
        hook = client.call('webhooks.get', {'id': args.id}, 'GET')['webhook']
        emit({k: hook.get(k) for k in ('id', 'status', 'url', 'event_types')})
    elif args.command == 'upload':
        for file in args.files:
            emit({'file': file, 'artifact': client.upload(file)})
    elif args.command == 'download':
        emit({'artifact': args.artifact, 'path': client.download(args.artifact, args.output)})
    elif args.command == 'download-files':
        response = agent_response(json.loads(Path(args.event).read_text()))
        if not response:
            raise ToolError('Expected a saved ai_agent_response callback')
        emit({'downloads': download_references(client, response, args.directory)})
    elif args.command == 'invoke':
        hook = client.call('webhooks.get', {'id': args.webhook}, 'GET')['webhook']
        if hook.get('status') != 'active' or 'ai_agent_response' not in hook.get('event_types', []):
            raise ToolError('Webhook must be active and subscribed to ai_agent_response')
        user_id = args.user or client.current_user()
        args.agent = canonical_agent(args.agent, user_id)
        args.parent = ensure_dm(client, args.agent, user_id, args.session_label,
                                args.state_dir, args.parent, args.title)
        args.session = args.parent
        print(json.dumps({'parent': args.parent, 'session_label': args.session_label}), file=sys.stderr)
        artifacts = list(args.artifact)
        for file in args.file:
            aid = client.upload(file)
            artifacts.append(aid)
            print(json.dumps({'uploaded_file': file, 'artifact': aid}), file=sys.stderr)
        body = invoke_body(args, artifacts)
        receipt = args.receipt or f'run-{uuid.uuid4()}.json'
        info = {'request_id': args.request_id, 'session_object': args.session,
                'session_label': args.session_label, 'parent': args.parent, 'agent': args.agent,
                'artifacts': artifacts, 'status': 'submitting', 'request': body}
        save_json(receipt, info)
        emit({'receipt': receipt, 'request_id': args.request_id, 'session_object': args.session})
        response = client.call('ai-agents.events.execute-async', body)
        info.update(status='accepted', response=response)
        save_json(receipt, info)
        emit(info)
        print('Requested container runtime; gateway rollout and session routing determine the actual runtime. Live callbacks contain progress and final text, not token deltas.', file=sys.stderr)
        if args.wait:
            return watch(args.events, args.request_id, args.session, args.agent, args.timeout,
                         args.json, args.result, client, args.download_dir)
    elif args.command == 'watch':
        return watch(args.events, args.request_id, args.session, args.agent, args.timeout,
                     args.json, args.result, client, args.download_dir)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except (ToolError, OSError, ValueError, KeyError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(1)

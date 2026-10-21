#!/usr/bin/env python3
"""AI Control API for a configured two-GPU SSH dispatcher."""
import asyncio
import base64
from collections import deque
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import time

from control.allocation import allocation_plan, normalise_allocation

from aiohttp import ClientError, ClientSession, ClientTimeout, web
from aiohttp_session import get_session, new_session, setup
from aiohttp_session.cookie_storage import EncryptedCookieStorage
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError

ROOT = Path(__file__).resolve().parent
TASKS = web.AppKey('tasks', set)
USER = web.RequestKey('user', str)


def validate_catalogue(catalogue):
    """Fail at startup when required runtime rules or safe SSH identifiers are missing."""
    if not isinstance(catalogue, dict):
        raise ValueError('catalogue must be an object.')
    for key in ['media', 'models', 'services']:
        if key not in catalogue or not isinstance(catalogue[key], list):
            raise ValueError(f'catalogue.{key} must be a list.')
    rules = catalogue.get('rules')
    if not isinstance(rules, dict):
        raise ValueError('catalogue.rules must be an object.')
    for key in ['media', 'profiles', 'roles', 'runtime_names', 'workloads']:
        if key not in rules or not isinstance(rules[key], dict):
            raise ValueError(f'catalogue.rules.{key} must be an object.')
    if rules['profiles'].get('off') != []:
        raise ValueError('catalogue.rules.profiles.off must be an empty list.')
    if rules['media'] != {'image': ['0', '1'], 'pair': ['0,1'], 'off': []}:
        raise ValueError('catalogue.rules.media must define the two-GPU image, pair and off choices.')
    if set(rules['profiles']) != {'off', *rules['workloads']}:
        raise ValueError('catalogue.rules.profiles must match the configured workloads.')
    safe = re.compile(r'[a-z0-9][a-z0-9_.-]*')
    for profile, item in rules['workloads'].items():
        if not isinstance(profile, str) or not safe.fullmatch(profile) or not isinstance(item, dict):
            raise ValueError('catalogue.rules.workloads contains an unsafe profile identifier.')
        for key in ['kind', 'label', 'runtime']:
            if not isinstance(item.get(key), str) or not item[key]:
                raise ValueError(f'catalogue.rules.workloads.{profile}.{key} is required.')
        if item['kind'] not in {'image', 'text', 'video'} or not safe.fullmatch(item['runtime']):
            raise ValueError(f'catalogue.rules.workloads.{profile} has an invalid kind or runtime.')
        if item['runtime'] not in rules['runtime_names']:
            raise ValueError(f'catalogue.rules.runtime_names.{item["runtime"]} is required.')
        allowed = rules['profiles'].get(profile)
        if not isinstance(allowed, list) or not allowed or any(value not in {'0', '1', '0,1'} for value in allowed):
            raise ValueError(f'catalogue.rules.profiles.{profile} needs supported GPU placements.')
    for kind in ['image', 'video']:
        profile = rules['roles'].get(kind)
        if not isinstance(profile, str) or profile not in rules['workloads'] or rules['workloads'][profile]['kind'] != kind:
            raise ValueError(f'catalogue.rules.roles.{kind} must name a configured {kind} profile.')
    for key, expected in [('models', {'off', *[profile for profile, item in rules['workloads'].items() if item['kind'] == 'text']}), ('media', {'off', 'image', 'pair'})]:
        items = catalogue[key]
        if any(not isinstance(item, dict) or not isinstance(item.get('id'), str) or not isinstance(item.get('label'), str) for item in items) or {item['id'] for item in items} != expected:
            raise ValueError(f'catalogue.{key} must label every configured choice.')
    for item in catalogue['services']:
        if not isinstance(item, dict) or any(not isinstance(item.get(key), str) or not item[key] for key in ['display_label', 'group', 'title', 'url']):
            raise ValueError('catalogue.services entries require display_label, group, title and url.')


def normalise(payload, catalogue):
    if not isinstance(payload, dict):
        raise ValueError('Supply a selection object.')
    kind = payload.get('kind')
    key = 'model' if kind == 'text' else 'mode'
    if kind not in {'text', 'media', 'video'}:
        raise ValueError('Choose text, media or video.')
    selected = payload.get(key)
    values = {item['id'] for item in catalogue['models' if kind == 'text' else 'media']}
    if kind == 'video':
        values = {'on', 'off'}
    if selected not in values:
        raise ValueError('Choose a known model or mode.')
    rule = 'profiles' if kind == 'text' else 'media'
    allowed = catalogue['rules']['profiles'][catalogue['rules']['roles']['video']] if kind == 'video' and selected == 'on' else ([] if kind == 'video' else catalogue['rules'][rule][selected])
    supplied = payload.get('gpus')
    # A fixed placement is a model requirement, never a scheduling decision.
    placement = allowed[0] if len(allowed) == 1 else (supplied or ('1' if kind in {'media', 'video'} else '0'))
    if allowed and placement not in allowed:
        raise ValueError('Choose a GPU placement supported by this selection.')
    if 'replace' in payload and type(payload['replace']) is not bool:
        raise ValueError('Replace must be true or false.')
    result = {'kind': kind, key: selected, 'gpus': placement if allowed else '0', 'replace': payload.get('replace', False)}
    if 'runtime' in payload:
        if kind != 'media' or selected != 'off' or payload['runtime'] != catalogue['rules']['workloads'][catalogue['rules']['roles']['image']]['runtime']:
            raise ValueError('Choose image media to stop.')
        result['runtime'] = payload['runtime']
    return result


def normalise_restart(payload, rules):
    if not isinstance(payload, dict) or set(payload) != {'runtime', 'profile', 'devices'}:
        raise ValueError('Supply the running workload, profile and GPUs to restart.')
    runtime, profile, devices = payload['runtime'], payload['profile'], payload['devices']
    if not isinstance(runtime, str) or runtime not in rules['runtime_names']:
        raise ValueError('Choose a known runtime.')
    if not isinstance(profile, str) or profile not in rules['workloads'] or rules['workloads'][profile]['runtime'] != runtime:
        raise ValueError('Choose a known workload for this runtime.')
    if not isinstance(devices, list) or any(not isinstance(d, str) for d in devices) or ','.join(sorted(devices)) not in rules['profiles'][profile]:
        raise ValueError('Choose the supported GPU placement of this workload.')
    return {'runtime': runtime, 'profile': profile, 'devices': sorted(devices)}


def selection_plan(selection, status, rules):
    kind = selection['kind']
    value = selection.get('model', selection.get('mode'))
    image_profile, video_profile = rules['roles']['image'], rules['roles']['video']
    image_runtime = rules['workloads'][image_profile]['runtime']
    video_runtime = rules['workloads'][video_profile]['runtime']
    targets = []
    if value != 'off':
        if kind == 'text':
            targets = [(rules['workloads'][value]['runtime'], value, selection['gpus'])]
        elif kind == 'video':
            targets = [(video_runtime, video_profile, selection['gpus'])]
        elif value == 'pair':
            targets = [(image_runtime, image_profile, '0'), (video_runtime, video_profile, '1')]
        else:
            targets = [(image_runtime, image_profile, selection['gpus'])]
    allocation = [{'profile': profile, 'devices': devices.split(',')} for _, profile, devices in targets]
    stopping = set()
    if value == 'off':
        stopping = {video_runtime} if kind == 'video' else {item['runtime'] for item in rules['workloads'].values() if item['kind'] == 'text'}
        if kind == 'media':
            stopping = {selection['runtime']} if 'runtime' in selection else {image_runtime, video_runtime}
    preview = allocation_plan(allocation, status, rules=rules, stopping=stopping, complete=False)
    # The selection API lists requested starts even when they are already healthy.
    return {'selection': selection, 'stop': preview['stop'], 'start': [{'runtime': r, 'profile': p, 'devices': d.split(',')} for r, p, d in targets]}


def selection_command(selection):
    kind = selection['kind']
    value = selection.get('model', selection.get('mode'))
    if kind == 'text':
        return f"text:start --gpus {selection['gpus']} --model {value}" + (' --replace' if selection['replace'] else '')
    if kind == 'media':
        if value == 'off' and 'runtime' in selection:
            return 'media:stop'
        if not selection['replace'] and value != 'off':
            if value == 'pair':
                raise ValueError('The image/video pair requires an explicit Apply selection.')
            return f"media:start --gpus {selection['gpus']}"
        return f"media:select --gpus {selection['gpus']} --mode {value}"
    return 'video:stop' if value == 'off' else f"video:start --gpus {selection['gpus']}" + (' --replace' if selection['replace'] else '')


async def ssh(command, on_output=None):
    process = await asyncio.create_subprocess_exec('ssh', '-F', '/ssh/config', os.environ['CONTROL_SSH_HOST'], command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=262144)

    async def read(stream):
        output = ''
        while line := await stream.readline():
            value = line.decode(errors='replace')
            # Keep command results bounded even for long build or benchmark logs.
            output = (output + value)[-60000:]
            if on_output:
                on_output(value)
        return output

    try:
        stdout, stderr = await asyncio.wait_for(asyncio.gather(read(process.stdout), read(process.stderr)), timeout=1200)
        await process.wait()
    except (Exception, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    return process.returncode, stdout, stderr


def radiance_dashboard(html):
    # Replace the pinned dashboard's browser key handling with same-origin requests.
    start, end = 'let apiKey = "";', 'const HIST ='
    if html.count(start) != 1 or html.count(end) != 1:
        raise web.HTTPBadGateway(text='The Radiance dashboard changed; its authentication adapter needs updating.')
    before, rest = html.split(start, 1)
    _, after = rest.split(end, 1)
    adapter = """async function api(path) {
  const r = await fetch("/radiance/" + path, { cache: "no-store" });
  if (r.status === 401) { location.assign("/"); throw new Error("Sign in to AI Control."); }
  return r;
}
"""
    html = before + adapter + end + after
    scripts = re.findall(r'<script>(.*?)</script>', html, re.S)
    if len(scripts) != 1:
        raise web.HTTPBadGateway(text='The Radiance dashboard script changed; its authentication adapter needs updating.')
    digest = base64.b64encode(hashlib.sha256(scripts[0].encode()).digest()).decode()
    policy = f"default-src 'none'; connect-src 'self'; img-src 'self' data:; script-src 'sha256-{digest}'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    return html, policy


def create_app(config, runner=ssh, secure_cookies=True):
    validate_catalogue(config['catalogue'])
    session_key = hashlib.sha256(config['session_key'].encode()).digest()
    jobs = {}
    operation = None
    status_cache = None
    status_time = 0
    status_lock = asyncio.Lock()
    login_attempts = {}
    hasher = PasswordHasher()
    radiance_client = None

    def remember_job(job):
        if len(jobs) >= 30:
            finished = next((key for key, value in jobs.items() if value['state'] != 'running'), None)
            if finished:
                jobs.pop(finished)
        jobs[job['id']] = job

    @web.middleware
    async def authenticate(request, handler):
        if (request.path.startswith('/api/') or request.path.startswith('/radiance/')) and request.path not in {'/api/health', '/api/login'}:
            bearer = request.headers.get('Authorization', '')
            token_auth = bool(bearer.startswith('Bearer ') and hmac.compare_digest(bearer[7:], config['api_key']))
            session = await get_session(request)
            user = session.get('user')
            if session.get('expires', 0) <= time.time() or user not in config['users']:
                user = None
            if not token_auth and not user:
                if request.path == '/radiance/':
                    raise web.HTTPFound('/')
                raise web.HTTPUnauthorized(text='Sign in to AI Control.')
            request[USER] = 'api' if token_auth else user
            if request.method != 'GET' and not token_auth:
                if request.headers.get('Origin') not in config['origins'] or request.headers.get('X-AI-Control') != '1':
                    raise web.HTTPForbidden(text='Use the AI Control interface for this request.')
        try:
            response = await handler(request)
        except ValueError as error:
            response = web.json_response({'error': str(error)}, status=400)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        if 'Content-Security-Policy' not in response.headers:
            response.headers['Content-Security-Policy'] = "default-src 'self'; connect-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    app = web.Application(client_max_size=16384)
    setup(app, EncryptedCookieStorage(session_key, cookie_name='ai_session', httponly=True, secure=secure_cookies, samesite='Strict', max_age=28800))
    app.middlewares.append(authenticate)

    async def login(request):
        if request.headers.get('Origin') not in config['origins']:
            raise web.HTTPForbidden()
        attempts = login_attempts.setdefault(request.remote, deque(maxlen=10))
        now = time.time()
        if len([t for t in attempts if now - t < 60]) >= 5:
            raise web.HTTPTooManyRequests(text='Try signing in again in a minute.')
        attempts.append(now)
        payload = await request.json()
        name, password = payload.get('username', ''), payload.get('password', '')
        stored = config['users'].get(name)
        valid = False
        if stored and isinstance(password, str) and len(password) < 1024:
            try:
                valid = await asyncio.to_thread(hasher.verify, stored, password)
            except (VerificationError, InvalidHashError):
                pass
        if not valid:
            raise web.HTTPUnauthorized(text='Check your username and password.')
        session = await new_session(request)
        session['user'] = name
        session['expires'] = int(now) + 28800
        response = web.json_response({'username': name})
        return response

    async def status(request=None, fresh=False):
        nonlocal status_cache, status_time
        async with status_lock:
            if fresh or status_cache is None or time.monotonic() - status_time > 3:
                code, output, _ = await runner('gpu:status --json')
                if code:
                    raise web.HTTPServiceUnavailable(text='Runtime status is unavailable.')
                status_cache = json.loads(output)
                status_time = time.monotonic()
        data = dict(status_cache)
        data['job'] = jobs.get(operation) if operation else None
        return web.json_response(data) if request else data

    async def plan(request):
        selected = normalise(await request.json(), config['catalogue'])
        return web.json_response(selection_plan(selected, await status(fresh=True), config['catalogue']['rules']))

    async def execute(job, command):
        nonlocal operation, status_time
        try:
            def progress(value):
                job['updated_at'] = time.time()
                if command != 'check' and value.startswith('@@ai-stage '):
                    job['stage'] = value[len('@@ai-stage '):].strip()
                else:
                    job['output'] = (job['output'] + value)[-30000:]
            code, output, error = await runner(command, on_output=progress)
            job['finished_at'] = time.time()
            job['stage'] = 'Ready' if code == 0 else 'Failed'
            job.update(state='completed' if code == 0 else 'failed', exit_code=code, output=job['output'] or (output + error)[-30000:])
        except Exception:
            job.update(state='failed', stage='Connection Lost', finished_at=time.time(), exit_code=1, output=job['output'] + '\nThe control connection failed. Check live status before retrying.')
        finally:
            status_time = 0
            if operation == job['id']:
                operation = None

    def start_gpu_job(command, **details):
        nonlocal operation
        operation = secrets.token_hex(12)
        job = {'id': operation, 'state': 'running', **details, 'stage': 'Preparing', 'started_at': time.time(), 'updated_at': time.time(), 'output': ''}
        remember_job(job)
        task = asyncio.create_task(execute(job, command))
        app[TASKS].add(task)
        task.add_done_callback(app[TASKS].discard)
        return web.json_response(job, status=202)

    async def submit(request):
        if operation:
            raise web.HTTPConflict(text='A control change is already running.')
        selected = normalise(await request.json(), config['catalogue'])
        preview = selection_plan(selected, await status(fresh=True), config['catalogue']['rules'])
        if preview['stop'] and not selected['replace']:
            return web.json_response({'error': 'Runtime conflict: review and confirm the replacement.', 'plan': preview}, status=409)
        # Check again after status I/O so two concurrent requests cannot both start.
        if operation:
            raise web.HTTPConflict(text='A control change is already running.')
        return start_gpu_job(selection_command(selected), selection=selected)

    async def board_plan(request):
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError('Supply a GPU board object.')
        allocation = normalise_allocation(payload.get('allocation'), config['catalogue']['rules'])
        return web.json_response(allocation_plan(allocation, await status(fresh=True), rules=config['catalogue']['rules']))

    async def board_submit(request):
        if operation:
            raise web.HTTPConflict(text='A control change is already running.')
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError('Supply a GPU board object.')
        allocation = normalise_allocation(payload.get('allocation'), config['catalogue']['rules'])
        preview = allocation_plan(allocation, await status(fresh=True), rules=config['catalogue']['rules'])
        if operation:
            raise web.HTTPConflict(text='A control change is already running.')
        if payload.get('signature') != preview['signature']:
            raise web.HTTPConflict(text='The live allocation changed. Review the refreshed preview before applying.')
        if payload.get('replace') is not True:
            raise web.HTTPConflict(text='Review and explicitly apply the complete allocation.')
        encoded = json.dumps(allocation, separators=(',', ':')).encode().hex()
        return start_gpu_job('gpu:apply --allocation ' + encoded, allocation=allocation)

    async def restart(request):
        if operation:
            raise web.HTTPConflict(text='A control change is already running.')
        selected = normalise_restart(await request.json(), config['catalogue']['rules'])
        current = (await status(fresh=True)).get('runtimes', {}).get(selected['runtime'], {})
        if operation:
            raise web.HTTPConflict(text='A control change is already running.')
        if not current.get('running'):
            raise web.HTTPConflict(text='Only a running workload can be restarted.')
        # Restart exactly the workload the user reviewed; never a replacement or new placement.
        if current.get('profile') != selected['profile'] or current.get('devices') != selected['devices']:
            raise web.HTTPConflict(text='The live workload changed. Review the refreshed status before restarting.')
        command = f"gpu:restart --runtime {selected['runtime']} --profile {selected['profile']} --gpus {','.join(selected['devices'])}"
        return start_gpu_job(command, restart=selected)

    async def maintenance(request):
        nonlocal operation
        payload = await request.json()
        command = {'benchmark': 'text:bench', 'check': 'check'}.get(payload.get('action'))
        if not command:
            raise ValueError('Choose benchmark or check.')
        if operation:
            raise web.HTTPConflict(text='A control change is already running.')
        operation = secrets.token_hex(12)
        job = {'id': operation, 'state': 'running', 'action': payload['action'], 'stage': 'Running ' + payload['action'].title(), 'started_at': time.time(), 'updated_at': time.time(), 'output': ''}
        remember_job(job)
        task = asyncio.create_task(execute(job, command))
        app[TASKS].add(task)
        task.add_done_callback(app[TASKS].discard)
        return web.json_response(job, status=202)

    async def history(request):
        return web.json_response([{key: value for key, value in job.items() if key != 'output'} for job in list(jobs.values())[-5:][::-1]])

    async def job(request):
        result = jobs.get(request.match_info['id'])
        if not result:
            raise web.HTTPNotFound()
        return web.json_response(result)

    async def catalogue(request):
        return web.json_response(config['catalogue'])

    async def session(request):
        return web.json_response({'username': request[USER]})

    async def logout(request):
        response = web.json_response({})
        (await get_session(request)).invalidate()
        return response

    async def health(request):
        return web.json_response({'status': 'healthy'})

    async def radiance_root(request):
        raise web.HTTPPermanentRedirect('/radiance/')

    async def radiance(request):
        path = request.match_info.get('path', '')
        if path not in {'', 'graph', 'server_info', 'sessions', 'stats'}:
            raise web.HTTPNotFound()
        if not config.get('radiance_url') or not config.get('radiance_api_key'):
            raise web.HTTPServiceUnavailable(text='Radiance dashboard access is not configured.')
        try:
            async with radiance_client.get(config['radiance_url'].rstrip('/') + '/' + path,
                                           headers={'Authorization': 'Bearer ' + config['radiance_api_key']},
                                           allow_redirects=False) as upstream:
                if upstream.status != 200:
                    raise web.HTTPBadGateway(text='Radiance is unavailable. Check its runtime status.')
                body = await upstream.read()
                if not path:
                    html, policy = radiance_dashboard(body.decode())
                    return web.Response(text=html, content_type='text/html', headers={'Content-Security-Policy': policy})
                return web.Response(body=body, content_type='application/json')
        except (ClientError, asyncio.TimeoutError):
            raise web.HTTPBadGateway(text='Radiance is unavailable. Check its runtime status.') from None

    async def radiance_connection(app):
        nonlocal radiance_client
        async with ClientSession(timeout=ClientTimeout(total=10)) as client:
            radiance_client = client
            yield

    async def static(request):
        name = request.match_info.get('name', 'index.html')
        if name not in {'index.html', 'app.js', 'app.css', 'THIRD-PARTY-LICENCES.txt'}:
            raise web.HTTPNotFound()
        return web.FileResponse(ROOT / 'web/dist' / name)

    async def cleanup(app):
        for task in app[TASKS]:
            task.cancel()
        await asyncio.gather(*app[TASKS], return_exceptions=True)

    app[TASKS] = set()
    app.on_cleanup.append(cleanup)
    app.cleanup_ctx.append(radiance_connection)
    app.add_routes([web.get('/api/health', health), web.post('/api/login', login), web.get('/api/session', session), web.post('/api/logout', logout), web.get('/api/catalogue', catalogue), web.get('/api/status', status), web.post('/api/plan', plan), web.post('/api/board/plan', board_plan), web.post('/api/board', board_submit), web.post('/api/selections', submit), web.post('/api/restart', restart),web.post('/api/maintenance', maintenance), web.get('/api/jobs', history), web.get('/api/jobs/{id}', job), web.get('/radiance', radiance_root), web.get('/radiance/{path:.*}', radiance), web.get('/', static), web.get('/{name}', static)])
    return app


if __name__ == '__main__':
    configuration = json.loads(Path(os.environ['CONTROL_CONFIG']).read_text())
    configuration.update(api_key=os.environ['CONTROL_API_KEY'], session_key=os.environ['CONTROL_SESSION_KEY'], radiance_api_key=os.environ.get('RADIANCE_API_KEY', ''))
    web.run_app(create_app(configuration), host='0.0.0.0', port=1337, access_log=None)

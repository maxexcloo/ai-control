"""Smoke-test a built Docker image using private, synthetic runtime configuration."""
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def check(image):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('image_fixture', root / 'tests/fixtures.py')
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    catalogue = fixture.synthetic_catalogue()
    environment = dict(os.environ, CONTROL_API_KEY=secrets.token_hex(32), CONTROL_SESSION_KEY=secrets.token_hex(32))
    name = 'ai-control-check-' + secrets.token_hex(8)
    stage = 'container startup'
    created = False
    with tempfile.TemporaryDirectory(prefix='ai-control-image-') as temporary:
        config = Path(temporary) / 'control.json'
        config.write_text(json.dumps({'catalogue': catalogue, 'origins': ['http://smoke.invalid'], 'users': {}, 'engine': fixture.synthetic_engine()}))
        config.chmod(0o600)
        try:
            subprocess.run([
                'docker', 'run', '--detach', '--rm', '--name', name,
                '--publish', '127.0.0.1::1337',
                '--mount', f'type=bind,src={config},dst=/config/control.json,readonly',
                '--env', 'CONTROL_CONFIG=/config/control.json',
                '--env', 'CONTROL_API_KEY', '--env', 'CONTROL_SESSION_KEY', image,
            ], env=environment, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            created = True
            binding = subprocess.check_output(['docker', 'port', name, '1337/tcp'], text=True).strip()
            if not binding.startswith('127.0.0.1:') or '\n' in binding:
                raise RuntimeError('The smoke-test port was not bound to loopback.')
            base = 'http://' + binding

            def request(path, authenticated=False, bearer=None):
                token = bearer if bearer is not None else environment['CONTROL_API_KEY'] if authenticated else None
                headers = {'Authorization': 'Bearer ' + token} if token is not None else {}
                try:
                    with urllib.request.urlopen(urllib.request.Request(base + path, headers=headers), timeout=3) as response:
                        return response.status, response.read(2 * 1024 * 1024)
                except urllib.error.HTTPError as error:
                    error.close()
                    return error.code, b''

            deadline = time.monotonic() + 60
            while True:
                try:
                    if request('/api/health')[0] == 200:
                        break
                except (OSError, urllib.error.URLError):
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError('The container did not become healthy.')
                time.sleep(0.25)
            stage = 'built UI'
            code, html = request('/')
            if code != 200 or b'AI Control' not in html or b'app.js' not in html or request('/app.js')[0] != 200:
                raise RuntimeError('The built UI is unavailable.')
            stage = 'authentication'
            if request('/api/catalogue')[0] != 401 or request('/api/status')[0] != 401:
                raise RuntimeError('Unauthenticated API requests were not rejected.')
            if request('/api/catalogue', bearer=secrets.token_hex(32))[0] != 401:
                raise RuntimeError('An incorrect bearer token was not rejected.')
            code, body = request('/api/catalogue', authenticated=True)
            if code != 200 or json.loads(body) != catalogue:
                raise RuntimeError('The supplied catalogue was not returned to the authenticated client.')
            print('Image smoke passed: health, built UI, unauthenticated rejection and supplied catalogue. No engine operations were requested.')
        except Exception as error:
            raise SystemExit(f'Image smoke failed during {stage} ({type(error).__name__}); private test values withheld.') from None
        finally:
            if created:
                subprocess.run(['docker', 'rm', '--force', name], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python3 scripts/check_image.py IMAGE')
    check(sys.argv[1])

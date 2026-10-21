"""Select the explicitly configured container engine and its Compose files."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import unquote, urlparse

ROOT = Path(os.environ['AI_ROOT'])


def engine():
    try:
        value = os.environ['AI_CONTAINER_ENGINE']
    except KeyError:
        raise ValueError('AI_CONTAINER_ENGINE is required: docker or podman.') from None
    if value not in {'docker', 'podman'}:
        raise ValueError('AI_CONTAINER_ENGINE must be docker or podman.')
    return value


def command(*arguments):
    return [engine(), *arguments]


def compose(*arguments):
    selected = engine()
    prefix = ['docker', 'compose'] if selected == 'docker' else ['podman-compose']
    prefix.extend(['--file', str(ROOT / 'compose.yaml')])
    if selected == 'podman':
        prefix.extend(['--file', str(ROOT / 'compose.podman.yaml')])
    return [*prefix, *arguments]


def compose_environment():
    """Resolve inactive text-service interpolation without selecting a workload."""
    from deployment.workloads import CONTAINERS

    return dict(os.environ, GPU_CONTAINER=CONTAINERS['text'])


def socket_path():
    selected = engine()
    override = os.environ.get('CONTROL_ENGINE_SOCKET')
    if override is not None:
        if not Path(override).is_absolute():
            raise ValueError('CONTROL_ENGINE_SOCKET must be an absolute path.')
        return override
    if selected == 'podman':
        directory = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}'))
        if not directory.is_absolute():
            raise ValueError('XDG_RUNTIME_DIR must be an absolute path.')
        return str(directory / 'podman/podman.sock')
    if 'DOCKER_HOST' in os.environ:
        endpoint = os.environ['DOCKER_HOST']
    else:
        result = subprocess.run(['docker', 'context', 'inspect'], check=True, capture_output=True, text=True)
        endpoint = json.loads(result.stdout)[0]['Endpoints']['docker']['Host']
    parsed = urlparse(endpoint)
    if parsed.scheme != 'unix' or parsed.netloc or not Path(unquote(parsed.path)).is_absolute():
        raise ValueError('Docker requires a local unix socket endpoint; set CONTROL_ENGINE_SOCKET explicitly.')
    return unquote(parsed.path)


if __name__ == '__main__':
    try:
        if sys.argv[1:2] == ['--socket']:
            parser = argparse.ArgumentParser(description='Print only the selected local engine socket path.')
            parser.add_argument('--socket', action='store_true')
            parser.add_argument('--default-engine', choices=['docker', 'podman'])
            arguments = parser.parse_args()
            if arguments.default_engine:
                os.environ.setdefault('AI_CONTAINER_ENGINE', arguments.default_engine)
            print(socket_path())
        else:
            raise SystemExit(subprocess.run(compose(*sys.argv[1:])).returncode)
    except ValueError as error:
        raise SystemExit(str(error)) from None

#!/usr/bin/env python3
"""Verify installed worker images and externally supplied Compose configuration."""
import os
import pathlib
import re
import subprocess

import yaml
from deployment import runtime as container_runtime

root = pathlib.Path(os.environ['AI_ROOT'])


def check_comfyui_versions(root, services):
    images = sorted({service['image'] for name, service in services.items() if name.startswith('comfyui-')})
    for image in images:
        labels = '.Config.Labels' if container_runtime.engine() == 'docker' else '.Labels'
        expected = subprocess.check_output(container_runtime.command(
            'image', 'inspect', '--format',
            '{{index ' + labels + ' "io.excloo.comfyui.version"}}', image,
        ), text=True).strip().removeprefix('v')
        if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', expected):
            raise SystemExit('Missing ComfyUI image version metadata.')
        code = "import runpy; print(runpy.run_path('/root/ComfyUI/comfyui_version.py')['__version__'])"
        actual = subprocess.check_output(container_runtime.command(
            'run', '--rm', '--entrypoint', 'python3', image, '-c', code,
        ), text=True).strip()
        if actual != expected:
            raise SystemExit(f'{image}: packaged ComfyUI core {actual} differs from image metadata {expected}.')


def check_runtime():
    from deployment.studio_worker import verify

    services = yaml.safe_load((root / 'compose.yaml').read_text())['services']
    check_comfyui_versions(root, services)
    print('Packaged ComfyUI core versions match image metadata.')
    verify(root)

    # Text profiles share a container name because GPU selection is mutually exclusive.
    profiles = sorted({profile for service in services.values() for profile in service.get('profiles', [])})
    for profile in profiles:
        subprocess.run(container_runtime.compose('--profile', profile, 'config'), check=True,
                       stdout=subprocess.DEVNULL, cwd=root)
    print(f'Compose configuration passed for {len(profiles)} individual profiles.')



#!/usr/bin/env python3
"""Configure AI Control while retaining private access settings."""
import argparse
import json
import os
import pathlib
import re
import subprocess
from urllib.parse import urlparse

import yaml

from deployment.address import endpoints
from deployment.engine_config import resolve
from deployment.workloads import CONTAINERS, GPU_RULES, PROFILES, WORKLOADS, catalogue

SERVICES = {profile: item['service'] for profile, item in WORKLOADS.items() if 'service' in item}


def service_links(root):
    dns, secure = endpoints()

    def container(name, service):
        value = service['container_name']
        if value.startswith('${'):
            variable = value[2:].split(':', 1)[0].rstrip('}')
            if variable == 'GPU_CONTAINER' and name in PROFILES:
                return CONTAINERS[PROFILES[name][0]]
            value = os.environ[variable]
        return value

    result = []
    services = yaml.safe_load((root / 'compose.yaml').read_text())['services']
    for name, service in services.items():
        if name not in SERVICES:
            continue
        if service.get('network_mode') == 'host':
            raw = re.search(r'(?:^|\s)--port\s+(\d+)', ' '.join(service['command'])).group(1)
        else:
            binding = service['ports'][0]
            raw = binding.rsplit(':', 2)[-2]
        if raw.startswith('${'):
            variable = raw[2:-1]
            raw = os.environ[variable]
        port = int(raw)
        metadata = SERVICES[name]
        link = {'group': metadata['group'], 'display_label': metadata['display_label'], 'container': container(name, service), 'target': '_blank', 'title': metadata['title'], 'url': secure.get(port, f'http://{dns}:{port}/')}
        if metadata.get('radiance'):
            link['profiles'] = sorted(profile for profile in PROFILES if PROFILES[profile][0] == 'text' and services[profile]['image'] == service['image'])
        result.append(link)
    return result


def apply(root):
    """Retain the existing owner boundary and prepare the native control service."""
    output = pathlib.Path(os.environ['AI_DATA_DIR']) / 'control/data/control.json'
    if not output.is_file():
        raise SystemExit('Restore existing Control access settings before configuring the deployment.')
    current = json.loads(output.read_text())
    users = current['users']
    if not users:
        raise SystemExit('No existing authorised local users; no public access was created.')
    links = service_links(root)
    links.sort(key=lambda link: link['title'].casefold())
    inventory = catalogue()
    inventory['services'] = links
    radiance_title = next(item['title'] for item in SERVICES.values() if item.get('radiance'))
    radiance = next(link for link in links if link['title'] == radiance_title)
    radiance_url = radiance['url']
    radiance['url'] = '/radiance/'
    control_port = int(os.environ['CONTROL_PORT'])
    dns, secure = endpoints()
    dns = urlparse(secure.get(control_port, f'http://{dns}:{control_port}/')).hostname
    origins = current.get('origins', [])
    if current.get('host', dns) != dns:
        origins = [origin for origin in origins if origin != 'https://' + current['host']]
    origins += ['https://' + dns, f'http://{dns}:{control_port}']
    origins += [origin.strip() for origin in os.environ.get('CONTROL_ALLOWED_ORIGINS', '').split(',') if origin.strip()]
    if control_port in secure:
        origins.append(secure[control_port].rstrip('/'))
    if os.environ.get('AI_HOST'):
        origins.append(f"http://{os.environ['AI_HOST']}:{control_port}")
    origins = sorted(set(origins))
    engine = resolve(root)
    engine.update(socket='/run/engine.sock', state_dir='/state', lease_dir='/run/ai-gpu',
                  host_proc='/host/proc', host_sys='/host/sys')
    output.write_text(json.dumps({'engine': engine, 'catalogue': inventory, 'host': dns, 'origins': origins, 'radiance_url': radiance_url, 'users': users}, indent=2) + '\n')
    output.chmod(0o600)
    # New private keys join the user's single secret file; existing values survive.
    import secrets
    env = root / '.env'
    text = env.read_text() if env.exists() else ''
    for key in ['CONTROL_API_KEY', 'CONTROL_SESSION_KEY']:
        if not re.search(r'^' + key + r'=', text, re.M):
            text += '\n' + key + '=' + json.dumps(secrets.token_urlsafe(32)) + '\n'
    env.write_text(text)
    env.chmod(0o600)
    print('AI Control prepared; existing owner accounts and service boundaries retained.')
    for link in links:
        print(link['title'] + ': ' + link['url'])


if __name__ == '__main__':
    argparse.ArgumentParser(description=__doc__).parse_args()
    apply(pathlib.Path(os.environ['AI_ROOT']))

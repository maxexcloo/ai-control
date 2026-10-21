"""Check host-owned HTTPS aliases without changing live services."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

from deployment import address


class AddressTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {'AI_DNS': '', 'AI_HOST': '', 'AI_PROXY_ROUTES': ''})
        environment.start()
        self.addCleanup(environment.stop)

    def test_explicit_dns_precedes_host_without_requiring_tailscale(self):
        with patch.dict(os.environ, {'AI_DNS': 'custom.example', 'AI_HOST': '100.64.0.1'}), patch.object(address.subprocess, 'run', side_effect=FileNotFoundError) as run, patch.object(address, 'proxy_endpoints', return_value={1337: 'https://control.example/'}):
            self.assertEqual(address.endpoints(), ('custom.example', {1337: 'https://control.example/'}))
        run.assert_not_called()

    def test_explicit_host_is_the_dns_fallback(self):
        with patch.dict(os.environ, {'AI_HOST': '100.64.0.1'}), patch.object(address.subprocess, 'run', side_effect=FileNotFoundError), patch.object(address, 'proxy_endpoints', return_value={}):
            self.assertEqual(address.endpoints(), ('100.64.0.1', {}))

    def test_discovery_failure_without_an_override_is_not_hidden(self):
        with patch.object(address.subprocess, 'run', side_effect=FileNotFoundError), self.assertRaises(FileNotFoundError):
            address.endpoints()

    def test_tailscale_dns_name_and_proxy_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'routes.yaml'
            path.write_text(yaml.safe_dump({'http': {
                'routers': {'studio': {'rule': 'Host(`studio.example.com`)', 'service': 'studio', 'tls': {}}},
                'services': {'studio': {'loadBalancer': {'servers': [{'url': 'http://127.0.0.1:8788'}]}}},
            }}))
            status = {'Self': {'DNSName': 'bento.tail.example.'}}
            run = lambda arguments, **kwargs: subprocess.CompletedProcess(arguments, 0, stdout=json.dumps(status))
            with patch.dict(os.environ, AI_PROXY_ROUTES=str(path)), patch.object(address.subprocess, 'run', run):
                self.assertEqual(address.endpoints(), ('bento.tail.example', {8788: 'https://studio.example.com/'}))

    def test_proxy_routes_are_optional_and_explicit(self):
        self.assertEqual(address.proxy_endpoints(), {})
        with patch.dict(os.environ, AI_PROXY_ROUTES='relative/routes.yaml'):
            with self.assertRaisesRegex(ValueError, 'AI_PROXY_ROUTES'):
                address.proxy_endpoints()

    def test_missing_proxy_configuration_preserves_existing_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(address.proxy_endpoints(Path(directory) / 'missing.yaml'), {})

    def test_path_router_preserves_its_prefix_and_control_hostname(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'routes.yaml'
            path.write_text(yaml.safe_dump({'http': {
                'routers': {
                    'ai': {'rule': 'Host(`ai.example.com`)', 'service': 'ai', 'tls': {}},
                    'docs': {'rule': 'Host(`ai.example.com`) && (Path(`/docs`) || PathPrefix(`/docs/`))',
                             'service': 'docs', 'tls': {}},
                },
                'services': {name: {'loadBalancer': {'servers': [{'url': f'http://127.0.0.1:{port}'}]}}
                             for name, port in [('ai', 1337), ('docs', 8080)]},
            }}))
            self.assertEqual(address.proxy_endpoints(path), {1337: 'https://ai.example.com/', 8080: 'https://ai.example.com/docs/'})

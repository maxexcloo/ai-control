"""Exercise default socket ownership while retaining explicit runner injection."""
import json
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import AioHTTPTestCase

from control import server
from fixtures import synthetic_catalogue, synthetic_engine


class DefaultTransportTests(AioHTTPTestCase):
    async def get_application(self):
        self.dispatcher = AsyncMock()
        self.dispatcher.return_value = (0, json.dumps({'runtimes': {}}), '')
        self.factory = patch.object(server, 'SocketDispatcher', return_value=self.dispatcher)
        self.factory_mock = self.factory.start()
        self.addCleanup(self.factory.stop)
        self.config = {'api_key': 'synthetic-api-key', 'session_key': 'synthetic-session-key',
                       'users': {}, 'origins': ['http://test.invalid'],
                       'catalogue': synthetic_catalogue(), 'engine': synthetic_engine()}
        return server.create_app(self.config, secure_cookies=False)

    async def test_default_transport_handles_status_and_is_closed_on_cleanup(self):
        self.factory_mock.assert_called_once_with(self.config)
        result = await self.client.get('/api/status', headers={'Authorization': 'Bearer synthetic-api-key'})
        self.assertEqual(result.status, 200)
        self.dispatcher.assert_awaited_once_with('gpu:status --json')
        self.dispatcher.close.assert_not_awaited()
        await self.client.close()
        self.dispatcher.close.assert_awaited_once()

    async def test_health_and_catalogue_do_not_open_engine_connections(self):
        self.assertEqual((await self.client.get('/api/health')).status, 200)
        result = await self.client.get('/api/catalogue', headers={'Authorization': 'Bearer synthetic-api-key'})
        self.assertEqual(result.status, 200)
        self.dispatcher.assert_not_awaited()


class InjectedTransportTests(AioHTTPTestCase):
    async def get_application(self):
        self.runner = AsyncMock(return_value=(0, json.dumps({'runtimes': {}}), ''))
        self.factory = patch.object(server, 'SocketDispatcher')
        self.factory_mock = self.factory.start()
        self.addCleanup(self.factory.stop)
        return server.create_app({'api_key': 'synthetic-api-key', 'session_key': 'synthetic-session-key',
                                  'users': {}, 'origins': [], 'catalogue': synthetic_catalogue()},
                                 runner=self.runner, secure_cookies=False)

    async def test_injected_runner_is_used_without_constructing_or_closing_a_dispatcher(self):
        result = await self.client.get('/api/status', headers={'Authorization': 'Bearer synthetic-api-key'})
        self.assertEqual(result.status, 200)
        self.runner.assert_awaited_once_with('gpu:status --json')
        self.factory_mock.assert_not_called()
        await self.client.close()
        self.runner.close.assert_not_awaited()

"""Exercise authentication, placement rules and conflict handling without GPUs or a live engine."""
import asyncio
import importlib.util
import json
from pathlib import Path
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestServer
from argon2 import PasswordHasher

from fixtures import synthetic_catalogue

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('control_server',ROOT/'control/server.py')
server=importlib.util.module_from_spec(spec);spec.loader.exec_module(server)

class ControlAPITests(AioHTTPTestCase):
    async def get_application(self):
        self.calls=[]
        self.hold=asyncio.Event();self.hold.set()
        catalogue=synthetic_catalogue()
        self.status={'runtimes':{'text':{'running':True,'profile':'dual-text','devices':['0','1'],'health':'healthy','config_current':True}}}
        async def runner(command,on_output=None):
            self.calls.append(command)
            if command=='gpu:status --json':return 0,json.dumps(self.status),''
            if on_output:
                on_output('@@ai-stage Waiting For Readiness\n')
                on_output('Loading model weights\n')
            await self.hold.wait()
            return 0,'Applied',''
        return server.create_app({'api_key':'test-only-api-key','session_key':'test-only-session-key','users':{'owner':PasswordHasher().hash('test-password')},'origins':['http://test.example'],'catalogue':catalogue},runner,secure_cookies=False)
    @property
    def headers(self):return {'Authorization':'Bearer test-only-api-key'}
    async def test_failed_job_retains_error_after_streamed_output(self):
        async def failing_runner(command, on_output=None):
            if command == 'gpu:status --json':
                return 0, json.dumps(self.status), ''
            on_output('@@ai-stage Starting Workload\n')
            on_output('Checking GPU reservations\n')
            return 1, 'Checking GPU reservations\n', 'A GPU has an unknown reservation.'
        app = server.create_app({'api_key': 'test-only-api-key', 'session_key': 'test-only-session-key',
                                 'users': {}, 'origins': [], 'catalogue': synthetic_catalogue()},
                                failing_runner, secure_cookies=False)
        async with TestServer(app) as instance:
            from aiohttp import ClientSession
            async with ClientSession() as client:
                result = await client.post(instance.make_url('/api/maintenance'), headers=self.headers,
                                           json={'action': 'benchmark'})
                job = await result.json()
                await asyncio.sleep(.01)
                result = await client.get(instance.make_url('/api/jobs/' + job['id']), headers=self.headers)
                finished = await result.json()
        self.assertEqual(finished['state'], 'failed')
        self.assertEqual(finished['output'], 'Checking GPU reservations\nA GPU has an unknown reservation.')
    async def test_status_requires_authentication(self):
        result=await self.client.get('/api/status');self.assertEqual(result.status,401)
        self.assertEqual(self.calls,[])
    async def test_unknown_profile_is_rejected_before_engine_dispatch(self):
        result=await self.client.post('/api/selections',headers=self.headers,json={'kind':'text','model':'shell;command'})
        self.assertEqual(result.status,400);self.assertEqual(self.calls,[])
    async def test_fixed_model_automatically_uses_both_gpus(self):
        result=await self.client.post('/api/selections',headers=self.headers,json={'kind':'text','model':'dual-text','gpus':'0'})
        self.assertEqual(result.status,202);job=await result.json()
        await asyncio.sleep(.01)
        self.assertIn('text:start --gpus 0,1 --model dual-text',self.calls)
        result=await self.client.get('/api/jobs/'+job['id'],headers=self.headers)
        self.assertEqual((await result.json())['state'],'completed')
    async def test_model_library_routes_stay_in_studio(self):
        for path in ['/api/library', '/api/downloads']:
            result = await self.client.get(path, headers=self.headers)
            self.assertEqual(result.status, 404, path)
            result = await self.client.post(path, headers=self.headers, json={})
            self.assertIn(result.status, {404, 405}, path)
        self.assertEqual(self.calls, [])
        result = await self.client.get('/api/status', headers=self.headers)
        self.assertNotIn('download_job', await result.json())

    async def test_conflicting_conservative_start_never_changes_state(self):
        result=await self.client.post('/api/selections',headers=self.headers,json={'kind':'text','model':'single-text','gpus':'0'})
        self.assertEqual(result.status,409);self.assertEqual(self.calls,['gpu:status --json'])
    async def test_replacement_is_explicit_and_serialised(self):
        self.hold.clear()
        selection={'kind':'text','model':'single-text','gpus':'0','replace':True}
        result=await self.client.post('/api/selections',headers=self.headers,json=selection);self.assertEqual(result.status,202)
        result=await self.client.post('/api/selections',headers=self.headers,json=selection);self.assertEqual(result.status,409)
        self.hold.set()
    async def test_restart_dispatches_only_the_reviewed_running_workload(self):
        self.hold.clear()
        payload={'runtime':'text','profile':'dual-text','devices':['1','0']}
        result=await self.client.post('/api/restart',headers=self.headers,json=payload)
        self.assertEqual(result.status,202);job=await result.json()
        self.assertEqual(job['restart'],{'runtime':'text','profile':'dual-text','devices':['0','1']})
        result=await self.client.post('/api/restart',headers=self.headers,json=payload)
        self.assertEqual(result.status,409)
        await asyncio.sleep(.01)
        self.assertEqual([call for call in self.calls if call!='gpu:status --json'],[{'action':'gpu:restart','runtime':'text','profile':'dual-text','devices':['0','1']}])
        self.hold.set()

    async def test_restart_rejects_stopped_changed_or_invalid_workloads_before_engine_dispatch(self):
        for payload in [{'runtime':'shell;command','profile':'single-text','devices':['0']},{'runtime':'text','profile':'render-model','devices':['0']},{'runtime':'text','profile':'dual-text','devices':['0']},{'runtime':'text','profile':'single-text','devices':[]},{'runtime':'text','profile':'single-text','devices':['0'],'replace':True},['text']]:
            result=await self.client.post('/api/restart',headers=self.headers,json=payload)
            self.assertEqual(result.status,400)
        self.assertEqual(self.calls,[])
        for payload in [{'runtime':'render-pool','profile':'render-model','devices':['0']},{'runtime':'text','profile':'single-text','devices':['0']}]:
            result=await self.client.post('/api/restart',headers=self.headers,json=payload)
            self.assertEqual(result.status,409)
        self.assertTrue(all(command=='gpu:status --json' for command in self.calls))

    async def test_restart_requires_control_interface_for_sessions(self):
        result=await self.client.post('/api/login',headers={'Origin':'http://test.example'},json={'username':'owner','password':'test-password'})
        self.assertEqual(result.status,200)
        payload={'runtime':'text','profile':'dual-text','devices':['0','1']}
        result=await self.client.post('/api/restart',headers={'Origin':'http://test.example'},json=payload)
        self.assertEqual(result.status,403)
        result=await self.client.post('/api/restart',headers={'Origin':'http://other.example','X-AI-Control':'1'},json=payload)
        self.assertEqual(result.status,403)
        self.assertEqual(self.calls,[])
        result=await self.client.post('/api/restart',headers={'Origin':'http://test.example','X-AI-Control':'1'},json=payload)
        self.assertEqual(result.status,202)
        result=await self.client.post('/api/restart',headers={'Origin':'http://test.example','X-Other-Control':'1'},json=payload)
        self.assertEqual(result.status,403)

    async def test_password_session_needs_same_origin_for_changes(self):
        result=await self.client.post('/api/login',headers={'Origin':'http://test.example'},json={'username':'owner','password':'test-password'})
        self.assertEqual(result.status,200)
        result=await self.client.get('/api/session');self.assertEqual(result.status,200)
        result=await self.client.post('/api/selections',json={'kind':'text','model':'off','replace':True})
        self.assertEqual(result.status,403)
        self.assertEqual(self.calls,[])
    async def test_session_cookie_security_tampering_expiry_and_logout(self):
        from unittest.mock import patch
        origin = {'Origin': 'http://test.example', 'X-AI-Control': '1'}
        async def login():
            result = await self.client.post('/api/login', headers=origin, json={'username': 'owner', 'password': 'test-password'})
            self.assertEqual(result.status, 200)
            return result.cookies['ai_session']
        cookie = await login()
        self.assertTrue(cookie['httponly'])
        self.assertEqual(cookie['samesite'], 'Strict')
        self.assertEqual(cookie['max-age'], '28800')
        result = await self.client.post('/api/logout', headers=origin)
        self.assertEqual(result.status, 200)
        self.assertEqual((await self.client.get('/api/session')).status, 401)
        await login()
        with patch.object(server.time, 'time', return_value=server.time.time() + 28801):
            self.assertEqual((await self.client.get('/api/session')).status, 401)
        await login()
        self.client.session.cookie_jar.clear()
        result = await self.client.get('/api/session', headers={'Cookie': 'ai_session=' + cookie.value[:-4] + 'AAAA'})
        self.assertEqual(result.status, 401)

    async def test_plan_reports_exact_conflict_and_pair_placement(self):
        result=await self.client.post('/api/plan',headers=self.headers,json={'kind':'media','mode':'pair','gpus':'0'})
        data=await result.json();self.assertEqual(data['stop'],['text'])
        self.assertEqual([item['devices'] for item in data['start']],[['0'],['1']])

    async def test_shared_video_allocation_is_separate_and_conflicts_with_selections(self):
        self.status={'runtimes':{'motion-pool':{'running':True,'profile':'motion-model','devices':['1'],'health':'healthy','config_current':True}}}
        allocation=[{'profile':'render-model','devices':['0']},{'profile':'motion-model','devices':['1']}]
        rules=synthetic_catalogue()['rules']
        plan=server.allocation_plan(server.normalise_allocation(allocation,rules),self.status,rules=rules)
        self.assertEqual(plan['keep'],['motion-pool'])
        self.assertEqual(plan['start'],[{'profile':'render-model','devices':['0'],'runtime':'render-pool'}])
        result=await self.client.post('/api/plan',headers=self.headers,json={'kind':'text','model':'single-text','gpus':'1'})
        self.assertEqual((await result.json())['stop'],['motion-pool'])

    async def test_selection_preserves_unrelated_runtime_and_requested_healthy_start(self):
        self.status = {'runtimes': {
            'text': {'running': True, 'profile': 'single-text', 'devices': ['0'], 'health': 'healthy', 'config_current': True},
            'render-pool': {'running': True, 'profile': 'render-model', 'devices': ['1'], 'health': 'healthy', 'config_current': True},
        }}
        result = await self.client.post('/api/plan', headers=self.headers, json={'kind': 'text', 'model': 'single-text', 'gpus': '0'})
        preview = await result.json()
        self.assertEqual(preview['stop'], [])
        self.assertEqual(preview['start'], [{'runtime': 'text', 'profile': 'single-text', 'devices': ['0']}])
        result = await self.client.post('/api/board/plan', headers=self.headers, json={'allocation': [{'profile': 'single-text', 'devices': ['0']}]})
        preview = await result.json()
        self.assertEqual(preview['keep'], ['text'])
        self.assertEqual(preview['start'], [])
        self.assertEqual(preview['stop'], ['render-pool'])

    async def test_selection_stop_is_scoped_to_requested_runtime(self):
        self.status = {'runtimes': {
            'text': {'running': True, 'profile': 'single-text', 'devices': ['0'], 'health': 'healthy', 'config_current': True},
            'render-pool': {'running': True, 'profile': 'render-model', 'devices': ['1'], 'health': 'healthy', 'config_current': True},
            'motion-pool': {'running': True, 'profile': 'motion-model', 'devices': ['0'], 'health': 'healthy', 'config_current': True},
        }}
        result = await self.client.post('/api/plan', headers=self.headers, json={'kind': 'media', 'mode': 'off', 'runtime': 'render-pool'})
        preview = await result.json()
        self.assertEqual(preview['stop'], ['render-pool'])
        self.assertEqual(preview['start'], [])

    async def test_changed_configuration_requires_apply_for_the_same_allocation(self):
        self.status['runtimes']['text']['config_current'] = False
        result = await self.client.post('/api/board/plan', headers=self.headers,
                                       json={'allocation': [{'profile': 'dual-text', 'devices': ['0', '1']}]})
        plan = await result.json()
        self.assertEqual(plan['keep'], [])
        self.assertEqual(plan['stop'], ['text'])
        self.assertEqual(plan['start'][0]['profile'], 'dual-text')

    async def test_malformed_login_and_maintenance_payloads_return_bad_request(self):
        for payload in [[], {'username': []}, {'password': []}]:
            result = await self.client.post('/api/login', headers={'Origin': 'http://test.example'}, json=payload)
            self.assertEqual(result.status, 400)
        for payload in [[], {'action': []}]:
            result = await self.client.post('/api/maintenance', headers=self.headers, json=payload)
            self.assertEqual(result.status, 400)

    async def test_board_preview_identifies_unchanged_dual_model(self):
        result=await self.client.post('/api/board/plan',headers=self.headers,json={'allocation':[{'profile':'dual-text','devices':['0','1']}]})
        self.assertEqual(result.status,200)
        data=await result.json()
        self.assertEqual(data['keep'],['text']);self.assertEqual(data['start'],[]);self.assertEqual(data['stop'],[])

    async def test_board_rejects_retired_studio_workloads_before_engine_dispatch(self):
        for retired in ['render-model-x', 'motion-model-x']:
            allocation=[{'profile':'render-model','devices':['0']},{'profile':retired,'devices':['1']}]
            for endpoint in ['/api/board/plan','/api/board']:
                result=await self.client.post(endpoint,headers=self.headers,json={'allocation':allocation,'replace':True})
                self.assertEqual(result.status,400)
        self.assertEqual(self.calls,[])

    async def test_selection_reports_gpu_conflict(self):
        self.status={'runtimes':{'render-pool':{'running':True,'profile':'render-model','devices':['1'],'health':'healthy','config_current':True}}}
        selection={'kind':'video','mode':'on','gpus':'1'}
        result=await self.client.post('/api/plan',headers=self.headers,json=selection)
        self.assertEqual((await result.json())['stop'],['render-pool'])
        result=await self.client.post('/api/selections',headers=self.headers,json=selection)
        self.assertEqual(result.status,409)
        self.assertTrue(all(command=='gpu:status --json' for command in self.calls))

    async def test_board_rejects_stale_preview_without_mutation(self):
        allocation=[{'profile':'single-text','devices':['0']}]
        result=await self.client.post('/api/board/plan',headers=self.headers,json={'allocation':allocation})
        preview=await result.json()
        self.status['runtimes']['text']['health']='starting'
        result=await self.client.post('/api/board',headers=self.headers,json={'allocation':allocation,'signature':preview['signature'],'replace':True})
        self.assertEqual(result.status,409)
        self.assertTrue(all(command=='gpu:status --json' for command in self.calls))

    async def test_board_requires_confirmation_and_one_operation(self):
        allocation=[{'profile':'render-model','devices':['0']},{'profile':'motion-model','devices':['1']}]
        result=await self.client.post('/api/board/plan',headers=self.headers,json={'allocation':allocation})
        preview=await result.json()
        payload={'allocation':allocation,'signature':preview['signature']}
        result=await self.client.post('/api/board',headers=self.headers,json=payload)
        self.assertEqual(result.status,409)
        payload['replace']=True;self.hold.clear()
        result=await self.client.post('/api/board',headers=self.headers,json=payload)
        self.assertEqual(result.status,202);job=await result.json()
        result=await self.client.post('/api/board',headers=self.headers,json=payload)
        self.assertEqual(result.status,409)
        await asyncio.sleep(.01)
        commands=[command for command in self.calls if isinstance(command,dict) and command['action']=='gpu:apply']
        self.assertEqual(len(commands),1)
        self.assertEqual(commands[0]['allocation'],allocation)
        self.hold.set();await asyncio.sleep(.01)
        result=await self.client.get('/api/jobs/'+job['id'],headers=self.headers)
        self.assertEqual((await result.json())['state'],'completed')

    async def test_invalid_board_never_reaches_engine_dispatch(self):
        for allocation in [[{'profile':'single-text','devices':['0']},{'profile':'render-model','devices':['0']}],[{'profile':'dual-text','devices':['0']}],[{'profile':'render-model','devices':['0','1']}]]:
            result=await self.client.post('/api/board',headers=self.headers,json={'allocation':allocation,'replace':True})
            self.assertEqual(result.status,400)
        self.assertEqual(self.calls,[])

    async def test_running_job_exposes_progress_before_command_finishes(self):
        self.hold.clear()
        result=await self.client.post('/api/selections',headers=self.headers,json={'kind':'text','model':'dual-text'})
        job=await result.json();await asyncio.sleep(.01)
        result=await self.client.get('/api/jobs/'+job['id'],headers=self.headers)
        live=await result.json()
        self.assertEqual(live['state'],'running')
        self.assertEqual(live['stage'],'Waiting For Readiness')
        self.assertIn('Loading model weights',live['output'])
        self.assertGreaterEqual(live['updated_at'],live['started_at'])
        self.hold.set()

    async def test_video_plan_and_start_honour_either_single_gpu(self):
        for device in ['0','1']:
            result=await self.client.post('/api/plan',headers=self.headers,json={'kind':'video','mode':'on','gpus':device})
            self.assertEqual((await result.json())['start'][0]['devices'],[device])
            result=await self.client.post('/api/selections',headers=self.headers,json={'kind':'video','mode':'on','gpus':device,'replace':True})
            self.assertEqual(result.status,202);await asyncio.sleep(.01)
            self.assertIn('video:start --gpus '+device+' --replace',self.calls)

    async def test_recent_jobs_are_available_after_a_client_refresh(self):
        result=await self.client.post('/api/maintenance',headers=self.headers,json={'action':'check'})
        job=await result.json();await asyncio.sleep(.01)
        result=await self.client.get('/api/jobs',headers=self.headers)
        history=await result.json()
        self.assertEqual(history[0]['id'],job['id'])
        self.assertEqual(history[0]['action'],'check')
        self.assertEqual(history[0]['state'],'completed')
        self.assertNotIn('output',history[0])
        result=await self.client.get('/api/jobs/'+job['id'],headers=self.headers)
        self.assertIn('Loading model weights',(await result.json())['output'])
        result=await self.client.get('/api/jobs',headers={})
        self.assertEqual(result.status,401)


class RadianceProxyTests(AioHTTPTestCase):
    async def get_application(self):
        self.requests=[]
        self.upstream_status=200
        self.html='<html><script>let apiKey = ""; localStorage.getItem("radiance.key"); const HIST = 120;</script></html>'
        async def upstream(request):
            self.requests.append((request.path,request.headers.get('Authorization')))
            if self.upstream_status!=200:return web.Response(status=self.upstream_status,text='Private upstream diagnostic')
            if request.path=='/':return web.Response(text=self.html,content_type='text/html')
            return web.json_response({'live':True})
        backend=web.Application();backend.router.add_get('/{path:.*}',upstream)
        self.backend=TestServer(backend);await self.backend.start_server()
        async def unused_runner(command, on_output=None):
            raise AssertionError('Proxy tests must not invoke the engine.')
        app=server.create_app({'api_key':'control-only-test-key','session_key':'session-only-test-key','radiance_api_key':'radiance-only-test-key','radiance_url':str(self.backend.make_url('/')),'users':{'owner':PasswordHasher().hash('test-password')},'origins':['http://test.example'],'catalogue':synthetic_catalogue()},runner=unused_runner,secure_cookies=False)
        async def close_backend(app):await self.backend.close()
        app.on_cleanup.append(close_backend)
        return app
    @property
    def headers(self):return {'Authorization':'Bearer control-only-test-key'}
    async def test_dashboard_and_data_require_control_authentication(self):
        result=await self.client.get('/radiance/',allow_redirects=False);self.assertEqual(result.status,302);self.assertEqual(result.headers['Location'],'/')
        result=await self.client.get('/radiance/stats');self.assertEqual(result.status,401);self.assertEqual(self.requests,[])
    async def test_dashboard_uses_same_origin_requests_without_browser_key(self):
        result=await self.client.get('/radiance/',headers=self.headers);self.assertEqual(result.status,200)
        html=await result.text();self.assertIn('fetch("/radiance/" + path',html);self.assertNotIn('localStorage',html);self.assertNotIn('radiance-only-test-key',html)
        self.assertIn("script-src 'sha256-",result.headers['Content-Security-Policy']);self.assertEqual(result.headers['Cache-Control'],'no-store')
        self.assertEqual(self.requests,[('/','Bearer radiance-only-test-key')])
    async def test_session_opens_live_dashboard_data(self):
        result=await self.client.post('/api/login',headers={'Origin':'http://test.example'},json={'username':'owner','password':'test-password'});self.assertEqual(result.status,200)
        for path in ['stats','server_info','graph','sessions']:
            result=await self.client.get('/radiance/'+path);self.assertEqual(result.status,200);self.assertEqual(await result.json(),{'live':True})
        self.assertTrue(all(key=='Bearer radiance-only-test-key' for path,key in self.requests))
    async def test_only_dashboard_get_endpoints_are_forwarded(self):
        result=await self.client.get('/radiance/v1/models',headers=self.headers);self.assertEqual(result.status,404)
        result=await self.client.post('/radiance/stats',headers=self.headers);self.assertEqual(result.status,405)
        self.assertEqual(self.requests,[])
    async def test_unavailable_backend_hides_upstream_diagnostics(self):
        self.upstream_status=401
        result=await self.client.get('/radiance/stats',headers=self.headers);self.assertEqual(result.status,502);self.assertNotIn('Private upstream',await result.text())
    async def test_changed_dashboard_fails_closed(self):
        self.html='<html><script>changed upstream</script></html>'
        result=await self.client.get('/radiance/',headers=self.headers);self.assertEqual(result.status,502)

    async def test_bare_dashboard_path_redirects_to_the_authenticated_path(self):
        result = await self.client.get('/radiance', allow_redirects=False)
        self.assertEqual(result.status, 308)
        self.assertEqual(result.headers['Location'], '/radiance/')
        self.assertEqual(self.requests, [])

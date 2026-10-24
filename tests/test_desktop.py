"""Desktop lifecycle checks with fake GUI and real owned loopback server; no model calls."""
import asyncio
import http.client
import json
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jianying_engine.desktop import DesktopHost, activate_existing, navigation_allowed, window_dimensions
from jianying_engine.workbench import Workbench, create_app, main


class FakeWindow:
    def __init__(self, confirm=True):
        self.confirm = confirm
        self.confirmations = 0
        self.native = None
        self.restored = self.shown = 0
    def create_confirmation_dialog(self, title, message):
        self.confirmations += 1
        return self.confirm
    def restore(self):
        self.restored += 1
    def show(self):
        self.shown += 1


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='desktop-workbench-test-')
        self.root = Path(self.temp.name)
        self.service = Workbench(self.root / 'state', self.root)
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.port = self.sock.getsockname()[1]
        self.host = DesktopHost(self.service, self.sock, f'http://127.0.0.1:{self.port}/', self.root)
        self.host.window = FakeWindow()
    def tearDown(self):
        self.host.shutdown()
        self.temp.cleanup()
    def start(self):
        app = create_app(self.service, 'page-secret', self.port, desktop=self.host, activate_token='activate-secret')
        self.host.start_server(app)
    def post(self, token, origin=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        headers={'X-Workbench-Token':token, 'Content-Type':'application/json'}
        if origin:
            headers['Origin']=origin
        try:
            connection.request('POST','/api/activate',body='{}',headers=headers)
            response=connection.getresponse(); response.read()
            return response.status
        finally:
            connection.close()
    def test_idle_close_exits_owned_server_thread(self):
        self.start()
        self.assertTrue(self.host.thread.is_alive())
        self.assertTrue(self.host.request_close())
        self.assertEqual(self.host.window.confirmations,0)
        self.assertFalse(self.host.thread.is_alive())
        self.assertEqual(self.sock.fileno(),-1)
        self.host.shutdown()
    def test_busy_cancel_preserves_state_then_confirm_persists_interruption(self):
        session=self.service.new_session()
        session['status']='running'
        self.service.active_id=session['id']
        self.start()
        self.host.window.confirm=False
        before=json.dumps(self.service.state())
        self.assertFalse(self.host.request_close())
        self.assertEqual(json.dumps(self.service.state()),before)
        self.assertTrue(self.host.thread.is_alive())
        self.host.window.confirm=True
        self.assertTrue(self.host.request_close())
        self.assertFalse(self.host.thread.is_alive())
        saved=json.loads(self.service.file.read_text(encoding='utf-8'))
        self.assertEqual(saved['sessions'][0]['status'],'error')
        self.assertIn('中断',saved['sessions'][0]['error'])
    def test_activation_requires_separate_token_and_local_origin(self):
        self.start()
        self.assertEqual(self.post('page-secret'),401)
        self.assertEqual(self.post('activate-secret','https://example.com'),403)
        self.assertEqual(self.post('activate-secret'),200)
        self.assertEqual(self.host.window.restored,1)
        activate_existing({'mode':'desktop','port':self.port,'activate_token':'activate-secret'})
        self.assertEqual(self.host.window.restored,2)
    def test_existing_browser_is_not_taken_over(self):
        with patch('jianying_engine.desktop.http.client.HTTPConnection') as connect:
            with self.assertRaisesRegex(RuntimeError,'浏览器模式'):
                activate_existing({'mode':'browser','port':self.port})
            connect.assert_not_called()
    def test_startup_failure_stops_thread_and_closes_socket(self):
        class FailedServer:
            started=False
            should_exit=force_exit=False
            def __init__(self, config): pass
            async def serve(self, sockets): raise RuntimeError('fake bind failure')
        with patch('uvicorn.Server',FailedServer):
            with self.assertRaisesRegex(RuntimeError,'服务'):
                self.host.start_server(object(),timeout=1)
        self.host.shutdown()
        self.assertFalse(self.host.thread.is_alive())
        self.assertEqual(self.sock.fileno(),-1)
    def test_navigation_denies_external_file_and_wrong_port(self):
        local=self.host.url
        self.assertTrue(navigation_allowed(local+'#section',local))
        self.assertTrue(navigation_allowed(local+'api/state',local))
        for target in ('https://example.com','file:///C:/secret','javascript:alert(1)',
                       'http://127.0.0.1:1/','http://localhost:'+str(self.port),
                       'http://user@127.0.0.1:'+str(self.port)):
            self.assertFalse(navigation_allowed(target,local),target)
    def test_window_dimensions_respect_work_area_and_dpi(self):
        self.assertEqual(window_dimensions((0,0,1366,728)),(1229,655))
        self.assertEqual(window_dimensions((0,0,1920,1040),144),(1152,624))
        self.assertEqual(window_dimensions((0,0,3840,2080),144),(1280,720))
    def test_main_gui_failure_releases_single_instance_lock(self):
        args=['--desktop','--workspace',str(self.root),'--state-dir',str(self.root/'failed-state')]
        with patch.object(DesktopHost,'run',side_effect=RuntimeError('fake GUI unavailable')), \
             patch('jianying_engine.desktop.show_error') as visible:
            self.assertEqual(main(args),1)
            self.assertEqual(main(args),1)  # Reaches GUI again; a leaked lock would take the activate path.
            self.assertEqual(visible.call_count,2)
        self.assertFalse((self.root/'failed-state/server.json').exists())
    def test_native_picker_is_used_without_javascript_api(self):
        self.host._webview=SimpleNamespace(FileDialog=SimpleNamespace(OPEN=1,FOLDER=2))
        self.host.window.create_file_dialog=lambda kind,**kwargs: ('C:/fixture.mp4',) if kind==1 else None
        self.assertEqual(self.host.pick('files'),['C:/fixture.mp4'])
        self.assertEqual(self.host.pick('folder'),[])
        with self.assertRaises(ValueError): self.host.pick('unknown')


if __name__=='__main__':
    unittest.main()

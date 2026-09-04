import asyncio
import copy
import json
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from pythonosc.udp_client import SimpleUDPClient
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosedError

import srv
from shocking_vrchat import ServiceController
from srv.config_manager import (
    DEFAULT_BASIC_SETTINGS,
    DEFAULT_SETTINGS,
    ConfigManager,
    parse_endpoint,
    parse_parameter_lines,
    validate_config,
)
from srv.udp_relay import create_udp_relay
from srv.steamvr_autostart import (
    APPLICATION_KEY,
    OpenVRApplicationsBackend,
    configure_steamvr_autostart,
    write_manifest,
)
from srv.win32_ui import COPYRIGHT_ENTRIES, FRONTEND_CONTRIBUTORS, DesktopApplication


def free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def free_tcp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class ConfigManagerTests(unittest.TestCase):
    def test_first_run_creates_unified_appdata_config(self):
        with tempfile.TemporaryDirectory() as root:
            manager = ConfigManager(app_dir=Path(root) / 'app', config_dir=Path(root) / 'config')
            settings, basic = manager.load()
            self.assertTrue(manager.path.exists())
            self.assertIsNotNone(settings['ws']['master_uuid'])
            self.assertEqual(basic['dglab3']['channel_a']['strength_limit'], 100)
            self.assertFalse(settings['general']['steamvr_auto_start'])

    def test_v02_files_are_migrated_without_removal(self):
        with tempfile.TemporaryDirectory() as root:
            app_dir = Path(root) / 'app'
            config_dir = Path(root) / 'config'
            app_dir.mkdir()
            advanced = copy.deepcopy(DEFAULT_SETTINGS)
            basic = copy.deepcopy(DEFAULT_BASIC_SETTINGS)
            advanced['version'] = 'v0.2'
            advanced.pop('relay')
            advanced['ws']['master_uuid'] = '86c053e8-4ce1-466b-b123-9e2944b8c490'
            basic['version'] = 'v0.2'
            basic['dglab3']['channel_a']['strength_limit'] = 77
            advanced_path = app_dir / 'settings-advanced-v0.2.yaml'
            basic_path = app_dir / 'settings-v0.2.yaml'
            advanced_path.write_text(yaml.safe_dump(advanced), encoding='utf-8')
            basic_path.write_text(yaml.safe_dump(basic), encoding='utf-8')

            manager = ConfigManager(app_dir=app_dir, config_dir=config_dir)
            settings, migrated_basic = manager.load()

            self.assertEqual(migrated_basic['dglab3']['channel_a']['strength_limit'], 77)
            self.assertIn('relay', settings)
            self.assertTrue(advanced_path.exists())
            self.assertTrue(basic_path.exists())
            self.assertEqual(manager.migrated_from, str(app_dir))

    def test_parameter_bulk_paste_is_cleaned_and_deduplicated(self):
        value = '''
        # comment
        /avatar/parameters/test
        /avatar/parameters/test
        /avatar/parameters/other/*
        '''
        self.assertEqual(
            parse_parameter_lines(value),
            ['/avatar/parameters/test', '/avatar/parameters/other/*'],
        )

    def test_steamvr_auto_start_requires_a_boolean(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['general']['steamvr_auto_start'] = 'true'
        with self.assertRaisesRegex(ValueError, 'general.steamvr_auto_start'):
            validate_config(settings, copy.deepcopy(DEFAULT_BASIC_SETTINGS))

    def test_endpoint_parser_supports_ipv4_and_ipv6(self):
        self.assertEqual(parse_endpoint('127.0.0.1:9001'), ('127.0.0.1', 9001))
        self.assertEqual(parse_endpoint('[::1]:9021'), ('::1', 9021))

    def test_relay_cannot_bind_to_its_internal_target(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        basic = copy.deepcopy(DEFAULT_BASIC_SETTINGS)
        settings['ws']['master_uuid'] = '86c053e8-4ce1-466b-b123-9e2944b8c490'
        settings['relay']['enabled'] = True
        settings['relay']['internal_port'] = settings['relay']['listen_port']
        with self.assertRaises(ValueError):
            validate_config(settings, basic)

    def test_relay_rejects_vrcft_loop_and_duplicate_targets(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        basic = copy.deepcopy(DEFAULT_BASIC_SETTINGS)
        settings['ws']['master_uuid'] = '86c053e8-4ce1-466b-b123-9e2944b8c490'
        settings['relay']['enabled'] = True
        settings['relay']['vrcft_port'] = settings['relay']['listen_port']
        with self.assertRaises(ValueError):
            validate_config(settings, basic)

        settings['relay']['vrcft_port'] = 9011
        settings['relay']['internal_host'] = settings['relay']['vrcft_host']
        settings['relay']['internal_port'] = settings['relay']['vrcft_port']
        with self.assertRaises(ValueError):
            validate_config(settings, basic)


class DatagramReceiver(asyncio.DatagramProtocol):
    def __init__(self, future):
        self.future = future

    def datagram_received(self, data, address):
        if not self.future.done():
            self.future.set_result((data, address))


class UDPRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_datagram_is_forwarded_unchanged_to_both_targets(self):
        loop = asyncio.get_running_loop()
        futures = [loop.create_future(), loop.create_future()]
        receivers = []
        targets = []
        for future in futures:
            transport, _ = await loop.create_datagram_endpoint(
                lambda item=future: DatagramReceiver(item),
                local_addr=('127.0.0.1', 0),
            )
            receivers.append(transport)
            targets.append(transport.get_extra_info('sockname'))

        relay_transport, _ = await create_udp_relay(
            loop,
            ('127.0.0.1', 0),
            targets,
        )
        sender, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol,
            remote_addr=relay_transport.get_extra_info('sockname'),
        )
        try:
            packet = b'\x2favatar\x00\x00raw-osc-payload'
            sender.sendto(packet)
            received = await asyncio.wait_for(asyncio.gather(*futures), timeout=2)
            self.assertEqual([item[0] for item in received], [packet, packet])
        finally:
            sender.close()
            relay_transport.close()
            for receiver in receivers:
                receiver.close()


class ServiceControllerTests(unittest.TestCase):
    def test_service_can_start_and_stop_without_a_device(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        basic = copy.deepcopy(DEFAULT_BASIC_SETTINGS)
        settings['SERVER_IP'] = '127.0.0.1'
        settings['ws']['master_uuid'] = '86c053e8-4ce1-466b-b123-9e2944b8c490'
        settings['ws']['listen_host'] = '127.0.0.1'
        settings['ws']['listen_port'] = free_tcp_port()
        settings['web_server']['listen_port'] = free_tcp_port()
        settings['osc']['listen_port'] = free_udp_port()
        settings['chatbox']['enable'] = False
        controller = ServiceController()
        try:
            controller.start(settings, basic)
            self.assertEqual(controller.state, 'running')
            self.assertFalse(controller.snapshot()['connected'])
        finally:
            controller.stop()
        self.assertEqual(controller.state, 'stopped')

    def test_real_udp_dispatch_does_not_attempt_to_reply_with_a_task(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        basic = copy.deepcopy(DEFAULT_BASIC_SETTINGS)
        settings['SERVER_IP'] = '127.0.0.1'
        settings['ws']['master_uuid'] = '86c053e8-4ce1-466b-b123-9e2944b8c490'
        settings['ws']['listen_host'] = '127.0.0.1'
        settings['ws']['listen_port'] = free_tcp_port()
        settings['web_server']['listen_port'] = free_tcp_port()
        settings['osc']['listen_port'] = free_udp_port()
        settings['chatbox']['enable'] = False
        basic['dglab3']['channel_a']['avatar_params'] = ['/avatar/parameters/integration']
        controller = ServiceController()
        loop_errors = []
        try:
            controller.start(settings, basic)
            controller.runtime.loop.call_soon_threadsafe(
                controller.runtime.loop.set_exception_handler,
                lambda _loop, context: loop_errors.append(
                    context.get('exception') or context.get('message')
                ),
            )
            client = SimpleUDPClient('127.0.0.1', settings['osc']['listen_port'])
            try:
                # Invalid payloads are ignored without escaping into asyncio's
                # exception handler or changing the last valid value.
                client.send_message('/avatar/parameters/integration', ['invalid'])
                client.send_message('/avatar/parameters/integration', [0.2, 0.3])
                client.send_message('/avatar/parameters/integration', [0.4])
                deadline = time.monotonic() + 2
                raw_value = -1
                while time.monotonic() < deadline:
                    raw_value = (
                        controller.snapshot()['channels']
                        .get('A', {})
                        .get('raw_value', -1)
                    )
                    if abs(raw_value - 0.4) < 0.00001:
                        break
                    time.sleep(0.02)
                self.assertAlmostEqual(raw_value, 0.4, places=5)
                time.sleep(0.1)
                self.assertEqual(loop_errors, [])
            finally:
                client._sock.close()
        finally:
            controller.stop()

    def test_failed_web_thread_start_closes_server_and_runtime(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        basic = copy.deepcopy(DEFAULT_BASIC_SETTINGS)
        settings['SERVER_IP'] = '127.0.0.1'
        settings['ws']['master_uuid'] = '86c053e8-4ce1-466b-b123-9e2944b8c490'
        settings['ws']['listen_host'] = '127.0.0.1'
        settings['ws']['listen_port'] = free_tcp_port()
        settings['web_server']['listen_port'] = free_tcp_port()
        settings['osc']['listen_port'] = free_udp_port()
        settings['chatbox']['enable'] = False

        class FakeServer:
            closed = False

            def serve_forever(self):
                pass

            def server_close(self):
                self.closed = True

        class FailingThread:
            def start(self):
                raise RuntimeError('thread start failed')

        fake_server = FakeServer()
        real_thread = threading.Thread

        def thread_factory(*args, **kwargs):
            if kwargs.get('name') == 'status-web-server':
                return FailingThread()
            return real_thread(*args, **kwargs)

        controller = ServiceController()
        with patch('shocking_vrchat.make_server', return_value=fake_server), patch(
            'shocking_vrchat.Thread', side_effect=thread_factory
        ):
            with self.assertRaisesRegex(RuntimeError, 'thread start failed'):
                controller.start(settings, basic)

        self.assertTrue(fake_server.closed)
        self.assertIsNone(controller.web_server)
        self.assertIsNone(controller.web_thread)
        self.assertIsNone(controller.runtime)
        self.assertEqual(controller.state, 'error')


class WebSocketPairingTests(unittest.IsolatedAsyncioTestCase):
    def make_settings(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        basic = copy.deepcopy(DEFAULT_BASIC_SETTINGS)
        settings['SERVER_IP'] = '127.0.0.1'
        settings['ws']['master_uuid'] = '86c053e8-4ce1-466b-b123-9e2944b8c490'
        settings['ws']['listen_host'] = '127.0.0.1'
        settings['ws']['listen_port'] = free_tcp_port()
        settings['web_server']['listen_port'] = free_tcp_port()
        settings['osc']['listen_port'] = free_udp_port()
        settings['chatbox']['enable'] = False
        basic['dglab3']['channel_a']['avatar_params'] = ['/avatar/parameters/v4-test']
        return settings, basic

    async def wait_for_message_type(self, websocket, frame_type, timeout=2):
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                self.fail(f'未收到 V4 {frame_type} 帧')
            frame = json.loads(await asyncio.wait_for(websocket.recv(), remaining))
            if frame.get('type') == frame_type:
                return frame

    async def test_official_v4_pairing_snapshot_ping_and_wave_flow(self):
        settings, basic = self.make_settings()
        controller = ServiceController()
        client = None
        try:
            controller.start(settings, basic)
            uri = (
                f'ws://127.0.0.1:{settings["ws"]["listen_port"]}/'
                f'?tid={settings["ws"]["master_uuid"]}'
            )
            async with websocket_connect(uri) as websocket:
                hello = json.loads(await asyncio.wait_for(websocket.recv(), 2))
                attached = json.loads(await asyncio.wait_for(websocket.recv(), 2))
                devices_request = json.loads(await asyncio.wait_for(websocket.recv(), 2))
                self.assertEqual(hello['type'], 'hello')
                self.assertRegex(hello['clientId'], r'^[0-9a-f]{8}$')
                self.assertEqual(
                    attached,
                    {'type': 'controller_attached', 'clientId': settings['ws']['master_uuid']},
                )
                self.assertEqual(devices_request['type'], 'message')
                self.assertEqual(devices_request['data']['m'], 'devices.get')
                self.assertTrue(controller.snapshot()['app_connected'])
                self.assertFalse(controller.snapshot()['connected'])

                await websocket.send(json.dumps({'type': 'ping'}))
                pong = await self.wait_for_message_type(websocket, 'pong')
                self.assertIsInstance(pong['ts'], int)

                await websocket.send(json.dumps({
                    'type': 'message',
                    'data': {
                        't': 'ev',
                        'ev': 'devices.snapshot',
                        'devices': [{
                            'slotId': 'coyote-slot',
                            'name': 'Coyote 3',
                            'type': 'COYOTE_030',
                            'props': {'intensityA': 12, 'intensityB': 7},
                            'slotState': {
                                'hasDevice': True,
                                'channelA': {'intensityMax': 80},
                                'channelB': {'intensityMax': 60},
                            },
                        }],
                    },
                }))
                deadline = time.monotonic() + 2
                snapshot = {}
                while time.monotonic() < deadline:
                    snapshot = controller.snapshot()
                    if snapshot.get('connected'):
                        break
                    await asyncio.sleep(0.02)
                self.assertTrue(snapshot['connected'])
                self.assertEqual(snapshot['protocol'], 'v4')
                self.assertEqual(snapshot['device_id'], 'coyote-slot')
                self.assertEqual(snapshot['channels']['A']['upper_strength'], 80)

                client = SimpleUDPClient('127.0.0.1', settings['osc']['listen_port'])
                client.send_message('/avatar/parameters/v4-test', [0.4])
                deadline = asyncio.get_running_loop().time() + 2
                operation = None
                strength_operations = []
                while asyncio.get_running_loop().time() < deadline:
                    remaining = deadline - asyncio.get_running_loop().time()
                    frame = json.loads(await asyncio.wait_for(websocket.recv(), remaining))
                    data = frame.get('data') or {}
                    if frame.get('type') == 'message' and data.get('m') == 'device.op':
                        candidate = data['data']
                        if candidate.get('t') == 0:
                            operation = candidate
                            break
                        strength_operations.append(candidate)
                self.assertIsNotNone(operation)
                self.assertEqual(
                    [(item['c'], item['t'], item['v']) for item in strength_operations],
                    [(0, 3, 68), (1, 3, 53)],
                )
                self.assertEqual(operation['s'], 'coyote-slot')
                self.assertEqual(operation['t'], 0)
                self.assertEqual(operation['c'], 0)
                self.assertEqual(operation['d'], 100)
                self.assertTrue(operation['im'])

                await websocket.send(json.dumps({
                    'type': 'message',
                    'data': {
                        't': 'ev',
                        'ev': 'slots.patch',
                        'slots': [{
                            'slotId': 'coyote-slot',
                            'slotState': {'hasDevice': False},
                        }],
                    },
                }))
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and controller.snapshot()['connected']:
                    await asyncio.sleep(0.02)
                snapshot = controller.snapshot()
                self.assertFalse(snapshot['connected'])
                self.assertTrue(snapshot['app_connected'])
        finally:
            if client is not None:
                client._sock.close()
            controller.stop()
            self.assertEqual(srv.get_ws_connections(), ())

    async def test_wrong_v4_target_id_is_rejected(self):
        settings, basic = self.make_settings()
        controller = ServiceController()
        try:
            controller.start(settings, basic)
            uri = f'ws://127.0.0.1:{settings["ws"]["listen_port"]}/?tid=wrong'
            async with websocket_connect(uri) as websocket:
                with self.assertRaises(ConnectionClosedError) as raised:
                    await websocket.recv()
                self.assertEqual(raised.exception.code, 1008)
        finally:
            controller.stop()

    async def test_legacy_v3_pairing_path_remains_compatible(self):
        settings, basic = self.make_settings()
        controller = ServiceController()
        try:
            controller.start(settings, basic)
            uri = (
                f'ws://127.0.0.1:{settings["ws"]["listen_port"]}/'
                f'{settings["ws"]["master_uuid"]}'
            )
            async with websocket_connect(uri) as websocket:
                bind = json.loads(await asyncio.wait_for(websocket.recv(), 2))
                self.assertEqual(bind['type'], 'bind')
                self.assertEqual(bind['message'], 'targetId')
                await websocket.send(json.dumps({
                    'type': 'bind',
                    'clientId': settings['ws']['master_uuid'],
                    'targetId': bind['clientId'],
                    'message': 'targetId',
                }))
                result = json.loads(await asyncio.wait_for(websocket.recv(), 2))
                self.assertEqual(result['type'], 'bind')
                self.assertEqual(result['message'], '200')
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and not controller.snapshot()['connected']:
                    await asyncio.sleep(0.02)
                snapshot = controller.snapshot()
                self.assertTrue(snapshot['connected'])
                self.assertEqual(snapshot['protocol'], 'v3')
        finally:
            controller.stop()


class DesktopApplicationTests(unittest.TestCase):
    def test_copyright_page_lists_all_requested_sources(self):
        self.assertEqual(
            COPYRIGHT_ENTRIES,
            (
                ('DG-LAB', 'https://github.com/dungeonlab-open', '设备、开放协议与技术生态'),
                ('Shocking-VRChat', 'https://github.com/VRChatNext/Shocking-VRChat', '原始项目与代码来源'),
                ('DG-LAB-VRCOSC', 'https://github.com/ccvrc/DG-LAB-VRCOSC', 'Chatbox 发送部分来源'),
            ),
        )
        self.assertEqual(FRONTEND_CONTRIBUTORS, ('WenX1ang', '猫橘Citrus', 'ChatGPT'))

    def test_polished_window_has_room_for_full_labels(self):
        self.assertGreaterEqual(DesktopApplication.WIDTH, 1120)
        self.assertGreaterEqual(DesktopApplication.HEIGHT, 760)

    def test_busy_save_does_not_write_partially_applied_settings(self):
        calls = []

        class BusyApplication:
            action_running = True

            def _message(self, message, error=False):
                calls.append((message, error))

            def _read_form(self):
                raise AssertionError('busy operation must not read or save the form')

        DesktopApplication._save_and_restart(BusyApplication())
        self.assertEqual(calls, [('当前操作尚未完成，请稍候再试。', True)])


class SteamVRAutoStartTests(unittest.TestCase):
    class Backend:
        def __init__(self, installed=True, auto_launch=False, install_on_add=True):
            self.installed = installed
            self.auto_launch = auto_launch
            self.install_on_add = install_on_add
            self.added = []
            self.removed = []
            self.set_values = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def is_installed(self):
            return self.installed

        def add_manifest(self, path):
            self.added.append(Path(path))
            if self.install_on_add:
                self.installed = True

        def remove_manifest(self, path):
            self.removed.append(Path(path))
            self.installed = False

        def set_auto_launch(self, enabled):
            self.set_values.append(enabled)
            self.auto_launch = enabled

        def get_auto_launch(self):
            return self.auto_launch

    def test_manifest_uses_absolute_binary_and_required_overlay_flag(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = Path(temp_dir) / 'ShockingVRChat.exe'
            manifest_path = write_manifest(temp_dir, executable, '')
            document = json.loads(manifest_path.read_text(encoding='utf-8'))
            application = document['applications'][0]
            self.assertEqual(application['app_key'], APPLICATION_KEY)
            self.assertEqual(application['binary_path_windows'], str(executable.resolve()))
            self.assertTrue(application['is_dashboard_overlay'])

    def test_openvr_utility_mode_handles_running_runtime_without_a_headset(self):
        calls = []
        applications = object()

        class HmdNotFound(Exception):
            pass

        def init(application_type):
            calls.append(application_type)
            if application_type == 3:
                raise HmdNotFound

        fake_openvr = SimpleNamespace(
            VRApplication_Background=3,
            VRApplication_Utility=4,
            error_code=SimpleNamespace(InitError_Init_HmdNotFound=HmdNotFound),
            init=init,
            shutdown=lambda: calls.append('shutdown'),
            VRApplications=lambda: applications,
        )
        with patch.dict(sys.modules, {'openvr': fake_openvr}):
            with OpenVRApplicationsBackend() as backend:
                self.assertIs(backend.applications, applications)
        self.assertEqual(calls, [3, 'shutdown', 4, 'shutdown'])

    def test_enable_registers_manifest_and_verifies_auto_launch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            backend = self.Backend(installed=False)
            result = configure_steamvr_autostart(
                temp_dir,
                True,
                executable=Path(temp_dir) / 'ShockingVRChat.exe',
                backend_factory=lambda: backend,
            )
            self.assertTrue(result.enabled)
            self.assertFalse(result.pending_restart)
            self.assertEqual(len(backend.added), 1)
            self.assertEqual(backend.set_values, [True])
            self.assertTrue(backend.auto_launch)

    def test_first_registration_can_report_required_steamvr_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            backend = self.Backend(installed=False, install_on_add=False)
            result = configure_steamvr_autostart(
                temp_dir,
                True,
                executable=Path(temp_dir) / 'ShockingVRChat.exe',
                backend_factory=lambda: backend,
            )
            self.assertTrue(result.pending_restart)
            self.assertEqual(backend.set_values, [])

    def test_disable_verifies_setting_and_removes_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = write_manifest(
                temp_dir,
                Path(temp_dir) / 'ShockingVRChat.exe',
                '',
            )
            backend = self.Backend(installed=True, auto_launch=True)
            result = configure_steamvr_autostart(
                temp_dir,
                False,
                backend_factory=lambda: backend,
            )
            self.assertFalse(result.enabled)
            self.assertEqual(backend.set_values, [False])
            self.assertEqual(backend.removed, [manifest_path])
            self.assertFalse(manifest_path.exists())


if __name__ == '__main__':
    unittest.main()

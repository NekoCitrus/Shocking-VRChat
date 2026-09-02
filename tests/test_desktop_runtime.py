import asyncio
import copy
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from pythonosc.udp_client import SimpleUDPClient

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
from srv.win32_ui import DesktopApplication


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


class DesktopApplicationTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()

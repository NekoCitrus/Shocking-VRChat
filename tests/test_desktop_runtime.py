import asyncio
import copy
import socket
import tempfile
import unittest
from pathlib import Path

import yaml

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


if __name__ == '__main__':
    unittest.main()

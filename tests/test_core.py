import asyncio
import copy
import json
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import shocking_vrchat
from srv.advanced_chatbox_manager import AdvancedChatboxManager
from srv.handler.base_handler import BaseHandler
from srv.handler.shock_handler import ShockHandler


class FakeDGConnection:
    def __init__(self):
        self.waves = []
        self.cleared = []

    async def broadcast_wave(self, channel, wavestr):
        self.waves.append((channel, json.loads(wavestr)))

    async def broadcast_clear_wave(self, channel):
        self.cleared.append(channel)


def make_settings(mode='distance'):
    return {
        'dglab3': {
            'channel_a': {
                'mode': mode,
                'strength_limit': 100,
                'avatar_params': ['/avatar/parameters/test'],
                'mode_config': {
                    'distance': {'freq_ms': 10},
                    'shock': {
                        'duration': 2,
                        'wave': json.dumps(['0A0A0A0A64646464'] * 10),
                    },
                    'trigger_range': {'bottom': 0.0, 'top': 1.0},
                },
            },
        },
    }


class BaseHandlerTests(unittest.TestCase):
    def test_bool_is_normalized_to_integer(self):
        self.assertEqual(BaseHandler.param_sanitizer((True,)), 1)
        self.assertIs(type(BaseHandler.param_sanitizer((True,))), int)

    def test_multiple_arguments_are_rejected(self):
        with self.assertRaises(ValueError):
            BaseHandler.param_sanitizer((0.1, 0.2))


class ShockHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_distance_updates_activity_state(self):
        handler = ShockHandler(make_settings(), FakeDGConnection(), 'A')
        await handler.handler_distance(0.5)
        self.assertTrue(handler.is_active)
        self.assertEqual(handler.current_strength_percentage, 0.5)

    async def test_short_shock_sends_partial_wave(self):
        connection = FakeDGConnection()
        handler = ShockHandler(make_settings('shock'), connection, 'A')
        wave = json.dumps(['0A0A0A0A64646464'] * 10)
        with patch('srv.handler.shock_handler.asyncio.sleep', new=AsyncMock()):
            await handler.send_shock_wave(0.5, wave)
        self.assertEqual(len(connection.waves), 1)
        self.assertEqual(len(connection.waves[0][1]), 5)

    async def test_fractional_shock_uses_final_partial_wave(self):
        connection = FakeDGConnection()
        handler = ShockHandler(make_settings('shock'), connection, 'A')
        wave = json.dumps(['0A0A0A0A64646464'] * 10)
        with patch('srv.handler.shock_handler.asyncio.sleep', new=AsyncMock()):
            await handler.send_shock_wave(2.5, wave)
        self.assertEqual([len(item[1]) for item in connection.waves], [10, 10, 5])


class ConfigAndApiTests(unittest.TestCase):
    def setUp(self):
        self.original_global_settings = copy.deepcopy(shocking_vrchat.SETTINGS)
        self.settings = copy.deepcopy(self.original_global_settings)
        self.basic = copy.deepcopy(shocking_vrchat.SETTINGS_BASIC)
        self.settings['ws']['master_uuid'] = str(uuid.uuid4())

    def tearDown(self):
        shocking_vrchat.SETTINGS.clear()
        shocking_vrchat.SETTINGS.update(self.original_global_settings)

    def test_default_config_is_valid(self):
        shocking_vrchat.validate_config(self.settings, self.basic)

    def test_invalid_trigger_range_is_rejected(self):
        self.settings['dglab3']['channel_a']['mode_config']['trigger_range']['top'] = 0.0
        with self.assertRaises(ValueError):
            shocking_vrchat.validate_config(self.settings, self.basic)

    def test_debug_sendwav_route_was_removed(self):
        response = shocking_vrchat.app.test_client().get('/sendwav')
        self.assertEqual(response.status_code, 404)

    def test_control_api_is_disabled_by_default(self):
        shocking_vrchat.SETTINGS['api']['control_enabled'] = False
        response = shocking_vrchat.app.test_client().get(
            '/api/v1/shock/A/1',
            headers={'User-Agent': 'UnityPlayer/test'},
        )
        self.assertEqual(response.status_code, 401)

    def test_wave_request_validation(self):
        self.assertEqual(
            shocking_vrchat.normalize_wave_request('a', '2', '0A0A0A0A64646464'),
            ('A', 2, '0A0A0A0A64646464'),
        )
        with self.assertRaises(ValueError):
            shocking_vrchat.normalize_wave_request('C', 2, '0A0A0A0A64646464')


class ChatboxTests(unittest.TestCase):
    def test_disabled_manager_still_accepts_channel_updates(self):
        manager = AdvancedChatboxManager({'chatbox': {'enable': False}})
        manager.update_channel_mode('A', 'distance', 0.5, is_active=True)
        self.assertTrue(manager.channel_modes['A']['is_active'])


if __name__ == '__main__':
    unittest.main()

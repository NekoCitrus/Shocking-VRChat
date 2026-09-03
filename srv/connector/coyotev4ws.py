"""DG-LAB Socket V4 adapter for a directly connected App client.

The desktop application acts as the V4 controller and relay endpoint at the
same time.  This keeps the single-device architecture while speaking the
official V4 frames expected by current DG-LAB App releases.
"""

import asyncio
import json
import secrets
import time

from loguru import logger

from srv import add_ws_connection, remove_ws_connection


COYOTE_TYPES = {'COYOTE_020', 'COYOTE_030'}


def _merge_dict(current, patch):
    if not isinstance(current, dict) or not isinstance(patch, dict):
        return patch
    merged = dict(current)
    for key, value in patch.items():
        merged[key] = _merge_dict(current.get(key), value)
    return merged


class DGV4Connection:
    protocol_version = 'v4'

    def __init__(self, ws_connection, settings):
        self.ws_conn = ws_connection
        self.SETTINGS = settings
        self.master_uuid = str(settings['ws']['master_uuid'])
        self.uuid = secrets.token_hex(4)
        self.strength = {'A': 0, 'B': 0}
        self.strength_max = {'A': 200, 'B': 200}
        self.strength_limit = {
            'A': settings['dglab3']['channel_a']['strength_limit'],
            'B': settings['dglab3']['channel_b']['strength_limit'],
        }
        self.devices = {}
        self.slot_id = None
        self._request_counter = 0
        self._send_lock = asyncio.Lock()
        add_ws_connection(self)

    @staticmethod
    def _validate_channel(channel):
        channel = str(channel).upper()
        if channel not in ('A', 'B'):
            raise ValueError(f'Invalid channel: {channel}')
        return channel

    def is_device_ready(self):
        return self.slot_id is not None

    def get_upper_strength(self, channel='A'):
        channel = self._validate_channel(channel)
        return min(self.strength_max[channel], self.strength_limit[channel])

    async def _send_frame(self, frame):
        message = json.dumps(frame, ensure_ascii=False, separators=(',', ':'))
        logger.debug('V4 ID {}, SENDING {}', self.uuid, message)
        async with self._send_lock:
            await self.ws_conn.send(message)

    async def _send_request(self, method, data=None):
        if not self.slot_id and method.startswith('device.'):
            return False
        self._request_counter += 1
        request = {
            't': 'req',
            'reqId': f'{self.uuid}-{self._request_counter}',
            'm': method,
        }
        if data is not None:
            request['data'] = data
        await self._send_frame({'type': 'message', 'data': request})
        return True

    def _replace_devices(self, devices):
        self.devices = {
            device['slotId']: dict(device)
            for device in devices
            if isinstance(device, dict) and isinstance(device.get('slotId'), str)
        }
        self._select_coyote()

    def _patch_devices(self, added, removed):
        for device in added:
            if isinstance(device, dict) and isinstance(device.get('slotId'), str):
                self.devices[device['slotId']] = dict(device)
        for slot_id in removed:
            if isinstance(slot_id, str):
                self.devices.pop(slot_id, None)
        self._select_coyote()

    def _patch_slots(self, slots):
        for patch in slots:
            if not isinstance(patch, dict):
                continue
            slot_id = patch.get('slotId')
            if slot_id not in self.devices:
                continue
            self.devices[slot_id] = _merge_dict(self.devices[slot_id], patch)
        self._select_coyote()

    def _select_coyote(self):
        previous = self.slot_id
        self.slot_id = next(
            (
                slot_id
                for slot_id, device in self.devices.items()
                if device.get('type') in COYOTE_TYPES
                and (device.get('slotState') or {}).get('hasDevice', True)
            ),
            None,
        )
        if self.slot_id is None:
            self.strength = {'A': 0, 'B': 0}
            self.strength_max = {'A': 200, 'B': 200}
        else:
            self._update_device_state(self.devices[self.slot_id])
        if previous != self.slot_id:
            if self.slot_id:
                logger.info('V4 郊狼设备已就绪：{}', self.slot_id)
            elif previous:
                logger.warning('V4 郊狼设备已断开：{}', previous)

    def _update_device_state(self, device):
        props = device.get('props') or {}
        slot_state = device.get('slotState') or {}
        for channel in ('A', 'B'):
            strength = props.get(f'intensity{channel}')
            if isinstance(strength, (int, float)):
                self.strength[channel] = max(0, min(200, int(strength)))
            channel_state = slot_state.get(f'channel{channel}') or {}
            maximum = channel_state.get('intensityMax')
            if isinstance(maximum, (int, float)):
                self.strength_max[channel] = max(0, min(200, int(maximum)))

    def _handle_data(self, data):
        if not isinstance(data, dict):
            return False
        if data.get('t') == 'ev':
            event = data.get('ev')
            if event == 'devices.snapshot':
                self._replace_devices(data.get('devices') or [])
                return True
            elif event == 'devices.patch':
                self._patch_devices(data.get('added') or [], data.get('removed') or [])
                return True
            elif event == 'slots.patch':
                self._patch_slots(data.get('slots') or [])
                return True
        elif data.get('t') == 'resp':
            result = data.get('result')
            if isinstance(result, dict) and isinstance(result.get('devices'), list):
                self._replace_devices(result['devices'])
                return True
        return False

    async def _sync_strength_limits(self):
        if not self.slot_id:
            return
        for channel in ('A', 'B'):
            await self.set_strength(channel, mode='2', value=self.get_upper_strength(channel))

    async def _handle_message(self, raw_message):
        if isinstance(raw_message, bytes):
            raw_message = raw_message.decode('utf-8')
        try:
            frame = json.loads(raw_message)
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning('V4 忽略无法解析的 WebSocket 消息。')
            return
        if not isinstance(frame, dict):
            return
        frame_type = frame.get('type')
        if frame_type == 'message':
            if self._handle_data(frame.get('data')):
                await self._sync_strength_limits()
        elif frame_type == 'ping':
            await self._send_frame({'type': 'pong', 'ts': int(time.time() * 1000)})
        elif frame_type not in ('pong', 'heartbeat'):
            logger.debug('V4 忽略未知帧类型：{}', frame_type)

    async def _heartbeat(self):
        while True:
            await asyncio.sleep(30)
            await self._send_frame({'type': 'heartbeat'})

    async def serve(self):
        logger.info('V4 App 已连接，连接 ID：{}', self.uuid)
        heartbeat_task = None
        try:
            await self._send_frame({'type': 'hello', 'clientId': self.uuid})
            await self._send_frame({'type': 'controller_attached', 'clientId': self.master_uuid})
            await self._send_request('devices.get')
            heartbeat_task = asyncio.create_task(self._heartbeat())
            async for message in self.ws_conn:
                await self._handle_message(message)
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
            remove_ws_connection(self)
            logger.info('V4 App 已断开，连接 ID：{}', self.uuid)

    async def set_strength(self, channel='A', mode='2', value=0, force=False):
        channel = self._validate_channel(channel)
        if not self.slot_id:
            return
        value = int(value)
        if not force and not 0 <= value <= 200:
            raise ValueError('Strength must be between 0 and 200.')
        upper = self.get_upper_strength(channel)
        channel_index = 0 if channel == 'A' else 1
        if mode == '2':
            target = max(0, min(value, upper))
            delta = target - self.strength[channel]
        elif mode == '1':
            delta = abs(value)
            target = min(upper, self.strength[channel] + delta)
            delta = target - self.strength[channel]
        elif mode == '0':
            delta = -abs(value)
            target = max(0, self.strength[channel] + delta)
            delta = target - self.strength[channel]
        else:
            raise ValueError(f'Invalid strength mode: {mode}')
        if delta == 0:
            return
        self.strength[channel] = target
        if target == 0 and mode == '2':
            operation = {'s': self.slot_id, 't': 7, 'c': channel_index, 'p': 1, 'v': 0}
        else:
            operation = {'s': self.slot_id, 't': 3, 'c': channel_index, 'p': 1, 'v': delta}
        await self._send_request('device.op', operation)

    async def set_strength_0_to_1(self, channel='A', value=0):
        value = float(value)
        if not 0 <= value <= 1:
            raise ValueError('Normalized strength must be between 0 and 1.')
        channel = self._validate_channel(channel)
        await self.set_strength(channel, mode='2', value=int(self.get_upper_strength(channel) * value))

    async def send_wave(self, channel='A', wavestr='[]'):
        channel = self._validate_channel(channel)
        try:
            frames = json.loads(wavestr)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError('Invalid V4 waveform JSON.') from exc
        if not frames or not all(isinstance(frame, str) for frame in frames):
            raise ValueError('V4 waveform must contain hexadecimal string frames.')
        operation = {
            's': self.slot_id,
            't': 0,
            'c': 0 if channel == 'A' else 1,
            'p': 1,
            'd': len(frames) * 100,
            'im': True,
            'v': frames,
        }
        await self._send_request('device.op', operation)

    async def clear_wave(self, channel='A'):
        channel = self._validate_channel(channel)
        await self._send_request(
            'device.op.clear',
            {'s': self.slot_id, 'c': 0 if channel == 'A' else 1},
        )

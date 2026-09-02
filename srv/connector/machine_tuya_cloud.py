import asyncio
import threading
import time

from loguru import logger
from tuya_connector import TuyaOpenAPI


class TuYaConnection:
    def __init__(
        self,
        access_id: str,
        access_key: str,
        device_ids: list,
        api_endpoint='https://openapi.tuyacn.com',
        mq_endpoint='wss://mqe.tuyacn.com:8285/',
        cmd_gap=0.2,
    ) -> None:
        self.tyapi = TuyaOpenAPI(api_endpoint, access_id, access_key)
        self.tyapi.connect()
        self.last_cmd_time = 0.0
        self.cmd_gap = max(float(cmd_gap), 0.0)
        self.device_ids = tuple(device_ids)
        self.mq_endpoint = mq_endpoint
        self.current_level = 1
        self._command_lock = threading.Lock()
        self._closed = False
        self.set_switch(True)

    def _sendcmd_sync(self, code, value):
        with self._command_lock:
            current_time = time.monotonic()
            if code in ('level', 'mode') and current_time - self.last_cmd_time < self.cmd_gap:
                logger.debug(f'Skip cmd: {code}:{value}')
                return False

            success = True
            for device_id in self.device_ids:
                response = self.tyapi.post(
                    f'/v1.0/iot-03/devices/{device_id}/commands',
                    {'commands': [{'code': code, 'value': value}]},
                )
                if response.get('success') is True:
                    logger.success(f'{device_id}:{code}:{value}')
                else:
                    success = False
                    logger.error(f'{device_id}:{code}:{value}: {response}')
            self.last_cmd_time = time.monotonic()
            return success

    async def sendcmd(self, code, value):
        # Tuya SDK 是同步客户端，移到工作线程以免阻塞 WebSocket/OSC 事件循环。
        return await asyncio.to_thread(self._sendcmd_sync, code, value)

    def sendcmd_sync(self, code, value):
        return self._sendcmd_sync(code, value)

    def set_switch(self, switch: bool = True):
        return self.sendcmd_sync('switch', bool(switch))

    async def set_level(self, level: int = 1):
        level = max(int(level), 1)
        if await self.sendcmd('level', f'level_{level}'):
            self.current_level = level

    async def set_mode(self, mode: str = 'A'):
        await self.sendcmd('mode', f'level_{mode}')

    async def close(self):
        if self._closed:
            return
        self._closed = True
        await asyncio.to_thread(self.set_switch, False)

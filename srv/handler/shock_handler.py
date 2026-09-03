from .base_handler import BaseHandler
from loguru import logger
import time, asyncio, math, json

from ..connector.coyotev3ws import DGConnection


class ShockHandler(BaseHandler):
    def __init__(self, SETTINGS: dict, DG_CONN: DGConnection, channel_name: str, event_callback=None) -> None:
        self.SETTINGS = SETTINGS
        self.DG_CONN = DG_CONN
        self.channel = channel_name.upper()
        self.shock_settings = SETTINGS['dglab3'][f'channel_{channel_name.lower()}']
        self.mode_config    = self.shock_settings['mode_config']

        self.shock_mode = self.shock_settings['mode']

        self.current_mode = self.shock_mode
        self.is_active = False
        self.current_strength_percentage = 0.0
        
        if self.shock_mode == 'distance':
            self._handler = self.handler_distance
        elif self.shock_mode == 'shock':
            self._handler = self.handler_shock
        else:
            raise ValueError(f"Not supported mode: {self.shock_mode}")
        
        self.distance_update_time_window = 0.1
        self.distance_current_strength = 0

        self.to_clear_time    = 0
        self.is_cleared       = True
        self.chatbox_manager = None
        self._background_tasks = set()
        self.event_callback = event_callback
        self.last_parameter = ''
        self.last_raw_value = 0.0

    def set_chatbox_manager(self, chatbox_manager):
        """设置Chatbox管理器引用"""
        self.chatbox_manager = chatbox_manager
    
    def get_mode_info(self):
        """获取当前模式信息"""
        return {
            'channel': self.channel,
            'mode': self.current_mode,
            'is_active': self.is_active,
            'strength_percentage': self.current_strength_percentage,
            'parameter': self.last_parameter,
            'raw_value': self.last_raw_value,
            'config': {
                'trigger_bottom': self.mode_config['trigger_range']['bottom'],
                'trigger_top': self.mode_config['trigger_range']['top']
            }
        }
    
    def start_background_jobs(self):
        self._track_task(self.clear_check())
        if self.shock_mode == 'distance':
            self._track_task(self.distance_background_wave_feeder())

    def _track_task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._task_finished)
        return task

    def _task_finished(self, task):
        self._background_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error(f'Channel {self.channel} background task failed: {task.exception()}')

    async def stop_background_jobs(self):
        tasks = tuple(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def osc_handler(self, address, *args):
        logger.debug(f"VRCOSC: CHANN {self.channel}: {address}: {args}")
        try:
            val = self.param_sanitizer(args)
        except ValueError as exc:
            logger.warning(f'通道 {self.channel} 忽略无效 OSC 参数 {address}: {exc}')
            return None
        self.last_parameter = address
        self.last_raw_value = float(val)
        self._emit_debug_event()
        self._track_task(self._handler(val))
        # A non-None return value is treated by python-osc as a reply address.
        return None

    def _emit_debug_event(self):
        if self.event_callback is None:
            return
        self.event_callback({
            'type': 'channel',
            'channel': self.channel,
            'parameter': self.last_parameter,
            'raw_value': self.last_raw_value,
            'strength_percentage': self.current_strength_percentage,
            'active': self.is_active,
        })

    async def clear_check(self):
        # logger.info(f'Channel {self.channel} started clear check.')
        sleep_time = 0.05
        while 1:
            await asyncio.sleep(sleep_time)
            current_time = time.monotonic()
            # logger.debug(f"{str(self.is_cleared)}, {current_time}, {self.to_clear_time}")
            if not self.is_cleared and current_time > self.to_clear_time:
                self.is_cleared = True
                self.distance_current_strength = 0
                self.is_active = False
                self.current_strength_percentage = 0.0
                self._emit_debug_event()
                await self.DG_CONN.broadcast_clear_wave(self.channel)
                logger.info(f'Channel {self.channel}, wave cleared after timeout.')
    
    async def feed_wave(self):
        raise NotImplementedError
        logger.info(f'Channel {self.channel} started wave feeding.')
        sleep_time = 1
        while 1:
            await asyncio.sleep(sleep_time)
            await self.DG_CONN.broadcast_wave(channel=self.channel, wavestr=self.shock_settings['shock_wave'])

    async def set_clear_after(self, val):
        self.is_cleared = False
        self.to_clear_time = time.monotonic() + val

    @staticmethod
    def generate_wave_100ms(freq, from_, to_):
        if not isinstance(freq, int) or not 0 <= freq <= 255:
            raise ValueError('波形频率必须是 0~255 之间的整数。')
        if not 0 <= from_ <= 1 or not 0 <= to_ <= 1:
            raise ValueError('波形强度必须位于 0~1。')
        from_ = int(100*from_)
        to_   = int(100*to_)
        ret = ["{:02X}".format(freq)]*4
        delta = (to_ - from_) // 4
        ret += ["{:02X}".format(min(max(from_ + delta*i, 0),100)) for i in range(1,5,1)]
        ret = ''.join(ret)
        return json.dumps([ret],separators=(',', ':'))

    async def handler_distance(self, distance):
        await self.set_clear_after(0.5)
        strength = 0
        trigger_bottom = self.mode_config['trigger_range']['bottom']
        trigger_top = self.mode_config['trigger_range']['top']
        if distance > trigger_bottom:
            strength = (
                    distance - trigger_bottom
                ) / (
                    trigger_top - trigger_bottom
                )
            strength = min(max(strength, 0.0), 1.0)

        self.distance_current_strength = strength
        self.current_strength_percentage = strength
        self.is_active = strength > 0
        self._emit_debug_event()

        if self.chatbox_manager:
            self.chatbox_manager.update_channel_mode(
                self.channel, 
                'distance', 
                strength,
                is_active=self.is_active
            )

    async def distance_background_wave_feeder(self):
        last_strength    = 0
        while 1:
            await asyncio.sleep(self.distance_update_time_window)
            current_strength = self.distance_current_strength
            if current_strength == last_strength == 0:
                continue
            wave = self.generate_wave_100ms(
                self.mode_config['distance']['freq_ms'], 
                last_strength, 
                current_strength
            )
            logger.debug(
                'Channel {}, strength {:.3f} to {:.3f}, Sending {}',
                self.channel,
                last_strength,
                current_strength,
                wave,
            )
            last_strength = current_strength
            await self.DG_CONN.broadcast_wave(self.channel, wavestr=wave)
    
    async def send_shock_wave(self, shock_time, shockwave: str):
        try:
            wave_segments = json.loads(shockwave)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError('电击波形必须是合法的 JSON 数组。') from exc
        if not wave_segments or not all(isinstance(segment, str) for segment in wave_segments):
            raise ValueError('电击波形必须包含至少一个波形片段。')

        remaining_segments = math.ceil(max(float(shock_time), 0.0) / 0.1)
        while remaining_segments > 0:
            chunk_size = min(remaining_segments, len(wave_segments))
            chunk = json.dumps(wave_segments[:chunk_size], separators=(',', ':'))
            await self.DG_CONN.broadcast_wave(self.channel, wavestr=chunk)
            remaining_segments -= chunk_size
            if remaining_segments:
                await asyncio.sleep(chunk_size * 0.1)
    
    async def handler_shock(self, distance):
        current_time = time.monotonic()
        if distance > self.mode_config['trigger_range']['bottom'] and current_time > self.to_clear_time:
            shock_duration = self.mode_config['shock']['duration']
            await self.set_clear_after(shock_duration)
            self.is_active = True
            self.current_strength_percentage = 1.0
            self._emit_debug_event()
            logger.success(f'Channel {self.channel}: Shocking for {shock_duration} s.')

            if self.chatbox_manager:
                self.chatbox_manager.update_channel_mode(
                    self.channel, 
                    'shock', 
                    1.0,
                    is_active=True,
                    duration=shock_duration
                )

            self._track_task(self.send_shock_wave(shock_duration, self.mode_config['shock']['wave']))


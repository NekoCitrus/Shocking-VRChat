# srv/advanced_chatbox_manager.py
import time
from loguru import logger
from pythonosc.udp_client import SimpleUDPClient

class AdvancedChatboxManager:
    def __init__(self, settings):
        self.enabled = settings.get('chatbox', {}).get('enable', True)
        self.settings = settings.get('chatbox', {})
        self.osc_client = None
        self.update_interval = float(self.settings.get('update_interval', 3.0))
        self.last_update_time = 0.0

        # 即使禁用 Chatbox，也要保留状态容器；ShockHandler 仍可能上报状态。
        self.channel_modes = {
            channel: {
                'mode': 'unknown',
                'strength_percentage': 0.0,
                'is_active': False,
                'last_active_time': 0.0,
                'duration': 0.0,
            }
            for channel in ('A', 'B')
        }

        if self.enabled:
            self.osc_client = SimpleUDPClient(
                self.settings.get('osc_host', '127.0.0.1'),
                int(self.settings.get('osc_port', 9000)),
            )
            if self.settings.get('set_avatar_parameter', True):
                # 可选的自定义 Avatar 参数，不是 /chatbox/input 所必需。
                try:
                    self.osc_client.send_message('/avatar/parameters/ChatboxEnable', 1.0)
                    logger.info('Chatbox功能已启用')
                except Exception as exc:
                    logger.warning(f'启用Chatbox失败: {exc}')

    def update_channel_mode(self, channel, mode, strength_percentage, is_active=False, duration=0):
        """更新通道模式信息。"""
        channel = channel.upper()
        if channel in self.channel_modes:
            channel_info = self.channel_modes[channel]
            channel_info['mode'] = mode
            channel_info['strength_percentage'] = strength_percentage
            channel_info['is_active'] = is_active
            channel_info['duration'] = duration
            if is_active:
                channel_info['last_active_time'] = time.monotonic()

    def get_mode_display_name(self, mode):
        """获取模式显示名称。"""
        mode_names = {
            'distance': '距离模式',
            'shock': '电击模式',
            'unknown': '未知模式',
        }
        return mode_names.get(mode, mode)

    def get_activity_indicator(self, channel_info):
        """获取活动状态指示器。"""
        if channel_info['is_active']:
            return '🔴'
        if time.monotonic() - channel_info['last_active_time'] < 5:
            return '🟡'
        return '⚫'

    def _refresh_handler_status(self, shock_handlers):
        if not shock_handlers:
            return
        for handler in shock_handlers:
            if not hasattr(handler, 'get_mode_info'):
                continue
            mode_info = handler.get_mode_info()
            self.update_channel_mode(
                mode_info['channel'],
                mode_info['mode'],
                mode_info['strength_percentage'],
                mode_info['is_active'],
            )

    def format_device_status(self, connections, shock_handlers=None):
        """格式化精简设备状态。"""
        if not connections:
            return '未连接设备'

        self._refresh_handler_status(shock_handlers)
        status_lines = [
            f"MAX A:{conn.strength_max.get('A', 0)} B:{conn.strength_max.get('B', 0)}"
            for conn in connections
        ]
        if len(status_lines) > 1:
            return '郊狼状态 - 多设备:\n' + '\n'.join(status_lines)
        return '状态: ' + status_lines[0]

    def format_detailed_status(self, connections, shock_handlers=None):
        """格式化详细状态信息。"""
        if not connections:
            return '郊狼: 未连接设备'

        self._refresh_handler_status(shock_handlers)
        detailed_lines = []
        for conn in connections:
            strength_a = conn.strength.get('A', 0)
            strength_b = conn.strength.get('B', 0)
            max_a = conn.strength_max.get('A', 0)
            max_b = conn.strength_max.get('B', 0)
            mode_a = self.get_mode_display_name(self.channel_modes['A']['mode'])
            mode_b = self.get_mode_display_name(self.channel_modes['B']['mode'])
            strength_pct_a = int(self.channel_modes['A']['strength_percentage'] * 100)
            strength_pct_b = int(self.channel_modes['B']['strength_percentage'] * 100)
            activity_a = self.get_activity_indicator(self.channel_modes['A'])
            activity_b = self.get_activity_indicator(self.channel_modes['B'])
            detailed_lines.append(
                f"设备 {conn.uuid[:8]}:\n"
                f"A: {strength_a}/{max_a} ({strength_pct_a}%) {mode_a}{activity_a}\n"
                f"B: {strength_b}/{max_b} ({strength_pct_b}%) {mode_b}{activity_b}"
            )
        return '\n\n'.join(detailed_lines)

    async def update_chatbox(self, connections, shock_handlers=None, detailed=False):
        """按配置的间隔更新 Chatbox。"""
        if not self.enabled:
            return
        current_time = time.monotonic()
        if current_time - self.last_update_time < self.update_interval:
            return
        self.last_update_time = current_time
        status_text = (
            self.format_detailed_status(connections, shock_handlers)
            if detailed
            else self.format_device_status(connections, shock_handlers)
        )
        try:
            self.osc_client.send_message('/chatbox/input', [status_text, True, False])
            logger.debug(f'Chatbox更新: {status_text}')
        except Exception as exc:
            logger.warning(f'Chatbox更新失败: {exc}')

    def send_custom_message(self, message):
        """发送自定义消息到 Chatbox。"""
        if not self.enabled:
            return
        try:
            self.osc_client.send_message('/chatbox/input', [str(message), True, False])
            logger.debug(f'Chatbox自定义消息: {message}')
        except Exception as exc:
            logger.warning(f'Chatbox自定义消息发送失败: {exc}')

    def cleanup(self):
        """清理 Chatbox 状态。"""
        if not self.enabled:
            return
        try:
            self.send_custom_message('郊狼设备已断开')
            if self.settings.get('set_avatar_parameter', True):
                self.osc_client.send_message('/avatar/parameters/ChatboxEnable', 0.0)
            logger.info('Chatbox功能已清理')
        except Exception as exc:
            logger.warning(f'Chatbox清理失败: {exc}')

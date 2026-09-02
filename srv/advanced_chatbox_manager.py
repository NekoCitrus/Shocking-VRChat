# srv/advanced_chatbox_manager.py
import asyncio
import time
import logging
from pythonosc.udp_client import SimpleUDPClient
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

class AdvancedChatboxManager:
    def __init__(self, settings):
        self.enabled = settings.get('chatbox', {}).get('enable', True)
        self.settings = settings.get('chatbox', {})
        
        if self.enabled:
            self.osc_client = SimpleUDPClient(
                self.settings.get('osc_host', '127.0.0.1'),
                self.settings.get('osc_port', 9000)
            )
            self.update_interval = self.settings.get('update_interval', 3.0)
            self.last_update_time = 0
            
            # 通道状态存储
            self.channel_modes = {
                'A': {
                    'mode': 'unknown',
                    'strength_percentage': 0.0,
                    'is_active': False,
                    'last_active_time': 0,
                    'duration': 0
                },
                'B': {
                    'mode': 'unknown', 
                    'strength_percentage': 0.0,
                    'is_active': False,
                    'last_active_time': 0,
                    'duration': 0
                }
            }
            
            # 波形信息
            self.waveform_names = {
                'A': '默认波形',
                'B': '默认波形'
            }
            
            # 启用Chatbox
            try:
                self.osc_client.send_message("/avatar/parameters/ChatboxEnable", 1.0)
                logger.info("Chatbox功能已启用")
            except Exception as e:
                logger.warning(f"启用Chatbox失败: {e}")
    
    def update_channel_mode(self, channel, mode, strength_percentage, is_active=False, duration=0):
        """更新通道模式信息"""
        channel = channel.upper()
        if channel in self.channel_modes:
            self.channel_modes[channel]['mode'] = mode
            self.channel_modes[channel]['strength_percentage'] = strength_percentage
            self.channel_modes[channel]['is_active'] = is_active
            if is_active:
                self.channel_modes[channel]['last_active_time'] = time.time()
                self.channel_modes[channel]['duration'] = duration
    
    def set_waveform(self, channel, waveform_name):
        """设置波形名称"""
        channel = channel.upper()
        if channel in self.waveform_names:
            self.waveform_names[channel] = waveform_name
    
    def get_mode_display_name(self, mode):
        """获取模式显示名称"""
        mode_names = {
            'distance': '距离模式',
            'shock': '电击模式', 
            'unknown': '未知模式'
        }
        return mode_names.get(mode, mode)
    
    def get_activity_indicator(self, channel_info):
        """获取活动状态指示器"""
        if channel_info['is_active']:
            return "🔴"  # 红色圆点表示激活
        elif time.time() - channel_info['last_active_time'] < 5:  # 5秒内活动过
            return "🟡"  # 黄色圆点表示最近活动
        else:
            return "⚫"  # 黑色圆点表示未活动
    
    def format_device_status(self, connections, shock_handlers=None):
        """格式化设备状态信息，基于第二个项目的完整格式"""
        if not connections:
            return "未连接设备"
        
        # 如果有handlers，从handlers更新模式信息
        if shock_handlers:
            for handler in shock_handlers:
                if hasattr(handler, 'get_mode_info'):
                    mode_info = handler.get_mode_info()
                    channel = mode_info['channel']
                    self.update_channel_mode(
                        channel,
                        mode_info['mode'],
                        mode_info['strength_percentage'],
                        mode_info['is_active']
                    )
        
        status_lines = []
        for i, conn in enumerate(connections):
            # 获取设备连接状态
            strength_a = conn.strength.get('A', 0)
            strength_b = conn.strength.get('B', 0)
            max_a = conn.strength_max.get('A', 0)
            max_b = conn.strength_max.get('B', 0)
            limit_a = conn.strength_limit.get('A', 0)
            limit_b = conn.strength_limit.get('B', 0)
            
            # 获取模式信息
            mode_a = self.channel_modes['A']['mode']
            mode_b = self.channel_modes['B']['mode']
            
            # 获取活动状态指示器
            activity_a = self.get_activity_indicator(self.channel_modes['A'])
            activity_b = self.get_activity_indicator(self.channel_modes['B'])
            
            # 基于第二个项目的显示格式
            device_info = (
                f"MAX A:{max_a} B:{max_b} | "
                # f"Mode A:{self.get_mode_display_name(mode_a)}{activity_a} "
                # f"B:{self.get_mode_display_name(mode_b)}{activity_b} | "
                # f"Current A:{strength_a} B:{strength_b}"
            )
            status_lines.append(device_info)
        
        # 如果有多设备，显示所有设备信息
        if len(status_lines) > 1:
            return "郊狼状态 - 多设备:\n" + "\n".join(status_lines)
        else:
            return "状态: " + status_lines[0]
    
    def format_detailed_status(self, connections, shock_handlers=None):
        """格式化详细状态信息"""
        if not connections:
            return "郊狼: 未连接设备"
        
        # 更新模式信息
        if shock_handlers:
            for handler in shock_handlers:
                if hasattr(handler, 'get_mode_info'):
                    mode_info = handler.get_mode_info()
                    channel = mode_info['channel']
                    self.update_channel_mode(
                        channel,
                        mode_info['mode'],
                        mode_info['strength_percentage'],
                        mode_info['is_active']
                    )
        
        detailed_lines = []
        for conn in connections:
            # 基础设备信息
            strength_a = conn.strength.get('A', 0)
            strength_b = conn.strength.get('B', 0)
            max_a = conn.strength_max.get('A', 0)
            max_b = conn.strength_max.get('B', 0)
            
            # 模式详细信息
            mode_a = self.get_mode_display_name(self.channel_modes['A']['mode'])
            mode_b = self.get_mode_display_name(self.channel_modes['B']['mode'])
            strength_pct_a = int(self.channel_modes['A']['strength_percentage'] * 100)
            strength_pct_b = int(self.channel_modes['B']['strength_percentage'] * 100)
            
            activity_a = self.get_activity_indicator(self.channel_modes['A'])
            activity_b = self.get_activity_indicator(self.channel_modes['B'])
            
            device_details = (
                f"设备 {conn.uuid[:8]}:\n"
                f"A: {strength_a}/{max_a} ({strength_pct_a}%) {mode_a}{activity_a}\n"
                f"B: {strength_b}/{max_b} ({strength_pct_b}%) {mode_b}{activity_b}"
            )
            detailed_lines.append(device_details)
        
        return "\n\n".join(detailed_lines)
    
    async def update_chatbox(self, connections, shock_handlers=None, detailed=False):
        """更新Chatbox显示"""
        if not self.enabled:
            return
        
        current_time = time.time()
        if current_time - self.last_update_time < self.update_interval:
            return
        
        self.last_update_time = current_time
        
        # 生成状态文本
        if detailed:
            status_text = self.format_detailed_status(connections, shock_handlers)
        else:
            status_text = self.format_device_status(connections, shock_handlers)
        
        # 发送到Chatbox
        try:
            # 使用VRChat的Chatbox输入功能
            self.osc_client.send_message("/chatbox/input", [status_text, True, False])
            logger.debug(f"Chatbox更新: {status_text}")
        except Exception as e:
            logger.warning(f"Chatbox更新失败: {e}")
    
    def send_custom_message(self, message):
        """发送自定义消息到Chatbox"""
        if self.enabled:
            try:
                self.osc_client.send_message("/chatbox/input", [message, True, False])
                logger.debug(f"Chatbox自定义消息: {message}")
            except Exception as e:
                logger.warning(f"Chatbox自定义消息发送失败: {e}")
    
    def cleanup(self):
        """清理资源"""
        if self.enabled:
            try:
                # 发送关闭消息并禁用Chatbox
                self.send_custom_message("郊狼设备已断开")
                time.sleep(0.1)  # 短暂延迟确保消息发送
                self.osc_client.send_message("/avatar/parameters/ChatboxEnable", 0.0)
                logger.info("Chatbox功能已清理")
            except Exception as e:
                logger.warning(f"Chatbox清理失败: {e}")
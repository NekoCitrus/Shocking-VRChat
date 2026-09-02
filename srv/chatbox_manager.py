# srv/chatbox_manager.py
import asyncio
import time
import logging
from pythonosc.udp_client import SimpleUDPClient
from typing import List

logger = logging.getLogger(__name__)

class ChatboxManager:
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
            
            # 启用Chatbox
            try:
                self.osc_client.send_message("/avatar/parameters/ChatboxEnable", 1.0)
                logger.info("Chatbox功能已启用")
            except Exception as e:
                logger.warning(f"启用Chatbox失败: {e}")
    
    def format_device_status(self, connections):
        """简化版的设备状态格式化"""
        if not connections:
            return "郊狼: 未连接设备"
        
        status_lines = []
        for i, conn in enumerate(connections):
            strength_a = conn.strength.get('A', 0)
            strength_b = conn.strength.get('B', 0)
            max_a = conn.strength_max.get('A', 0)
            max_b = conn.strength_max.get('B', 0)
            
            device_info = f"A:{strength_a}/{max_a} B:{strength_b}/{max_b}"
            status_lines.append(device_info)
        
        return "郊狼: " + " | ".join(status_lines)
    
    async def update_chatbox(self, connections, shock_handlers=None):
        """更新Chatbox显示"""
        if not self.enabled:
            return
        
        current_time = time.time()
        if current_time - self.last_update_time < self.update_interval:
            return
        
        self.last_update_time = current_time
        
        # 生成状态文本
        status_text = self.format_device_status(connections)
        
        # 发送到Chatbox
        try:
            self.osc_client.send_message("/chatbox/input", [status_text, True, False])
            logger.debug(f"Chatbox更新: {status_text}")
        except Exception as e:
            logger.warning(f"Chatbox更新失败: {e}")
    
    def cleanup(self):
        """清理资源"""
        if self.enabled:
            try:
                self.osc_client.send_message("/avatar/parameters/ChatboxEnable", 0.0)
            except:
                pass
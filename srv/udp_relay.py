import asyncio

from loguru import logger


class UDPRelayProtocol(asyncio.DatagramProtocol):
    """Forward each datagram unchanged to all configured destinations."""

    def __init__(self, targets, on_packet=None):
        self.targets = tuple((str(host), int(port)) for host, port in targets)
        self.on_packet = on_packet
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, address):
        for target in self.targets:
            self.transport.sendto(data, target)
        if self.on_packet is not None:
            self.on_packet(len(data), address)

    def error_received(self, exc):
        logger.warning(f'UDP 分流发送失败：{exc}')


async def create_udp_relay(loop, listen, targets, on_packet=None):
    return await loop.create_datagram_endpoint(
        lambda: UDPRelayProtocol(targets, on_packet=on_packet),
        local_addr=(str(listen[0]), int(listen[1])),
    )

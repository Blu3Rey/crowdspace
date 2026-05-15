"""
ble_mesh — Bluetooth Low Energy Mesh Network
============================================
A modular, production-grade BLE mesh stack built on bleak (central) and bless
(peripheral) supporting reliable bi-directional multi-hop communication.

Typical usage::

    from ble_mesh import MeshNode, MeshConfig
    from ble_mesh.features import DirectMessaging, GroupChat, DeviceLocator

    cfg  = MeshConfig(node_name="Node-A")
    node = MeshNode(cfg)
    msg  = DirectMessaging(node)

    async def main():
        node.register_feature(msg)
        await node.start()
        await msg.send("Hello mesh!", dst_id=some_node_id)
        await node.run_forever()
"""

from .config import MeshConfig
from .core.node import MeshNode
from .core.protocol import MsgType, Flags, BROADCAST_ADDR
from .core.packet import Packet

__all__ = ["MeshConfig", "MeshNode", "MsgType", "Flags", "BROADCAST_ADDR", "Packet"]
__version__ = "1.0.0"
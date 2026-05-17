# ble_p2p/__init__.py
"""
ble_p2p — Bluetooth Low Energy decentralised P2P messaging framework.

Quick start
-----------
from ble_p2p.node import BLEMeshNode
from ble_p2p.features.direct_message import DirectMessageFeature

node = BLEMeshNode(name="Alice")
dm   = DirectMessageFeature(node)
node.register_feature(dm)

async def on_dm(from_name, from_id_hex, text, ts_ms):
    print(f"{from_name}: {text}")

dm.on_message(on_dm)
await node.start()
"""

from .node                        import BLEMeshNode
from .device                      import LocalDevice
from .constants                   import FeatureID, Capability, MsgType, MsgFlags
from .features.base               import Feature
from .features.direct_message     import DirectMessageFeature
from .features.group_chat         import GroupChatFeature
from .features.device_locator     import DeviceLocatorFeature

__version__ = "1.0.0"
__all__ = [
    "BLEMeshNode",
    "LocalDevice",
    "Feature",
    "DirectMessageFeature",
    "GroupChatFeature",
    "DeviceLocatorFeature",
    "FeatureID",
    "Capability",
    "MsgType",
    "MsgFlags",
]
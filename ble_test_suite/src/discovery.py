import asyncio
from bleak import BleakScanner
import logging

log = logging.getLogger(f"BLE_Test_Suite.{__name__}")

async def run_discovery_mode():
    # Setting return_adv=True returns a dict: {address: (BLEDevice, AdvertisementData)}
    devices = await BleakScanner.discover(return_adv=True)

    for address, (device, adv_data) in devices.items():
        print(f"Device: {device.name} ({address})")
        print(f"    RSSI: {adv_data.rssi} dBm")
        print(f"    Service UUIDs: {adv_data.service_uuids}")
        print(f"    Manufacturer Data: {adv_data.manufacturer_data}")
        print(f"    Tx Power: {adv_data.tx_power}")
        print("-" * 20)

if __name__ == "__main__":
    asyncio.run(run_discovery_mode())
```python {label='std-ble-sig-uuids'}
# 16-bit short form (Bluetooth SIG assigned)
HEART_RATE_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
HEART_RATE_MEASUREMENT_CHAR = "00002a37-0000-1000-8000-00805f9b34fb"
BODY_SENSOR_LOC_CHAR = "00002a38-0000-1000-8000-00805f9b34fb"
HEART_RATE_CTRL_POINT_CHAR = "00002a39-0000-1000-8000-00805f9b34fb"

DEVICE_INFO_SERVICE = "0000180a-0000-1000-8000-00805f9b34fb"
MANUFACTURER_NAME_CHAR = "00002a29-0000-1000-8000-00805f9b34fb"
MODEL_NUMBER_CHAR = "00002a24-0000-1000-8000-00805f9b34fb"

BATTERY_SERVICE = "0000180f-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_CHAR = "00002a19-0000-1000-8000-00805f9b34fb"

# 128-bit custom UUID (vendor-specific, generate randomly)
MY_CUSTOM_SERVICE = "12345678-1234-5678-1234-56789abcdef0"
MY_CUSTOM_CHAR = "12345678-1234-5678-1234-56789abcdef1"
```
> [!INFO]
> Use the [Bluetooth Assigned Numbers](https://www.bluetooth.com/specifications/assigned-numbers/) spec to look up any standardized UUID. For your own custom services, generate a random UUID v4 — do not reuse Bluetooth SIG UUIDs for non-standard purposes.

# Scanning
```run-python {label='ble-central-scanning', import='std-ble-sig-uuids'}
import asyncio
from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

def print_advertiser(device: BLEDevice, adv: AdvertisementData = None):
	print(f"[{device.address}] {device.name or '(unnamed)'}")
	if not adv:
		return
	print(f"  RSSI:          {adv.rssi} dBm")
	print(f"  Service UUIDs: {adv.service_uuids}")
	print(f"  Local Name:    {adv.local_name}")
	
	# Manufacturer-specific data: key is company ID (uint16)
	for company_id, payload in adv.manufacturer_data.items():
		print(f" Mfr [0x{company_id:04X}]: {payload.hex()}")
	
	# Service data: key is UUID
	for uuid, data in adv.service_data.items():
		print(f" SvcData [{uuid}]: {data.hex()}")

async def scan_with_callback():
	"""
	Active scanning with a callback - fires for every advertisement received. This is the preferred pattern over passive scan-then-return.
	"""
	async with BleakScanner(detection_callback=print_advertiser) as scanner:
		await asyncio.sleep(10.0)  # Scan for 10 seconds

async def scan_for_device(name: str, timeout: float = 10.0) -> BLEDevice | None:
	"""Find a specific device by name."""
	return await BleakScanner.find_device_by_name(name, timeout=timeout)
    
async def scan_for_service(service_uuid: str) -> list[BLEDevice]:
    """Filter devices advertising a specific service UUID."""
    devices = await BleakScanner.discover(
        timeout=5.0,
        service_uuids=[service_uuid]
    )
    return devices

if __name__ == "__main__":
	print_advertiser(asyncio.run(scan_for_device("Acme HRM-1000")))
	print_advertiser(*asyncio.run(scan_for_service(HEART_RATE_SERVICE)))
	# asyncio.run(scan_with_callback())
```
# Connecting and Enumerating Services
```run-python
import asyncio
from bleak import BleakClient
from bleak.exc import BleakError

TARGET_ADDRESS = "AA:BB:CC:DD:EE:FF"  # or a UUID on macOS

async def explore_device(address: str):
	"""
	Connect and print the full GATT attribute table.
	BleakClient is a context manager - it disconnects automatically.
	"""
	async with BleakClient(address, timeout=10.0) as client:
		print(f"Connected: {client.is_connected}")
		print(f"MTU: {client.mtu_size} bytes")  # max payload per packet
		
		for service in client.services:
			print(f"\nService: {service.uuid}")
			print(f"  Description: {service.description}")
			
			for char in service.characteristics:
				props = ", ".join(char.properties)
				print(f" Char: {char.uuid}  [{props}]")
				print(f"   Description: {char.description}")
				print(f"   Handle: 0x{char.handle:04X}")
				
				for desc in char.descriptors:
					print(f"    Desc: {desc.uuid} ({desc.description})")

if __name__ == "__main__":
	asyncio.run(explore_device("BC:03:58:30:D8:3C"))
```
# Reading and Writing
```run-python {label='ble-central-read-write', import='std-ble-sig-uuids'}
import asyncio
import struct
from bleak import BleakClient

async def read_write_example(address: str):
	async with BleakClient(address) as client:
		
		# --- READ ---
		# Returns raw bytes; you must parse them per the spec
		raw = await client.read_gatt_char(BATTERY_LEVEL_CHAR)
		battery_pct = raw[0]  # Battery Level is a single uint8
		print(f"Battery: {battery_pct}%")
		
		# Parsing a multi-field Heart Rate Measurement characteristic:
		# Byte 0: flags (bit 0 = HR format: 0=uint8, 1=uint16)
		# Byte 1-2: heart rate value
		# Byte 3-4 (optional): energy expended
		# Byte 5-6 (optional): RR interval(s)
		raw_hr = await client.read_gatt_char(HR_MEASUREMENT_CHAR)
		flags = raw_hr[0]
		if flags & 0x01:  # uint16 format
			hr = struct.unpack_from("<H", raw_hr, 1)[0]
		else:
			hr = raw_hr[1]
		print(f"Heart Rate: {hr} bpm")
		
		# --- WRITE WITH RESPONSE (reliable, slower) ---
		payload = struct.pack("<BH", 0x01, 1500)  # example structured payload
		await client.write_gatt_char(MY_CUSTOM_CHAR, payload, response=True)
		
		# --- WRITE WITHOUT RESPONSE (fast, no ACK, can drop) ---
		await client.write_gatt_char(MY_CUSTOM_CHAR, b"\x00", response=False)

if __name__ == "__main__":
	asyncio.run(read_write_example("5C:A5:F1:51:95:77"))
```
# Notifications and Indications (Push Model)
Notifications are the most important BLE pattern - they let the peripheral push data to the central without polling.
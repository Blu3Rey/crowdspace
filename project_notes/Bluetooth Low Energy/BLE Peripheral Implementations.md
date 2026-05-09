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

# Heart-Rate Monitor
```run-python {label='heart-rate-monitor-peripheral', import='std-ble-sig-uuids'}
"""
BLE Heart Rate Monitor Peripheral
Implements the official Bluetooth SIG Heart Rate Profile (HRP).

Standard UUIDs so any BLE client (phone apps, bleak, etc.) can connect without needing to know anything about your device in advance.
"""

import asyncio
import logging
import math
import random
import struct
from typing import Any

from bless import (
	BlessServer,
	BlessGATTCharacteristic,
	GATTCharacteristicProperties,
	GATTAttributePermissions,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# Body Sensor Location values (Bluetooth spec §3.101)
BODY_SENSOR_WRIST = 0x06

def encode_hr_measurement(
	heart_rate: int,
	rr_intervals: list[float] | None = None,
	energy_expended: int | None = None,
) -> bytes:
	"""
	Encode a Heart Rate Measurement characteristic value.
	
	Byte 0: Flags
		Bit 0: HR format (0 = uint8, 1 = uint16)
		Bit 2: Sensor contact supported
		Bit 3: Sensor contact detected
		Bit 4: Energy expended present
		Bit 5: RR interval present
	
	Byte 1 (or 1-2): Heart rate value
	Byte N (optional): Energy expended (uint16 kJ)
	Byte N (optional): RR interval(s) (uint16, unit = 1/1024 seconds)
	"""
	flags = 0x00
	payload = bytearray()
	
	# Heart rate value
	if heart_rate > 255:
		flags |= 0x01  # uint16 format
		payload += struct.pack("<H", heart_rate)
	else:
		payload += struct.pack("<B", heart_rate)
	
	# Sensor contact (supported + detected)
	flags |= (0x04 | 0x08)
	
	# Optional: energy expended
	if energy_expended is not None:
		flags |= 0x10
		payload += struct.pack("<H", energy_expended)
	
	# Optional: RR intervals (in 1/1024 second units)
	if rr_intervals:
		flags |= 0x20
		for rr in rr_intervals:
			rr_units = int(rr * 1024)  # convert seconds -> 1/1024s units
			payload += struct.pack("<H", min(rr_units, 0xFFFF))
	
	return bytes([flags]) + bytes(payload)

class HeartRateMonitor:
	"""
	Simulates a wrist-worn heart rate monitor with realistic physiology.
	Generates HR values following a sinusoidal baseline with HRV noise,
	mimicking a resting-to-light-activity pattern.
	"""
	
	def __init__(self):
		self._server: BlessServer | None = None
		self._time = 0.0
		self._energy_expended = 0 # cumulative kJ
		self._connected = False
	
	def _simulate_hr(self) -> tuple[int, list[float]]:
		"""
		Generate physiologically plausible HR + RR intervals.
		Baseline ~68 bpm with ±8 bpm sinusoidal drift and ±3 bpm noise.
		"""
		baseline = 68 + 8 * math.sin(self._time * 0.05)
		noise = random.gauss(0, 3)
		hr = max(40, min(200, int(baseline + noise)))
		
		# RR interval = 60 / HR (seconds), with HRV noise
		rr_base = 60.0 / hr
		rr_intervals = [
			max(0.3, rr_base + random.gauss(0, 0.02))
			for _ in range(random.randint(1, 2))
		]
		return hr, rr_intervals
	
	# --- GATT request handlers -----------------------
	
	def on_read(self, characteristic: BlessGATTCharacteristic, **kwargs) -> bytearray:
		"""Called whenever a central reads a characteristic."""
		uuid = characteristic.uuid.lower()
		
		if uuid == BODY_SENSOR_LOC_CHAR:
			return bytearray([BODY_SENSOR_WRIST])
		
		if uuid == BATTERY_LEVEL_CHAR:
			# Simulate slow battery drain
			pct = max(0, 100 - int(self._time / 360))
			return bytearray([pct])
		
		if uuid == MANUFACTURER_NAME_CHAR:
			return bytearray(b"Acme Wearables")
		
		if uuid == MODEL_NUMBER_CHAR:
			return bytearray(b"HRM-1000")
		
		log.warning(f"Unhandled read for {uuid}")
		return bytearray()
	
	def on_write(self, characteristic: BlessGATTCharacteristic, value: Any, **kwargs):
		"""Called whenever a central writes to a characteristic."""
		uuid = characteristic.uuid.lower()
		
		if uuid == HEART_RATE_CTRL_POINT_CHAR:
			# Spec §3.115.3: value 0x01 = reset energy expended
			if value and value[0] == 0x01:
				log.info("Energy expended reset by central")
				self._energy_expended = 0
	
	# --- Server setup -------------------------------------
	
	async def _build_gatt_table(self):
		"""Register all services and characteristics with the GATT server."""
		
		# --- Heart Rate Service ---
		await self._server.add_new_service(HEART_RATE_SERVICE)
		
		# Heart Rate Measurement - Notify only (spec requirement)
		await self._server.add_new_characteristic(
			HEART_RATE_SERVICE,
			HEART_RATE_MEASUREMENT_CHAR,
			GATTCharacteristicProperties.notify,
			None,
			GATTAttributePermissions.readable,
		)
		
		# Body Sensor Location - READ only
		await self._server.add_new_characteristic(
			HEART_RATE_SERVICE,
			BODY_SENSOR_LOC_CHAR,
			GATTCharacteristicProperties.read,
			bytearray([BODY_SENSOR_WRIST]),
			GATTAttributePermissions.readable,
		)
		
		# HR Control Point - WRITE only
		await self._server.add_new_characteristic(
			HEART_RATE_SERVICE,
			HEART_RATE_CTRL_POINT_CHAR,
			GATTCharacteristicProperties.write,
			None,
			GATTAttributePermissions.writeable,
		)
		
		# --- Battery Service ---
		await self._server.add_new_service(BATTERY_SERVICE)
		
		await self._server.add_new_characteristic(
			BATTERY_SERVICE,
			BATTERY_LEVEL_CHAR,
			(GATTCharacteristicProperties.read | GATTCharacteristicProperties.notify),
			bytearray([100]),
			GATTAttributePermissions.readable,
		)
		
		# --- Device Information Service ---
		await self._server.add_new_service(DEVICE_INFO_SERVICE)
		
		await self._server.add_new_characteristic(
			DEVICE_INFO_SERVICE,
			MANUFACTURER_NAME_CHAR,
			GATTCharacteristicProperties.read,
			bytearray(b"Acme Wearables"),
			GATTAttributePermissions.readable,
		)
		
		await self._server.add_new_characteristic(
			DEVICE_INFO_SERVICE,
			MODEL_NUMBER_CHAR,
			GATTCharacteristicProperties.read,
			bytearray(b"HRM-1000"),
			GATTAttributePermissions.readable,
		)
	
	async def _notify_loop(self):
		"""
		Send HR notifications at 1Hz - the standard update rate for HR monitors.
		bless.update_value() updates the characteristic value AND sends a notification to all subscribed centrals in one call.
		"""
		while True:
			await asyncio.sleep(1.0)
			self._time += 1.0
			
			hr, rr_intervals = self._simulate_hr()
			
			# ~5 kcal/min at rest ≈ 0.08 kJ/s
			self._energy_expended += 1
			
			payload = encode_hr_measurement(
				heart_rate=hr,
				rr_intervals=rr_intervals,
				energy_expended=self._energy_expended,
			)
			
			log.info(f"HR: {hr} bpm | RR: {[f'{r:.3f}s' for r in rr_intervals]}")
			
			# This both sets the value and sends a BLE notification
			self._server.update_value(HEART_RATE_SERVICE, HEART_RATE_MEASUREMENT_CHAR)
			char = self._server.get_characteristic(HEART_RATE_MEASUREMENT_CHAR)
			char.value = bytearray(payload)
	
	async def run(self):
		self._server = BlessServer(name="Acme HRM-1000")
		self._server.read_request_func = self.on_read
		self._server.write_request_func = self.on_write
		
		await self._build_gatt_table()
		
		log.info("Starting Heart Rate Monitor peripheral...")
		await self._server.start()
		log.info("Advertising as 'Acme HRM-1000' - connect with any BLE HR client")
		
		try:
			await self._notify_loop()
		except asyncio.CancelledError:
			pass
		finally:
			await self._server.stop()
			log.info("Peripheral stopped")
	
if __name__ == "__main__":
	monitor = HeartRateMonitor()
	asyncio.run(monitor.run())
```

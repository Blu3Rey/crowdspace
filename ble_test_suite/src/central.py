import asyncio
import logging
import os
from bleak import BleakScanner, BleakClient

log = logging.getLogger(f"BLE_Test_Suite.{__name__}")

# CENTRAL MODE (Acts as the Jointer for CrowdSpace's "HOST" mode)
async def run_central_mode():
    log.info("Starting Script Central (Joiner Mode)...")

    SERVICE_UUID = os.getenv("SERVICE_UUID")
    TX_CHAR_UUID = os.getenv("TX_CHAR_UUID")
    RX_CHAR_UUID = os.getenv("RX_CHAR_UUID")

    if not SERVICE_UUID or not TX_CHAR_UUID or not RX_CHAR_UUID:
        raise ValueError("Missing crucial environment variables.")
    
    log.info(f"Scanning for SERVICE_UUID: {SERVICE_UUID}")

    device = await BleakScanner.find_device_by_filter(
        lambda d, ad: SERVICE_UUID.lower() in [s.lower() for s in ad.service_uuids]
    )

    if not device:
        log.error("Could not find CrowdSpace Host. Is the App in 'Host a Session' mode?")
        return

    log.info(f"Found Host: {device.name or device.address}. Connecting...")

    async with BleakClient(device) as client:
        log.info("Connected! Subscribing to notifications...")

        def notification_handler(sender, data):
            try:
                log.info(f"Message recieved from App: {data.decode('utf-8')}")
            except Exception as e:
                log.error(f"Data decode error: {e}")
        
        await client.start_notify(TX_CHAR_UUID, notification_handler)

        try:
            while True:
                msg = await asyncio.get_event_loop().run_in_executor(None, input, "Type message to App (or 'exit'): ")
                if msg.lower() == 'exit':
                    break

                await client.write_gatt_char(RX_CHAR_UUID, bytearray(msg, "utf-8"))
                log.info(f"Message sent to App: {msg}")
        except KeyboardInterrupt:
            pass
        finally:
            await client.stop_notify(TX_CHAR_UUID)
            log.info("Central disconnected.")
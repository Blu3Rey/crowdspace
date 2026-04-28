import os
import asyncio
import platform
import logging
from bless import (
    BlessServer,
    BlessGATTCharacteristic,
    GATTCharacteristicProperties,
    GATTAttributePermissions
)

log = logging.getLogger(f"BLE_Test_Suite.{__name__}")

# PERIPHERAL MODE (Acts as the Host for CrowdSpace's "JOIN" mode)
async def run_peripheral_mode():
    log.info("Starting BLE Peripheral (Host Mode)...")

    SERVICE_UUID = os.getenv("SERVICE_UUID")
    TX_CHAR_UUID = os.getenv("TX_CHAR_UUID")
    RX_CHAR_UUID = os.getenv("RX_CHAR_UUID")
    if not SERVICE_UUID or not TX_CHAR_UUID or not RX_CHAR_UUID:
        raise ValueError("Environment Variable missing.")

    server = BlessServer(name="BLE_TEST")

    # Define request handler for RX Characteristic (App -> Script)
    def write_request_handler(characteristic: BlessGATTCharacteristic, value: bytearray):
        try:
            message = value.decode("utf-8")
            log.info(f"Message recieved from App: {message}")
        except Exception as e:
            log.error(f"Failed to decode message: {e}")
    
    # Setup GATT Profile
    await server.add_new_service(SERVICE_UUID)

    # TX Characteristic (Script -> App)
    # Allows the App to Read and Subscribe (Notify)
    await server.add_new_characteristic(
        SERVICE_UUID,
        TX_CHAR_UUID,
        (GATTCharacteristicProperties.read |
         GATTCharacteristicProperties.notify),
         bytearray(b"Hello from Python"),
         GATTAttributePermissions.readable
    )

    # RX Characteristic (App -> Script)
    # Allows the App to Write
    await server.add_new_characteristic(
        SERVICE_UUID,
        RX_CHAR_UUID,
        (GATTCharacteristicProperties.write |
         GATTCharacteristicProperties.write_without_response),
         None,
         GATTAttributePermissions.writeable
    )

    # Assign the write handler
    server.write_request_func = write_request_handler

    await server.start()
    log.info("Advertising started. Open your App and select 'Join a Session'.")

    # Keep alive and allow manual terminal input to send to App
    try:
        while True:
            # We use run_in_executor to avoid blocking the asyncio loop with input()
            msg = await asyncio.get_event_loop().run_in_executor(None, input, "Type message to App (or 'exit'): ")
            if msg.lower() == 'exit':
                break
            
            payload = bytearray(msg, "utf-8")

            # Manually update the characteristic's value first
            characteristic = server.get_characteristic(TX_CHAR_UUID)
            if characteristic:
                characteristic.value = payload
                
                # Trigger update_value
                try:
                    server.update_value(SERVICE_UUID, TX_CHAR_UUID)
                    log.info("Message sent to App: {msg}")
                except Exception as e:
                    log.error(f"Failed to trigger update_value: {e}")
            else:
                log.error(f"Could not find characteristic {TX_CHAR_UUID} to update.")
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log.error(f"Runtime error: {e}")
    finally:
        await server.stop()
        log.info("Peripheral stopped.")

if __name__ == "__main__":
    asyncio.run(run_peripheral_mode())
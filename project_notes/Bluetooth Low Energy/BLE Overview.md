> [!Note] Definition
> **Bluetooth Low-Energy (BLE):** A wireless protocol optimized for intermittent, short-burst communication with multiple power consumption.

Unlike **Classic Bluetooth (BR/EDR)**, designed for continuous data streaming like audio, BLE is built for devices that wake up, transmit a small payload, and go back to sleep, e.g, heart rate monitors, beacons, smart locks, and IoT sensors.

The key differentiator between BLE and BR is the **duty cycle:** a BLE peripheral might transmit for 1ms and sleep for 1000ms, enabling coin-cell battery life measured in years.

# The BLE Protocol Stack
BLE is organized into a strict layered architecture:
![[Pasted image 20260506235339.png]]
Each layer has a precise job. **controller** (PHY + Link Layer) lives in hardware or firmware on the Bluetooth chipset. The **host** (everything above HCI) is software - on embedded systems this runs in firmware too, but on a laptop or phone it's the OS Bluetooth stack (BlueZ on Linux, Core Bluetooth on macOS/iOS, WinRT on Windows).
___
# Layer 1 - The Physical Layer
BLE operates in the **2.4 GHz ISM band**, divided into 40 channels of 2MHz each. Three of those channels (37, 38, 39) are **advertising channels**, and 37 channels are **data channels** for connections.

BLE 5.0 introduced three PHY modes:
- `1M PHY` - the classic 1 Mbps mode, universally supported. ~10m range
- `2M PHY` - 2 Mbps, shorter range, lower power due to faster transmission
- Coded PHY - 125 Kbps or 500 Kbps with Forward Error Correction, enables up to 400m+ range at the cost of power
BLE uses **FHSS (Frequency Hopping Spread Spectrum)** on data channels to avoid interference - it hops pseudo-randomly through all 37 data channels.
___
# Layer 2 - The Link Layer & Device Roles
The Link Layer defines the fundamental **device roles:**

| Device Role | Description                                           | Functional Roles                                                                                           |
| ----------- | ----------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| Peripheral  | The "server" side of a connection (formerly "Slave")  | Advertiser (Broadcasts advertising packets on channels 37/38/39)                                           |
| Central     | The "client" side of a connection (formerly "Master") | Scanner (Passively or actively listens for advertisements), Initiator (Actively sends connection requests) |
A peripheral advertises $\rightarrow$ a central scans $\rightarrow$ the central initiates a connection $\rightarrow$ both become connected peripheral/central.
___
# GAP - Generic Access Profile
GATT is the **data model** of BLE. Once connected, all data exchange is done through GATT. It defines a strict hierarchy:
![[Pasted image 20260507001335.png]]
## Services
A **service** is a logical grouping of related data. Every service has a **UUID** - either a 16-bit Bluetooth SIG-assigned UUID (e.g, `Ox180D` for Heart Rate) or a 128-bit vendor-specific UUID. The full 128-bit form of a 16-bit UUID uses the Bluetooth base UUID: `0000XXXX-0000-1000-8000-00805F9B34FB`.
## Characteristics
A **characteristic** is the fundamental data unit - it holds a value (up to 512 bytes) and has **properties** that define how it can be accessed:

| Property                 | Operation                              |
| ------------------------ | -------------------------------------- |
| `READ`                   | Client reads the current value         |
| `WRITE`                  | Client writes, waits for ACK           |
| `WRITE WITHOUT RESPONSE` | Client writes, no ACK (faster, lossy)  |
| `NOTIFY`                 | Server pushes updates, no ACK required |
| `INDICATE`               | Server pushes updates, ACK required    |
| `BROADCAST`              | Value included in advertising packets  |
## Descriptors
**Descriptors** are metadata attached to a characteristic. The most important one is the **CCCD (Client Characteristic Configuration Descriptor, `0x2902`)** - writing `0x0001` to it enables notifications, `0x0002` enables indications, `0x0000` disables both. This is the mechanism behind all push-based BLE communication.
___

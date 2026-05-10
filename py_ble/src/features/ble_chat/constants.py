
# Any BLE scanner that finds CHAR_SERV knows it's talking to this app.
CHAT_SERV   = "bada5500-c0de-cafe-babe-000000000001"
TX_CHAR     = "bada5500-c0de-cafe-babe-000000000002" # Peripheral -> Central (NOTIFY)
RX_CHAR     = "bada5500-c0de-cafe-babe-000000000003" # Central -> Peripheral (WRITE)

DEVICE_NAME     = "BLE-Messenger"
SCAN_TIMEOUT    = 6.0   # seconds to scan before deciding to become peripheral
PING_INTERVAL   = 15.0  # keepalive period (seconds)
PING_TIMEOUT    = 5.0   # max wait for PONG before flagging (seconds)
CHUNK_SIZE      = 180   # payload bytes per BLE packet (well under 512-byte MTU)
MAX_MSG_ID      = 256   # msg_id wraps at this value
INTER_PKT_GAP   = 0.020 # seconds between consecutive BLE packets (avoids congestion)
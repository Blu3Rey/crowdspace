import {
    addDeviceFoundListener,
    addEventListener,
    connect,
    disconnect,
    discoverServices,
    isBluetoothEnabled,
    requestBluetoothPermission,
    setServices,
    startAdvertising,
    startScan,
    stopAdvertising,
    stopScan,
    writeCharacteristic,
} from 'munim-bluetooth';
import React, { useEffect, useRef, useState } from 'react';
import {
    ActivityIndicator,
    FlatList,
    KeyboardAvoidingView,
    Platform,
    SafeAreaView,
    StyleSheet,
    Text,
    TextInput,
    TouchableOpacity,
    View,
} from 'react-native';

const CHAT_SERVICE_UUID = '6e400001-b5a3-f393-e0a9-e50e24dcca9e';
const CHAT_CHARACTERISTIC_UUID = '6e400004-b5a3-f393-e0a9-e50e24dcca9e';

const stringToHex = (str: string): string => {
    return Array.from(str)
    .map((c) => c.charCodeAt(0).toString(16).padStart(2, '0'))
    .join('');
}

const hexToString = (hex: string): string => {
    let str = '';
    for (let i = 0; i < hex.length; i += 2) {
        str += String.fromCharCode(parseInt(hex.substr(i, 2), 16));
    }
    return str;
}

interface Message {
    id: string;
    sender: string;
    text: string;
    timestamp: number;
    isMe: boolean;
}

interface Peer {
    id: string;
    name: string;
    rssi?: number;
}

export default function App() {
  const [isReady, setIsReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [myName] = useState(`User-${Math.floor(Math.random() * 10000)}`);
  
  const [peers, setPeers] = useState<Map<string, Peer>>(new Map());
  const [messages, setMessages] = useState<Message[]>([]);
  const [inputText, setInputText] = useState('');
  const [isSending, setIsSending] = useState(false);

  const flatListRef = useRef<FlatList>(null);

  // ------------------------------------------------------------------
  // BLE Initialization & Lifecycle
  // ------------------------------------------------------------------
  useEffect(() => {
    let removeDeviceFound: () => void;
    let removeWriteReq: () => void;

    const initializeBluetooth = async () => {
      try {
        const hasPermission = await requestBluetoothPermission();
        if (!hasPermission) throw new Error('Bluetooth permission denied');

        const enabled = await isBluetoothEnabled();
        if (!enabled) throw new Error('Bluetooth is turned off');

        // 1. Setup Peripheral Mode (Listening for messages)
        await setServices([
          {
            uuid: CHAT_SERVICE_UUID,
            characteristics: [
              {
                uuid: CHAT_CHARACTERISTIC_UUID,
                properties: ['read', 'write', 'writeWithoutResponse', 'notify'],
                value: stringToHex('Ready'),
              },
            ],
          },
        ]);

        await startAdvertising({
          serviceUUIDs: [CHAT_SERVICE_UUID],
          localName: myName,
        });

        // 2. Setup Central Mode (Scanning for peers)
        removeDeviceFound = addDeviceFoundListener((device) => {
          setPeers((prev) => {
            const newPeers = new Map(prev);
            newPeers.set(device.id, {
              id: device.id,
              name: device.localName || device.name || 'Unknown Peer',
              rssi: device.rssi,
            });
            return newPeers;
          });
        });

        await startScan({
          serviceUUIDs: [CHAT_SERVICE_UUID],
          allowDuplicates: false,
          scanMode: 'balanced',
        });

        // 3. Listen for incoming Ephemeral connections and writes
        removeWriteReq = addEventListener('peripheralWriteRequest', (event) => {
          if (event.characteristicUUID.toUpperCase() === CHAT_CHARACTERISTIC_UUID.toUpperCase()) {
            try {
              const decodedStr = hexToString(event.value);
              const payload = JSON.parse(decodedStr);
              
              if (payload.text && payload.sender) {
                setMessages((prev) => {
                  // Prevent duplicate messages if the peer retries
                  if (prev.some((m) => m.id === payload.id)) return prev;
                  return [...prev, { ...payload, isMe: false, timestamp: Date.now() }];
                });
              }
            } catch (err) {
              console.log('Failed to parse incoming message:', err);
            }
          }
        });

        setIsReady(true);
      } catch (err: any) {
        setError(err.message || 'Failed to initialize Bluetooth');
      }
    };

    initializeBluetooth();

    // Cleanup on unmount
    return () => {
      stopScan();
      stopAdvertising();
      if (removeDeviceFound) removeDeviceFound();
      if (removeWriteReq) removeWriteReq();
    };
  }, [myName]);

  // ------------------------------------------------------------------
  // Ephemeral Messaging Logic
  // ------------------------------------------------------------------
  const sendMessage = async () => {
    if (!inputText.trim() || peers.size === 0) return;

    setIsSending(true);
    const messageId = Date.now().toString();
    const payload = {
      id: messageId,
      sender: myName,
      text: inputText.trim(),
    };

    // Optimistically add to UI
    setMessages((prev) => [
      ...prev,
      { ...payload, isMe: true, timestamp: Date.now() },
    ]);
    setInputText('');

    const hexPayload = stringToHex(JSON.stringify(payload));
    const currentPeers = Array.from(peers.values());

    // Ephemeral Connection Strategy: Connect -> Discover -> Write -> Disconnect
    // We do this sequentially to avoid overloading the BLE radio
    for (const peer of currentPeers) {
      try {
        await connect(peer.id);
        await discoverServices(peer.id);
        await writeCharacteristic(
          peer.id,
          CHAT_SERVICE_UUID,
          CHAT_CHARACTERISTIC_UUID,
          hexPayload,
          'write' // Try 'writeWithoutResponse' if payloads get larger/faster
        );
      } catch (err) {
        console.log(`Failed to send to peer ${peer.id}:`, err);
      } finally {
        // Always ensure we disconnect to keep the airwaves clear (Ephemeral)
        await disconnect(peer.id);
      }
    }
    
    setIsSending(false);
  };

  // ------------------------------------------------------------------
  // UI Renderers
  // ------------------------------------------------------------------
  if (error) {
    return (
      <View style={styles.centerContainer}>
        <Text style={styles.errorText}>⚠️ {error}</Text>
      </View>
    );
  }

  if (!isReady) {
    return (
      <View style={styles.centerContainer}>
        <ActivityIndicator size="large" color="#0066CC" />
        <Text style={styles.loadingText}>Initializing Mesh Network...</Text>
      </View>
    );
  }

  return (
    <SafeAreaView style={styles.container}>
      <KeyboardAvoidingView 
        style={styles.container} 
        behavior={Platform.OS === 'ios' ? 'padding' : undefined}
      >
        {/* Header */}
        <View style={styles.header}>
          <Text style={styles.headerTitle}>BLE Mesh Chat</Text>
          <Text style={styles.headerSubtitle}>
            {myName} • {peers.size} peer(s) nearby
          </Text>
        </View>

        {/* Message List */}
        <FlatList
          ref={flatListRef}
          data={messages}
          keyExtractor={(item) => item.id}
          contentContainerStyle={styles.messageList}
          onContentSizeChange={() => flatListRef.current?.scrollToEnd({ animated: true })}
          renderItem={({ item }) => (
            <View style={[styles.messageBubble, item.isMe ? styles.messageBubbleMe : styles.messageBubbleThem]}>
              {!item.isMe && <Text style={styles.messageSender}>{item.sender}</Text>}
              <Text style={[styles.messageText, item.isMe ? styles.messageTextMe : styles.messageTextThem]}>
                {item.text}
              </Text>
            </View>
          )}
        />

        {/* Input Area */}
        <View style={styles.inputContainer}>
          <TextInput
            style={styles.input}
            placeholder={peers.size === 0 ? "Waiting for peers..." : "Type a message..."}
            placeholderTextColor="#999"
            value={inputText}
            onChangeText={setInputText}
            editable={peers.size > 0 && !isSending}
          />
          <TouchableOpacity 
            style={[styles.sendButton, (!inputText.trim() || peers.size === 0) && styles.sendButtonDisabled]} 
            onPress={sendMessage}
            disabled={!inputText.trim() || peers.size === 0 || isSending}
          >
            {isSending ? (
              <ActivityIndicator color="#FFF" size="small" />
            ) : (
              <Text style={styles.sendButtonText}>Send</Text>
            )}
          </TouchableOpacity>
        </View>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

// ------------------------------------------------------------------
// Styles
// ------------------------------------------------------------------
const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: '#F5F7FA',
  },
  centerContainer: {
    flex: 1,
    justifyContent: 'center',
    alignItems: 'center',
    backgroundColor: '#F5F7FA',
    padding: 20,
  },
  errorText: {
    color: '#D32F2F',
    fontSize: 16,
    textAlign: 'center',
    fontWeight: 'bold',
  },
  loadingText: {
    marginTop: 12,
    color: '#555',
    fontSize: 16,
  },
  header: {
    padding: 20,
    backgroundColor: '#FFFFFF',
    borderBottomWidth: 1,
    borderBottomColor: '#E0E0E0',
    alignItems: 'center',
  },
  headerTitle: {
    fontSize: 20,
    fontWeight: '800',
    color: '#1A1A1A',
  },
  headerSubtitle: {
    fontSize: 14,
    color: '#666',
    marginTop: 4,
  },
  messageList: {
    padding: 16,
    flexGrow: 1,
    justifyContent: 'flex-end',
  },
  messageBubble: {
    maxWidth: '80%',
    padding: 12,
    borderRadius: 20,
    marginBottom: 12,
  },
  messageBubbleMe: {
    alignSelf: 'flex-end',
    backgroundColor: '#0066CC',
    borderBottomRightRadius: 4,
  },
  messageBubbleThem: {
    alignSelf: 'flex-start',
    backgroundColor: '#FFFFFF',
    borderBottomLeftRadius: 4,
    borderWidth: 1,
    borderColor: '#E0E0E0',
  },
  messageSender: {
    fontSize: 12,
    color: '#888',
    marginBottom: 4,
    fontWeight: '600',
  },
  messageText: {
    fontSize: 16,
  },
  messageTextMe: {
    color: '#FFFFFF',
  },
  messageTextThem: {
    color: '#1A1A1A',
  },
  inputContainer: {
    flexDirection: 'row',
    padding: 16,
    backgroundColor: '#FFFFFF',
    borderTopWidth: 1,
    borderTopColor: '#E0E0E0',
  },
  input: {
    flex: 1,
    backgroundColor: '#F0F2F5',
    borderRadius: 24,
    paddingHorizontal: 16,
    paddingVertical: 12,
    fontSize: 16,
    maxHeight: 100,
  },
  sendButton: {
    backgroundColor: '#0066CC',
    borderRadius: 24,
    justifyContent: 'center',
    alignItems: 'center',
    paddingHorizontal: 20,
    marginLeft: 12,
  },
  sendButtonDisabled: {
    backgroundColor: '#A0C4E8',
  },
  sendButtonText: {
    color: '#FFFFFF',
    fontWeight: '700',
    fontSize: 16,
  },
});
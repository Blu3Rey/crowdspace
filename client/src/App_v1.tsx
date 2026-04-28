/**
 * React Native BLE Bi-Directional Chat
 * Implements Central (Client) and Peripheral (Host) modes in a single application.
 */

import React, { useState, useEffect, useRef } from 'react';
import {
  SafeAreaView,
  View,
  Text,
  TextInput,
  TouchableOpacity,
  FlatList,
  StyleSheet,
  Platform,
  PermissionsAndroid,
  KeyboardAvoidingView,
  ActivityIndicator,
} from 'react-native';
import { SERVICE_UUID, TX_CHAR_UUID, RX_CHAR_UUID } from '@env';

// External BLE Libraries
import { BleManager, Device } from 'react-native-ble-plx';
import Peripheral, { Permission, Property } from 'react-native-multi-ble-peripheral';
import { Buffer } from 'buffer';

// -------------------------------------------------------------------------
// POLYFILLS & UTILS
// -------------------------------------------------------------------------

// react-native-ble-plx writes/reads in Base64. This is a lightweight polyfill 
// so we don't need additional external base64 libraries.
const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=';
const btoa = (input: string) => {
  let str = input;
  let output = '';
  for (let block = 0, charCode, i = 0, map = chars;
    str.charAt(i | 0) || (map = '=', i % 1);
    output += map.charAt(63 & block >> 8 - i % 1 * 8)) {
    charCode = str.charCodeAt(i += 3 / 4);
    block = block << 8 | charCode;
  }
  return output;
};

const atob = (input: string) => {
  let str = input.replace(/=+$/, '');
  let output = '';
  for (let bc = 0, bs = 0, buffer, i = 0;
    buffer = str.charAt(i++);
    ~buffer && (bs = bc % 4 ? bs * 64 + buffer : buffer,
      bc++ % 4) ? output += String.fromCharCode(255 & bs >> (-2 * bc & 6)) : 0
  ) {
    buffer = chars.indexOf(buffer);
  }
  return output;
};

// -------------------------------------------------------------------------
// CONSTANTS & TYPES
// -------------------------------------------------------------------------

type Role = 'IDLE' | 'HOST' | 'JOIN';
type Message = { id: string; text: string; sender: 'me' | 'them'; timestamp: number };

// -------------------------------------------------------------------------
// PERMISSIONS
// -------------------------------------------------------------------------

const requestBluetoothPermissions = async (): Promise<boolean> => {
  if (Platform.OS === 'android') {
    if (Platform.Version >= 31) {
      const granted = await PermissionsAndroid.requestMultiple([
        PermissionsAndroid.PERMISSIONS.BLUETOOTH_SCAN,
        PermissionsAndroid.PERMISSIONS.BLUETOOTH_CONNECT,
        PermissionsAndroid.PERMISSIONS.BLUETOOTH_ADVERTISE,
        PermissionsAndroid.PERMISSIONS.ACCESS_FINE_LOCATION,
      ]);
      return (
        granted['android.permission.BLUETOOTH_SCAN'] === PermissionsAndroid.RESULTS.GRANTED &&
        granted['android.permission.BLUETOOTH_CONNECT'] === PermissionsAndroid.RESULTS.GRANTED &&
        granted['android.permission.BLUETOOTH_ADVERTISE'] === PermissionsAndroid.RESULTS.GRANTED
      );
    } else {
      const granted = await PermissionsAndroid.request(
        PermissionsAndroid.PERMISSIONS.ACCESS_FINE_LOCATION
      );
      return granted === PermissionsAndroid.RESULTS.GRANTED;
    }
  }
  return true; // iOS is handled automatically if Info.plist is configured
};

// -------------------------------------------------------------------------
// MAIN APPLICATION COMPONENT
// -------------------------------------------------------------------------

export default function App() {
  const [role, setRole] = useState<Role>('IDLE');
  const [status, setStatus] = useState<string>('');
  const [messages, setMessages] = useState<Message[]>([]);
  const [inputText, setInputText] = useState<string>('');
  const [isConnected, setIsConnected] = useState<boolean>(false);

  // References to BLE instances
  const bleManagerRef = useRef<BleManager | null>(null);
  const peripheralRef = useRef<Peripheral | null>(null);
  const connectedDeviceRef = useRef<Device | null>(null);

  // Initialize the Central Manager once
  useEffect(() => {
    bleManagerRef.current = new BleManager();
    return () => {
      bleManagerRef.current?.destroy();
      // Optional: Stop peripheral if running
    };
  }, []);

  // --- HOST (PERIPHERAL) LOGIC ---
  const startHost = async () => {
    const hasPermission = await requestBluetoothPermissions();
    if (!hasPermission) {
      setStatus('Permissions denied.');
      return;
    }

    setRole('HOST');
    setStatus('Initializing Host...');

    try {
      Peripheral.setDeviceName('BLE_Chat_App');
      const peripheral = new Peripheral();
      peripheralRef.current = peripheral;

      peripheral.on('ready', async () => {
        setStatus('Setting up services...');
        await peripheral.addService(SERVICE_UUID, true);

        // TX Characteristic: We push data to Central via NOTIFY
        await peripheral.addCharacteristic(
          SERVICE_UUID,
          TX_CHAR_UUID,
          Property.READ | Property.NOTIFY,
          Permission.READABLE
        );

        // RX Characteristic: Central pushes data to us via WRITE
        await peripheral.addCharacteristic(
          SERVICE_UUID,
          RX_CHAR_UUID,
          Property.WRITE | Property.WRITE_NO_RESPONSE,
          Permission.WRITEABLE
        );

        await peripheral.startAdvertising();
        setStatus('Advertising... Waiting for Central to connect.');
      });

      // Handle messages received from Central device
      peripheral.on('WriteRequest', (req: any) => {
        try {
          const charUUID = req?.characteristic?.toUpperCase();
          if (charUUID === RX_CHAR_UUID.toUpperCase()) {
            // Depending on the exact lib version, value is either raw base64 or array
            let valueStr = '';
            if (typeof req.value === 'string') {
              valueStr = Buffer.from(req.value, 'base64').toString('utf8');
            } else if (Array.isArray(req.value)) {
              valueStr = Buffer.from(req.value).toString('utf8');
            }
            receiveMessage(valueStr || '[Empty Message]');
          }
        } catch (e) {
          console.error('Failed to decode incoming WriteRequest', e);
        }
      });

      // Note: react-native-multi-ble-peripheral connection events 
      // vary by version, safely catching potential connection updates.
      peripheral.on('Connected', () => {
        setIsConnected(true);
        setStatus('Central Connected!');
      });

      peripheral.on('Disconnected', () => {
        setIsConnected(false);
        setStatus('Central disconnected. Re-advertising...');
      });
    } catch (e: any) {
      setStatus(`Host setup error: ${e.message}`);
    }
  };

  // --- JOIN (CENTRAL) LOGIC ---
  const startJoin = async () => {
    const hasPermission = await requestBluetoothPermissions();
    if (!hasPermission) {
      setStatus('Permissions denied.');
      return;
    }

    setRole('JOIN');
    setStatus('Scanning for Hosts...');

    if (!bleManagerRef.current) return;

    // Scan explicitly for our custom Chat Service UUID
    bleManagerRef.current.startDeviceScan([SERVICE_UUID], null, async (error, device) => {
      if (error) {
        setStatus(`Scan error: ${error.message}`);
        return;
      }

      if (device) {
        bleManagerRef.current?.stopDeviceScan();
        setStatus(`Found Host: ${device.name || device.id}. Connecting...`);

        try {
          const connectedDevice = await device.connect();
          connectedDeviceRef.current = connectedDevice;
          setIsConnected(true);
          setStatus('Discovering services...');

          await connectedDevice.discoverAllServicesAndCharacteristics();
          setStatus('Connected and ready!');

          // Subscribe to Host's TX Characteristic to receive messages
          connectedDevice.monitorCharacteristicForService(
            SERVICE_UUID,
            TX_CHAR_UUID,
            (err, char) => {
              if (err) {
                console.log('Monitor error', err);
                return;
              }
              if (char?.value) {
                const decodedText = atob(char.value);
                receiveMessage(decodedText);
              }
            }
          );

          connectedDevice.onDisconnected(() => {
            setIsConnected(false);
            setStatus('Disconnected from Host.');
          });
        } catch (e: any) {
          setStatus(`Connection failed: ${e.message}`);
        }
      }
    });
  };

  // --- SHARED CHAT LOGIC ---
  const receiveMessage = (text: string) => {
    setMessages((prev) => [
      ...prev,
      { id: Date.now().toString(), text, sender: 'them', timestamp: Date.now() },
    ]);
  };

  const sendMessage = async () => {
    if (!inputText.trim()) return;
    const textToSend = inputText.trim();
    setInputText('');

    try {
      if (role === 'HOST' && peripheralRef.current) {
        // Peripheral: Send via TX characteristic notification
        await peripheralRef.current.updateValue(
          SERVICE_UUID,
          TX_CHAR_UUID,
          Buffer.from(textToSend) // The native bridge handles buffer passing
        );
      } else if (role === 'JOIN' && connectedDeviceRef.current) {
        // Central: Send via writing to RX characteristic (requires base64)
        const base64Data = btoa(textToSend);
        await connectedDeviceRef.current.writeCharacteristicWithResponseForService(
          SERVICE_UUID,
          RX_CHAR_UUID,
          base64Data
        );
      }
      
      // Update local UI
      setMessages((prev) => [
        ...prev,
        { id: Date.now().toString(), text: textToSend, sender: 'me', timestamp: Date.now() },
      ]);
    } catch (e: any) {
      console.error('Send error', e);
      setStatus(`Failed to send: ${e.message}`);
    }
  };

  const resetSession = () => {
    if (role === 'HOST' && peripheralRef.current) {
      // Not all peripheral libs support a clean explicit stop, catching to be safe
      try { peripheralRef.current.stopAdvertising?.(); } catch (e) {}
    } else if (role === 'JOIN' && bleManagerRef.current) {
      bleManagerRef.current.stopDeviceScan();
      if (connectedDeviceRef.current) {
        connectedDeviceRef.current.cancelConnection();
      }
    }
    
    setRole('IDLE');
    setIsConnected(false);
    setMessages([]);
    setStatus('');
    connectedDeviceRef.current = null;
    peripheralRef.current = null;
  };

  // -------------------------------------------------------------------------
  // RENDERERS
  // -------------------------------------------------------------------------

  if (role === 'IDLE') {
    return (
      <SafeAreaView style={styles.container}>
        <View style={styles.idleContent}>
          <Text style={styles.title}>BLE Two-Way Chat</Text>
          <Text style={styles.subtitle}>Select your device role to begin</Text>

          <TouchableOpacity style={styles.roleCard} onPress={startHost}>
            <Text style={styles.cardTitle}>📱 Host a Session</Text>
            <Text style={styles.cardDesc}>Broadcast as a Peripheral device and wait for connections.</Text>
          </TouchableOpacity>

          <TouchableOpacity style={styles.roleCard} onPress={startJoin}>
            <Text style={styles.cardTitle}>🔍 Join a Session</Text>
            <Text style={styles.cardDesc}>Scan for nearby Central devices and establish a link.</Text>
          </TouchableOpacity>
        </View>
      </SafeAreaView>
    );
  }

  return (
    <SafeAreaView style={styles.container}>
      <KeyboardAvoidingView 
        style={styles.flex1} 
        behavior={Platform.OS === 'ios' ? 'padding' : 'height'}
      >
        {/* Header Section */}
        <View style={styles.header}>
          <TouchableOpacity onPress={resetSession} style={styles.backButton}>
            <Text style={styles.backButtonText}>← End</Text>
          </TouchableOpacity>
          <View style={styles.headerTitleContainer}>
            <Text style={styles.headerRoleText}>
              {role === 'HOST' ? 'Hosting (Peripheral)' : 'Joined (Central)'}
            </Text>
            <Text style={[styles.headerStatusText, isConnected ? styles.textGreen : styles.textYellow]}>
              {status}
            </Text>
          </View>
        </View>

        {/* Loading Indicator */}
        {!isConnected && (
          <View style={styles.loadingContainer}>
             <ActivityIndicator size="large" color="#3B82F6" />
             <Text style={styles.loadingText}>{status}</Text>
          </View>
        )}

        {/* Chat Messages List */}
        <FlatList
          data={messages}
          keyExtractor={(item) => item.id}
          style={styles.chatList}
          contentContainerStyle={styles.chatContent}
          renderItem={({ item }) => {
            const isMe = item.sender === 'me';
            return (
              <View style={[styles.messageWrapper, isMe ? styles.messageMe : styles.messageThem]}>
                <View style={[styles.messageBubble, isMe ? styles.bubbleMe : styles.bubbleThem]}>
                  <Text style={styles.messageText}>{item.text}</Text>
                </View>
              </View>
            );
          }}
        />

        {/* Input Area */}
        <View style={styles.inputArea}>
          <TextInput
            style={styles.input}
            value={inputText}
            onChangeText={setInputText}
            placeholder="Type a message..."
            placeholderTextColor="#6B7280"
            editable={isConnected} // Only allow typing when connected
          />
          <TouchableOpacity
            style={[styles.sendButton, !isConnected && styles.sendButtonDisabled]}
            onPress={sendMessage}
            disabled={!isConnected}
          >
            <Text style={styles.sendButtonText}>Send</Text>
          </TouchableOpacity>
        </View>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

// -------------------------------------------------------------------------
// STYLES
// -------------------------------------------------------------------------

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: '#111827', // Premium dark mode background
  },
  flex1: {
    flex: 1,
  },
  idleContent: {
    flex: 1,
    justifyContent: 'center',
    padding: 24,
  },
  title: {
    fontSize: 28,
    fontWeight: '700',
    color: '#F9FAFB',
    textAlign: 'center',
    marginBottom: 8,
  },
  subtitle: {
    fontSize: 16,
    color: '#9CA3AF',
    textAlign: 'center',
    marginBottom: 48,
  },
  roleCard: {
    backgroundColor: '#1F2937',
    borderRadius: 16,
    padding: 24,
    marginBottom: 20,
    borderWidth: 1,
    borderColor: '#374151',
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 4 },
    shadowOpacity: 0.3,
    shadowRadius: 5,
    elevation: 5,
  },
  cardTitle: {
    fontSize: 20,
    fontWeight: '600',
    color: '#E5E7EB',
    marginBottom: 8,
  },
  cardDesc: {
    fontSize: 14,
    color: '#9CA3AF',
    lineHeight: 20,
  },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    padding: 16,
    borderBottomWidth: 1,
    borderBottomColor: '#1F2937',
    backgroundColor: '#111827',
  },
  backButton: {
    padding: 8,
    marginRight: 8,
  },
  backButtonText: {
    color: '#EF4444',
    fontSize: 16,
    fontWeight: '600',
  },
  headerTitleContainer: {
    flex: 1,
  },
  headerRoleText: {
    color: '#F3F4F6',
    fontSize: 16,
    fontWeight: '700',
  },
  headerStatusText: {
    fontSize: 12,
    marginTop: 2,
  },
  textGreen: { color: '#10B981' },
  textYellow: { color: '#F59E0B' },
  loadingContainer: {
    position: 'absolute',
    top: '40%',
    left: 0,
    right: 0,
    alignItems: 'center',
  },
  loadingText: {
    color: '#9CA3AF',
    marginTop: 12,
    fontSize: 16,
  },
  chatList: {
    flex: 1,
    paddingHorizontal: 16,
  },
  chatContent: {
    paddingVertical: 16,
  },
  messageWrapper: {
    marginBottom: 12,
    flexDirection: 'row',
  },
  messageMe: {
    justifyContent: 'flex-end',
  },
  messageThem: {
    justifyContent: 'flex-start',
  },
  messageBubble: {
    maxWidth: '80%',
    padding: 12,
    borderRadius: 20,
  },
  bubbleMe: {
    backgroundColor: '#2563EB',
    borderBottomRightRadius: 4,
  },
  bubbleThem: {
    backgroundColor: '#374151',
    borderBottomLeftRadius: 4,
  },
  messageText: {
    color: '#FFFFFF',
    fontSize: 16,
    lineHeight: 22,
  },
  inputArea: {
    flexDirection: 'row',
    padding: 12,
    backgroundColor: '#1F2937',
    borderTopWidth: 1,
    borderTopColor: '#374151',
  },
  input: {
    flex: 1,
    backgroundColor: '#374151',
    color: '#F9FAFB',
    borderRadius: 24,
    paddingHorizontal: 16,
    paddingVertical: 10,
    fontSize: 16,
    marginRight: 10,
  },
  sendButton: {
    backgroundColor: '#3B82F6',
    borderRadius: 24,
    paddingHorizontal: 20,
    justifyContent: 'center',
    alignItems: 'center',
  },
  sendButtonDisabled: {
    backgroundColor: '#4B5563',
  },
  sendButtonText: {
    color: '#FFFFFF',
    fontWeight: '600',
    fontSize: 16,
  },
});
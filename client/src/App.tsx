import React, { useState, useEffect, useRef } from 'react';
import {
  SafeAreaView, View, Text, TextInput, TouchableOpacity,
  FlatList, StyleSheet, Platform, PermissionsAndroid,
  KeyboardAvoidingView, ActivityIndicator, ListRenderItem
} from 'react-native';

import { BleManager, Device, Characteristic, BleError } from 'react-native-ble-plx';
import Peripheral, { Permission, Property } from 'react-native-multi-ble-peripheral';
import { Buffer } from 'buffer';

import { SERVICE_UUID, TX_CHAR_UUID, RX_CHAR_UUID } from '@env';

Peripheral.setDeviceName("BLE_Chat_App")

type Role = 'IDLE' | 'HOST' | 'JOIN';

interface ChatMessage {
  id: string;
  text: string;
  sender: 'me' | 'them';
  timestamp: number;
}

interface WriteRequestPayload {
  value: string | number[];
  characteristic: string;
  service: string;
  device?: string;
}

export default function App() {
  const [role, setRole] = useState<Role>('IDLE');
  const [status, setStatus] = useState<string>('');
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [inputText, setInputText] = useState<string>('');
  const [isConnected, setIsConnected] = useState<boolean>(false);

  // References with explicit types to resolve VS Code "red" lines
  const bleManagerRef = useRef<BleManager | null>(null);
  const peripheralRef = useRef<Peripheral | null>(null);
  const connectedDeviceRef = useRef<Device | null>(null);

  useEffect(() => {
    bleManagerRef.current = new BleManager();
    return () => {
      // Cleanup connections and managers
      if (connectedDeviceRef.current) {
        connectedDeviceRef.current.cancelConnection().catch(() => {});
      }
      bleManagerRef.current?.destroy();
      if (peripheralRef.current) {
        // Safe check for advertising stop
        try {
          (peripheralRef.current as any).stopAdvertising();
        } catch (e) {
          console.warn('Could not stop advertising:', e);
        }
      }
    };
  }, []);

  // -------------------------------------------------------------------------
  // PERMISSIONS
  // -------------------------------------------------------------------------

  const requestPermissions = async (): Promise<boolean> => {
    if (Platform.OS === 'ios') return true;
    const apiLevel = Platform.Version as number;

    if (apiLevel < 31) {
      const granted = await PermissionsAndroid.request(
        PermissionsAndroid.PERMISSIONS.ACCESS_FINE_LOCATION
      );
      return granted === PermissionsAndroid.RESULTS.GRANTED;
    } else {
      const result = await PermissionsAndroid.requestMultiple([
        PermissionsAndroid.PERMISSIONS.BLUETOOTH_SCAN,
        PermissionsAndroid.PERMISSIONS.BLUETOOTH_CONNECT,
        PermissionsAndroid.PERMISSIONS.BLUETOOTH_ADVERTISE,
        PermissionsAndroid.PERMISSIONS.ACCESS_FINE_LOCATION,
      ]);
      return Object.values(result).every(res => res === PermissionsAndroid.RESULTS.GRANTED);
    }
  };

  // -------------------------------------------------------------------------
  // HOST (PERIPHERAL) LOGIC
  // -------------------------------------------------------------------------

  const startHost = async () => {
    if (!(await requestPermissions())) return setStatus('Permissions Denied');
    setRole('HOST');
    setStatus('Initializing Host...');

    try {
      const p = new Peripheral();
      peripheralRef.current = p;

      p.on('ready', async () => {
        await p.addService(SERVICE_UUID, true);
        
        // TX: Central subscribes here to receive messages from Host
        await p.addCharacteristic(
          SERVICE_UUID,
          TX_CHAR_UUID,
          Property.NOTIFY | Property.READ,
          Permission.READABLE
        );

        // RX: Host receives messages here from Central
        await p.addCharacteristic(
          SERVICE_UUID,
          RX_CHAR_UUID,
          Property.WRITE | Property.WRITE_NO_RESPONSE,
          Permission.WRITEABLE
        );

        await p.startAdvertising();
        setStatus('Advertising... Waiting for connection');
      });

      p.on('WriteRequest', (req: WriteRequestPayload) => {
        const value = req.value;
        const decoded = typeof value === 'string'
          ? Buffer.from(value, 'base64').toString('utf8')
          : Buffer.from(value).toString('utf8');
        
        if (decoded) addMessage(decoded, 'them');
      });

      p.on('Connected', () => {
        setIsConnected(true);
        setStatus('Client connected');
      });

      p.on('Disconnected', () => {
        setIsConnected(false);
        setStatus('Disconnected. Still advertising...');
      });
    } catch (e: any) {
      setStatus(`Host Error: ${e.message}`);
    }
  };

  // -------------------------------------------------------------------------
  // JOIN (CENTRAL) LOGIC
  // -------------------------------------------------------------------------

  const startJoin = async () => {
    if (!(await requestPermissions())) return setStatus('Permissions Denied');
    setRole('JOIN');
    setStatus('Scanning...');

    if (!bleManagerRef.current) return;

    bleManagerRef.current.startDeviceScan([SERVICE_UUID], null, async (err: BleError | null, device: Device | null) => {
      if (err) return setStatus(`Scan Error: ${err.message}`);
      
      if (device) {
        bleManagerRef.current?.stopDeviceScan();
        try {
          const connected = await device.connect();
          
          // Request MTU to handle longer text strings (Android specific)
          if (Platform.OS === 'android') {
            await connected.requestMTU(512);
          }

          await connected.discoverAllServicesAndCharacteristics();
          connectedDeviceRef.current = connected;
          setIsConnected(true);
          setStatus('Connected to Host');

          // Monitor the Host's TX characteristic
          connected.monitorCharacteristicForService(
            SERVICE_UUID,
            TX_CHAR_UUID,
            (mErr: BleError | null, char: Characteristic | null) => {
              if (mErr) {
                console.error('Monitor Error:', mErr);
                return;
              }
              if (char?.value) {
                const decoded = Buffer.from(char.value, 'base64').toString('utf8');
                addMessage(decoded, 'them');
              }
            }
          );

          connected.onDisconnected(() => {
            setIsConnected(false);
            setStatus('Disconnected from Host');
          });
        } catch (e: any) {
          setStatus(`Conn Error: ${e.message}`);
        }
      }
    });
  };

  // -------------------------------------------------------------------------
  // SHARED CHAT LOGIC
  // -------------------------------------------------------------------------

  const addMessage = (text: string, sender: 'me' | 'them') => {
    setMessages(prev => [
      { id: Date.now().toString() + Math.random(), text, sender, timestamp: Date.now() },
      ...prev
    ]);
  };

  const sendMessage = async () => {
    if (!inputText.trim() || !isConnected) return;
    const msg = inputText.trim();
    setInputText('');

    try {
      if (role === 'HOST' && peripheralRef.current) {
        // Send to central via Notification
        await peripheralRef.current.updateValue(
          SERVICE_UUID,
          TX_CHAR_UUID,
          Buffer.from(msg)
        );
      } else if (role === 'JOIN' && connectedDeviceRef.current) {
        // Write to peripheral's RX characteristic
        await connectedDeviceRef.current.writeCharacteristicWithResponseForService(
          SERVICE_UUID,
          RX_CHAR_UUID,
          Buffer.from(msg).toString('base64')
        );
      }
      addMessage(msg, 'me');
    } catch (e: any) {
      console.error('Send Error:', e);
      setStatus(`Send Fail: ${e.message}`);
    }
  };

  const renderMessage: ListRenderItem<ChatMessage> = ({ item }) => (
    <View style={[styles.msg, item.sender === 'me' ? styles.msgMe : styles.msgThem]}>
      <Text style={styles.msgText}>{item.text}</Text>
    </View>
  );

  // -------------------------------------------------------------------------
  // RENDER
  // -------------------------------------------------------------------------

  if (role === 'IDLE') {
    return (
      <SafeAreaView style={styles.container}>
        <View style={styles.center}>
          <Text style={styles.title}>BLE Chat (TS)</Text>
          <TouchableOpacity style={styles.button} onPress={startHost}>
            <Text style={styles.btnText}>Host (Peripheral)</Text>
          </TouchableOpacity>
          <TouchableOpacity style={styles.button} onPress={startJoin}>
            <Text style={styles.btnText}>Join (Central)</Text>
          </TouchableOpacity>
        </View>
      </SafeAreaView>
    );
  }

  return (
    <SafeAreaView style={styles.container}>
      <View style={styles.header}>
        <View>
          <Text style={styles.statusLabel}>{role === 'HOST' ? 'HOST MODE' : 'JOIN MODE'}</Text>
          <Text style={[styles.statusValue, isConnected ? styles.connected : styles.disconnected]}>
            {status}
          </Text>
        </View>
        <TouchableOpacity onPress={() => setRole('IDLE')} style={styles.resetBtn}>
          <Text style={styles.resetText}>Exit</Text>
        </TouchableOpacity>
      </View>

      <KeyboardAvoidingView 
        behavior={Platform.OS === 'ios' ? 'padding' : 'height'} 
        style={styles.chatArea}
        keyboardVerticalOffset={Platform.OS === 'ios' ? 90 : 0}
      >
        <FlatList
          inverted
          data={messages}
          renderItem={renderMessage}
          keyExtractor={item => item.id}
          contentContainerStyle={styles.listContent}
        />
        
        <View style={styles.inputRow}>
          <TextInput 
            style={styles.input} 
            value={inputText} 
            onChangeText={setInputText} 
            placeholder={isConnected ? "Type message..." : "Waiting for connection..."} 
            placeholderTextColor="#999"
            editable={isConnected}
          />
          <TouchableOpacity 
            onPress={sendMessage} 
            style={[styles.send, !isConnected && styles.sendDisabled]}
            disabled={!isConnected}
          >
            <Text style={styles.sendText}>Send</Text>
          </TouchableOpacity>
        </View>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: '#111', padding: 40 },
  center: { flex: 1, justifyContent: 'center', alignItems: 'center', padding: 20 },
  title: { fontSize: 24, fontWeight: 'bold', color: '#fff', marginBottom: 30 },
  button: { backgroundColor: '#007AFF', padding: 18, borderRadius: 12, marginVertical: 8, width: '80%', alignItems: 'center' },
  btnText: { color: 'white', fontWeight: 'bold', fontSize: 16 },
  header: { padding: 16, borderBottomWidth: 1, borderColor: '#333', flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', backgroundColor: '#1a1a1a' },
  statusLabel: { color: '#888', fontSize: 10, fontWeight: 'bold' },
  statusValue: { fontSize: 14, marginTop: 2 },
  connected: { color: '#4cd964' },
  disconnected: { color: '#ff3b30' },
  resetBtn: { padding: 8 },
  resetText: { color: '#ff3b30', fontWeight: 'bold' },
  chatArea: { flex: 1 },
  listContent: { padding: 16 },
  msg: { marginVertical: 4, padding: 12, borderRadius: 18, maxWidth: '80%' },
  msgMe: { alignSelf: 'flex-end', backgroundColor: '#007AFF', borderBottomRightRadius: 2 },
  msgThem: { alignSelf: 'flex-start', backgroundColor: '#333', borderBottomLeftRadius: 2 },
  msgText: { color: 'white', fontSize: 15 },
  inputRow: { flexDirection: 'row', padding: 12, borderTopWidth: 1, borderColor: '#333', backgroundColor: '#1a1a1a', alignItems: 'center' },
  input: { flex: 1, backgroundColor: '#222', borderRadius: 20, paddingHorizontal: 16, height: 40, color: 'white', borderWidth: 1, borderColor: '#444' },
  send: { marginLeft: 12, paddingHorizontal: 16, paddingVertical: 8 },
  sendDisabled: { opacity: 0.3 },
  sendText: { color: '#007AFF', fontWeight: 'bold', fontSize: 16 }
});
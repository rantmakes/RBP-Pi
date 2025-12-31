import time
import socket
import struct
import json
import os
import math
import random

# ==============================================================================
# ============================== CONFIGURATION =================================
# ==============================================================================

HOST_SERIAL = "Mock Probe Host" 
SERVER_PORT = 5050
MULTICAST_GROUP = '224.0.0.1'

# Timers
SYNC_SEND_RATE = 2.0
TEMP_SEND_RATE = 0.5 
FORCE_START_DELAY = 6.0 

# ==============================================================================
# ============================ PROTOCOL CONSTANTS ==============================
# ==============================================================================

RDP_VERSION_1_0 = "RDP_1.0"
KEY_VERSION = "RPVersion"
KEY_SERIAL = "RPSerial"
KEY_EPOCH = "RPEpoch"
KEY_PAYLOAD = "RPPayload"
KEY_EVENT_TYPE = "RPEventType"
KEY_CHANNEL = "RPChannel"
KEY_VALUE = "RPValue"
KEY_META = "RPMetaType"

EVENT_SYN = 1
EVENT_ACK = 2
EVENT_TEMP = 3 

# Meta Types
META_BT = 3000       
META_MET = 3002      
META_EXHAUST = 3004  
META_AMBIENT = 3005  

# ==============================================================================
# ============================ CLASSES & LOGIC =================================
# ==============================================================================

class HostState:
    SEARCHING = 0
    CONNECTED = 1

class MockProbeHost:
    def __init__(self):
        self.state = HostState.SEARCHING
        self.server_address = None 
        self.last_sync_time = 0
        self.last_temp_time = 0
        self.start_time = time.monotonic()
        
        # CRITICAL FIX 1: Start Time at 0.0 (Relative Time)
        # Most roasting apps plot X-Axis as "Time from start", not "Time since 1970"
        self.roast_time_counter = 0.0 
        
        # Setup UDP Socket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack('b', 1))
        self.sock.bind(('', SERVER_PORT))
        self.sock.setblocking(False)

        # We will ignore this list and SHOTGUN data to all channels in send_temps
        self.probes = [] 

    def write_web_log(self, datagram):
        file_path = "/var/www/html/rdp_packet.json"
        log_data = datagram.copy()
        try:
            temp_path = file_path + ".tmp"
            with open(temp_path, 'w') as f:
                json.dump(log_data, f)
            os.replace(temp_path, file_path)
        except Exception:
            pass

    def read_incoming(self):
        try:
            data, addr = self.sock.recvfrom(4096)
            message = data.decode('utf-8')
            try:
                packet = json.loads(message)
            except json.JSONDecodeError:
                return 

            if (packet.get(KEY_VERSION) == RDP_VERSION_1_0 and 
                packet.get(KEY_SERIAL) == HOST_SERIAL and 
                str(packet.get(KEY_EVENT_TYPE)) == str(EVENT_ACK)):
                
                print(f"SUCCESS: Handshake Complete! ACK received from {addr[0]}")
                self.server_address = (addr[0], SERVER_PORT)
                self.state = HostState.CONNECTED
                self.start_time = 0 
                
        except BlockingIOError:
            pass 

    def send_syn(self):
        payload_array = [{ KEY_EVENT_TYPE: EVENT_SYN }]
        
        # SYN packets usually use 0 or a timestamp, doesn't matter much
        # But let's send 0 to be consistent with "Start of Roast"
        current_epoch = 0 
        
        datagram = {
            KEY_VERSION: RDP_VERSION_1_0,
            KEY_SERIAL: HOST_SERIAL,
            KEY_EPOCH: current_epoch,
            KEY_PAYLOAD: json.dumps(payload_array)
        }
        
        msg_bytes = json.dumps(datagram).encode('utf-8') + b'\r\n'
        
        print(f"Sending SYN... (Waking up App)")
        self.write_web_log(datagram)
        self.sock.sendto(msg_bytes, (MULTICAST_GROUP, SERVER_PORT))

    def send_temps(self):
        # Generate a nice sine wave value
        # Base 200, +/- 50 degrees
        val = 200 + 50 * math.sin(self.roast_time_counter * 0.2)
        val_native = round(float(val), 2)
        
        payload_list = []
        
        # CRITICAL FIX 2: Shotgun approach
        # Send the SAME value to Channels 1, 2, 3, and 4
        # This guarantees we hit the configured curve, whatever channel it is
        for ch in [1, 2, 3, 4]:
            event = {
                KEY_EVENT_TYPE: EVENT_TEMP, 
                KEY_CHANNEL: ch,
                KEY_VALUE: val_native,  # Sending NUMBER
                KEY_META: META_BT       # Claiming they are all Bean Temps
            }
            payload_list.append(event)

        datagram = {
            KEY_VERSION: RDP_VERSION_1_0,
            KEY_SERIAL: HOST_SERIAL,
            
            # CRITICAL FIX 3: Sending Relative Time (0.0, 0.5, 1.0...)
            # This places the data at the START of the graph
            KEY_EPOCH: self.roast_time_counter, 
            
            KEY_PAYLOAD: json.dumps(payload_list)
        }

        # Terminator included
        msg_bytes = json.dumps(datagram).encode('utf-8') + b'\r\n'
        self.write_web_log(datagram)

        target = self.server_address if self.server_address else (MULTICAST_GROUP, SERVER_PORT)
        self.sock.sendto(msg_bytes, target)
        print(f".", end="", flush=True)

        # Increment logical clock by 0.5 seconds
        self.roast_time_counter += 0.5

    def run(self):
        print(f"Roastmaster DIAGNOSTIC Host Started.")
        print(f"Serial: {HOST_SERIAL}")
        print(f"Strategy: Relative Time (0.0s...) + All Channels + Float Values")
        
        while True:
            current_time = time.monotonic()
            self.read_incoming()

            if self.state == HostState.SEARCHING and (current_time - self.start_time > FORCE_START_DELAY):
                print("\n\n*** TIMEOUT: Forcing Connection Mode ***")
                self.state = HostState.CONNECTED

            if self.state == HostState.SEARCHING:
                if current_time - self.last_sync_time > SYNC_SEND_RATE:
                    self.send_syn()
                    self.last_sync_time = current_time
            
            elif self.state == HostState.CONNECTED:
                if current_time - self.last_temp_time > TEMP_SEND_RATE:
                    self.send_temps()
                    self.last_temp_time = current_time
            
            time.sleep(0.01)

if __name__ == "__main__":
    host = MockProbeHost()
    try:
        host.run()
    except KeyboardInterrupt:
        print("\nStopping Mock Host...")

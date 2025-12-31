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
        
        # Setup UDP Socket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack('b', 1))
        self.sock.bind(('', SERVER_PORT))
        self.sock.setblocking(False)

        self.probes = [
            {"channel": 1, "meta_type": META_BT, "base": 200, "amp": 20, "freq": 0.1},
            {"channel": 2, "meta_type": META_EXHAUST, "base": 150, "amp": 15, "freq": 0.1},
            {"channel": 3, "meta_type": META_AMBIENT, "base": 45, "amp": 2, "freq": 1.0},
            {"channel": 4, "meta_type": META_MET, "base": 800, "amp": 100, "freq": 0.05}
        ]

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
        current_epoch = time.time()
        
        datagram = {
            KEY_VERSION: RDP_VERSION_1_0,
            KEY_SERIAL: HOST_SERIAL,
            KEY_EPOCH: current_epoch,
            KEY_PAYLOAD: json.dumps(payload_array)
        }
        
        # ADDED: \r\n Terminator (Crucial for Arduino-like parsing)
        msg_bytes = json.dumps(datagram).encode('utf-8') + b'\r\n'
        
        print(f"Sending SYN... (Waking up App)")
        self.write_web_log(datagram)
        self.sock.sendto(msg_bytes, (MULTICAST_GROUP, SERVER_PORT))

    def generate_fake_data(self):
        t = time.time()
        data = []
        for p in self.probes:
            if p["channel"] == 3: val = p["base"] + random.uniform(-1, 1) * p["amp"]
            elif p["channel"] == 4: val = p["base"] + (p["amp"] * (math.sin(t * p["freq"]) + 1))
            else: val = p["base"] + p["amp"] * math.sin(t * p["freq"])
            
            data.append({"channel": p["channel"], "meta": p["meta_type"], "val": val})
        return data

    def send_temps(self):
        sensor_data = self.generate_fake_data()
        payload_list = []
        
        for item in sensor_data:
            # BACK TO STRINGS (Matches 0-0-14 which Linked successfully)
            # Formatted to 2 decimal places: "200.55"
            val_str = "{:.2f}".format(item["val"])
            
            event = {
                KEY_EVENT_TYPE: EVENT_TEMP, 
                KEY_CHANNEL: item["channel"],
                KEY_VALUE: val_str, # Sending String
                KEY_META: item["meta"]
            }
            payload_list.append(event)

        current_epoch = time.time()

        datagram = {
            KEY_VERSION: RDP_VERSION_1_0,
            KEY_SERIAL: HOST_SERIAL,
            KEY_EPOCH: current_epoch,
            KEY_PAYLOAD: json.dumps(payload_list)
        }

        # ADDED: \r\n Terminator (The likely missing piece for Graphing)
        msg_bytes = json.dumps(datagram).encode('utf-8') + b'\r\n'
        
        # Debug Log 10% of packets to verify format
        if random.random() < 0.1:
            print(f"DEBUG OUT: {json.dumps(datagram)[:60]}...")

        self.write_web_log(datagram)

        target = self.server_address if self.server_address else (MULTICAST_GROUP, SERVER_PORT)
        self.sock.sendto(msg_bytes, target)
        print(".", end="", flush=True)

    def run(self):
        print(f"Roastmaster FINAL TEST Host Started.")
        print(f"Serial: {HOST_SERIAL}")
        print(f"Config: Unix Epoch + String Values + \\r\\n Terminator")
        
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

import time
import socket
import struct
import json
import os
import board
import busio
import digitalio
import adafruit_mcp9600
import adafruit_scd4x
import adafruit_bitbangio as bitbangio

# ==============================================================================
# ============================== CONFIGURATION =================================
# ==============================================================================

# Network Configuration
HOST_SERIAL = "My Probe Host"
SERVER_PORT = 5050
MULTICAST_GROUP = '224.0.0.1'

# Timers (in seconds)
# Datasheet advises sending at least once per second 
SYNC_SEND_RATE = 2.0
TEMP_SEND_RATE = 1.0

# Status LED Pin
STATUS_LED_PIN = board.D2
STATUS_LED_ACTIVE_LOW = True 

# --- HARDWARE CONFIGURATION ---

# 1. Primary Thermocouple (MCP9600) -> Hardware I2C (GPIO 2/3)
# No pin config needed, uses board default.

# 2. Secondary Sensor (SCD-41) -> Software I2C (Custom Pins)
# Define your separate hardware pins here:
SCD_SDA_PIN = board.D23
SCD_SCL_PIN = board.D24

# ==============================================================================
# ============================ RDP PROTOCOL CONSTANTS ==========================
# ==============================================================================

RDP_VERSION_1_0 = "RDP_1.0"
KEY_VERSION = "RPVersion"
KEY_SERIAL = "RPSerial"
KEY_EPOCH = "RPEpoch"
KEY_PAYLOAD = "RPPayload"
KEY_EVENT_TYPE = "RPEventType"
KEY_CHANNEL = "RPChannel"
KEY_VALUE = "RPValue"

# Event Types [cite: 366]
EVENT_SYN = 1
EVENT_ACK = 2
EVENT_TEMP = 3 

# ==============================================================================
# ============================ CLASSES & LOGIC =================================
# ==============================================================================

class HostState:
    SEARCHING = 0
    CONNECTED = 1

class ProbeHost:
    def __init__(self):
        self.state = HostState.SEARCHING
        self.send_count = 0  # Epoch Counter [cite: 294]
        self.server_address = None
        self.last_sync_time = 0
        self.last_temp_time = 0
        
        # Initialize LED
        self.led = None
        if STATUS_LED_PIN:
            self.led = digitalio.DigitalInOut(STATUS_LED_PIN)
            self.led.direction = digitalio.Direction.OUTPUT
            self.set_led(False)

        # Initialize UDP Socket [cite: 218]
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack('b', 1))
        self.sock.bind(('', SERVER_PORT))
        self.sock.setblocking(False)

        # List to hold our diverse sensor objects
        # Each entry acts as a virtual "Probe" mapped to an RDP Channel
        self.probes = []

        # --- INIT SENSORS ---
        self.init_mcp9600()
        self.init_scd41()

    def init_mcp9600(self):
        try:
            # Hardware I2C (GPIO 2, 3)
            i2c = board.I2C() 
            mcp = adafruit_mcp9600.MCP9600(i2c)
            
            # Channel 1: Thermocouple
            self.probes.append({
                "channel": 1,
                "label": "Bean Temp",
                "handle": mcp,
                "read_func": lambda s: s.temperature, 
                "val": None,
                "error": False
            })
            print(f"Initialized MCP9600 (Bean Temp) on Channel 1")
        except Exception as e:
            print(f"Error initializing MCP9600: {e}")

    def init_scd41(self):
        try:
            # Software I2C on separate pins
            i2c_soft = bitbangio.I2C(SCD_SCL_PIN, SCD_SDA_PIN)
            scd = adafruit_scd4x.SCD4X(i2c_soft)
            scd.start_periodic_measurement()
            
            print(f"Initialized SCD-41 on Pins {SCD_SDA_PIN}/{SCD_SCL_PIN}")

            # Channel 2: Ambient Temp
            self.probes.append({
                "channel": 2,
                "label": "Ambient Temp",
                "handle": scd,
                "read_func": lambda s: s.temperature,
                "val": None,
                "error": False
            })

            # Channel 3: Humidity
            # Note: We send this as EVENT_TEMP (Type 3) so Roastmaster graphs it.
            self.probes.append({
                "channel": 3,
                "label": "Humidity",
                "handle": scd,
                "read_func": lambda s: s.relative_humidity,
                "val": None,
                "error": False
            })

            # Channel 4: CO2
            # Note: We send this as EVENT_TEMP (Type 3) so Roastmaster graphs it.
            self.probes.append({
                "channel": 4,
                "label": "CO2",
                "handle": scd,
                "read_func": lambda s: s.CO2,
                "val": None,
                "error": False
            })

        except Exception as e:
            print(f"Error initializing SCD-41: {e}")

    def set_led(self, on):
        if not self.led: return
        if STATUS_LED_ACTIVE_LOW:
            self.led.value = not on
        else:
            self.led.value = on

    def blink_led(self, times, duration=0.1):
        if not self.led: return
        for _ in range(times):
            self.set_led(True)
            time.sleep(duration)
            self.set_led(False)
            time.sleep(duration)

    def write_web_log(self, datagram):
        """Writes current packet to JSON for the Apache web monitor"""
        file_path = "/var/www/html/rdp_packet.json"
        log_data = datagram.copy()
        log_data['LocalTimestamp'] = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
        
        try:
            temp_path = file_path + ".tmp"
            with open(temp_path, 'w') as f:
                json.dump(log_data, f)
            os.replace(temp_path, file_path)
        except Exception as e:
            pass

    def read_incoming(self):
        try:
            data, addr = self.sock.recvfrom(1024)
            message = data.decode('utf-8')
            try:
                packet = json.loads(message)
            except json.JSONDecodeError:
                return 

            # Check for Handshake ACK [cite: 352]
            if (packet.get(KEY_VERSION) == RDP_VERSION_1_0 and 
                packet.get(KEY_SERIAL) == HOST_SERIAL and 
                packet.get(KEY_EVENT_TYPE) == str(EVENT_ACK)):
                
                print(f"Received ACK from Server at {addr[0]}")
                self.server_address = (addr[0], SERVER_PORT)
                self.state = HostState.CONNECTED
                self.last_temp_time = 0
                
        except BlockingIOError:
            pass 

    def send_syn(self):
        """Sends a Discovery Packet [cite: 337]"""
        payload_array = [{ KEY_EVENT_TYPE: EVENT_SYN }]
        datagram = {
            KEY_VERSION: RDP_VERSION_1_0,
            KEY_SERIAL: HOST_SERIAL,
            KEY_EPOCH: self.send_count,
            KEY_PAYLOAD: json.dumps(payload_array)
        }
        msg_bytes = json.dumps(datagram).encode('utf-8')
        
        print(f"Sending SYN...")
        self.write_web_log(datagram)
        self.sock.sendto(msg_bytes, (MULTICAST_GROUP, SERVER_PORT))
        self.send_count += 1
        self.blink_led(2)

    def read_sensors(self):
        for p in self.probes:
            try:
                # Check data readiness if applicable (SCD41)
                if hasattr(p["handle"], "data_ready") and not p["handle"].data_ready:
                     pass # Use previous value if new data isn't ready yet

                val = p["read_func"](p["handle"])
                
                if val is not None:
                    p["val"] = val
                    p["error"] = False
                else:
                    p["error"] = True
                    
            except Exception as e:
                p["error"] = True

        if any(p["error"] for p in self.probes):
            self.blink_led(5, 0.05)

    def send_temps(self):
        self.set_led(True)
        self.read_sensors()
        
        payload_list = []
        
        # Iterate through all probes (MCP9600 & SCD-41 channels)
        # And pack them into a single Payload Array 
        for p in self.probes:
            val_str = "null"
            if not p["error"] and p["val"] is not None:
                val_str = "{:.2f}".format(p["val"])
            
            event = {
                KEY_EVENT_TYPE: EVENT_TEMP, # Always Type 3 for Graphing 
                KEY_CHANNEL: p["channel"],  # Unique Channel ID [cite: 317]
                KEY_VALUE: val_str          # Value as String [cite: 303]
            }
            payload_list.append(event)

        if not payload_list:
            self.set_led(False)
            return

        # Construct the final RDP Datagram
        datagram = {
            KEY_VERSION: RDP_VERSION_1_0,
            KEY_SERIAL: HOST_SERIAL,
            KEY_EPOCH: self.send_count,      # Epoch for ordering [cite: 294]
            KEY_PAYLOAD: json.dumps(payload_list) # Array of events
        }

        msg_bytes = json.dumps(datagram).encode('utf-8')
        self.write_web_log(datagram)

        if self.server_address:
            self.sock.sendto(msg_bytes, self.server_address)
            self.send_count += 1
        
        self.set_led(False)

    def run(self):
        print(f"Roastmaster RDP Host Started.")
        print(f"Monitoring {len(self.probes)} Data Streams.")
        
        while True:
            current_time = time.monotonic()
            self.read_incoming()

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
    host = ProbeHost()
    try:
        host.run()
    except KeyboardInterrupt:
        print("\nStopping Roastmaster Host...")

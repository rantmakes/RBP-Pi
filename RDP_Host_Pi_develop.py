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
SYNC_SEND_RATE = 2.0
TEMP_SEND_RATE = 1.0

# Status LED Pin (Optional)
STATUS_LED_PIN = board.D2
STATUS_LED_ACTIVE_LOW = True 

# --- SENSOR CONFIGURATION ---

# 1. Primary Thermocouple (MCP9600) on Hardware I2C (GPIO 2/3)
# No pin config needed, uses board default.

# 2. Secondary Sensor (SCD-41) on Software I2C (Custom Pins)
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
        self.send_count = 0 
        self.server_address = None
        self.last_sync_time = 0
        self.last_temp_time = 0
        
        # Initialize LED
        self.led = None
        if STATUS_LED_PIN:
            self.led = digitalio.DigitalInOut(STATUS_LED_PIN)
            self.led.direction = digitalio.Direction.OUTPUT
            self.set_led(False)

        # Initialize UDP Socket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack('b', 1))
        self.sock.bind(('', SERVER_PORT))
        self.sock.setblocking(False)

        # List to hold our diverse sensor objects
        # Format: {"channel": int, "type": "mcp"|"scd", "data_key": str, "handle": obj, "temp": float, "error": bool}
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
                "read_func": lambda s: s.temperature, # Lambda to normalize reading method
                "val": None,
                "error": False
            })
            print(f"Initialized MCP9600 (Bean Temp) on Channel 1 (Hardware I2C)")
        except Exception as e:
            print(f"Error initializing MCP9600: {e}")

    def init_scd41(self):
        try:
            # Software I2C on separate pins
            i2c_soft = bitbangio.I2C(SCD_SCL_PIN, SCD_SDA_PIN)
            scd = adafruit_scd4x.SCD4X(i2c_soft)
            scd.start_periodic_measurement()
            
            print(f"Initialized SCD-41 on Pins {SCD_SDA_PIN}/{SCD_SCL_PIN} (Software I2C)")

            # We treat one physical sensor as 3 separate RDP probes/channels
            
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
            self.probes.append({
                "channel": 3,
                "label": "Humidity",
                "handle": scd,
                "read_func": lambda s: s.relative_humidity,
                "val": None,
                "error": False
            })

            # Channel 4: CO2
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
        # SCD-41 check data ready (it updates every ~5 seconds by default)
        # We need to be careful not to block heavily, but for RDP 1s interval it's okay to just read latest.
        # The library caches the last read value if data isn't ready, which is fine.
        
        for p in self.probes:
            try:
                # Execute the lambda function stored in setup to get the specific value
                if hasattr(p["handle"], "data_ready") and not p["handle"].data_ready:
                     # If SCD41 data isn't new, we just keep the old value or skip
                     # Ideally we just read; the library handles stale data gracefully usually
                     pass

                val = p["read_func"](p["handle"])
                
                # Basic validation
                if val is not None:
                    p["val"] = val
                    p["error"] = False
                else:
                    # Keep previous value if read fails transiently? 
                    # Or flag error? Let's flag error to be safe.
                    p["error"] = True
                    
            except Exception as e:
                # print(f"Read Error Ch {p['channel']}: {e}")
                p["error"] = True

        # Check for aggregate error for LED notification
        if any(p["error"] for p in self.probes):
            self.blink_led(5, 0.05)

    def send_temps(self):
        self.set_led(True)
        self.read_sensors()
        
        payload_list = []
        for p in self.probes:
            # Send if no error, or if you want to send nulls for errors
            val_str = "null"
            if not p["error"] and p["val"] is not None:
                val_str = "{:.2f}".format(p["val"])
            
            event = {
                KEY_EVENT_TYPE: EVENT_TEMP,
                KEY_CHANNEL: p["channel"],
                KEY_VALUE: val_str
            }
            payload_list.append(event)

        if not payload_list:
            self.set_led(False)
            return

        datagram = {
            KEY_VERSION: RDP_VERSION_1_0,
            KEY_SERIAL: HOST_SERIAL,
            KEY_EPOCH: self.send_count,
            KEY_PAYLOAD: json.dumps(payload_list) 
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

# ==============================================================================
# ================================= MAIN =======================================
# ==============================================================================

if __name__ == "__main__":
    host = ProbeHost()
    try:
        host.run()
    except KeyboardInterrupt:
        print("\nStopping Roastmaster Host...")

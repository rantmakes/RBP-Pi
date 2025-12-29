import time
import socket
import struct
import json
import board
import digitalio
import adafruit_max31855
import adafruit_bitbangio as bitbangio  # <--- UPDATED IMPORT

# ==============================================================================
# ============================== CONFIGURATION =================================
# ==============================================================================

# Network Configuration
# WiFi Connection is handled by the Raspberry Pi OS (wpa_supplicant).
# Ensure your Pi is connected to the same network as the Roastmaster device.

# The Serial Number for this host (Must match Roastmaster Probe definition)
HOST_SERIAL = "424242"

# The port set in the Roastmaster Probe definition (Standard is 5050)
SERVER_PORT = 5050

# Multicast Address for discovery (Standard RDP)
MULTICAST_GROUP = '224.0.0.1'

# Timers (in seconds)
SYNC_SEND_RATE = 2.0
TEMP_SEND_RATE = 1.0

# GPIO Pin Definitions (BCM Numbering)
# Update these to match your wiring on the Raspberry Pi Zero 2 W
# Example: CLK=Pin 11 (GPIO17), CS=Pin 24 (GPIO8), DO=Pin 21 (GPIO9)
PIN_CLK = board.D17
PIN_CS  = board.D8
PIN_DO  = board.D9

# Status LED Pin (Optional)
# Set to None if not using an LED.
STATUS_LED_PIN = board.D2
STATUS_LED_ACTIVE_LOW = True 

# ==============================================================================
# ============================ RDP PROTOCOL CONSTANTS ==========================
# ==============================================================================

RDP_VERSION_1_0 = "RDP_1.0"

# Keys
KEY_VERSION = "RPVersion"
KEY_SERIAL = "RPSerial"
KEY_EPOCH = "RPEpoch"
KEY_PAYLOAD = "RPPayload"
KEY_EVENT_TYPE = "RPEventType"
KEY_CHANNEL = "RPChannel"
KEY_VALUE = "RPValue"

# Event Types
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
        self.send_count = 0  # Epoch
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
        
        # Allow reuse of address
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        # Set Multicast TTL to 1 (local network only)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack('b', 1))
        
        # Bind to the port to listen for ACKs
        # We bind to '' to listen on all interfaces
        self.sock.bind(('', SERVER_PORT))
        
        # Set non-blocking so we don't hang waiting for packets
        self.sock.setblocking(False)

        # Initialize Sensor (MAX31855) via Software SPI
        # RDP supports up to 16 channels, you can add more sensor objects to this list
        self.probes = []
        try:
            # UPDATED: Use the Linux-compatible bitbangio library
            spi = bitbangio.SPI(PIN_CLK, MOSI=None, MISO=PIN_DO)
            cs = digitalio.DigitalInOut(PIN_CS)
            sensor = adafruit_max31855.MAX31855(spi, cs)
            
            self.probes.append({
                "channel": 1, # Roastmaster Channel ID
                "handle": sensor,
                "temp": None,
                "error": False
            })
            print(f"Initialized Probe on Channel 1")
        except Exception as e:
            print(f"Error initializing SPI/Sensor: {e}")
            print("Ensure wiring is correct and adafruit-circuitpython-bitbangio is installed.")

    def set_led(self, on):
        if not self.led: return
        # Handle Active Low logic
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

    def read_incoming(self):
        try:
            data, addr = self.sock.recvfrom(1024)
            message = data.decode('utf-8')
            
            try:
                packet = json.loads(message)
            except json.JSONDecodeError:
                return # Ignore non-JSON packets

            # Check for ACK
            if (packet.get(KEY_VERSION) == RDP_VERSION_1_0 and 
                packet.get(KEY_SERIAL) == HOST_SERIAL and 
                packet.get(KEY_EVENT_TYPE) == str(EVENT_ACK)): # JSON values sometimes come as strings
                
                print(f"Received ACK from Server at {addr[0]}")
                self.server_address = (addr[0], SERVER_PORT)
                self.state = HostState.CONNECTED
                
                # Reset timers to send immediately
                self.last_temp_time = 0
                
        except BlockingIOError:
            pass # No data available

    def send_syn(self):
        # Construct Payload
        payload_array = [{
            KEY_EVENT_TYPE: EVENT_SYN
        }]
        
        # Construct Datagram
        datagram = {
            KEY_VERSION: RDP_VERSION_1_0,
            KEY_SERIAL: HOST_SERIAL,
            KEY_EPOCH: self.send_count,
            KEY_PAYLOAD: json.dumps(payload_array) # RDP expects payload as a serialized JSON string
        }
        
        msg_bytes = json.dumps(datagram).encode('utf-8')
        
        print(f"Sending SYN to {MULTICAST_GROUP}:{SERVER_PORT}")
        self.sock.sendto(msg_bytes, (MULTICAST_GROUP, SERVER_PORT))
        
        self.send_count += 1
        self.blink_led(2) # 2 Blinks for SYN

    def read_probes(self):
        # Read all probes
        for p in self.probes:
            try:
                # The Adafruit library handles linearization automatically
                temp_c = p["handle"].temperature
                p["temp"] = temp_c
                p["error"] = False
            except RuntimeError as e:
                # Usually means open circuit or short to ground
                print(f"Probe Error on Ch {p['channel']}: {e}")
                p["temp"] = None
                p["error"] = True
                self.blink_led(5, 0.05) # Fast 5 blinks for error

    def send_temps(self):
        self.set_led(True) # LED on during read/transmit
        
        self.read_probes()
        
        payload_list = []
        
        for p in self.probes:
            # Logic: Send if good read, or if we are configured to transmit errors (transmitOnReadError=True)
            if not p["error"] or True: 
                val_str = "null"
                if p["temp"] is not None:
                    val_str = "{:.2f}".format(p["temp"])
                
                event = {
                    KEY_EVENT_TYPE: EVENT_TEMP,
                    KEY_CHANNEL: p["channel"],
                    KEY_VALUE: val_str # RDP expects string values for temps
                }
                
                payload_list.append(event)

        if not payload_list:
            self.set_led(False)
            return

        # RDP requires the payload to be a stringified JSON array
        payload_str = json.dumps(payload_list)

        datagram = {
            KEY_VERSION: RDP_VERSION_1_0,
            KEY_SERIAL: HOST_SERIAL,
            KEY_EPOCH: self.send_count,
            KEY_PAYLOAD: payload_str 
        }

        msg_bytes = json.dumps(datagram).encode('utf-8')

        if self.server_address:
            # print(f"Sending Temps to {self.server_address}")
            self.sock.sendto(msg_bytes, self.server_address)
            self.send_count += 1
        
        self.set_led(False)

    def run(self):
        print(f"Roastmaster RDP Host Started. Serial: {HOST_SERIAL}")
        
        while True:
            current_time = time.monotonic()
            
            # Check for network data (ACKs)
            self.read_incoming()

            if self.state == HostState.SEARCHING:
                if current_time - self.last_sync_time > SYNC_SEND_RATE:
                    self.send_syn()
                    self.last_sync_time = current_time
            
            elif self.state == HostState.CONNECTED:
                if current_time - self.last_temp_time > TEMP_SEND_RATE:
                    self.send_temps()
                    self.last_temp_time = current_time
            
            # Tiny sleep to prevent 100% CPU usage
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

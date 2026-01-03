import logging
import struct
import board
import busio  # REQUIRED for setting frequency
import digitalio
import adafruit_mcp9600
import adafruit_scd4x

# Bluezero Imports
import bluezero.localGATT
from bluezero import peripheral
from bluezero import adapter
from bluezero import async_tools

# ==============================================================================
# ============================== CONFIGURATION =================================
# ==============================================================================

SERVER_NAME = "RoastProbe"
SERIAL_NUMBER = "12345"
UPDATE_RATE_MS = 500

# --- Status LED ---
STATUS_LED_PIN = board.D2
STATUS_LED_ACTIVE_LOW = True 

# ==============================================================================
# ============================ RBP PROTOCOL UUIDS ==============================
# ==============================================================================
def rbp_uuid(mask):
    return f"4ac9{mask}-0b71-11e8-b8f5-b827ebe1d493"

UUID_SERVICE_RBP_SENSING = rbp_uuid("0000")
UUID_SERVICE_DEVICE_INFO = "0000180a-0000-1000-8000-00805f9b34fb"

UUID_CHAR_TEMP_1     = rbp_uuid("0001") # Bean Temp
UUID_CHAR_TEMP_2     = rbp_uuid("0002") # Exhaust Temp
UUID_CHAR_HUMIDITY_1 = rbp_uuid("000b") # Humidity
UUID_CHAR_USER_1     = rbp_uuid("0015") # CO2

UUID_DESC_HUMIDITY_SCALE = rbp_uuid("001c") 

UUID_CHAR_MANUF_NAME = "00002a29-0000-1000-8000-00805f9b34fb"
UUID_CHAR_SERIAL_NUM = "00002a25-0000-1000-8000-00805f9b34fb"

# ==============================================================================
# ============================ SMART MONKEY PATCH ==============================
# ==============================================================================
CAPTURED_CHARS = {}
OriginalCharacteristic = bluezero.localGATT.Characteristic

class CapturingCharacteristic(OriginalCharacteristic):
    def __init__(self, service_id, char_id, uuid, *args, **kwargs):
        super().__init__(service_id, char_id, uuid, *args, **kwargs)
        CAPTURED_CHARS[str(uuid).lower()] = self

        # AUTO-ADD DESCRIPTOR FOR HUMIDITY
        if str(uuid).lower() == UUID_CHAR_HUMIDITY_1.lower():
            logging.info("Detected Humidity Characteristic -> Adding Scale Descriptor...")
            try:
                # Correct Signature for your version:
                bluezero.localGATT.Descriptor(
                    service_id,                 
                    char_id,                    
                    1,                          
                    UUID_DESC_HUMIDITY_SCALE,   
                    [0x01],                     
                    ['read']                    
                )
                logging.info(" - Descriptor Added Successfully")
            except Exception as e:
                logging.error(f" - Failed to add descriptor: {e}")

# Apply Patch
bluezero.localGATT.Characteristic = CapturingCharacteristic

# ==============================================================================
# ============================ HARDWARE LOGIC ==================================
# ==============================================================================

class HardwareInterface:
    def __init__(self):
        self.probes = []
        self.led = None
        self.init_gpio()
        self.init_sensors()

    def init_gpio(self):
        if STATUS_LED_PIN:
            try:
                self.led = digitalio.DigitalInOut(STATUS_LED_PIN)
                self.led.direction = digitalio.Direction.OUTPUT
                self.set_led(False)
            except Exception as e:
                logging.error(f"GPIO Init Error: {e}")

    def set_led(self, on):
        if not self.led: return
        self.led.value = not on if STATUS_LED_ACTIVE_LOW else on

    def register_probe(self, name, handle, read_func, uuid):
        self.probes.append({
            "name": name,
            "handle": handle,
            "read_func": read_func,
            "uuid": str(uuid).lower()
        })

    def init_sensors(self):
        logging.info("Initializing Hardware Sensors...")

        # --- Initialize Shared I2C Bus with LOW FREQUENCY ---
        try:
            # We use busio.I2C to request 10kHz (10000) frequency
            # This helps prevent the SCD-41 from clock-stretching the MCP9600
            i2c = busio.I2C(board.SCL, board.SDA, frequency=10000)
            logging.info(" - I2C Bus initialized at 10kHz (Low Speed Mode)")
        except Exception as e:
            logging.error(f"CRITICAL: Failed to initialize I2C bus: {e}")
            logging.error("If 'Resource Busy', ensure no other scripts are using I2C.")
            return

        # --- 1. MCP9600 (Bean Temp) ---
        try:
            mcp = adafruit_mcp9600.MCP9600(i2c)
            self.register_probe("Bean Temp", mcp, lambda s: s.temperature, UUID_CHAR_TEMP_1)
            logging.info(" - MCP9600 (Bean Temp) OK")
        except Exception as e:
            logging.error(f" - MCP9600 Init Error: {e}")
            logging.info("   -> Ensure MCP9600 wiring is secure and address is 0x67")

        # --- 2. SCD-41 (Exhaust, Humidity, CO2) ---
        try:
            scd = adafruit_scd4x.SCD4X(i2c)
            scd.start_periodic_measurement()
            
            self.register_probe("Exhaust", scd, lambda s: s.temperature, UUID_CHAR_TEMP_2)
            self.register_probe("Humidity", scd, lambda s: s.relative_humidity, UUID_CHAR_HUMIDITY_1)
            self.register_probe("CO2", scd, lambda s: s.CO2, UUID_CHAR_USER_1)
            
            logging.info(" - SCD-41 (Exhaust, Hum, CO2) OK")
        except Exception as e:
            logging.error(f" - SCD-41 Init Error: {e}")

    def encode_rbp_value(self, value):
        if value is None: return [0x00]*4
        try:
            int_val = int(round(value * 100))
            return list(struct.pack('<i', int_val))
        except Exception:
            return [0x00]*4

    def update_sensors(self):
        self.set_led(True)
        
        for probe in self.probes:
            try:
                if hasattr(probe["handle"], "data_ready") and not probe["handle"].data_ready:
                    continue
                
                val = probe["read_func"](probe["handle"])
                if val is not None:
                    encoded = self.encode_rbp_value(val)
                    if probe['uuid'] in CAPTURED_CHARS:
                        CAPTURED_CHARS[probe['uuid']].set_value(encoded)
            except Exception as e:
                logging.error(f"Error reading {probe['name']}: {e}")
        
        self.set_led(False)
        return True 

# ==============================================================================
# ============================ MAIN EXECUTION ==================================
# ==============================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    hw = HardwareInterface()

    try:
        adapter_address = list(adapter.Adapter.available())[0].address
        logging.info(f"Using Bluetooth Adapter: {adapter_address}")
    except IndexError:
        logging.error("No Bluetooth adapter found. Check hardware/permissions.")
        exit(1)

    ble_server = peripheral.Peripheral(adapter_address, local_name=SERVER_NAME)

    # --- RBP SENSING SERVICE (ID 1) ---
    ble_server.add_service(1, UUID_SERVICE_RBP_SENSING, True)
    
    ble_server.add_characteristic(1, 1, UUID_CHAR_TEMP_1, [0x00]*4, True, ['read', 'notify'], None, None)
    ble_server.add_characteristic(1, 2, UUID_CHAR_TEMP_2, [0x00]*4, True, ['read', 'notify'], None, None)
    ble_server.add_characteristic(1, 3, UUID_CHAR_HUMIDITY_1, [0x00]*4, True, ['read', 'notify'], None, None)
    ble_server.add_characteristic(1, 4, UUID_CHAR_USER_1, [0x00]*4, True, ['read', 'notify'], None, None)

    # --- DEVICE INFO SERVICE (ID 2) ---
    ble_server.add_service(2, UUID_SERVICE_DEVICE_INFO, True)
    ble_server.add_characteristic(2, 1, UUID_CHAR_MANUF_NAME, list("RBP_Pi".encode('utf-8')), False, ['read'], None, None)
    ble_server.add_characteristic(2, 2, UUID_CHAR_SERIAL_NUM, list(SERIAL_NUMBER.encode('utf-8')), False, ['read'], None, None)

    async_tools.add_timer_ms(UPDATE_RATE_MS, hw.update_sensors)

    logging.info("Starting RoastProbe (Shared Bus @ 10kHz)...")
    try:
        ble_server.publish() 
    except KeyboardInterrupt:
        logging.info("Stopping...")

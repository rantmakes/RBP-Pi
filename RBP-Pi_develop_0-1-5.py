import logging
import struct
import board
import digitalio
import adafruit_mcp9600
import adafruit_scd4x
import adafruit_bitbangio as bitbangio

# Bluezero Imports
from bluezero import peripheral
from bluezero import adapter
from bluezero import async_tools

# ==============================================================================
# ============================== CONFIGURATION =================================
# ==============================================================================

SERVER_NAME = "RoastProbe"
SERIAL_NUMBER = "12345"
UPDATE_RATE_MS = 500  # 500ms

# --- Status LED ---
STATUS_LED_PIN = board.D2
STATUS_LED_ACTIVE_LOW = True 

# --- SENSORS ---
SCD_SDA_PIN = board.D23
SCD_SCL_PIN = board.D24

# ==============================================================================
# ============================ RBP PROTOCOL UUIDS ==============================
# ==============================================================================
def rbp_uuid(mask):
    return f"4ac9{mask}-0b71-11e8-b8f5-b827ebe1d493"

# Services
UUID_SERVICE_RBP_SENSING = rbp_uuid("0000")
UUID_SERVICE_DEVICE_INFO = "0000180a-0000-1000-8000-00805f9b34fb"

# Characteristics
UUID_CHAR_TEMP_1     = rbp_uuid("0001") # Bean Temp
UUID_CHAR_TEMP_2     = rbp_uuid("0002") # Exhaust Temp
UUID_CHAR_HUMIDITY_1 = rbp_uuid("000b") # Humidity
UUID_CHAR_USER_1     = rbp_uuid("0015") # User Defined 1 (CO2)

UUID_CHAR_MANUF_NAME = "00002a29-0000-1000-8000-00805f9b34fb"
UUID_CHAR_SERIAL_NUM = "00002a25-0000-1000-8000-00805f9b34fb"

# ==============================================================================
# ============================ HARDWARE LOGIC ==================================
# ==============================================================================

class HardwareInterface:
    def __init__(self):
        self.probes = []
        self.led = None
        self.init_gpio()
        self.init_sensors()
        self.value_cache = {} 

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

    def init_sensors(self):
        # 1. MCP9600 (Bean Temp)
        try:
            i2c = board.I2C()
            mcp = adafruit_mcp9600.MCP9600(i2c)
            self.probes.append({
                "name": "Bean Temp",
                "uuid": UUID_CHAR_TEMP_1,
                "handle": mcp,
                "read_func": lambda s: s.temperature
            })
            self.value_cache[UUID_CHAR_TEMP_1] = [0x00]*4
            logging.info("Initialized MCP9600 (Bean Temp)")
        except Exception as e:
            logging.error(f"MCP9600 Init Error: {e}")

        # 2. SCD-41 (Exhaust, Hum, CO2)
        try:
            i2c_soft = bitbangio.I2C(SCD_SCL_PIN, SCD_SDA_PIN)
            scd = adafruit_scd4x.SCD4X(i2c_soft)
            scd.start_periodic_measurement()
            
            self.probes.append({"name": "Exhaust", "uuid": UUID_CHAR_TEMP_2, "handle": scd, "read_func": lambda s: s.temperature})
            self.probes.append({"name": "Humidity", "uuid": UUID_CHAR_HUMIDITY_1, "handle": scd, "read_func": lambda s: s.relative_humidity})
            self.probes.append({"name": "CO2", "uuid": UUID_CHAR_USER_1, "handle": scd, "read_func": lambda s: s.CO2})
            
            for p in self.probes[-3:]:
                self.value_cache[p["uuid"]] = [0x00]*4
            logging.info("Initialized SCD-41")
        except Exception as e:
            logging.error(f"SCD-41 Init Error: {e}")

    def encode_rbp_value(self, value):
        if value is None: return [0x00]*4
        int_val = int(round(value * 100))
        return list(struct.pack('<i', int_val))

    def update_sensors(self, ble_server):
        self.set_led(True)
        for probe in self.probes:
            try:
                if hasattr(probe["handle"], "data_ready") and not probe["handle"].data_ready:
                    continue
                
                val = probe["read_func"](probe["handle"])
                if val is not None:
                    encoded = self.encode_rbp_value(val)
                    self.value_cache[probe["uuid"]] = encoded
                    
                    try:
                        # RBP Sensing Service ID is 1
                        srv = ble_server.services[1] 
                        # Characteristic ID is stored in the probe dict now? 
                        # No, we need to find it by UUID or strict ID mapping.
                        # bluezero stores characteristics in a list or dict. 
                        # Using the simplified set_value approach:
                        for char in srv.characteristics:
                            if char.uuid == probe["uuid"]:
                                char.set_value(encoded)
                    except Exception:
                        pass 

            except Exception as e:
                logging.error(f"Error reading {probe['name']}: {e}")
        
        self.set_led(False)
        return True 

# ==============================================================================
# ============================ MAIN EXECUTION ==================================
# ==============================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # 1. Hardware
    hw = HardwareInterface()

    # 2. Adapter
    try:
        adapter_address = list(adapter.Adapter.available())[0].address
        logging.info(f"Using adapter: {adapter_address}")
    except IndexError:
        logging.error("No Bluetooth adapter found.")
        exit(1)

    # 3. Peripheral
    ble_server = peripheral.Peripheral(adapter_address, local_name=SERVER_NAME)

    # --- SERVICE 1: RBP SENSING ---
    # ID: 1
    ble_server.add_service(1, UUID_SERVICE_RBP_SENSING, True)

    # Helper to add characteristics with strict IDs
    # Args: (srv_id, chr_id, uuid, value, notifying, flags, read_cb, write_cb)
    def add_rbp_char(chr_id, uuid):
        ble_server.add_characteristic(
            1,                  # Service ID
            chr_id,             # Characteristic ID
            uuid,               # UUID
            [0x00]*4,           # Value
            True,               # Notifying (Bool)
            ['read', 'notify'], # Flags
            None,               # Read Callback
            None                # Write Callback
        )

    # Add Sensors (IDs 1-4)
    add_rbp_char(1, UUID_CHAR_TEMP_1)
    add_rbp_char(2, UUID_CHAR_TEMP_2)
    add_rbp_char(3, UUID_CHAR_HUMIDITY_1)
    add_rbp_char(4, UUID_CHAR_USER_1)

    # --- SERVICE 2: DEVICE INFO ---
    # ID: 2
    ble_server.add_service(2, UUID_SERVICE_DEVICE_INFO, True)

    # Manufacturer (ID 1 of Service 2)
    ble_server.add_characteristic(
        2, 1, UUID_CHAR_MANUF_NAME,
        list("RBP_Pi".encode('utf-8')),
        False, ['read'], None, None
    )

    # Serial Number (ID 2 of Service 2)
    ble_server.add_characteristic(
        2, 2, UUID_CHAR_SERIAL_NUM,
        list(SERIAL_NUMBER.encode('utf-8')),
        False, ['read'], None, None
    )

    # 4. Run
    async_tools.add_timer(UPDATE_RATE_MS, lambda: hw.update_sensors(ble_server))

    logging.info("Starting RoastProbe (Bluezero)...")
    try:
        ble_server.publish() 
    except KeyboardInterrupt:
        logging.info("Stopping...")

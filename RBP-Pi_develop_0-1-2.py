import logging
import struct
import board
import digitalio
import adafruit_mcp9600
import adafruit_scd4x
import adafruit_bitbangio as bitbangio
import bluezero

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
        
        # Cache for encoded values (Little Endian Int32)
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
            
            # Map sensors
            self.probes.append({"name": "Exhaust", "uuid": UUID_CHAR_TEMP_2, "handle": scd, "read_func": lambda s: s.temperature})
            self.probes.append({"name": "Humidity", "uuid": UUID_CHAR_HUMIDITY_1, "handle": scd, "read_func": lambda s: s.relative_humidity})
            self.probes.append({"name": "CO2", "uuid": UUID_CHAR_USER_1, "handle": scd, "read_func": lambda s: s.CO2})
            
            # Init cache
            for p in self.probes[-3:]:
                self.value_cache[p["uuid"]] = [0x00]*4
                
            logging.info("Initialized SCD-41 (Exhaust, Hum, CO2)")
        except Exception as e:
            logging.error(f"SCD-41 Init Error: {e}")

    def encode_rbp_value(self, value):
        # RBP Encoding: Float x 10^2, Int32, Little Endian
        if value is None: return [0x00]*4
        int_val = int(round(value * 100))
        return list(struct.pack('<i', int_val))

    def update_sensors(self, ble_server):
        """
        Reads sensors and updates the BLE server characteristics.
        """
        self.set_led(True)
        for probe in self.probes:
            try:
                # Hardware check
                if hasattr(probe["handle"], "data_ready") and not probe["handle"].data_ready:
                    continue
                
                val = probe["read_func"](probe["handle"])
                if val is not None:
                    encoded = self.encode_rbp_value(val)
                    self.value_cache[probe["uuid"]] = encoded
                    
                    # Update Bluezero Characteristic
                    try:
                        srv = ble_server.services[UUID_SERVICE_RBP_SENSING]
                        char = srv.characteristics[probe["uuid"]]
                        char.set_value(encoded)
                    except KeyError:
                        pass # Service/Char not ready yet

            except Exception as e:
                logging.error(f"Error reading {probe['name']}: {e}")
        
        self.set_led(False)
        return True # Keep timer running

# ==============================================================================
# ============================ MAIN EXECUTION ==================================
# ==============================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.info(f"Bluezero Version: {bluezero.__version__}")

    # 1. Initialize Hardware
    hw = HardwareInterface()

    # 2. Get Bluetooth Adapter
    try:
        adapter_address = list(adapter.Adapter.available())[0].address
        logging.info(f"Using adapter: {adapter_address}")
    except IndexError:
        logging.error("No Bluetooth adapter found. Check hardware/permissions.")
        exit(1)

    # 3. Create Peripheral
    ble_server = peripheral.Peripheral(adapter_address, local_name=SERVER_NAME)

    # --- Add RBP Sensing Service ---
    # FIX: Explicitly naming 'primary=True' to avoid positional argument errors
    ble_server.add_service(UUID_SERVICE_RBP_SENSING, primary=True)

    # Helper for adding characteristics
    def add_sensor_char(uuid):
        ble_server.add_characteristic(
            UUID_SERVICE_RBP_SENSING,
            uuid,
            [0x00, 0x00, 0x00, 0x00], # Initial Value
            ['read', 'notify'],       # Flags
            ['read'],                 # Permissions
            None,                     # Read Callback
            None                      # Write Callback
        )

    # Add all sensor characteristics
    for uid in [UUID_CHAR_TEMP_1, UUID_CHAR_TEMP_2, UUID_CHAR_HUMIDITY_1, UUID_CHAR_USER_1]:
        add_sensor_char(uid)

    # --- Add Device Info Service ---
    ble_server.add_service(UUID_SERVICE_DEVICE_INFO, primary=True)
    
    # Manufacturer Name
    ble_server.add_characteristic(
        UUID_SERVICE_DEVICE_INFO,
        UUID_CHAR_MANUF_NAME,
        list("RBP_Pi_Bluezero".encode('utf-8')),
        ['read'], ['read'], None, None
    )
    
    # Serial Number (Required for RBP Uniquing)
    ble_server.add_characteristic(
        UUID_SERVICE_DEVICE_INFO,
        UUID_CHAR_SERIAL_NUM,
        list(SERIAL_NUMBER.encode('utf-8')),
        ['read'], ['read'], None, None
    )

    # 4. Start Update Timer
    async_tools.add_timer(UPDATE_RATE_MS, lambda: hw.update_sensors(ble_server))

    # 5. Run Server
    logging.info("Starting RoastProbe (Bluezero)...")
    try:
        ble_server.publish() 
    except KeyboardInterrupt:
        logging.info("Stopping...")

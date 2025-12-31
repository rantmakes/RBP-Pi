import logging
import struct
import inspect
import board
import digitalio
import adafruit_mcp9600
import adafruit_scd4x
import adafruit_bitbangio as bitbangio

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

# --- SENSOR PINS ---
SCD_SDA_PIN = board.D23
SCD_SCL_PIN = board.D24

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

# Descriptors
UUID_DESC_HUMIDITY_SCALE = rbp_uuid("001c") 

UUID_CHAR_MANUF_NAME = "00002a29-0000-1000-8000-00805f9b34fb"
UUID_CHAR_SERIAL_NUM = "00002a25-0000-1000-8000-00805f9b34fb"

# ==============================================================================
# ============================ SMART MONKEY PATCH ==============================
# ==============================================================================
# This logic captures the Bluetooth objects and safely adds the required 
# descriptor by inspecting the installed library version.

CAPTURED_CHARS = {}
OriginalCharacteristic = bluezero.localGATT.Characteristic

class CapturingCharacteristic(OriginalCharacteristic):
    def __init__(self, service_id, char_id, uuid, *args, **kwargs):
        super().__init__(service_id, char_id, uuid, *args, **kwargs)
        
        # 1. Capture the object reference
        CAPTURED_CHARS[str(uuid).lower()] = self

        # 2. AUTO-ADD DESCRIPTOR FOR HUMIDITY
        if str(uuid).lower() == UUID_CHAR_HUMIDITY_1.lower():
            logging.info("Detected Humidity Characteristic -> Attempting to add Scale Descriptor...")
            self._add_humidity_descriptor()

    def _add_humidity_descriptor(self):
        """
        Inspects the Descriptor constructor to ensure arguments are passed correctly.
        """
        try:
            desc_cls = bluezero.localGATT.Descriptor
            sig = inspect.signature(desc_cls.__init__)
            params = list(sig.parameters.keys())
            
            # Prepare our data
            args_map = {
                'characteristic': self,
                'descriptor_id': 1,
                'uuid': UUID_DESC_HUMIDITY_SCALE,
                'value': [0x01],
                'flags': ['read'],
                'notifying': False 
            }
            
            # Construct ordered arguments based on what the library expects
            call_args = []
            for p_name in params:
                if p_name == 'self': continue
                if p_name in args_map:
                    call_args.append(args_map[p_name])
                else:
                    # Handle unknown params (e.g. 'service') if any
                    call_args.append(None)
            
            logging.info(f" - Inspecting Descriptor Init: Expecting {params}")
            
            # Create the Descriptor
            desc_cls(self, *call_args[1:]) # Skip 'self' in the call
            logging.info(" - Descriptor Added Successfully")
            
        except Exception as e:
            logging.error(f" - Failed to add descriptor (Auto-Inspect Mode): {e}")
            # Fallback: Try the most common signature if inspection fails
            try:
                bluezero.localGATT.Descriptor(self, 1, UUID_DESC_HUMIDITY_SCALE, [0x01], ['read'])
                logging.info(" - Descriptor Added via Fallback")
            except Exception as e2:
                logging.error(f" - Fallback also failed: {e2}")

# Apply the patch
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

        # --- 1. MCP9600 (Bean Temp) ---
        try:
            i2c = board.I2C()
            mcp = adafruit_mcp9600.MCP9600(i2c)
            self.register_probe("Bean Temp", mcp, lambda s: s.temperature, UUID_CHAR_TEMP_1)
            logging.info(" - MCP9600 (Bean Temp) OK")
        except Exception as e:
            logging.error(f" - MCP9600 Init Error: {e}")

        # --- 2. SCD-41 (Exhaust, Humidity, CO2) ---
        try:
            i2c_soft = bitbangio.I2C(SCD_SCL_PIN, SCD_SDA_PIN)
            scd = adafruit_scd4x.SCD4X(i2c_soft)
            scd.start_periodic_measurement()
            
            # Exhaust
            self.register_probe("Exhaust", scd, lambda s: s.temperature, UUID_CHAR_TEMP_2)
            # Humidity
            self.register_probe("Humidity", scd, lambda s: s.relative_humidity, UUID_CHAR_HUMIDITY_1)
            # CO2
            self.register_probe("CO2", scd, lambda s: s.CO2, UUID_CHAR_USER_1)
            
            logging.info(" - SCD-41 (Exhaust, Hum, CO2) OK")
        except Exception as e:
            logging.error(f" - SCD-41 Init Error: {e}")

    def encode_rbp_value(self, value):
        # RBP Encoding: Float x 10^2, Int32, Little Endian [cite: 154, 159, 164]
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
                # Check hardware readiness
                if hasattr(probe["handle"], "data_ready") and not probe["handle"].data_ready:
                    continue
                
                # Read Value
                val = probe["read_func"](probe["handle"])
                
                if val is not None:
                    encoded = self.encode_rbp_value(val)
                    target_uuid = probe['uuid']
                    
                    # Update Bluetooth Object if captured
                    if target_uuid in CAPTURED_CHARS:
                        char_obj = CAPTURED_CHARS[target_uuid]
                        try:
                            char_obj.set_value(encoded)
                        except Exception:
                            pass # Suppress transient update errors

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

    # Create Server
    ble_server = peripheral.Peripheral(adapter_address, local_name=SERVER_NAME)

    # --- RBP SENSING SERVICE [cite: 55] ---
    ble_server.add_service(1, UUID_SERVICE_RBP_SENSING, True)
    
    # Add Characteristics
    ble_server.add_characteristic(1, 1, UUID_CHAR_TEMP_1, [0x00]*4, True, ['read', 'notify'], None, None)
    ble_server.add_characteristic(1, 2, UUID_CHAR_TEMP_2, [0x00]*4, True, ['read', 'notify'], None, None)
    
    # Humidity (Auto-adds descriptor via patch)
    ble_server.add_characteristic(1, 3, UUID_CHAR_HUMIDITY_1, [0x00]*4, True, ['read', 'notify'], None, None)
    
    # CO2
    ble_server.add_characteristic(1, 4, UUID_CHAR_USER_1, [0x00]*4, True, ['read', 'notify'], None, None)

    # --- DEVICE INFO SERVICE [cite: 62] ---
    ble_server.add_service(2, UUID_SERVICE_DEVICE_INFO, True)
    # Manufacturer
    ble_server.add_characteristic(2, 1, UUID_CHAR_MANUF_NAME, list("RBP_Pi".encode('utf-8')), False, ['read'], None, None)
    # Serial Number (Critical for RBP Uniquing [cite: 200, 209])
    ble_server.add_characteristic(2, 2, UUID_CHAR_SERIAL_NUM, list(SERIAL_NUMBER.encode('utf-8')), False, ['read'], None, None)

    # Start Loop
    async_tools.add_timer_ms(UPDATE_RATE_MS, hw.update_sensors)

    logging.info("Starting RoastProbe (Production Mode)...")
    try:
        ble_server.publish() 
    except KeyboardInterrupt:
        logging.info("Stopping...")

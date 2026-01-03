import logging
import struct
import time
import board
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
UPDATE_RATE_MS = 2000 

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
UUID_CHAR_USER_1     = rbp_uuid("0015") # CO2 (Now g/m3)

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
                bluezero.localGATT.Descriptor(
                    service_id, char_id, 1, UUID_DESC_HUMIDITY_SCALE,   
                    [0x01], ['read']
                )
                logging.info(" - Descriptor Added Successfully")
            except Exception as e:
                logging.error(f" - Failed to add descriptor: {e}")

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

    # --- MATH HELPER: Convert ppm to g/m3 ---
    def co2_ppm_to_g_m3(self, sensor_obj):
        try:
            ppm = sensor_obj.CO2
            temp_c = sensor_obj.temperature
            
            # Constants
            MW_CO2 = 44.01    # g/mol
            P_STD = 101325    # Pa (Standard Pressure)
            R_GAS = 8.314     # J/(mol*K)
            
            # Guard against sensor startup None values
            if ppm is None or temp_c is None:
                return 0.0
            
            # Kelvin Temp
            temp_k = temp_c + 273.15
            
            # Calculation: (ppm * MW * P) / (R * T) * 10^-6
            density = (ppm * MW_CO2 * P_STD) / (R_GAS * temp_k)
            g_m3 = density / 1_000_000
            
            return g_m3
        except Exception:
            return 0.0

    def init_sensors(self):
        logging.info("Initializing Hardware Sensors...")
        
        try:
            i2c = board.I2C()
        except Exception as e:
            logging.error(f"CRITICAL: I2C Bus Init Failed: {e}")
            return

        # --- 1. MCP9600 ---
        try:
            mcp = adafruit_mcp9600.MCP9600(i2c)
            self.register_probe("Bean Temp", mcp, lambda s: s.temperature, UUID_CHAR_TEMP_1)
            logging.info(" - MCP9600 (Bean Temp) OK")
        except Exception as e:
            logging.error(f" - MCP9600 Init Error: {e}")

        # --- 2. SCD-41 ---
        try:
            scd = adafruit_scd4x.SCD4X(i2c)
            try:
                scd.stop_periodic_measurement()
                time.sleep(0.5) 
            except:
                pass 
            
            scd.start_periodic_measurement()
            
            # Register Probes
            self.register_probe("Exhaust", scd, lambda s: s.temperature, UUID_CHAR_TEMP_2)
            self.register_probe("Humidity", scd, lambda s: s.relative_humidity, UUID_CHAR_HUMIDITY_1)
            
            # Register CO2 with CONVERSION function
            # We pass 'self.co2_ppm_to_g_m3' which takes the 'scd' object as input
            self.register_probe("CO2 (g/m3)", scd, self.co2_ppm_to_g_m3, UUID_CHAR_USER_1)
            
            logging.info(" - SCD-41 (Exhaust, Hum, CO2) OK")
        except Exception as e:
            logging.error(f" - SCD-41 Init Error: {e}")

    def encode_rbp_value(self, value):
        # RBP Encoding: Value * 100, then Int32 Little Endian.
        # Example: 0.72 g/m3 -> 72 integer
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
                val = probe["read_func"](probe["handle"])
                if val is not None:
                    encoded = self.encode_rbp_value(val)
                    if probe['uuid'] in CAPTURED_CHARS:
                        CAPTURED_CHARS[probe['uuid']].set_value(encoded)
            except Exception:
                pass 
        
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
        logging.error("No Bluetooth adapter found.")
        exit(1)

    ble_server = peripheral.Peripheral(adapter_address, local_name=SERVER_NAME)

    # --- RBP SENSING ---
    ble_server.add_service(1, UUID_SERVICE_RBP_SENSING, True)
    ble_server.add_characteristic(1, 1, UUID_CHAR_TEMP_1, [0x00]*4, True, ['read', 'notify'], None, None)
    ble_server.add_characteristic(1, 2, UUID_CHAR_TEMP_2, [0x00]*4, True, ['read', 'notify'], None, None)
    ble_server.add_characteristic(1, 3, UUID_CHAR_HUMIDITY_1, [0x00]*4, True, ['read', 'notify'], None, None)
    ble_server.add_characteristic(1, 4, UUID_CHAR_USER_1, [0x00]*4, True, ['read', 'notify'], None, None)

    # --- DEVICE INFO ---
    ble_server.add_service(2, UUID_SERVICE_DEVICE_INFO, True)
    ble_server.add_characteristic(2, 1, UUID_CHAR_MANUF_NAME, list("RBP_Pi".encode('utf-8')), False, ['read'], None, None)
    ble_server.add_characteristic(2, 2, UUID_CHAR_SERIAL_NUM, list(SERIAL_NUMBER.encode('utf-8')), False, ['read'], None, None)

    async_tools.add_timer_ms(UPDATE_RATE_MS, hw.update_sensors)

    logging.info("Starting RoastProbe (g/m3 Mode)...")
    try:
        ble_server.publish() 
    except KeyboardInterrupt:
        logging.info("Stopping...")

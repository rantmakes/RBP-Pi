import logging
import struct
import time
import sys
import board
import digitalio
import adafruit_mcp9600
import adafruit_scd4x
import adafruit_ina260  # NEW LIBRARY

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

# I2C Addresses for INA260 Sensors
ADDR_FAN_SENSOR    = 0x40 # Default Address
ADDR_HEATER_SENSOR = 0x41 # Jumper A0 Soldered

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
UUID_CHAR_USER_1     = rbp_uuid("0015") # CO2 (g/m3)
UUID_CHAR_USER_2     = rbp_uuid("0016") # Heater Power (W)
UUID_CHAR_USER_3     = rbp_uuid("0017") # Fan Power (W)

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
        self.scd_sensor = None 
        
        self.init_gpio()
        self.init_sensors()

    def init_gpio(self):
        if STATUS_LED_PIN:
            try:
                try:
                    tmp = digitalio.DigitalInOut(STATUS_LED_PIN)
                    tmp.deinit()
                except:
                    pass
                self.led = digitalio.DigitalInOut(STATUS_LED_PIN)
                self.led.direction = digitalio.Direction.OUTPUT
                self.set_led(False)
            except Exception as e:
                logging.error(f"GPIO Init Error: {e}")

    def set_led(self, on):
        if not self.led: return
        self.led.value = not on if STATUS_LED_ACTIVE_LOW else on

    def cleanup(self):
        logging.info("--- SHUTTING DOWN HARDWARE ---")
        if self.led:
            self.set_led(False)
            self.led.deinit()
            logging.info(" - GPIO Released")
        
        if self.scd_sensor:
            try:
                self.scd_sensor.stop_periodic_measurement()
                logging.info(" - SCD-41 Measurement Stopped")
            except:
                pass

    def register_probe(self, name, handle, read_func, uuid):
        self.probes.append({
            "name": name,
            "handle": handle,
            "read_func": read_func,
            "uuid": str(uuid).lower()
        })

    def co2_ppm_to_g_m3(self, sensor_obj):
        try:
            ppm = sensor_obj.CO2
            temp_c = sensor_obj.temperature
            if ppm is None or temp_c is None: return 0.0
            density = (ppm * 44.01 * 101325) / (8.314 * (temp_c + 273.15))
            return density / 1_000_000
        except Exception:
            return 0.0

    def init_sensors(self):
        logging.info("Initializing Hardware Sensors...")
        
        try:
            i2c = board.I2C()
        except Exception as e:
            logging.error(f"CRITICAL: I2C Bus Init Failed: {e}")
            return

        # --- 1. MCP9600 (Bean Temp) ---
        try:
            mcp = adafruit_mcp9600.MCP9600(i2c)
            self.register_probe("Bean Temp", mcp, lambda s: s.temperature, UUID_CHAR_TEMP_1)
            logging.info(" - MCP9600 (Bean Temp) OK")
        except Exception as e:
            logging.error(f" - MCP9600 Init Error: {e}")

        # --- 2. SCD-41 (Environment) ---
        try:
            scd = adafruit_scd4x.SCD4X(i2c)
            self.scd_sensor = scd 
            try:
                scd.stop_periodic_measurement()
                time.sleep(1.0) 
            except:
                pass 
            scd.start_periodic_measurement()
            
            self.register_probe("Exhaust", scd, lambda s: s.temperature, UUID_CHAR_TEMP_2)
            self.register_probe("Humidity", scd, lambda s: s.relative_humidity, UUID_CHAR_HUMIDITY_1)
            self.register_probe("CO2 (g/m3)", scd, self.co2_ppm_to_g_m3, UUID_CHAR_USER_1)
            logging.info(" - SCD-41 Started")
        except Exception as e:
            logging.error(f" - SCD-41 Init Error: {e}")

        # --- 3. INA260 (Fan Power) - Default 0x40 ---
        try:
            ina_fan = adafruit_ina260.INA260(i2c, address=ADDR_FAN_SENSOR)
            # Map Power (Watts) to User 3. INA reports mW, so divide by 1000.
            self.register_probe("Fan Power", ina_fan, lambda s: s.power / 1000.0, UUID_CHAR_USER_3)
            logging.info(f" - INA260 (Fan) Found at {hex(ADDR_FAN_SENSOR)}")
        except ValueError:
            logging.warning(f" - INA260 (Fan) NOT FOUND at {hex(ADDR_FAN_SENSOR)}")
        except Exception as e:
            logging.error(f" - INA260 (Fan) Error: {e}")

        # --- 4. INA260 (Heater Power) - Jumper 0x41 ---
        try:
            ina_heat = adafruit_ina260.INA260(i2c, address=ADDR_HEATER_SENSOR)
            # Map Power (Watts) to User 2
            self.register_probe("Heater Power", ina_heat, lambda s: s.power / 1000.0, UUID_CHAR_USER_2)
            logging.info(f" - INA260 (Heater) Found at {hex(ADDR_HEATER_SENSOR)}")
        except ValueError:
            logging.warning(f" - INA260 (Heater) NOT FOUND at {hex(ADDR_HEATER_SENSOR)}")
            logging.warning("   (Did you remember to solder the A0 jumper?)")
        except Exception as e:
            logging.error(f" - INA260 (Heater) Error: {e}")

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
        sys.exit(1)

    ble_server = peripheral.Peripheral(adapter_address, local_name=SERVER_NAME)

    # --- RBP SENSING ---
    ble_server.add_service(1, UUID_SERVICE_RBP_SENSING, True)
    ble_server.add_characteristic(1, 1, UUID_CHAR_TEMP_1, [0x00]*4, True, ['read', 'notify'], None, None)
    ble_server.add_characteristic(1, 2, UUID_CHAR_TEMP_2, [0x00]*4, True, ['read', 'notify'], None, None)
    ble_server.add_characteristic(1, 3, UUID_CHAR_HUMIDITY_1, [0x00]*4, True, ['read', 'notify'], None, None)
    ble_server.add_characteristic(1, 4, UUID_CHAR_USER_1, [0x00]*4, True, ['read', 'notify'], None, None)
    
    # NEW: Heater and Fan Power
    ble_server.add_characteristic(1, 5, UUID_CHAR_USER_2, [0x00]*4, True, ['read', 'notify'], None, None)
    ble_server.add_characteristic(1, 6, UUID_CHAR_USER_3, [0x00]*4, True, ['read', 'notify'], None, None)

    # --- DEVICE INFO ---
    ble_server.add_service(2, UUID_SERVICE_DEVICE_INFO, True)
    ble_server.add_characteristic(2, 1, UUID_CHAR_MANUF_NAME, list("RBP_Pi".encode('utf-8')), False, ['read'], None, None)
    ble_server.add_characteristic(2, 2, UUID_CHAR_SERIAL_NUM, list(SERIAL_NUMBER.encode('utf-8')), False, ['read'], None, None)

    async_tools.add_timer_ms(UPDATE_RATE_MS, hw.update_sensors)

    logging.info("Starting RoastProbe (Dev v0.4.0 with Power Sensors)...")
    logging.info("Press Ctrl+C to stop safely.")
    
    try:
        ble_server.publish() 
    except KeyboardInterrupt:
        logging.info("\nUser stopping script...")
    except Exception as e:
        logging.error(f"Unexpected Error: {e}")
    finally:
        hw.cleanup()
        sys.exit(0)

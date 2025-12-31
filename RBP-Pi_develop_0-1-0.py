import logging
import struct
import board
import digitalio
import adafruit_mcp9600
import adafruit_scd4x
import adafruit_bitbangio as bitbangio

# Bluezero Imports
from bluezero import peripheral
from bluezero import device
from bluezero import adapter
from bluezero import async_tools

# ==============================================================================
# ============================== CONFIGURATION =================================
# ==============================================================================

SERVER_NAME = "RoastProbe"
SERIAL_NUMBER = "12345"
UPDATE_RATE_MS = 500  # 500ms = 0.5 seconds

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
                "read_func": lambda s: s.temperature,
                "char_obj": None # Will be linked later
            })
            logging.info("Initialized MCP9600 (Bean Temp)")
        except Exception as e:
            logging.error(f"MCP9600 Init Error: {e}")

        # 2. SCD-41 (Exhaust, Hum, CO2)
        try:
            i2c_soft = bitbangio.I2C(SCD_SCL_PIN, SCD_SDA_PIN)
            scd = adafruit_scd4x.SCD4X(i2c_soft)
            scd.start_periodic_measurement()
            
            self.probes.append({
                "name": "Exhaust Temp",
                "uuid": UUID_CHAR_TEMP_2,
                "handle": scd,
                "read_func": lambda s: s.temperature,
                "char_obj": None
            })
            self.probes.append({
                "name": "Humidity",
                "uuid": UUID_CHAR_HUMIDITY_1,
                "handle": scd,
                "read_func": lambda s: s.relative_humidity,
                "char_obj": None
            })
            self.probes.append({
                "name": "CO2",
                "uuid": UUID_CHAR_USER_1,
                "handle": scd,
                "read_func": lambda s: s.CO2,
                "char_obj": None
            })
            logging.info("Initialized SCD-41 (Exhaust, Hum, CO2)")
        except Exception as e:
            logging.error(f"SCD-41 Init Error: {e}")

    def encode_rbp_value(self, value):
        # RBP Encoding: Float x 10^2, Int32, Little Endian
        if value is None: return [0x00, 0x00, 0x00, 0x00]
        int_val = int(round(value * 100))
        # bluezero expects a list of bytes/integers, not a bytes object
        packed = struct.pack('<i', int_val)
        return list(packed)

    def update_loop(self):
        self.set_led(True)
        for probe in self.probes:
            if not probe["char_obj"]: continue
            
            try:
                if hasattr(probe["handle"], "data_ready") and not probe["handle"].data_ready:
                    continue
                
                val = probe["read_func"](probe["handle"])
                if val is not None:
                    encoded = self.encode_rbp_value(val)
                    # Set value updates the local GATT table
                    probe["char_obj"].set_value(encoded)
            except Exception as e:
                logging.error(f"Error reading {probe['name']}: {e}")
        
        self.set_led(False)
        return True # Return True to keep the timer running

# ==============================================================================
# ============================ MAIN EXECUTION ==================================
# ==============================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    # 1. Initialize Hardware
    hw = HardwareInterface()

    # 2. Initialize Bluezero Application
    # The 'Application' manages the GATT Services
    app = peripheral.Application()

    # --- RBP Sensing Service ---
    rbp_service = app.add_service(UUID_SERVICE_RBP_SENSING, True)

    # Helper to add characteristic and link it to hardware probe
    def add_rbp_char(uuid, probe_uuid):
        char = rbp_service.add_characteristic(
            uuid,
            ["read", "notify"],
            [0x00, 0x00, 0x00, 0x00] # Initial Value
        )
        # Link this characteristic object back to the hardware probe list
        for p in hw.probes:
            if p["uuid"] == uuid:
                p["char_obj"] = char
                # Callback for when notifications start (Optional logging)
                char.add_notify_callback(lambda n: logging.info(f"Notify started: {p['name']}"))

    # Add all characteristics
    for uid in [UUID_CHAR_TEMP_1, UUID_CHAR_TEMP_2, UUID_CHAR_HUMIDITY_1, UUID_CHAR_USER_1]:
        add_rbp_char(uid, uid)

    # --- Device Information Service ---
    # RBP requires this for uniquing
    dev_info_service = app.add_service(UUID_SERVICE_DEVICE_INFO, True)
    
    # Manufacturer Name
    dev_info_service.add_characteristic(
        UUID_CHAR_MANUF_NAME,
        ["read"],
        list("RBP_Pi_Bluezero".encode('utf-8'))
    )
    
    # Serial Number (Critical for RBP)
    dev_info_service.add_characteristic(
        UUID_CHAR_SERIAL_NUM,
        ["read"],
        list(SERIAL_NUMBER.encode('utf-8'))
    )

    # 3. Setup Advertising
    # The 'Advertisement' broadcasts the server existence
    advert = peripheral.Advertisement(SERVER_NAME, [UUID_SERVICE_RBP_SENSING])
    advert.service_UUIDs = [UUID_SERVICE_RBP_SENSING, UUID_SERVICE_DEVICE_INFO]
    
    # 4. Start the Sensor Loop
    # Bluezero uses the GLib MainLoop. We attach our hardware update to it.
    async_tools.add_timer(UPDATE_RATE_MS, hw.update_loop)

    # 5. Run
    logging.info(f"Starting RoastProbe (Bluezero)...")
    try:
        advert.start()
        app.start() # This blocks and runs the main loop
    except KeyboardInterrupt:
        logging.info("Stopping...")
        advert.stop()
        app.stop()

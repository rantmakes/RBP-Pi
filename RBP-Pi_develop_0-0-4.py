import asyncio
import struct
import logging
import board
import digitalio
import adafruit_mcp9600
import adafruit_scd4x
import adafruit_bitbangio as bitbangio

from bless import (
    BlessServer,
    BlessGATTCharacteristic,
    GATTCharacteristicProperties,
    GATTAttributePermissions
)

# ==============================================================================
# ============================== CONFIGURATION =================================
# ==============================================================================

SERVER_NAME = "RoastProbe"
SERIAL_NUMBER = "12345"
UPDATE_RATE = 0.5

# --- Status LED ---
STATUS_LED_PIN = board.D2
STATUS_LED_ACTIVE_LOW = True

# --- SENSORS ---
SCD_SDA_PIN = board.D23
SCD_SCL_PIN = board.D24

# ==============================================================================
# ============================ RBP PROTOCOL UUIDS ==============================
# ==============================================================================
# Base UUID as defined in RBP Data Sheet
def rbp_uuid(mask):
    return f"4ac9{mask}-0b71-11e8-b8f5-b827ebe1d493"

# Services
UUID_SERVICE_RBP_SENSING = rbp_uuid("0000")
UUID_SERVICE_DEVICE_INFO = "0000180a-0000-1000-8000-00805f9b34fb"

# Characteristics (Mapped from RBP Data Sheet)
UUID_CHAR_TEMP_1     = rbp_uuid("0001") # Bean Temp
UUID_CHAR_TEMP_2     = rbp_uuid("0002") # Exhaust Temp
UUID_CHAR_HUMIDITY_1 = rbp_uuid("000b") # Humidity
UUID_CHAR_USER_1     = rbp_uuid("0015") # User Defined 1 (CO2)

# Device Info Characteristics (Standard Bluetooth SIG)
UUID_CHAR_MANUF_NAME = "00002a29-0000-1000-8000-00805f9b34fb"
UUID_CHAR_SERIAL_NUM = "00002a25-0000-1000-8000-00805f9b34fb"

# ==============================================================================
# ============================ SERVER LOGIC ====================================
# ==============================================================================

class RoastmasterBLEServer:
    def __init__(self):
        self.server = None 
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
                "read_func": lambda s: s.temperature
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
                "read_func": lambda s: s.temperature
            })
            self.probes.append({
                "name": "Humidity",
                "uuid": UUID_CHAR_HUMIDITY_1,
                "handle": scd,
                "read_func": lambda s: s.relative_humidity
            })
            self.probes.append({
                "name": "CO2",
                "uuid": UUID_CHAR_USER_1,
                "handle": scd,
                "read_func": lambda s: s.CO2
            })
            logging.info("Initialized SCD-41 (Exhaust, Hum, CO2)")
        except Exception as e:
            logging.error(f"SCD-41 Init Error: {e}")

    def encode_rbp_value(self, value):
        # RBP Encoding: Float x 10^2, Int32, Little Endian
        if value is None: return bytearray([0x00]*4)
        int_val = int(round(value * 100))
        return struct.pack('<i', int_val)

    async def update_sensors(self):
        if not self.server: return
        self.set_led(True)
        
        for probe in self.probes:
            try:
                # Hardware check
                if hasattr(probe["handle"], "data_ready") and not probe["handle"].data_ready:
                    continue
                
                val = probe["read_func"](probe["handle"])
                if val is not None:
                    encoded = self.encode_rbp_value(val)
                    # Update GATT value and Notify
                    self.server.get_characteristic(probe["uuid"]).value = encoded
                    self.server.update_value(UUID_SERVICE_RBP_SENSING, probe["uuid"])
            except Exception as e:
                logging.error(f"Error reading {probe['name']}: {e}")
        
        self.set_led(False)

    async def run(self):
        logging.info("Initializing Bluetooth Server (Bless v0.3.0)...")
        self.server = BlessServer(name=SERVER_NAME)
        
        # --- Configure GATT ---
        logging.info("Adding Services...")
        
        # 1. RBP Sensing Service
        await self.server.add_new_service(UUID_SERVICE_RBP_SENSING)
        
        # Properties: Read + Notify
        props = GATTCharacteristicProperties.read | GATTCharacteristicProperties.notify
        # Permissions: Readable + Writeable (Writeable needed for server updates in some backends)
        perms = GATTAttributePermissions.readable | GATTAttributePermissions.writeable
        zero_val = bytearray([0x00, 0x00, 0x00, 0x00])

        for uuid in [UUID_CHAR_TEMP_1, UUID_CHAR_TEMP_2, UUID_CHAR_HUMIDITY_1, UUID_CHAR_USER_1]:
            await self.server.add_new_characteristic(UUID_SERVICE_RBP_SENSING, uuid, props, zero_val, perms)

        # 2. Device Info Service
        await self.server.add_new_service(UUID_SERVICE_DEVICE_INFO)
        await self.server.add_new_characteristic(UUID_SERVICE_DEVICE_INFO, UUID_CHAR_MANUF_NAME, 
                                                 GATTCharacteristicProperties.read, "RBP_Pi".encode('utf-8'), GATTAttributePermissions.readable)
        await self.server.add_new_characteristic(UUID_SERVICE_DEVICE_INFO, UUID_CHAR_SERIAL_NUM, 
                                                 GATTCharacteristicProperties.read, SERIAL_NUMBER.encode('utf-8'), GATTAttributePermissions.readable)

        # --- Start Advertising ---
        logging.info(f"Starting Advertising as '{SERVER_NAME}'...")
        try:
            # Bless 0.3.0: start_advertising is async and typically takes no args
            await self.server.start_advertising()
            logging.info("Advertising ACTIVE. Waiting for connections...")
        except Exception as e:
            logging.error(f"Advertising Failed: {e}")
            return

        # --- Main Loop ---
        while True:
            await self.update_sensors()
            await asyncio.sleep(UPDATE_RATE)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    rbp_host = RoastmasterBLEServer()
    
    try:
        asyncio.run(rbp_host.run())
    except KeyboardInterrupt:
        logging.info("Stopping...")

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

# [cite_start]RBP Requirement: Advertise a GAP name [cite: 257]
SERVER_NAME = "RoastProbe" 
# [cite_start]RBP Requirement: Unique Serial via Device Info Service [cite: 209]
SERIAL_NUMBER = "12345" 

# --- Timers ---
# RBP allows configuring this via 'Notify Frequency' descriptor, 
# but we will default to 0.5s for responsiveness.
UPDATE_RATE = 0.5 

# --- Status LED ---
STATUS_LED_PIN = board.D2
STATUS_LED_ACTIVE_LOW = True 

# --- SENSOR HARDWARE (Same as original RDP script) ---
SCD_SDA_PIN = board.D23
SCD_SCL_PIN = board.D24

# ==============================================================================
# ============================ RBP PROTOCOL UUIDS ==============================
# ==============================================================================
# [cite_start]RBP Base UUID: 4ac9????-0b71-11e8-b8f5-b827ebe1d493 [cite: 114]
def rbp_uuid(mask):
    return f"4ac9{mask}-0b71-11e8-b8f5-b827ebe1d493"

# Services
UUID_SERVICE_RBP_SENSING = rbp_uuid("0000")
UUID_SERVICE_DEVICE_INFO = "0000180a-0000-1000-8000-00805f9b34fb" # Bluetooth SIG Standard

# RBP Sensing Characteristics (from RBP Data Sheet page 6)
# Mapping your sensors to RBP slots:
UUID_CHAR_TEMP_1     = rbp_uuid("0001") # Bean Temp (MCP9600)
UUID_CHAR_TEMP_2     = rbp_uuid("0002") # Exhaust Temp (SCD41 Temp)
UUID_CHAR_HUMIDITY_1 = rbp_uuid("000b") # Ambient Humidity (SCD41 Humidity)
UUID_CHAR_USER_1     = rbp_uuid("0015") # CO2 (Mapped to User Defined 1)

# Device Info Characteristics (Bluetooth SIG Standard)
UUID_CHAR_MANUF_NAME = "00002a29-0000-1000-8000-00805f9b34fb"
UUID_CHAR_SERIAL_NUM = "00002a25-0000-1000-8000-00805f9b34fb"

# ==============================================================================
# ============================ CLASSES & LOGIC =================================
# ==============================================================================

class RoastmasterBLEServer:
    def __init__(self, loop):
        self.loop = loop
        self.server = BlessServer(name=SERVER_NAME, loop=loop)
        
        # Hardware Initialization
        self.probes = []
        self.led = None
        self.init_gpio()
        self.init_sensors()

    def init_gpio(self):
        if STATUS_LED_PIN:
            self.led = digitalio.DigitalInOut(STATUS_LED_PIN)
            self.led.direction = digitalio.Direction.OUTPUT
            self.set_led(False)

    def set_led(self, on):
        if not self.led: return
        self.led.value = not on if STATUS_LED_ACTIVE_LOW else on

    def init_sensors(self):
        """
        Initializes sensors exactly as the RDP script did, 
        but maps them to RBP UUIDs instead of Meta Types.
        """
        # 1. MCP9600 (Bean Temp) -> RBP Temperature 1
        try:
            i2c = board.I2C()
            mcp = adafruit_mcp9600.MCP9600(i2c)
            self.probes.append({
                "name": "Bean Temp",
                "uuid": UUID_CHAR_TEMP_1,
                "handle": mcp,
                "read_func": lambda s: s.temperature,
                "last_val": None
            })
            logging.info("Initialized MCP9600 (Bean Temp)")
        except Exception as e:
            logging.error(f"MCP9600 Init Error: {e}")

        # 2. SCD-41 (Exhaust, Humidity, CO2)
        try:
            i2c_soft = bitbangio.I2C(SCD_SCL_PIN, SCD_SDA_PIN)
            scd = adafruit_scd4x.SCD4X(i2c_soft)
            scd.start_periodic_measurement()
            
            # Exhaust Temp -> RBP Temperature 2
            self.probes.append({
                "name": "Exhaust Temp",
                "uuid": UUID_CHAR_TEMP_2,
                "handle": scd,
                "read_func": lambda s: s.temperature,
                "last_val": None
            })
            
            # Ambient Humidity -> RBP Humidity 1
            self.probes.append({
                "name": "Humidity",
                "uuid": UUID_CHAR_HUMIDITY_1,
                "handle": scd,
                "read_func": lambda s: s.relative_humidity,
                "last_val": None
            })

            # CO2 -> RBP User Defined 1
            # RBP doesn't have a CO2 specific field, so we use User Defined.
            self.probes.append({
                "name": "CO2",
                "uuid": UUID_CHAR_USER_1,
                "handle": scd,
                "read_func": lambda s: s.CO2,
                "last_val": None
            })
            logging.info("Initialized SCD-41 (Exhaust, Hum, CO2)")
        except Exception as e:
            logging.error(f"SCD-41 Init Error: {e}")

    async def setup_gatt(self):
        logging.info("Configuring GATT Table...")

        # --- 1. RBP Sensing Service ---
        await self.server.add_new_service(UUID_SERVICE_RBP_SENSING)

        # [cite_start]RBP Characteristics Properties: Read, Notify [cite: 94]
        # Permissions: Readable (and Writeable is required by Bless to update value internally)
        props = GATTCharacteristicProperties.read | GATTCharacteristicProperties.notify
        perms = GATTAttributePermissions.readable | GATTAttributePermissions.writeable
        
        # Add all probe characteristics
        # Initial value is 0x00000000 (Int32)
        zero_val = bytearray([0x00, 0x00, 0x00, 0x00])
        
        await self.server.add_new_characteristic(UUID_SERVICE_RBP_SENSING, UUID_CHAR_TEMP_1, props, zero_val, perms)
        await self.server.add_new_characteristic(UUID_SERVICE_RBP_SENSING, UUID_CHAR_TEMP_2, props, zero_val, perms)
        await self.server.add_new_characteristic(UUID_SERVICE_RBP_SENSING, UUID_CHAR_HUMIDITY_1, props, zero_val, perms)
        await self.server.add_new_characteristic(UUID_SERVICE_RBP_SENSING, UUID_CHAR_USER_1, props, zero_val, perms)

        # [cite_start]--- 2. Device Information Service [cite: 89] ---
        # This is CRITICAL for Roastmaster to identify the probe uniquely.
        await self.server.add_new_service(UUID_SERVICE_DEVICE_INFO)
        
        # Manufacturer Name
        await self.server.add_new_characteristic(
            UUID_SERVICE_DEVICE_INFO,
            UUID_CHAR_MANUF_NAME,
            GATTCharacteristicProperties.read,
            "RBP_Python_Host".encode('utf-8'),
            GATTAttributePermissions.readable
        )
        
        # [cite_start]Serial Number [cite: 209]
        # Roastmaster uses this to unique the device: "RoastProbe 12345"
        await self.server.add_new_characteristic(
            UUID_SERVICE_DEVICE_INFO,
            UUID_CHAR_SERIAL_NUM,
            GATTCharacteristicProperties.read,
            SERIAL_NUMBER.encode('utf-8'),
            GATTAttributePermissions.readable
        )

        logging.info("GATT Services Configured")

    def encode_rbp_value(self, value):
        """
        [cite_start]RBP Encoding Method: Float x 10^2 [cite: 155]
        [cite_start]Data Type: Int32 (Signed 32-bit Integer) [cite: 127]
        [cite_start]Byte Order: Little Endian [cite: 164]
        """
        if value is None:
            return bytearray([0x00]*4)
            
        # 1. Multiply by 100
        # 2. Round to nearest integer
        int_val = int(round(value * 100))
        
        # 3. Pack as Little Endian (<), Signed Integer (i) -> 4 bytes
        return struct.pack('<i', int_val)

    async def update_sensors(self):
        self.set_led(True)
        
        for probe in self.probes:
            try:
                # Check data ready (if hardware supports check)
                if hasattr(probe["handle"], "data_ready") and not probe["handle"].data_ready:
                    continue
                
                # Read Value
                val = probe["read_func"](probe["handle"])
                
                if val is not None:
                    # RBP requires data to be encoded as Float*100 in Int32
                    encoded_bytes = self.encode_rbp_value(val)
                    
                    # Update the BLE Characteristic
                    # Note: We must update the value property AND trigger the notification
                    self.server.get_characteristic(probe["uuid"]).value = encoded_bytes
                    self.server.update_value(UUID_SERVICE_RBP_SENSING, probe["uuid"])
                    
                    # Debug logging (optional)
                    # logging.debug(f"Update {probe['name']}: {val} -> {encoded_bytes.hex()}")

            except Exception as e:
                logging.error(f"Error reading {probe['name']}: {e}")
        
        self.set_led(False)

    async def run(self):
        # 1. Setup Services/Characteristics
        await self.setup_gatt()
        
        # [cite_start]2. Start Advertising [cite: 257]
        # 'bless' handles the DBus advertisement registration here
        await self.server.start_advertising(self.server.loop)
        logging.info(f"Advertising as '{SERVER_NAME}'...")
        logging.info(f"Serial Number: {SERIAL_NUMBER}")
        
        # 3. Main Loop
        while True:
            await self.update_sensors()
            await asyncio.sleep(UPDATE_RATE)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    # Create Event Loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    rbp_host = RoastmasterBLEServer(loop)
    
    try:
        loop.run_until_complete(rbp_host.run())
    except KeyboardInterrupt:
        logging.info("Stopping RBP Host...")
        # Note: BlessServer usually handles cleanup of advertisements on exit

import logging
import struct
import socket
import json
import bluezero.localGATT

# Bluezero Imports
from bluezero import peripheral
from bluezero import adapter
from bluezero import async_tools

# ==============================================================================
# ============================== CONFIGURATION =================================
# ==============================================================================

SERVER_NAME = "RoastProbe"
SERIAL_NUMBER = "12345"
UPDATE_RATE_MS = 500 

# ==============================================================================
# ============================ RBP PROTOCOL UUIDS ==============================
# ==============================================================================
def rbp_uuid(mask):
    return f"4ac9{mask}-0b71-11e8-b8f5-b827ebe1d493"

UUID_SERVICE_RBP_SENSING = rbp_uuid("0000")
UUID_SERVICE_DEVICE_INFO = "0000180a-0000-1000-8000-00805f9b34fb"

UUID_CHAR_TEMP_1     = rbp_uuid("0001") 
UUID_CHAR_TEMP_2     = rbp_uuid("0002") 
UUID_CHAR_USER_1     = rbp_uuid("0015") 

UUID_CHAR_MANUF_NAME = "00002a29-0000-1000-8000-00805f9b34fb"
UUID_CHAR_SERIAL_NUM = "00002a25-0000-1000-8000-00805f9b34fb"

# ==============================================================================
# ============================ OBJECT CAPTURE (MONKEY PATCH) ===================
# ==============================================================================

# Global registry to hold the captured objects
CAPTURED_CHARS = {}

# 1. Save the original class
OriginalCharacteristic = bluezero.localGATT.Characteristic

# 2. Define our wrapper class
class CapturingCharacteristic(OriginalCharacteristic):
    def __init__(self, service_id, char_id, uuid, *args, **kwargs):
        # Call the real constructor so Bluezero works normally
        super().__init__(service_id, char_id, uuid, *args, **kwargs)
        
        # CAPTURE: Save 'self' (the object) to our global dict using UUID as key
        logging.info(f"CAPTURED Characteristic: {uuid}")
        CAPTURED_CHARS[str(uuid).lower()] = self

# 3. Apply the patch
# Whenever 'peripheral' tries to create a Characteristic, it uses our class instead
bluezero.localGATT.Characteristic = CapturingCharacteristic

# ==============================================================================
# ============================ SIMULATION INTERFACE ============================
# ==============================================================================

class HardwareInterface:
    def __init__(self):
        self.probes = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('127.0.0.1', 9999))
        self.sock.setblocking(False)

    def register_probe(self, name, json_key, uuid):
        self.probes.append({
            "name": name,
            "json_key": json_key,
            "uuid": str(uuid).lower()
        })

    def encode_rbp_value(self, value):
        if value is None: return [0x00]*4
        int_val = int(round(value * 100))
        return list(struct.pack('<i', int_val))

    def update_sensors(self):
        # 1. READ UDP
        try:
            data, addr = self.sock.recvfrom(4096)
            sim_data = json.loads(data.decode('utf-8'))
        except BlockingIOError:
            return True
        except Exception as e:
            logging.error(f"UDP Error: {e}")
            return True

        # 2. UPDATE VALUES
        for probe in self.probes:
            key = probe["json_key"]
            if key in sim_data:
                val = sim_data[key]
                encoded = self.encode_rbp_value(val)
                target_uuid = probe['uuid']
                
                # Retrieve the object from our CAPTURED registry
                if target_uuid in CAPTURED_CHARS:
                    char_obj = CAPTURED_CHARS[target_uuid]
                    try:
                        char_obj.set_value(encoded)
                        # Optional: Debug logging
                        # if key == "temp1": logging.info(f"Updated {probe['name']}: {val}")
                    except Exception as e:
                        logging.error(f"Error updating {probe['name']}: {e}")

        return True 

# ==============================================================================
# ============================ MAIN EXECUTION ==================================
# ==============================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    hw = HardwareInterface()

    try:
        adapter_address = list(adapter.Adapter.available())[0].address
        logging.info(f"Using adapter: {adapter_address}")
    except IndexError:
        logging.error("No Bluetooth adapter found.")
        exit(1)

    # Create the Server (This will use our patched class internally!)
    ble_server = peripheral.Peripheral(adapter_address, local_name=SERVER_NAME)

    # ------------------------------------------------------------------
    # --- SETUP SERVICES & SENSORS ---
    # ------------------------------------------------------------------
    
    # RBP Sensing Service
    ble_server.add_service(1, UUID_SERVICE_RBP_SENSING, True)
    
    # Add Characteristics 
    # (The monkey patch will capture them as they are added)
    ble_server.add_characteristic(1, 1, UUID_CHAR_TEMP_1, [0x00]*4, True, ['read', 'notify'], None, None)
    ble_server.add_characteristic(1, 2, UUID_CHAR_TEMP_2, [0x00]*4, True, ['read', 'notify'], None, None)
    ble_server.add_characteristic(1, 3, UUID_CHAR_USER_1, [0x00]*4, True, ['read', 'notify'], None, None)

    # Register with Hardware Interface
    hw.register_probe("Bean Temp", "temp1", UUID_CHAR_TEMP_1)
    hw.register_probe("Exhaust",   "temp2", UUID_CHAR_TEMP_2)
    hw.register_probe("CO2",       "co2",   UUID_CHAR_USER_1)

    # Device Info Service
    ble_server.add_service(2, UUID_SERVICE_DEVICE_INFO, True)
    ble_server.add_characteristic(2, 1, UUID_CHAR_MANUF_NAME, list("RBP_Sim".encode('utf-8')), False, ['read'], None, None)
    ble_server.add_characteristic(2, 2, UUID_CHAR_SERIAL_NUM, list(SERIAL_NUMBER.encode('utf-8')), False, ['read'], None, None)

    # ------------------------------------------------------------------
    # --- START ---
    # ------------------------------------------------------------------
    async_tools.add_timer_ms(UPDATE_RATE_MS, hw.update_sensors)

    logging.info("Starting RoastProbe SIMULATOR (Monkey Patch Mode)...")
    try:
        ble_server.publish() 
    except KeyboardInterrupt:
        logging.info("Stopping...")

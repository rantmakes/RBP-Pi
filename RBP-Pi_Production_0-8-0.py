import logging
import struct
import time
import sys
import os
import csv
import json
import datetime
import subprocess
import pigpio

# Hardware Libraries
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
UPDATE_RATE_MS = 1000 

# --- PINS ---
PIN_LED_RED  = 19  # Button LED (+) via 330 ohm resistor
PIN_SHUTDOWN = 26  # Button Switch (GND)
PIN_ZCD      = 17  
PIN_FAN      = 27  
PIN_HEATER   = 22

# --- LOGGING PATHS ---
PATH_LOG_CSV = os.path.expanduser("~/roasty/logs/")
PATH_LOG_JSON = "/var/www/html/RBP-Pi/"

# ==============================================================================
# ============================ CALIBRATION DATA (1-9 SCALE) ====================
# ==============================================================================
# "1" is the floor. Any delay longer than the Setting 1 time is clamped to 1.
# "Off" (0) is handled by the signal timeout logic.

HEATER_CURVE = [
    (0.8, 9.0),  # Fast Firing = Max Power
    (2.1, 5.0),
    (2.9, 1.0)   # Slow Firing = Min Power (1)
]

FAN_CURVE = [
    (2.0, 9.0),
    (3.9, 5.0),
    (4.7, 1.0)
]

# ==============================================================================
# ============================ RBP PROTOCOL UUIDS ==============================
# ==============================================================================
def rbp_uuid(mask): return f"4ac9{mask}-0b71-11e8-b8f5-b827ebe1d493"

UUID_SERVICE_RBP_SENSING = rbp_uuid("0000")
UUID_SERVICE_DEVICE_INFO = "0000180a-0000-1000-8000-00805f9b34fb"

UUID_CHAR_TEMP_1     = rbp_uuid("0001") # Bean
UUID_CHAR_TEMP_2     = rbp_uuid("0002") # Exhaust
UUID_CHAR_HUMIDITY_1 = rbp_uuid("000b") # Humidity
UUID_CHAR_USER_1     = rbp_uuid("0015") # CO2
UUID_CHAR_USER_2     = rbp_uuid("0016") # Heater
UUID_CHAR_USER_3     = rbp_uuid("0017") # Fan

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
            try: bluezero.localGATT.Descriptor(service_id, char_id, 1, UUID_DESC_HUMIDITY_SCALE, [0x01], ['read'])
            except: pass

bluezero.localGATT.Characteristic = CapturingCharacteristic

# ==============================================================================
# ============================ LOGGING SYSTEM ==================================
# ==============================================================================

class DataLogger:
    def __init__(self):
        self.start_time = datetime.datetime.now()
        timestamp = self.start_time.strftime("%Y-%m-%d_%H-%M-%S")
        
        try:
            os.makedirs(PATH_LOG_CSV, exist_ok=True)
            if not os.access(os.path.dirname(PATH_LOG_JSON), os.W_OK):
                logging.warning(f"PERMISSION DENIED: Cannot write to {PATH_LOG_JSON}. JSON logging disabled.")
                self.json_enabled = False
            else:
                os.makedirs(PATH_LOG_JSON, exist_ok=True)
                self.json_enabled = True
        except Exception as e:
            logging.error(f"Logging Init Failed: {e}")
            self.csv_file = None
            return

        self.csv_filename = os.path.join(PATH_LOG_CSV, f"roast_{timestamp}.csv")
        self.csv_file = open(self.csv_filename, mode='w', newline='')
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow(["Timestamp", "Elapsed_Sec", "Bean_Temp", "Exhaust_Temp", "Humidity", "CO2_g_m3", "Heater_Set", "Fan_Speed"])
        logging.info(f"Logging to CSV: {self.csv_filename}")

        if self.json_enabled:
            self.json_filename = os.path.join(PATH_LOG_JSON, f"roast_{timestamp}.json")
            self.json_data = [] 

    def log_point(self, data):
        if not self.csv_file: return

        now = datetime.datetime.now()
        elapsed = (now - self.start_time).total_seconds()
        
        record = [
            now.isoformat(),
            round(elapsed, 1),
            data.get('bean', 0),
            data.get('exhaust', 0),
            data.get('humidity', 0),
            round(data.get('co2', 0), 4),
            data.get('heater', 0),
            data.get('fan', 0)
        ]
        
        try:
            self.csv_writer.writerow(record)
            self.csv_file.flush()
        except: pass

        if self.json_enabled:
            dict_record = {
                "timestamp": record[0],
                "elapsed": record[1],
                "bean_temp": record[2],
                "exhaust_temp": record[3],
                "humidity": record[4],
                "co2_density": record[5],
                "heater": record[6],
                "fan": record[7]
            }
            self.json_data.append(dict_record)
            try:
                with open(self.json_filename, 'w') as jf:
                    json.dump(self.json_data, jf, indent=2)
            except: pass

    def close(self):
        if self.csv_file:
            self.csv_file.close()

# ==============================================================================
# ============================ SYSTEM MONITOR ==================================
# ==============================================================================

class SystemMonitor:
    def __init__(self, shutdown_callback):
        self.pi = pigpio.pi()
        if not self.pi.connected:
            logging.error("CRITICAL: PIGPIO Daemon not running!")
            self.active = False
            return
        
        self.active = True
        self.shutdown_cb_func = shutdown_callback
        self.shutdown_press_start = 0

        # LED
        self.pi.set_mode(PIN_LED_RED, pigpio.OUTPUT)
        self.pi.write(PIN_LED_RED, 0)

        # Phase Monitor Pins
        self.last_zcd_tick = 0
        self.heater_delay_us = 0
        self.fan_delay_us = 0
        
        self.pi.set_mode(PIN_ZCD, pigpio.INPUT)
        self.pi.set_mode(PIN_FAN, pigpio.INPUT)
        self.pi.set_mode(PIN_HEATER, pigpio.INPUT)
        
        self.cb_zcd = self.pi.callback(PIN_ZCD, pigpio.RISING_EDGE, self._zcd_trigger)
        self.cb_fan = self.pi.callback(PIN_FAN, pigpio.RISING_EDGE, self._fan_trigger)
        self.cb_heat = self.pi.callback(PIN_HEATER, pigpio.RISING_EDGE, self._heater_trigger)

        # Shutdown Button
        self.pi.set_mode(PIN_SHUTDOWN, pigpio.INPUT)
        self.pi.set_pull_up_down(PIN_SHUTDOWN, pigpio.PUD_UP) 
        self.pi.set_glitch_filter(PIN_SHUTDOWN, 1000) 
        
        self.cb_sd_push = self.pi.callback(PIN_SHUTDOWN, pigpio.FALLING_EDGE, self._sd_push)
        self.cb_sd_rels = self.pi.callback(PIN_SHUTDOWN, pigpio.RISING_EDGE, self._sd_release)
        
        logging.info(" - Phase & Shutdown Monitor Active")

    def set_led(self, state):
        if self.active:
            self.pi.write(PIN_LED_RED, 1 if state else 0)

    # --- Phase Logic ---
    def _zcd_trigger(self, gpio, level, tick): self.last_zcd_tick = tick
    def _fan_trigger(self, gpio, level, tick): 
        if self.last_zcd_tick: self.fan_delay_us = pigpio.tickDiff(self.last_zcd_tick, tick)
    def _heater_trigger(self, gpio, level, tick):
        if self.last_zcd_tick: self.heater_delay_us = pigpio.tickDiff(self.last_zcd_tick, tick)

    # --- Shutdown Logic ---
    def _sd_push(self, gpio, level, tick):
        self.shutdown_press_start = tick

    def _sd_release(self, gpio, level, tick):
        if self.shutdown_press_start == 0: return
        press_duration = pigpio.tickDiff(self.shutdown_press_start, tick) / 1000000.0 
        
        if press_duration > 2.0:
            logging.warning(f"Shutdown Button Held for {press_duration:.1f}s. Shutting Down...")
            self.shutdown_cb_func()
        
        self.shutdown_press_start = 0

    # --- Data Getter (1-9 Scale with Auto-Off Detection) ---
    def get_settings(self):
        if not self.active: return (0, 0)
        
        # TIMEOUT CHECK: If no ZCD pulse for > 200ms, assume Roaster is OFF
        # (60Hz = 16.6ms per cycle, so 200ms is ~12 missed cycles)
        current_tick = self.pi.get_current_tick()
        if pigpio.tickDiff(self.last_zcd_tick, current_tick) > 200000:
            return (0, 0)

        h_set = self._interpolate(self.heater_delay_us / 1000.0, HEATER_CURVE)
        f_set = self._interpolate(self.fan_delay_us / 1000.0, FAN_CURVE)
        return (h_set, f_set)

    def _interpolate(self, current_ms, curve):
        # 1. Faster than max setting -> Clamp to Max (9)
        if current_ms <= curve[0][0]: 
            return curve[0][1]
            
        # 2. Iterate segments
        for i in range(len(curve) - 1):
            if curve[i][0] < current_ms <= curve[i+1][0]:
                fraction = (current_ms - curve[i][0]) / (curve[i+1][0] - curve[i][0])
                return round(curve[i][1] + ((curve[i+1][1] - curve[i][1]) * fraction), 1)
        
        # 3. Slower than min setting (1) -> Clamp to Min (1)
        # We DO NOT drop to 0 here, because 1 is the floor while running.
        return curve[-1][1]

    def cleanup(self):
        if self.active:
            self.set_led(False)
            self.cb_zcd.cancel(); self.cb_fan.cancel(); self.cb_heat.cancel()
            self.cb_sd_push.cancel(); self.cb_sd_rels.cancel()
            self.pi.stop()

# ==============================================================================
# ============================ HARDWARE LOGIC ==================================
# ==============================================================================

class HardwareInterface:
    def __init__(self):
        self.probes = []
        self.scd_sensor = None 
        self.sys_mon = None
        self.logger = None
        
        self.init_sensors()
        self.logger = DataLogger()

    def set_led(self, on):
        if self.sys_mon:
            self.sys_mon.set_led(on)

    def trigger_shutdown(self):
        if self.sys_mon:
            for _ in range(20):
                self.sys_mon.set_led(True); time.sleep(0.05)
                self.sys_mon.set_led(False); time.sleep(0.05)
        
        self.cleanup()
        subprocess.call(['sudo', 'shutdown', '-h', 'now'])

    def cleanup(self):
        if self.scd_sensor:
            try: self.scd_sensor.stop_periodic_measurement()
            except: pass
        if self.sys_mon: self.sys_mon.cleanup()
        if self.logger: self.logger.close()

    def register_probe(self, name, handle, read_func, uuid, log_key=None):
        self.probes.append({
            "name": name, "handle": handle, "read_func": read_func, 
            "uuid": str(uuid).lower(), "log_key": log_key
        })

    def co2_ppm_to_g_m3(self, sensor_obj):
        try: return (sensor_obj.CO2 * 44.01 * 101325) / (8.314 * (sensor_obj.temperature + 273.15)) / 1_000_000
        except: return 0.0

    def init_sensors(self):
        self.sys_mon = SystemMonitor(self.trigger_shutdown)
        self.register_probe("Heater Set", self.sys_mon, lambda pm: pm.get_settings()[0], UUID_CHAR_USER_2, "heater")
        self.register_probe("Fan Speed",  self.sys_mon, lambda pm: pm.get_settings()[1], UUID_CHAR_USER_3, "fan")

        try:
            i2c = board.I2C()
            
            try:
                mcp = adafruit_mcp9600.MCP9600(i2c)
                self.register_probe("Bean Temp", mcp, lambda s: s.temperature, UUID_CHAR_TEMP_1, "bean")
            except: logging.error("MCP9600 missing")

            try:
                scd = adafruit_scd4x.SCD4X(i2c)
                self.scd_sensor = scd 
                try: scd.stop_periodic_measurement(); time.sleep(1)
                except: pass 
                scd.start_periodic_measurement()
                
                self.register_probe("Exhaust", scd, lambda s: s.temperature, UUID_CHAR_TEMP_2, "exhaust")
                self.register_probe("Humidity", scd, lambda s: s.relative_humidity, UUID_CHAR_HUMIDITY_1, "humidity")
                self.register_probe("CO2", scd, self.co2_ppm_to_g_m3, UUID_CHAR_USER_1, "co2")
            except: logging.error("SCD-41 missing")
            
        except Exception as e: logging.error(f"I2C Bus Error: {e}")

    def encode_rbp_value(self, value):
        if value is None: return [0x00]*4
        try: return list(struct.pack('<i', int(round(value * 100))))
        except: return [0x00]*4

    def update_sensors(self):
        self.set_led(True) 
        
        log_data = {}
        for probe in self.probes:
            try:
                val = probe["read_func"](probe["handle"])
                if val is not None:
                    encoded = self.encode_rbp_value(val)
                    if probe['uuid'] in CAPTURED_CHARS:
                        CAPTURED_CHARS[probe['uuid']].set_value(encoded)
                    if probe['log_key']:
                        log_data[probe['log_key']] = val
            except: pass 
        
        if self.logger:
            self.logger.log_point(log_data)

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
        logging.info(f"Bluetooth Adapter: {adapter_address}")
    except: sys.exit(1)

    ble_server = peripheral.Peripheral(adapter_address, local_name=SERVER_NAME)

    ble_server.add_service(1, UUID_SERVICE_RBP_SENSING, True)
    ble_server.add_characteristic(1, 1, UUID_CHAR_TEMP_1, [0x00]*4, True, ['read', 'notify'], None, None)
    ble_server.add_characteristic(1, 2, UUID_CHAR_TEMP_2, [0x00]*4, True, ['read', 'notify'], None, None)
    ble_server.add_characteristic(1, 3, UUID_CHAR_HUMIDITY_1, [0x00]*4, True, ['read', 'notify'], None, None)
    ble_server.add_characteristic(1, 4, UUID_CHAR_USER_1, [0x00]*4, True, ['read', 'notify'], None, None)
    ble_server.add_characteristic(1, 5, UUID_CHAR_USER_2, [0x00]*4, True, ['read', 'notify'], None, None)
    ble_server.add_characteristic(1, 6, UUID_CHAR_USER_3, [0x00]*4, True, ['read', 'notify'], None, None)

    ble_server.add_service(2, UUID_SERVICE_DEVICE_INFO, True)
    ble_server.add_characteristic(2, 1, UUID_CHAR_MANUF_NAME, list("RBP_Pi".encode('utf-8')), False, ['read'], None, None)
    ble_server.add_characteristic(2, 2, UUID_CHAR_SERIAL_NUM, list(SERIAL_NUMBER.encode('utf-8')), False, ['read'], None, None)

    async_tools.add_timer_ms(UPDATE_RATE_MS, hw.update_sensors)

    logging.info("Starting RoastProbe v0.8.0 (1-9 Scale Mode)...")
    try:
        ble_server.publish() 
    except KeyboardInterrupt:
        pass
    finally:
        hw.cleanup()
        sys.exit(0)

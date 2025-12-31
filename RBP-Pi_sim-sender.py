import socket
import time
import json
import math
import random

# ================= CONFIGURATION =================
TARGET_IP = '127.0.0.1'
TARGET_PORT = 9999
UPDATE_RATE = 0.5  # Seconds

# ================= SIMULATION STATE =================
# We simulate a "Roast" starting now
start_time = time.time()

# Physics Constants
AMBIENT = 20.0
MAX_TEMP = 230.0
ROAST_DURATION = 15 * 60 # 15 Minutes

def get_simulated_values(elapsed):
    """
    Generates a logarithmic roast curve.
    """
    progress = elapsed / ROAST_DURATION
    if progress > 1.0: progress = 1.0
    
    # 1. Bean Temp (Logarithmic Rise)
    # T(t) = Ambient + (Max - Ambient) * log(1 + k*t)
    # Simplified: Just a curve that slows down as it gets hotter
    curve = math.sin(progress * (math.pi / 2)) # 0 to 1 sine wave quarter
    bean_temp = AMBIENT + (MAX_TEMP - AMBIENT) * curve
    
    # Add random noise (0.1 degree fluctuation)
    bean_temp += random.uniform(-0.1, 0.1)

    # 2. Exhaust Temp (Hotter than bean, leads slightly)
    exhaust_temp = bean_temp * 1.15
    if exhaust_temp > 250: exhaust_temp = 250 + random.uniform(-1, 1)

    # 3. Humidity (Drops as temp rises)
    humidity = 60.0 - (40.0 * progress)
    humidity += random.uniform(-0.5, 0.5)

    # 4. CO2 (Rises as roast progresses)
    co2 = 400 + (1000 * progress) + random.randint(-10, 10)

    return {
        "temp1": bean_temp,
        "temp2": exhaust_temp,
        "hum1":  humidity,
        "co2":   co2
    }

# ================= MAIN LOOP =================
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

print(f"--- RBP Simulator Started ---")
print(f"Sending Roast Data to {TARGET_IP}:{TARGET_PORT}")
print(f"Press Ctrl+C to stop.")

try:
    while True:
        elapsed = time.time() - start_time
        data = get_simulated_values(elapsed)
        
        # Pack as JSON
        payload = json.dumps(data).encode('utf-8')
        sock.sendto(payload, (TARGET_IP, TARGET_PORT))
        
        # Log to console
        print(f"[{int(elapsed)}s] BT: {data['temp1']:.1f}°C | ET: {data['temp2']:.1f}°C")
        
        time.sleep(UPDATE_RATE)

except KeyboardInterrupt:
    print("\nSimulation Stopped.")

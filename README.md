# RoastProbe-Pi (RBP-Pi)

**A Raspberry Pi Bluetooth Bridge for the SR800 Coffee Roaster**

This project turns a Raspberry Pi into a comprehensive sensor and control monitor for the Fresh Roast SR800 coffee roaster. It broadcasts data via Bluetooth Low Energy (BLE) using the **RoastProbe Protocol**, allowing direct connection to the **Roastmaster** iOS app.

Unlike standard temperature loggers, this system uses a custom **Phase Monitor** to "listen" to the roaster's internal circuitry, decoding the exact Fan Speed (1-9) and Heater Setting (1-9) in real-time by measuring SCR firing angles.

## Features

* **Bluetooth BLE Server:** Emulates the RoastProbe protocol for seamless Roastmaster integration.
* **Real-Time Phase Monitoring:** Uses `pigpio` DMA timing to measure the delay between AC Zero Crossing and SCR pulses (Fan & Heater) with microsecond precision.
* **Smart Auto-Off:** Automatically detects if the roaster is powered down (via ZCD timeout) and reports settings as "0".
* **Multi-Sensor Support:**
* **Bean Temperature:** via MCP9600 Thermocouple Amplifier.
* **Environmental Data:** Exhaust Temp, Humidity, and CO2 Density (g/m³) via SCD-41.


* **Dual Logging:** Automatically writes roast logs to:
* **CSV:** `~/roasty/logs/` (for archival).
* **JSON:** `/var/www/html/RBP-Pi/` (for web dashboards).


* **Physical Controls:** Rugged metal pushbutton for safe shutdown with status LED feedback.
* **Safety:** Automatic sensor cleanup and safe shutdown sequences to prevent file corruption or I2C bus lockups.

## Hardware Requirements

### Core Components

* **Raspberry Pi:** Pi Zero W, 3B+, or 4 (Requires Bluetooth & WiFi).
* **Adafruit MCP9600:** I2C Thermocouple Amplifier (Address `0x67`).
* **Adafruit SCD-41:** NDIR CO2, Temperature, and Humidity Sensor (Address `0x62`).
* **Type-K Thermocouple:** For Bean Probe.

### Phase Monitor Interface (High Voltage)

* 
**4-Channel Logic Level Shifter:** **Adafruit BSS138** (Critical for protecting Pi GPIOs from 5V logic).


* **Optocouplers:** To isolate AC Mains ZCD and SCR pulses (inside the roaster) from the logic shifter.
* **Resistors:** 330Ω (for Button LED).

### Controls

* **Momentary Pushbutton:** 16mm Rugged Metal Button with built-in Ring LED (e.g., Adafruit #1477).

## Wiring Guide

### GPIO Pinout

| Component | Pin / Function | Raspberry Pi Pin | Notes |
| --- | --- | --- | --- |
| **I2C Bus** | SDA | GPIO 2 (Physical 3) | Shared by MCP9600 & SCD-41 |
| **I2C Bus** | SCL | GPIO 3 (Physical 5) |  |
| **Phase Monitor** | ZCD Pulse | **GPIO 17** | From Level Shifter LV1 |
| **Phase Monitor** | Heater Pulse | **GPIO 22** | From Level Shifter LV3 |
| **Phase Monitor** | Fan Pulse | **GPIO 27** | From Level Shifter LV2 |
| **Control** | Shutdown Button | **GPIO 26** | Connect to GND when pressed |
| **Control** | Status LED (+) | **GPIO 19** | **Requires Series Resistor (330Ω)** |

### LED Button Wiring

* **Switch Contacts:** Connect one side to **GPIO 26**, the other to **GND**.
* **LED Contacts:** Connect Anode (+) to **GPIO 19** (via 330Ω resistor). Connect Cathode (-) to **GND**.

## Installation

### 1. System Setup

Start with a fresh Raspberry Pi OS (Lite recommended). Enable SSH and I2C.

```bash
sudo raspi-config
# Interface Options -> I2C -> Enable
# Interface Options -> SSH -> Enable

```

### 2. Install Dependencies

This project relies on `pigpio` for precise timing and `blinka` for CircuitPython hardware support.

```bash
sudo apt-get update
sudo apt-get install python3-pip pigpio python3-pigpio git

# Install Python Libraries
sudo pip3 install adafruit-circuitpython-mcp9600
sudo pip3 install adafruit-circuitpython-scd4x
sudo pip3 install bluezero

```

### 3. Enable the PIGPIO Daemon

The script requires the `pigpiod` daemon to be running for phase monitoring.

```bash
sudo systemctl enable pigpiod
sudo systemctl start pigpiod

```

### 4. Clone Repository

```bash
cd ~
git clone https://github.com/YOUR_USERNAME/RBP-Pi.git
cd RBP-Pi

```

## Usage

### Manual Run

To test the system, run the production script manually:

```bash
sudo python3 RBP-Pi_Production_0-8-0.py

```

* **LED Behavior:**
* **Solid/Blinking:** System Active / Sampling.
* **Rapid Flash:** Shutdown sequence initiated.



### Auto-Start Service (Systemd)

To have the software start automatically on boot:

1. Edit the service file:
```bash
sudo nano /etc/systemd/system/roastprobe.service

```


2. Paste the following configuration:
```ini
[Unit]
Description=RoastProbe Bluetooth Service
After=bluetooth.target pigpiod.service
Requires=pigpiod.service

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/RBP-Pi
ExecStart=/usr/bin/python3 /home/pi/RBP-Pi/RBP-Pi_Production_0-8-0.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target

```


3. Enable the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable roastprobe.service
sudo systemctl start roastprobe.service

```



## Phase Monitor Calibration

The script maps the delay between the **Zero Crossing** and the **SCR Firing Pulse** to a **1-9** setting.

**Logic Rules:**

1. **Floor Clamp:** The SR800 dial minimum is 1. Therefore, any delay longer than the "Setting 1" time is clamped to **1**.
2. **Auto-Off:** The setting reports **0** (Off) *only* if the Zero Crossing signal disappears for more than 200ms (indicating the roaster is unplugged or switched off at the base).

**Current Calibration (SR800 @ 60Hz):**

| Setting | Heater Delay (ms) | Fan Delay (ms) |
| --- | --- | --- |
| **9 (Max)** | 0.8 ms | 2.0 ms |
| **5 (Mid)** | 2.1 ms | 3.9 ms |
| **1 (Low)** | 2.9 ms | 4.7 ms |

*Note: These values are specific to 60Hz mains frequency. If used in a 50Hz region, recalibration is required.*

## Shutdown Button

To safely shut down the Pi:

1. **Press and Hold** the button for **2 seconds**.
2. The LED will flash rapidly to acknowledge.
3. Release the button.
4. The system will close all log files, stop sensors, and execute `sudo shutdown -h now`.
5. Once the LED turns off completely, it is safe to remove power.

## Safety Warning

**DANGER: HIGH VOLTAGE**
This project requires interfacing with the internal circuitry of a coffee roaster, which involves AC Mains voltage (120V/240V).

* **Isolation is mandatory.** Never connect the Raspberry Pi directly to the roaster's control board. You must use optocouplers and logic level shifters.
* **Do not attempt** this if you are not comfortable working with high-voltage electronics.
* The authors assume no liability for damage to equipment or personal injury.

---

*Developed for the home roasting community.*

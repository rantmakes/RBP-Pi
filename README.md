## RBP-Pi: Raspberry Pi Roastmaster Host
RBP-Pi turns a Raspberry Pi into a Bluetooth Low Energy (BLE) roasting station. It broadcasts real-time temperature, humidity, and CO2 data using the Roastmaster Bluetooth Protocol (RBP), making it fully compatible with the Roastmaster iOS app.
This project interfaces with hardware sensors (MCP9600 & SCD-41) and handles the specific GATT requirements (UUIDs and Descriptors) needed for seamless app integration.
## 🚀 Features
 * RBP Compliance: Fully implements the Roastmaster Bluetooth Protocol.
 * Dual Temperature Support:
   * Bean Temp: K-Type Thermocouple via MCP9600.
   * Exhaust Temp: Ambient/Air temp via SCD-41.
 * Environmental Data: Streams Relative Humidity and CO2 levels (mapped to User Defined fields in Roastmaster).
 * Smart Bluetooth Patch: Includes a custom monkey-patch for the bluezero library to correctly handle required Descriptors (fixing "Missing Descriptor" errors on iOS).
 * Status Indication: LED feedback for Bluetooth advertising and sensor reading.
## 🛠 Hardware Requirements
| Component | Purpose | Interface |
|---|---|---|
| Raspberry Pi | Host Computer | (Zero W, 3B+, 4, or 5) |
| Adafruit MCP9600 | Thermocouple Amp | I2C (Hardware) |
| Adafruit SCD-41 | CO2/Temp/Hum Sensor | I2C (Software/BitBang) |
| K-Type Thermocouple | Bean Probe | Connected to MCP9600 |
| LED + Resistor | Status Indicator | GPIO |
## Wiring Guide
1. MCP9600 (Bean Temp)
 * Connects to the Pi's default Hardware I2C bus.
 * VCC: 3.3V or 5V
 * GND: GND
 * SDA: GPIO 2 (Physical Pin 3)
 * SCL: GPIO 3 (Physical Pin 5)
2. SCD-41 (Exhaust, CO2, Humidity)
 * Connects via BitBang I2C to avoid address conflicts and improve stability.
 * VCC: 3.3V or 5V
 * GND: GND
 * SDA: GPIO 23 (Configurable in script)
 * SCL: GPIO 24 (Configurable in script)
3. Status LED
 * Anode (+): GPIO 2
 * Cathode (-): GND (via Resistor)
## 📦 Installation
1. Prerequisite System Setup
Ensure your Raspberry Pi OS is up to date and Bluetooth is enabled.
sudo apt update
sudo apt upgrade
sudo apt install python3-pip python3-venv libglib2.0-dev

2. Enable I2C
Run sudo raspi-config, navigate to Interface Options > I2C, and enable it.
3. Clone Repository & Setup Environment
git clone https://github.com/rantmakes/RBP-Pi.git
cd RBP-Pi
python3 -m venv venv
source venv/bin/activate

4. Install Dependencies
Install the required CircuitPython libraries and the Bluezero Bluetooth wrapper.
pip install bluezero adafruit-circuitpython-mcp9600 adafruit-circuitpython-scd4x adafruit-circuitpython-bitbangio adafruit-blinka RPi.GPIO

Note: The script includes an internal patch for bluezero, so you do not need to modify the library source code manually.
## 🏃 Usage
Run the script with sudo (often required for direct hardware access and Bluetooth advertising).
sudo ./venv/bin/python RBP-Pi_Production_Hardware_Only.py

LED Status Indicators
 * Solid/Blinking: The script is running, reading sensors, and updating the Bluetooth characteristics.
 * Off: The script has stopped or encountered a critical error.
## 🧩 How it Works
The Bluezero Monkey Patch
The standard bluezero library version 0.8.0 (and others) does not easily expose methods to add GATT Descriptors to characteristics, which are required by the RBP protocol for Humidity sensors.
This project includes a class wrapper (CapturingCharacteristic) that:
 * Intercepts the creation of Bluetooth characteristics.
 * Identifies the Humidity Characteristic by its UUID (000b).
 * Automatically injects the Humidity Scale Descriptor (001c) with the correct flags and value (0x01 for Relative %).
 * Captures object references to allow real-time data updates from the main loop.
RBP UUID Mapping
| Data | Source | RBP Characteristic | UUID |
|---|---|---|---|
| Bean Temp | MCP9600 | Temp 1 | 4ac90001-... |
| Exhaust | SCD-41 | Temp 2 | 4ac90002-... |
| Humidity | SCD-41 | Humidity | 4ac9000b-... |
| CO2 | SCD-41 | User Defined 1 | 4ac90015-... |
## ⚠️ Troubleshooting
"Failed to find MCP9600"
 * Check your wiring on GPIO 2 and 3.
 * Ensure I2C is enabled (ls /dev/i2c*).
"SCD-41 Bus Timed Out"
 * This sensor uses "BitBang" I2C on GPIO 23 and 24.
 * Ensure you have not connected it to the standard I2C pins.
 * Check for loose connections.
iOS App shows "Missing Descriptor"
 * Ensure you are running the Production version of the script.
 * Check the logs for Descriptor Added Successfully.

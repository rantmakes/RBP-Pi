## Important Notice

This is a fork from the "pre-release" software for beta testers of Roastmaster for iOS10, originally developed by Rainfrog Inc. This version has been adapted to run on a **Raspberry Pi Zero 2 W** (or similar SBC) using **Python 3**.

## Roastmaster RDP Probe Host (Python/Raspberry Pi)

`Roastmaster_RDP_Probe_Host` is a customizable Single Board Computer (SBC) application to send sensor readings via the Roastmaster Datagram Protocol (RDP) to Roastmaster iOS over a WiFi Network.

Roastmaster is coffee roasting toolkit and data logging software, in which users can log temperature data during their coffee roasting sessions. This logging can be done either manually or via separate electronic thermocouple reading "clients".

[cite_start]The RDP Protocol is an OpenSource communications protocol created by Rainfrog, Inc. for the purpose of standardizing the transmission of roasting information to Roastmaster[cite: 5].

This implementation supports multiple sensor types simultaneously, specifically handling the **MCP9600** (Thermocouple Amplifier) via Hardware I2C and the **SCD-41** (CO2, Temp, Humidity) via Software I2C.

## Software Features

* **Python 3 Implementation:** Runs on standard Linux distributions (Raspberry Pi OS).
* **Multi-Sensor Support:** Currently configured for MCP9600 (Thermocouple) and SCD-41 (CO2/Environmental).
* **Web Monitor:** Includes a local JSON logger to visualize packets via a local Apache web server.
* [cite_start]**Handles Handshaking:** Manages SYN/ACK handshaking with Roastmaster[cite: 10].
* [cite_start]**Unlimited Sensors:** Hosts multiple sensors, each sending on a unique channel (RDP supports 16)[cite: 10].
* [cite_start]**Packet Ordering:** Supports RDP "Epoch" values to ensure correct packet ordering over UDP[cite: 11].

## RDP Protocol Features

* [cite_start]Operates over the easy-to-use User Datagram Protocol (UDP)[cite: 10].
* [cite_start]Lightweight, consuming very little network bandwidth[cite: 10].
* [cite_start]Server is multicast discoverable (224.0.0.1)[cite: 10].
* [cite_start]Supports basic handshaking (SYN/ACK), simulating a "connection"[cite: 10].
* [cite_start]Data format is compact, human-readable JSON[cite: 11].

## Hardware Requirements

* **SBC:** Raspberry Pi Zero 2 W (or similar Raspberry Pi model).
* **Primary Sensor:** Adafruit MCP9600 I2C Thermocouple Amplifier (K-Type).
* **Secondary Sensor:** Adafruit SCD-41 (CO2, Temperature, Humidity).
* **Wiring:**
    * **MCP9600:** Connected to Hardware I2C (SDA: GPIO 2, SCL: GPIO 3).
    * **SCD-41:** Connected to Software I2C (Configured for SDA: GPIO 23, SCL: GPIO 24).

## Configuration & Setup

### 1. System Dependencies
Ensure your Raspberry Pi is up to date and has I2C enabled (`sudo raspi-config` -> Interface Options -> I2C).

Install the required Python libraries:
```bash
sudo apt-get update
sudo apt-get install python3-pip apache2
sudo pip3 install adafruit-circuitpython-mcp9600
sudo pip3 install adafruit-circuitpython-scd4x
sudo pip3 install adafruit-circuitpython-bitbangio
sudo pip3 install adafruit-blinka
```

### 2. Network Configuration

The Raspberry Pi handles WiFi connections via the OS (wpa_supplicant). Ensure your Pi is connected to the same WiFi network as your iOS device running Roastmaster.

### 3. Application Configuration

Edit the roastmaster_host.py file to match your Roastmaster settings:

Host Serial: Set HOST_SERIAL to match the "Serial" string defined in your Roastmaster probe definition.

Port: Set SERVER_PORT (Default: 5050).

Sensor Pins: Verify SCD_SDA_PIN and SCD_SCL_PIN match your wiring for the secondary sensor.

### 4. Web Monitor (Optional)

This host logs packets to /var/www/html/rdp_packet.json. To view the live data stream:

Ensure Apache is installed (sudo apt-get install apache2).

Grant write permissions to the web folder: sudo chmod 777 /var/www/html.

Place monitor.html in /var/www/html/.

Navigate to http://<your-pi-ip>/monitor.html in a browser.

RDP Channel Mapping
This implementation maps the sensors to the following RDP Channels. You must configure Roastmaster "Curves" to listen to these specific channels:

Channel 1: Bean Temp (MCP9600 Thermocouple) - RPMetaType: 3000 (BT Temp) 

Channel 2: Exhaust Temp (SCD-41 Temp) - RPMetaType: 3004 (Exhaust Temp) 

Channel 3: Humidity (SCD-41 RH%) - RPMetaType: 3005 (Ambient Temp) 

Channel 4: CO2 (SCD-41 PPM) - RPMetaType: 3002 (MET Temp) 

Note: Humidity and CO2 are sent as "Temperature" events (RPEventType: 3) so that Roastmaster can graph them as curves.

Resources
MCP9600 Library: https://docs.circuitpython.org/projects/mcp9600/en/latest/

SCD-41 Library: https://docs.circuitpython.org/projects/scd4x/en/latest/

RDP Protocol Datasheet: Included in repository.

Acknowledgements
Danny Hall (Rainfrog, Inc.): For the original RDP protocol and Arduino implementation.

Evan Graham: For the original digital reading prototype.

Robert Swift: For impetus, vision, and code prototyping.

License
MIT License

Copyright (c) 2016 Rainfrog, Inc. Written by Danny Hall, for Rainfrog, Inc., modified by Rich Anthony. Based on the prototyping of Robert Swift and input from countless Roastmaster users.

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

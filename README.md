# RoastProbe-Pi (RBP-Pi)

**A Raspberry Pi Bluetooth Bridge for the SR800 Coffee Roaster**

This project turns a Raspberry Pi into a comprehensive sensor monitor for the Fresh Roast SR800 coffee roaster. It broadcasts data via Bluetooth Low Energy (BLE) using the **RoastProbe Protocol**, allowing direct connection to the **Roastmaster** iOS app.

This version is designed for **Non-Invasive Sensing**. It relies entirely on external sensors and does **not** require connecting to (or risking) the roaster's internal high-voltage circuitry.

## Features

* **Bluetooth BLE Server:** Emulates the RoastProbe protocol for seamless Roastmaster integration.
* **Multi-Sensor Support:**
    * **Bean Temperature:** High-precision K-Type measurement via MCP9600.
    * **Environmental Data:** Exhaust Temp, Humidity, and CO2 Density (g/m³) via SCD-41.
* **Dual Logging:** Automatically writes roast logs to:
    * **CSV:** `~/roasty/logs/` (for archival).
    * **JSON:** `/var/www/html/RBP-Pi/` (for web dashboards).
* **Physical Controls:** Rugged metal pushbutton for safe shutdown with status LED feedback.
* **Safety:** Automatic sensor cleanup and safe shutdown sequences to prevent file corruption.

## Hardware Requirements

### Core Components
* **Raspberry Pi:** Pi Zero W, 3B+, or 4 (Requires Bluetooth & WiFi).
* **Adafruit MCP9600:** I2C Thermocouple Amplifier (Address `0x67`).
* **Adafruit SCD-41:** NDIR CO2, Temperature, and Humidity Sensor (Address `0x62`).
* **Type-K Thermocouple:** For Bean Probe.

### Controls
* **Momentary Pushbutton:** **NOYITO 12mm/16mm Chassis Switch** (Rated 3-6V) with built-in Ring LED.
    * *Note: This specific switch has a built-in resistor, allowing direct connection to the Pi.*

## Wiring Guide

### GPIO Pinout

| Component | Pin / Function | Raspberry Pi Pin | Notes |
| :--- | :--- | :--- | :--- |
| **I2C Bus** | SDA | GPIO 2 (Physical 3) | Shared by MCP9600 & SCD-41 |
| **I2C Bus** | SCL | GPIO 3 (Physical 5) | Shared by MCP9600 & SCD-41 |
| **Control** | Shutdown Button | **GPIO 26** (Physical 37) | Connect to GND when pressed |
| **Control** | Status LED (+) | **GPIO 19** (Physical 35) | Direct Connection (For NOYITO switch) |

### LED Button Wiring
* **Switch Contacts:** Connect one contact to **GPIO 26**, the other to **GND** (Physical 39).
* **LED Contacts:** Connect **POWER LED+** to **GPIO 19**. Connect **POWER LED-** to **GND** (Physical 34).

## Installation

### 1. System Setup
Start with a fresh Raspberry Pi OS (Lite recommended). Enable SSH and I2C.

```bash
sudo raspi-config
# Interface Options -> I2C -> Enable
# Interface Options -> SSH -> Enable
# TPMSTempNet

TPMSTempNet leverages parked vehicle TPMS (Tire Pressure Monitoring System) sensors via RTL-SDR to infer local ambient temperature. It routes decoded TPMS data through a custom WeeWX driver, sanitizes and filters the data to isolate stable cold-ambient readings, and broadcasts Urban Heat Island telemetry directly to Nostr relays (`relaying.earth` compliant).

### Prerequisites

* [rtl_433](https://github.com/merbanan/rtl_433) installed and accessible in your system path.
* [WeeWX](https://github.com/weewx/weewx) (v4 or v5) installed and running.
* **Python Cryptography & WebSocket Libraries:** Required for Nostr event signing and relay communication (`nostr`, `websocket-client`).


---

## Installation & Configuration


### 1. Install Python Dependencies

If your WeeWX environment runs inside a virtual environment (such as WeeWX v5), install the required Nostr publishing dependencies using `pip`:

```bash
# Example for a WeeWX virtual environment or pip installation:
pip install nostr websocket-client
```

### 2. Install the Custom Scripts

Save the provided Python scripts into your WeeWX `user` directory:

* For WeeWX v5 (pip install): `~/weewx-data/bin/user/`
* For WeeWX v4 (legacy install): `/usr/share/weewx/user/` or `/home/weewx/bin/user/`

Place `sdr.py`, `tpmstemp.py`, and `nostr_publisher.py` inside this directory. Additionally, keep the `tools/` directory alongside your setup for diagnostic utilities.

### 3. Configure `weewx.conf`

Open your `weewx.conf` configuration file and make the following changes:

**A. Enable the SDR Driver**
Change your station type to SDR:

```ini
station_type = SDR

```

Add the `[SDR]` block. *Note: Adjust frequency flags (`-f`) for your region.*

```ini
[SDR]
    driver = user.sdr
    cmd = /usr/local/bin/rtl_433 -M utc -F json -f 315M -f 433.92M -H 15 -s 1024k -g 42.1
    [[sensor_map]]
        outTemp = temperature.sane.UniversalTPMSPacket

```

> **Privacy Note:** This driver strips and anonymizes TPMS hardware IDs. No vehicle-identifying data is retained or transmitted; only sanitized temperature metrics are processed.
> 
> 

**B. Set Local Climate Bounds**
Add a standalone block to filter physical temperature limits. Values must be in Celsius.

```ini
[TPMSTempService]
    min_temp = 0.0     # Adjust for your winter extremes
    max_temp = 46.0    # Adjust for your summer extremes

```

**C. Add the Nostr Publisher & Map Registration**
Under `[StdRESTful]`, add the `[[Nostr]]` block. On its first startup, if `private_key` is left blank, the publisher will automatically generate a secure Nostr keypair and print to the terminal.

```ini
    [[Nostr]]
        # Enable the urban heat island broadcast
        enable = true
        
        # Target observation
        target_observation = outTemp
        
        # Geohash length (7 = ~150m x 150m resolution for street-level mapping)
        geohash_precision = 7
        
        # Hexadecimal Private Key (auto-generated on first run if left blank)
        private_key = ""
        
        # Target Relays (leave blank to use built-in default relay network)
        relays = 

```

**D. Enable Services in the Engine**
In the `[Engine]` -> `[[Services]]` section:

1. Append `user.tpmstemp.TPMSTempService` to your `data_services` line.
2. Append `user.nostr_publisher.Nostr` to your `process_services` line.


```ini
data_services = ..., user.tpmstemp.TPMSTempService
process_services = ..., user.nostr_publisher.Nostr

```

### 4. Restart WeeWX

Restart the WeeWX daemon to apply changes and broadcast your station profile (Kind 16158) to map frontends like `relaying.earth`:

```bash
sudo systemctl restart weewx

```

---

## Live Monitoring & Ephemeral Debugging

Because TPMSTempNet strictly anonymizes all vehicle data, specific TPMS models and hardware IDs are never written to your database or system logs.

To watch local tire traffic in real-time, simply run `weewxd` directly in your foreground terminal. The driver intercepts and force-prints the raw hardware data to your active window alongside the standard loop packets:

```text
[TPMS INTERCEPT] Model: Ford | ID: bhZw4ts6 | Temp: 25.0°C

```

> **Privacy Note:** This terminal output is volatile. It physically bypasses WeeWX's internal loggers and vanishes as soon as you close the session. Once WeeWX is deployed as a background daemon, this data becomes entirely invisible.

---

## Relay Diagnostics & Tooling

TPMSTempNet includes a lightweight diagnostic tool inside the `tools/` directory to query and inspect your live profile or telemetry events directly from the command line across any supported relay.

### Inspecting Events with `fetch_event.py`

To verify that your station profile or telemetry events are successfully indexed on the network, pass the event hex ID to the diagnostic script:

```bash
python3 tools/fetch_event.py <EVENT_ID_HEX>

```

You can optionally target a specific relay using the `--relay` flag:

```bash
python3 tools/fetch_event.py <EVENT_ID_HEX> --relay wss://nos.lol

```

---

## Adding New Vehicles

As of September 2026, all major TPMS models supported by `rtl_433` are mapped under the `UniversalTPMSPacket` class. If `rtl_433` adds support for new vehicle protocols in the future, you can easily bridge them by adding the new base identifier string to the `supported_tpms_models = []` array at the bottom of that class in `sdr.py`.

---

## Architecture Details

* **Gateway vs. Sensor:** The RTL-SDR dongle functions strictly as a local RF receiving gateway over a `usb`/`ethernet` backhaul, while the telemetry data source is globally declared as **Multi-TPMS** to reflect the asphalt sensor array.
* **Standards Compliance:**
    * **Kind 16158:** Automatically manages map registration on startup using explicit tags (`name`, `description`, `geohash`, `connectivity`, `sensor`, `sensor_status`) with an empty content payload.
    * **Kind 4223:** Transmits periodic temperature telemetry payloads utilizing standard `["temp", value, "Multi-TPMS"]` tags.
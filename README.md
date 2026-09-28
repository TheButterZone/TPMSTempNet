# TPMSTempNet

TPMSTempNet leverages parked vehicle TPMS (Tire Pressure Monitoring System) sensors via RTL-SDR to infer local ambient temperature. It routes decoded TPMS data through a custom WeeWX driver, sanitizes and filters the data to isolate stable cold-ambient readings, and can optionally broadcast this Urban Heat Island telemetry directly to Nostr relays (`relaying.earth` compliant) or seamlessly pass the metrics to standard weather networks like Weather Underground and CWOP.

### Prerequisites

* [rtl_433](https://github.com/merbanan/rtl_433) installed and accessible in your system path.
* [WeeWX](https://github.com/weewx/weewx) (v4 or v5) installed and running.
* **Python Cryptography & WebSocket Libraries (Optional):** Required only for Nostr event signing and relay communication (`nostr`, `websocket-client`). If you only want to use TPMSTempNet to sanitize temperature data for traditional networks like Weather Underground or CWOP, you can skip these dependencies entirely.

---

## Installation & Configuration


### 1. Install Python Dependencies (Optional)

If you intend to broadcast your data to Nostr, install the required publishing dependencies using `pip`. If you are only using WeeWX's built-in weather networks (like WU or CWOP), you can skip this step.

```bash
# Example for a WeeWX virtual environment or pip installation:
pip install nostr websocket-client
```

### 2. Install the Custom Scripts

Save the provided Python scripts into your WeeWX `user` directory:

* For WeeWX v5 (pip install): `~/weewx-data/bin/user/`
* For WeeWX v4 (legacy install): `/usr/share/weewx/user/` or `/home/weewx/bin/user/`

Place `sdr.py` and `nostr_publisher.py` inside this directory. Additionally, keep the `tools/` directory alongside your setup for standalone Nostr tools.

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
        cmd = /usr/local/bin/rtl_433 -M utc -F json -f 315M -f 433.92M -H 15 -s 1024k -g 42.1 -R 0 -R 59 -R 60 -R 82 -R 88 -R 89 -R 90 -R 95 -R 110 -R 123 -R 140 -R 156 -R 168 -R 180 -R 186 -R 201 -R 203 -R 208 -R 212 -R 225 -R 226 -R 241 -R 248 -R 252 -R 257 -R 275 -R 295 -R 298 -R 299 -R 321 -R 322 -R 328 -R 343 -R 352 -R 354 -R 355 -R 362 -R 365 -R 378 -R 380 -R 381

    # --- Optional TPMS Filter Tuning ---
    # The driver uses these built-in defaults automatically. 
    # Uncomment and adjust these values only if you need to change the filter tolerances:
    # max_age = 3600       # Seconds before a stale sensor is dropped
    # quarantine = 7200    # Seconds to ignore a sensor showing impossible rate spikes
    # max_rate = 0.11      # Maximum plausible temperature change (degrees per minute)
    # dwell_sec = 600      # Wait time before a new sensor is trusted
    # min_temp = 0.0       # Minimum valid physical temperature bound
    # max_temp = 46.0      # Maximum valid physical temperature bound

    [[sensor_map]]
        outTemp = temperature.sane.UniversalTPMSPacket

```

> **Privacy Note:** This driver strips and anonymizes TPMS hardware IDs. No vehicle-identifying data is retained or transmitted; only sanitized temperature metrics are processed.
> 
> 

**B. Add the Nostr Publisher & Map Registration (Optional)**

*(Skip this step if you do not wish to broadcast to Nostr. The Python script will safely ignore the missing dependencies.)*

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

**C. Enable the Nostr Publisher in the Engine (Optional)**

*(Skip this step if you do not wish to broadcast to Nostr.)*

In the `[Engine]` -> `[[Services]]` section:

Append `user.nostr_publisher.Nostr` to your `process_services` line.


```ini
process_services = ..., user.nostr_publisher.Nostr

```

### 4. Restart WeeWX

Restart the WeeWX daemon to apply changes. If Nostr is enabled, this will also broadcast your station profile (Kind 16158) to map frontends like `relaying.earth`:

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

## Nostr Relay Diagnostics & Tooling

TPMSTempNet includes lightweight diagnostic tools inside the `tools/` directory to manage and inspect your live profile or telemetry events directly from the command line across any supported relay.

### Manual Station Registration with `register_station.py`

While the plugin automatically registers your station on startup, you can use this tool to manually force an immediate profile update to the Nostr network. This is useful if you just updated your station's name or location in `weewx.conf` and want to push the changes to the map without having to restart the entire WeeWX daemon.

To manually broadcast your station profile, run the script and point it directly to your WeeWX configuration file:

```bash
python3 tools/register_station.py /path/to/weewx.conf

```

The script will securely parse your coordinates and private key, generate a compliant Kind 16158 profile event, and output the live acceptance status from each relay.

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

As of September 2026, all major TPMS models supported by `rtl_433` are mapped under the `UniversalTPMSPacket` class and included in the `weewx.conf` `[SDR]` block. If `rtl_433` adds support for new vehicle protocols in the future, you can easily bridge them in two steps:

1. Add the new base identifier string to the `supported_tpms_models = []` array at the bottom of that class in `sdr.py`.
2. Append the new protocol's ID number to the `cmd` string in your `weewx.conf` using the `-R <ID>` flag (e.g., `-R 000`).

---

## Architecture Details

* **Gateway vs. Sensor:** The RTL-SDR dongle functions strictly as a local RF receiving gateway over a `usb`/`ethernet` backhaul, while the telemetry data source is globally declared as **Multi-TPMS** to reflect the asphalt sensor array.
* **Standards Compliance:**
    * **Kind 16158:** Automatically manages map registration on startup using explicit tags (`name`, `description`, `geohash`, `connectivity`, `sensor`, `sensor_status`) with an empty content payload.
    * **Kind 4223:** Transmits periodic temperature telemetry payloads utilizing standard `["temp", value, "Multi-TPMS"]` tags.

### Ambient Baseline & Median Filtering Logic

To infer stable outdoor ambient temperatures from parked vehicle TPMS sensors while avoiding engine heat, direct sunlight, and erratic pressure/temperature fluctuations, the driver implements a multi-layer filtering and aggregation state machine:

1. **Ingestion & Rate-of-Change Validation:** Every incoming sensor reading checks the elapsed time since its last transmission. If the rate of temperature change per minute exceeds `max_rate` (e.g., a sudden crash or spike caused by a tire splashing through a puddle), the sensor is isolated and subjected to a penalty quarantine for the duration specified by `quarantine`.
2. **The Filter Pool:** Before a sensor's temperature is considered for the ambient baseline, it must pass four strict criteria:
   * **Max Age:** Sensors that haven't transmitted within `max_age` seconds are purged from memory entirely.
   * **Quarantine Status:** Quarantined sensors are ignored.
   * **Dwell Time:** New sensors must survive in the system longer than `dwell_sec` before their data is trusted, preventing fresh, uncalibrated tires from skewing the baseline.
   * **Physical Bounds:** The temperature must fall strictly between `min_temp` and `max_temp` (default 0°C to 46°C).
3. **Aggregation & Median Selection:** Once the pool of valid readings is compiled, the driver determines the final ambient temperature based on the sample size:
   * **1 Valid Reading:** Returns that single value directly.
   * **2 Valid Readings:** Computes the arithmetic mean of the two values.
   * **3 or More Valid Readings:** Uses Python's `statistics.median()` to isolate the true statistical median, neutralizing outlier sensors affected by localized heat sources.
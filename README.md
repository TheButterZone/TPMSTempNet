# TPMSTempNet

TPMSTempNet leverages parked vehicle TPMS (Tire Pressure Monitoring System) sensors via RTL-SDR to infer local ambient temperature. It routes decoded TPMS data through a custom WeeWX driver and uses a filtering service to reject elevated readings from hot or moving tires, leaving only stable, cold-ambient measurements.

### Prerequisites

* [rtl_433](https://github.com/merbanan/rtl_433) installed and accessible in your system path.
* [WeeWX](https://github.com/weewx/weewx) (v4 or v5) installed and running.

---

## Installation & Configuration

### 1. Install the Custom Scripts

Save the provided Python scripts into your WeeWX `user` directory:

* For WeeWX v5 (pip install): `~/weewx-data/bin/user/`
* For WeeWX v4 (legacy install): `/usr/share/weewx/user/` or `/home/weewx/bin/user/`

Place `sdr.py` and `mintemp.py` inside this directory.

### 2. Configure `weewx.conf`

Open your `weewx.conf` configuration file and make the following changes:

**A. Enable the SDR Driver**
Change your station type to SDR:

```ini
station_type = SDR

```

Add the `[SDR]` block above the Simulator block. *Note: If you are located outside North America, you may need to adjust the frequency hopping (`-f`) flags to match TPMS bands in your region.*

```ini
[SDR]
    driver = user.sdr
    cmd = /usr/local/bin/rtl_433 -M utc -F json -f 315M -f 433.92M -H 15 -s 1024k -g 42.1
    [[sensor_map]]
        extraTemp1 = temperature.sane.UniversalTPMSPacket

```

> **Privacy Note:** This driver strips and anonymizes TPMS hardware IDs to ensure privacy. No vehicle-identifying data is retained or transmitted to weather services; only sanitized temperature metrics are processed and stored.

**B. Enable the MinTemp Filtering Service**
Under the `[Engine]` -> `[[Services]]` section, locate the `data_services` list. Append the `MinTempService` to the end of the line:

```ini
data_services = ..., user.mintemp.MinTempService

```

**C. Set Local Climate Bounds**
Add a new standalone block anywhere in `weewx.conf` to configure the physical temperature limits for your specific climate. The service will discard any readings outside this range. Values must be in Celsius.

```ini
[MinTempService]
    min_temp = 0.0     # Adjust for your winter extremes
    max_temp = 46.0    # Adjust for your summer extremes (Default is San Diego, CA)

```

### 3. Restart WeeWX

Once configured, restart the WeeWX daemon to apply the changes and begin capturing TPMS data:

```bash
sudo systemctl restart weewx

```

*(Or launch WeeWX directly from your terminal if running in standalone/debug mode).*

### Adding New Vehicles

As of September 2026, all major TPMS models supported by `rtl_433` are mapped under the `UniversalTPMSPacket` class. If `rtl_433` adds support for new vehicle protocols in the future, you can easily bridge them by adding the new base identifier string to the `supported_tpms_models = []` array at the bottom of `sdr.py`.

### Live Monitoring & Ephemeral Debugging

Because TPMSTempNet strictly anonymizes all vehicle data, specific TPMS models and hardware IDs are never written to your database or system logs.

To watch local tire traffic in real-time, simply run `weewxd` directly in your foreground terminal. The driver intercepts and force-prints the raw hardware data to your active window alongside the standard loop packets:

```text
[TPMS INTERCEPT] Model: Ford | ID: bhZw4ts6 | Temp: 25.0°C

```

> **Privacy Note:** This terminal output is volatile. It physically bypasses WeeWX's internal loggers and vanishes as soon as you close the session. Once WeeWX is deployed as a background daemon, this data becomes entirely invisible.

**Raw SDR Tuning**
If you need to verify sensor reception or tune your antenna outside of the WeeWX engine, you can also run the SDR directly in your terminal (adjusting the frequency hopping (`-f`) flags for your regional frequencies, if needed):

```bash
/usr/local/bin/rtl_433 -M utc -F json -f 315M -f 433.92M -H 15 -s 1024k -g 42.1 | grep --line-buffered -i "tpms"

```

> **Note:** This terminal output is volatile and vanishes as soon as you close the session or kill the process, preserving absolute privacy.

---
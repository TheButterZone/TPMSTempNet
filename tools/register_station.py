import sys
import os
import json
import ssl
from websocket import create_connection
from nostr.event import Event
from nostr.key import PrivateKey

def parse_weewx_conf(conf_path):
    """Pure Python parser for weewx.conf to eliminate third-party config dependencies."""
    station_data = {}
    nostr_data = {}
    current_section = ""
    current_subsection = ""

    with open(conf_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            
            if line.startswith("[[") and line.endswith("]]"):
                current_subsection = line[2:-2].strip()
            elif line.startswith("[") and line.endswith("]"):
                current_section = line[1:-1].strip()
                current_subsection = ""
            elif "=" in line:
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                
                if current_section == "Station":
                    station_data[key] = val
                elif current_subsection == "Nostr" or current_section == "Nostr":
                    nostr_data[key] = val

    return station_data, nostr_data

def geohash_encode(latitude, longitude, precision=7):
    """Pure Python geohash encoder."""
    base32 = "0123456789bcdefghjkmnpqrstuvwxyz"
    lat_interval, lon_interval = (-90.0, 90.0), (-180.0, 180.0)
    geohash = []
    bits = [16, 8, 4, 2, 1]
    bit = 0
    ch = 0
    even = True

    while len(geohash) < precision:
        if even:
            mid = (lon_interval[0] + lon_interval[1]) / 2.0
            if longitude > mid:
                ch |= bits[bit]
                lon_interval = (mid, lon_interval[1])
            else:
                lon_interval = (lon_interval[0], mid)
        else:
            mid = (lat_interval[0] + lat_interval[1]) / 2.0
            if latitude > mid:
                ch |= bits[bit]
                lat_interval = (mid, lat_interval[1])
            else:
                lat_interval = (lat_interval[0], mid)
        even = not even
        if bit < 4:
            bit += 1
        else:
            geohash.append(base32[ch])
            bit = 0
            ch = 0
    return "".join(geohash)

def main():
    print("\n=== TPMSTempNet Station Registration ===")
    
    if len(sys.argv) < 2:
        print("Usage: python3 tools/register_station.py /path/to/weewx.conf")
        sys.exit(1)
        
    conf_path = sys.argv[1]
    if not os.path.exists(conf_path):
        print(f"[ERROR] Configuration file not found at: {conf_path}")
        sys.exit(1)

    print(f"Reading configuration from {conf_path}...")
    try:
        station_cfg, nostr_cfg = parse_weewx_conf(conf_path)
        
        station_name = station_cfg.get('location', 'TPMSTempNet Node')
        lat = float(station_cfg['latitude'])
        lon = float(station_cfg['longitude'])
        sk_hex = nostr_cfg.get('private_key', '').strip()
        
    except KeyError as e:
        print(f"\n[ERROR] Missing key in configuration: {e}")
        print("Ensure 'latitude' and 'longitude' are defined under [Station] in weewx.conf.")
        sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR] Failed to parse configuration: {e}")
        sys.exit(1)

    if not sk_hex:
        print("\n[ERROR] No 'private_key' found under [[Nostr]] in weewx.conf.")
        sys.exit(1)

    try:
        pk = PrivateKey(bytes.fromhex(sk_hex))
    except Exception as e:
        print(f"\n[ERROR] Invalid private_key hex format in config: {e}")
        sys.exit(1)

    geohash = geohash_encode(lat, lon)
    print(f"Found Station: '{station_name}'")
    print(f"Coordinates: {lat}, {lon} (Geohash: {geohash})")

    event = Event(
        public_key=pk.public_key.hex(),
        kind=16158,
        content="",
        tags=[
            ["name", station_name],
            ["description", "Urban Heat Island Asphalt TPMS Monitor (Multi-TPMS via RTL-SDR / WeeWX)"],
            ["g", geohash],
            ["power", "mains"],
            ["connectivity", "ethernet"],  # How the station publishes data to the network, change to wifi or satellite if not wired
            ["sensor", "temp", "Multi-TPMS"],
            ["sensor_status", "temp", "Multi-TPMS", "ok"]
        ]
    )
    pk.sign_event(event)

    payload = json.dumps(["EVENT", {
        "id": event.id,
        "pubkey": event.public_key,
        "created_at": event.created_at,
        "kind": event.kind,
        "tags": event.tags,
        "content": event.content,
        "sig": event.signature
    }])

    relays = [
        "wss://relay.relaying.earth",
        "wss://nos.lol",
        "wss://relay.primal.net",
        "wss://relay.snort.social",
        "wss://bitcoiner.social",
        "wss://relay.wellorder.net",
        "wss://relay.ditto.pub",
        "wss://relay.dreamith.to",
        "wss://relay.damus.io"
    ]

    print(f"\n[BROADCASTING] Registering npub: {pk.public_key.bech32()}...")
    
    for relay_url in relays:
        try:
            ws = create_connection(relay_url, sslopt={"cert_reqs": ssl.CERT_NONE}, timeout=4)
            ws.send(payload)
            response = ws.recv()
            res_data = json.loads(response)
            ws.close()
            
            if res_data[0] == "OK" and res_data[2]:
                print(f"  [SUCCESS] {relay_url} accepted profile")
            else:
                print(f"  [REJECTED] {relay_url}: {res_data[3] if len(res_data)>3 else 'Unknown error'}")
        except Exception:
            print(f"  [FAILED] {relay_url}: Connection dropped")

    print("\nRegistration complete. Your station profile is live on the network.")

if __name__ == "__main__":
    main()
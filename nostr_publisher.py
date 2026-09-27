import json
import ssl
import logging
import time
import weewx
from weewx.engine import StdService
try:
    from websocket import create_connection
    from nostr.event import Event
    from nostr.key import PrivateKey
    NOSTR_AVAILABLE = True
except ImportError:
    NOSTR_AVAILABLE = False

log = logging.getLogger(__name__)

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


class Nostr(StdService):
    """WeeWX service to broadcast TPMS asphalt temps to Nostr relays."""

    def __init__(self, engine, config_dict):
        super().__init__(engine, config_dict)

        if not NOSTR_AVAILABLE:
            log.info("Nostr: Publisher disabled (missing python-nostr or websocket-client libraries).")
            return

        site_dict = config_dict.get('StdRESTful', {}).get('Nostr', {})
        if not str(site_dict.get('enable', 'false')).lower() in ('true', '1', 'yes'):
            log.info("Nostr: Publisher disabled in configuration.")
            return

        self.target_observation = site_dict.get('target_observation', 'outTemp')
        self.geohash_precision = int(site_dict.get('geohash_precision', 7))
        self.broadcast_interval = int(site_dict.get('broadcast_interval', 300))
        self.last_broadcast_time = 0

        # Target Relays configuration with safe fallback for blank entries (excluding blacklisted relay.nostr.info)
        config_relays = site_dict.get('relays', '').strip()
        if config_relays:
            self.relays = [r.strip() for r in config_relays.split(',') if r.strip()]
        else:
            self.relays = [
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

        # Derive station identity from core WeeWX config
        station_dict = config_dict.get('Station', {})
        self.station_name = station_dict.get('location', 'TPMSTempNet Node')
        try:
            lat = float(station_dict['latitude'])
            lon = float(station_dict['longitude'])
            self.geohash = geohash_encode(lat, lon, self.geohash_precision)
        except (KeyError, ValueError) as e:
            log.error(f"Nostr: Missing or invalid Station latitude/longitude: {e}")
            return

        # Private key setup
        sk_hex = site_dict.get('private_key', '').strip()
        if not sk_hex:
            self.pk = PrivateKey()
            print(f"\n[NOSTR SETUP] No private key found. Generated npub: {self.pk.public_key.bech32()}", flush=True)
            print(f"[NOSTR SETUP] Save this hex key to weewx.conf under [[Nostr]] as private_key = '{self.pk.hex()}'\n", flush=True)
            log.info(f"Nostr: Generated new key npub: {self.pk.public_key.bech32()}")
        else:
            try:
                self.pk = PrivateKey(bytes.fromhex(sk_hex))
                # Broadcast the station profile (Kind 16158) automatically on startup
                self.broadcast_station_profile()
            except Exception as e:
                log.error(f"Nostr: Invalid private_key hex format: {e}")
                return

        self.bind(weewx.NEW_LOOP_PACKET, self.new_loop_packet)
        log.info(f"Nostr: Initialized for '{self.target_observation}' with geohash {self.geohash} (Interval: {self.broadcast_interval}s).")

    def broadcast_station_profile(self):
        """Automatically registers the node and its sensors on the map using the exact network schema."""
        event = Event(
            public_key=self.pk.public_key.hex(),
            kind=16158,
            content="",  # Empty content to match network standard
            tags=[
                ["name", self.station_name],
                ["description", "Urban Heat Island Asphalt Network (Multi-TPMS via RTL-SDR / WeeWX)"],
                ["g", self.geohash],
                ["power", "mains"],
                ["connectivity", "ethernet"],  # How the station publishes data to the network, change to wifi or satellite if not wired
                ["sensor", "temp", "Multi-TPMS"],
                ["sensor_status", "temp", "Multi-TPMS", "ok"]
            ]
        )
        self.pk.sign_event(event)
        
        event_dict = {
            "id": event.id, "pubkey": event.public_key, "created_at": event.created_at,
            "kind": event.kind, "tags": event.tags, "content": event.content, "sig": event.signature
        }
        payload = json.dumps(["EVENT", event_dict])

        success = 0
        for relay_url in self.relays:
            try:
                ws = create_connection(relay_url, sslopt={"cert_reqs": ssl.CERT_NONE}, timeout=4)
                ws.send(payload)
                response = ws.recv()
                res_data = json.loads(response)
                ws.close()
                if res_data[0] == "OK" and res_data[2]:
                    success += 1
            except Exception:
                pass
        
        if success > 0:
            log.info(f"Nostr: Automatically registered Station Profile (Kind 16158) to {success} relays.")
            print(f"[NOSTR SETUP] Station '{self.station_name}' registered on the map.", flush=True)

    def new_loop_packet(self, event):
        current_time = time.time()
        
        if current_time - self.last_broadcast_time < self.broadcast_interval:
            return

        packet = event.packet
        temp = packet.get(self.target_observation)
        if temp is None:
            return

        us_units = packet.get('usUnits', weewx.US)
        if us_units == weewx.US:
            temp_c = (float(temp) - 32.0) * 5.0 / 9.0
        else:
            temp_c = float(temp)

        temp_c_str = f"{temp_c:.1f}"
        
        if self.broadcast_event(temp_c_str):
            self.last_broadcast_time = current_time

    def broadcast_event(self, temp_c_str):
        pubkey_hex = self.pk.public_key.hex()
        event = Event(
            public_key=pubkey_hex,
            kind=4223,
            content="",  # Must be empty string to match network standard
            tags=[
                ["d", f"tpms-temp-{self.geohash}"],
                ["a", f"16158:{pubkey_hex}:"],
                ["t", "weather"],
                ["t", "urbanheatisland"],
                ["t", "tpmstempnet"],
                ["temp", temp_c_str, "Multi-TPMS"]  # Matches working station schema: ["temp", value, sensor]
            ]
        )
        self.pk.sign_event(event)

        event_dict = {
            "id": event.id, "pubkey": event.public_key, "created_at": event.created_at,
            "kind": event.kind, "tags": event.tags, "content": event.content, "sig": event.signature
        }

        payload = json.dumps(["EVENT", event_dict])
        success_count = 0

        for relay_url in self.relays:
            try:
                ws = create_connection(relay_url, sslopt={"cert_reqs": ssl.CERT_NONE}, timeout=2)
                ws.send(payload)
                ws.close()
                success_count += 1
            except Exception:
                pass
                
        if success_count > 0:
            print(f"[NOSTR BROADCAST] Sent {temp_c_str}°C to {success_count} relays", flush=True)
            
        return success_count > 0
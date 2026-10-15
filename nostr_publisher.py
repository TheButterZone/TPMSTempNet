import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
import ssl
import struct
import time
from urllib.parse import urlsplit

import weewx
from weewx.engine import StdService

# Nostr event signing (BIP-340 over secp256k1) and WebSocket publishing are
# implemented below using only Python's standard library.

log = logging.getLogger(__name__)

# secp256k1 domain parameters.
_SECP_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_SECP_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_SECP_G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)
_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _point_add(left, right):
    """Add two secp256k1 points; None represents the point at infinity."""
    if left is None:
        return right
    if right is None:
        return left
    x1, y1 = left
    x2, y2 = right
    if x1 == x2 and (y1 != y2 or y1 == 0):
        return None
    if left == right:
        slope = (3 * x1 * x1) * pow(2 * y1, _SECP_P - 2, _SECP_P)
    else:
        slope = (y2 - y1) * pow((x2 - x1) % _SECP_P, _SECP_P - 2, _SECP_P)
    slope %= _SECP_P
    x3 = (slope * slope - x1 - x2) % _SECP_P
    y3 = (slope * (x1 - x3) - y1) % _SECP_P
    return x3, y3


def _point_mul(scalar, point=_SECP_G):
    """Multiply a secp256k1 point by an integer using double-and-add."""
    scalar %= _SECP_N
    result = None
    addend = point
    while scalar:
        if scalar & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        scalar >>= 1
    return result


def _tagged_hash(tag, message):
    tag_hash = hashlib.sha256(tag.encode("ascii")).digest()
    return hashlib.sha256(tag_hash + tag_hash + message).digest()


def _schnorr_sign(message, secret_scalar):
    """Create a BIP-340 Schnorr signature with a zeroed auxiliary input."""
    if len(message) != 32:
        raise ValueError("BIP-340 message must be exactly 32 bytes")
    if not 1 <= secret_scalar < _SECP_N:
        raise ValueError("secp256k1 private scalar is out of range")

    public_point = _point_mul(secret_scalar)
    px = public_point[0].to_bytes(32, "big")
    d = secret_scalar if public_point[1] % 2 == 0 else _SECP_N - secret_scalar
    aux = bytes(32)
    masked_secret = bytes(a ^ b for a, b in zip(d.to_bytes(32, "big"), _tagged_hash("BIP0340/aux", aux)))
    nonce = int.from_bytes(_tagged_hash("BIP0340/nonce", masked_secret + px + message), "big") % _SECP_N
    if nonce == 0:
        raise RuntimeError("BIP-340 generated an invalid zero nonce")
    r_point = _point_mul(nonce)
    k = nonce if r_point[1] % 2 == 0 else _SECP_N - nonce
    rx = r_point[0].to_bytes(32, "big")
    challenge = int.from_bytes(_tagged_hash("BIP0340/challenge", rx + px + message), "big") % _SECP_N
    signature = rx + ((k + challenge * d) % _SECP_N).to_bytes(32, "big")
    if not _schnorr_verify(message, px, signature):
        raise RuntimeError("Internal BIP-340 signature verification failed")
    return signature


def _schnorr_verify(message, public_x, signature):
    """Verify a BIP-340 signature (also used as an internal signing check)."""
    if len(message) != 32 or len(public_x) != 32 or len(signature) != 64:
        return False
    px = int.from_bytes(public_x, "big")
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    if px >= _SECP_P or r >= _SECP_P or s >= _SECP_N:
        return False
    y_squared = (pow(px, 3, _SECP_P) + 7) % _SECP_P
    y = pow(y_squared, (_SECP_P + 1) // 4, _SECP_P)
    if (y * y) % _SECP_P != y_squared:
        return False
    if y & 1:
        y = _SECP_P - y
    public_point = (px, y)
    challenge = int.from_bytes(_tagged_hash("BIP0340/challenge", signature[:32] + public_x + message), "big") % _SECP_N
    result = _point_add(_point_mul(s), _point_mul((_SECP_N - challenge) % _SECP_N, public_point))
    return result is not None and result[1] % 2 == 0 and result[0] == r


def _convert_bits(data, from_bits, to_bits, pad=True):
    accumulator = 0
    bit_count = 0
    result = []
    max_value = (1 << to_bits) - 1
    for value in data:
        if value < 0 or value >> from_bits:
            raise ValueError("Invalid value for bit conversion")
        accumulator = (accumulator << from_bits) | value
        bit_count += from_bits
        while bit_count >= to_bits:
            bit_count -= to_bits
            result.append((accumulator >> bit_count) & max_value)
    if pad:
        if bit_count:
            result.append((accumulator << (to_bits - bit_count)) & max_value)
    elif bit_count >= from_bits or ((accumulator << (to_bits - bit_count)) & max_value):
        raise ValueError("Invalid incomplete bit group")
    return result


def _bech32_polymod(values):
    generators = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for index, generator in enumerate(generators):
            if (top >> index) & 1:
                checksum ^= generator
    return checksum


def _bech32_encode(hrp, payload):
    expanded_hrp = [ord(char) >> 5 for char in hrp] + [0] + [ord(char) & 31 for char in hrp]
    values = expanded_hrp + payload
    polymod = _bech32_polymod(values + [0] * 6) ^ 1
    checksum = [(polymod >> (5 * (5 - index))) & 31 for index in range(6)]
    return hrp + "1" + "".join(_BECH32_CHARSET[value] for value in payload + checksum)


class _PublicKey:
    def __init__(self, x_coordinate):
        self._x = x_coordinate

    def hex(self):
        return self._x.hex()

    def bech32(self):
        return _bech32_encode("npub", _convert_bits(self._x, 8, 5, True))


class PrivateKey:
    """Small in-file replacement for python-nostr's PrivateKey interface."""
    def __init__(self, raw_key=None):
        if raw_key is None:
            while True:
                raw_key = secrets.token_bytes(32)
                scalar = int.from_bytes(raw_key, "big")
                if 1 <= scalar < _SECP_N:
                    break
        if not isinstance(raw_key, bytes) or len(raw_key) != 32:
            raise ValueError("Nostr private key must be exactly 32 bytes")
        scalar = int.from_bytes(raw_key, "big")
        if not 1 <= scalar < _SECP_N:
            raise ValueError("Nostr private key scalar is out of range")
        self._raw_key = raw_key
        public_point = _point_mul(scalar)
        self.public_key = _PublicKey(public_point[0].to_bytes(32, "big"))
        self._scalar = scalar

    def hex(self):
        return self._raw_key.hex()

    def sign_event(self, event):
        event.id = event.calculate_id()
        event.signature = _schnorr_sign(bytes.fromhex(event.id), self._scalar).hex()


class Event:
    """Nostr event object with the fields used by this WeeWX service."""
    def __init__(self, public_key, kind, content="", tags=None, created_at=None):
        self.public_key = public_key
        self.kind = int(kind)
        self.content = content
        self.tags = [] if tags is None else tags
        self.created_at = int(time.time()) if created_at is None else int(created_at)
        self.id = None
        self.signature = None

    def calculate_id(self):
        serialized = json.dumps(
            [0, self.public_key, self.created_at, self.kind, self.tags, self.content],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()


class _WebSocketConnection:
    """Minimal RFC 6455 client supporting wss:// and ws:// relay connections."""
    def __init__(self, url, sslopt=None, timeout=None):
        parsed = urlsplit(url)
        if parsed.scheme not in ("ws", "wss") or not parsed.hostname:
            raise ValueError("WebSocket URL must use ws:// or wss://")
        self._is_tls = parsed.scheme == "wss"
        self._host = parsed.hostname
        self._port = parsed.port or (443 if self._is_tls else 80)
        self._timeout = timeout
        self._buffer = b""
        self._socket = socket.create_connection((self._host, self._port), timeout=timeout)
        self._socket.settimeout(timeout)
        if self._is_tls:
            options = sslopt or {}
            context = ssl.create_default_context()
            if options.get("cert_reqs") == ssl.CERT_NONE:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            self._socket = context.wrap_socket(self._socket, server_hostname=self._host)

        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        host_header = self._host
        if ":" in host_header and not host_header.startswith("["):
            host_header = "[" + host_header + "]"
        if self._port != (443 if self._is_tls else 80):
            host_header += ":" + str(self._port)
        client_key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {client_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "User-Agent: Python-stdlib-WebSocket\r\n\r\n"
        ).encode("ascii")
        self._socket.sendall(request)
        response = self._read_http_headers()
        lines = response.decode("iso-8859-1").split("\r\n")
        if not lines or " 101 " not in (lines[0] + " "):
            self.close()
            raise ConnectionError("WebSocket handshake rejected: " + (lines[0] if lines else "empty response"))
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        expected_accept = base64.b64encode(
            hashlib.sha1((client_key + _WEBSOCKET_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if not hmac.compare_digest(headers.get("sec-websocket-accept", ""), expected_accept):
            self.close()
            raise ConnectionError("Invalid WebSocket handshake accept value")
        if headers.get("upgrade", "").lower() != "websocket":
            self.close()
            raise ConnectionError("WebSocket handshake omitted Upgrade: websocket")

    def _read_http_headers(self):
        while b"\r\n\r\n" not in self._buffer:
            chunk = self._socket.recv(4096)
            if not chunk:
                raise ConnectionError("Connection closed during WebSocket handshake")
            self._buffer += chunk
            if len(self._buffer) > 65536:
                raise ConnectionError("WebSocket handshake headers are too large")
        response, self._buffer = self._buffer.split(b"\r\n\r\n", 1)
        return response

    def _read_exactly(self, length):
        while len(self._buffer) < length:
            chunk = self._socket.recv(max(4096, length - len(self._buffer)))
            if not chunk:
                raise ConnectionError("WebSocket connection closed")
            self._buffer += chunk
        data, self._buffer = self._buffer[:length], self._buffer[length:]
        return data

    def _send_frame(self, opcode, payload=b""):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        length = len(payload)
        first = bytes((0x80 | (opcode & 0x0F),))
        mask_key = os.urandom(4)
        if length < 126:
            header = first + bytes((0x80 | length,))
        elif length <= 0xFFFF:
            header = first + bytes((0x80 | 126,)) + struct.pack("!H", length)
        else:
            header = first + bytes((0x80 | 127,)) + struct.pack("!Q", length)
        masked = bytes(value ^ mask_key[index & 3] for index, value in enumerate(payload))
        self._socket.sendall(header + mask_key + masked)

    def send(self, payload):
        self._send_frame(0x1, payload)

    def _read_frame(self):
        first, second = self._read_exactly(2)
        final = bool(first & 0x80)
        if first & 0x70:
            raise ConnectionError("Unsupported WebSocket RSV bits")
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exactly(8))[0]
        if opcode >= 0x8 and (not final or length > 125):
            raise ConnectionError("Invalid WebSocket control frame")
        mask_key = self._read_exactly(4) if masked else b""
        payload = self._read_exactly(length)
        if masked:
            payload = bytes(value ^ mask_key[index & 3] for index, value in enumerate(payload))
        return final, opcode, payload

    def recv(self):
        fragments = []
        message_opcode = None
        while True:
            final, opcode, payload = self._read_frame()
            if opcode == 0x8:
                raise ConnectionError("WebSocket peer closed the connection")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                if message_opcode is not None:
                    raise ConnectionError("Unexpected new WebSocket data frame")
                message_opcode = opcode
                fragments = [payload]
            elif opcode == 0x0:
                if message_opcode is None:
                    raise ConnectionError("Unexpected WebSocket continuation frame")
                fragments.append(payload)
            else:
                raise ConnectionError("Unsupported WebSocket opcode")
            if final:
                data = b"".join(fragments)
                return data.decode("utf-8") if message_opcode == 0x1 else data

    def close(self):
        sock = getattr(self, "_socket", None)
        if sock is None:
            return
        try:
            if sock.fileno() >= 0:
                try:
                    self._send_frame(0x8, b"")
                except (OSError, ConnectionError):
                    pass
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()
        except OSError:
            pass


def create_connection(url, sslopt=None, timeout=None):
    """Drop-in subset of websocket-client's create_connection used below."""
    return _WebSocketConnection(url, sslopt=sslopt, timeout=timeout)

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

        site_dict = config_dict.get('StdRESTful', {}).get('Nostr', {})

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
                "wss://relay.nostrfy.org",
                "wss://relay.ditto.pub",
                "wss://relay.dreamith.to"
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

import socket
import ssl
import base64
import hashlib
import os
import struct
import json
import time

# --- Pure-Python Secp256k1 & BIP-340 Cryptography ---
P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G_X = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
G_Y = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8

def point_add(P1, P2):
    if P1 is None: return P2
    if P2 is None: return P1
    (x1, y1), (x2, y2) = P1, P2
    if x1 == x2:
        if y1 != y2: return None
        lam = (3 * x1 * x1) * pow(2 * y1, P - 2, P) % P
    else:
        lam = (y2 - y1) * pow(x2 - x1, P - 2, P) % P
    x3 = (lam * lam - x1 - x2) % P
    y3 = (lam * (x1 - x3) - y1) % P
    return (x3, y3)

def point_mul(k, P_pt):
    R = None
    addt = P_pt
    while k > 0:
        if k & 1:
            R = point_add(R, addt)
        addt = point_add(addt, addt)
        k >>= 1
    return R

def pubkey_from_secret(sk_bytes):
    sk_int = int.from_bytes(sk_bytes, 'big')
    pub_pt = point_mul(sk_int, (G_X, G_Y))
    return pub_pt[0].to_bytes(32, 'big')

def tagged_hash(tag_name, msg):
    tag_hash = hashlib.sha256(tag_name.encode('utf-8')).digest()
    return hashlib.sha256(tag_hash + tag_hash + msg).digest()

def schnorr_sign(msg32, sk_bytes):
    sk_int = int.from_bytes(sk_bytes, 'big')
    pub32 = pubkey_from_secret(sk_bytes)
    k_bytes = os.urandom(32)
    k_int = int.from_bytes(k_bytes, 'big') % N
    if k_int == 0: k_int = 1
    R_pt = point_mul(k_int, (G_X, G_Y))
    if R_pt[1] % 2 != 0: k_int = N - k_int
    r32 = R_pt[0].to_bytes(32, 'big')
    e_int = int.from_bytes(tagged_hash("BIP0340/challenge", r32 + pub32 + msg32), 'big') % N
    s_int = (k_int + e_int * sk_int) % N
    return r32 + s_int.to_bytes(32, 'big')

def send_deletion(sk_hex, event_ids, relays):
    sk = bytes.fromhex(sk_hex)
    pubkey_hex = pubkey_from_secret(sk).hex()
    print(f"\nSigning deletion for {len(event_ids)} event(s) using Pubkey: {pubkey_hex}")
    
    tags = [["e", eid.strip()] for eid in event_ids if eid.strip()]
    if not tags:
        print("No valid event IDs provided.")
        return

    created_at = int(time.time())
    content = "Manual station profile deletion"
    
    serialized = json.dumps([0, pubkey_hex, created_at, 5, tags, content], separators=(',', ':'), ensure_ascii=False)
    event_id = hashlib.sha256(serialized.encode('utf-8')).digest()
    sig = schnorr_sign(event_id, sk)

    del_event = {
        "id": event_id.hex(),
        "pubkey": pubkey_hex,
        "created_at": created_at,
        "kind": 5,
        "tags": tags,
        "content": content,
        "sig": sig.hex()
    }
    payload = json.dumps(["EVENT", del_event], separators=(',', ':'), ensure_ascii=False).encode('utf-8')

    for relay_url in relays:
        print(f"Broadcasting to: {relay_url}")
        try:
            host_port = relay_url[6:]
            parts = host_port.split("/", 1)
            hp = parts[0]
            path = "/" + parts[1] if len(parts) > 1 else "/"
            host = hp.split(":")[0]
            port = int(hp.split(":")[1]) if ":" in hp else 443

            sock = socket.create_connection((host, port), timeout=4)
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)

            sec_key = base64.b64encode(os.urandom(16)).decode('utf-8')
            handshake = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {sec_key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n"
                f"\r\n"
            )
            sock.sendall(handshake.encode('utf-8'))
            if b"101 Switching Protocols" not in sock.recv(1024):
                print(f"-> Handshake failed")
                sock.close()
                continue

            payload_len = len(payload)
            frame = bytearray([0x81])
            mask_key = os.urandom(4)
            if payload_len < 126:
                frame.append(0x80 | payload_len)
            elif payload_len < 65536:
                frame.append(0x80 | 126)
                frame.extend(struct.pack("!H", payload_len))
            else:
                frame.append(0x80 | 127)
                frame.extend(struct.pack("!Q", payload_len))
            frame.extend(mask_key)
            masked = bytearray(payload)
            for i in range(payload_len):
                masked[i] ^= mask_key[i % 4]
            frame.extend(masked)
            sock.sendall(frame)
            
            sock.settimeout(2)
            try:
                resp = sock.recv(1024)
                print(f"-> Relay Response: {resp.decode('utf-8', errors='ignore')[:120]}")
            except socket.timeout:
                print(f"-> Sent successfully (no immediate response)")
            sock.close()
        except Exception as e:
            print(f"-> Error: {e}")

if __name__ == "__main__":
    sk_hex = input("Enter your secret (private) key hex: ").strip()
    
    print("\nEnter target Event IDs (paste one per line, then press Ctrl+D or type 'DONE' on a blank line):")
    event_ids = []
    while True:
        try:
            line = input()
            if line.strip().upper() == 'DONE' or not line.strip():
                break
            event_ids.append(line.strip())
        except EOFError:
            break

    if not event_ids:
        print("No event IDs entered. Exiting.")
        exit(1)

    relays = [
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
    send_deletion(sk_hex, event_ids, relays)
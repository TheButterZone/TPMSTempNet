# Run: /weewx/weewx-venv/bin/python3 tools/fetch_event.py eventIDhexhere

import argparse
import json
import ssl
from websocket import create_connection

def fetch_event(event_id, relay_url="wss://relay.relaying.earth"):
    print(f"Connecting to {relay_url}...")
    try:
        ws = create_connection(relay_url, sslopt={"cert_reqs": ssl.CERT_NONE}, timeout=5)
    except Exception as e:
        print(f"Connection failed: {e}")
        return
    
    req = ["REQ", "fetch-sub", {"ids": [event_id]}]
    ws.send(json.dumps(req))
    
    print(f"Querying event ID: {event_id}\nWaiting for response...")
    try:
        while True:
            response = ws.recv()
            data = json.loads(response)
            if data[0] == "EVENT" and data[2]["id"] == event_id:
                print("\n--- Event JSON Found ---")
                print(json.dumps(data[2], indent=2))
                break
            elif data[0] == "EOSE":
                print("End of stored events on this relay (event not found).")
                break
    except Exception as e:
        print(f"Error during receive: {e}")
    finally:
        ws.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch and inspect Nostr events from a relay.")
    parser.add_argument("event_id", help="The hex event ID to retrieve")
    parser.add_argument("--relay", default="wss://relay.relaying.earth", help="Nostr relay URL")
    args = parser.parse_args()
    
    fetch_event(args.event_id, args.relay)
import time
import statistics
import weewx
from weewx.engine import StdService
import logging

log = logging.getLogger(__name__)

class TPMSTempService(StdService):
    def __init__(self, engine, config_dict):
        super(TPMSTempService, self).__init__(engine, config_dict)
        self.bind(weewx.NEW_LOOP_PACKET, self.handle_new_loop)
        
        # Operational Parameters
        service_dict = config_dict.get('TPMSTempService', {})
        self.max_age = int(service_dict.get('max_age', 3600))             # Purge after 1 hour
        self.quarantine_sec = int(service_dict.get('quarantine', 7200))   # 2-hour timeout for hot tires
        self.max_rate = float(service_dict.get('max_rate', 0.11))         # 0.11°C is approx 0.2°F per min
        self.dwell_sec = int(service_dict.get('dwell_sec', 600))          # NEW: 10 min wait for fresh tires
        
        # Sane bounds for the local climate of San Diego, California
        self.min_valid_temp = float(service_dict.get('min_temp', 0.0))
        self.max_valid_temp = float(service_dict.get('max_temp', 46.0))
        
        # State tracking: { 'sensor_id': {'val': 72.5, 'ts': 1690000000, 'first_seen': 168999000, 'quarantined_until': 0} }
        self.sensors = {}
        self.last_good_ambient = None

    def handle_new_loop(self, event):
        packet = event.packet
        now = packet.get('dateTime', time.time())
        tpms_id = packet.get('tpms_id')
        
        # 1. Ingest data and calculate Rate of Change
        for key, value in list(packet.items()):
            if key.startswith('outTemp') and value is not None:
                current_val = float(value)

                # Key by the underlying physical hardware ID if available
                sensor_key = tpms_id if tpms_id else key
                
                if sensor_key in self.sensors:
                    state = self.sensors[sensor_key]
                    dt_minutes = (now - state['ts']) / 60.0
                    
                    # Rate of change check (>1 min to avoid micro-jitter)
                    if dt_minutes > 1.0:
                        rate = (current_val - state['val']) / dt_minutes
                        
                        # If temperature spikes unnaturally fast, quarantine the sensor
                        if rate > self.max_rate:
                            state['quarantined_until'] = now + self.quarantine_sec
                            
                    state['val'] = current_val
                    state['ts'] = now
                else:
                    # First time seeing this sensor
                    self.sensors[sensor_key] = {
                        'val': current_val, 
                        'ts': now, 
                        'first_seen': now, 
                        'quarantined_until': 0
                    }

        # 2. Filter the pool of sensors
        valid_readings = []
        for sensor_key, state in list(self.sensors.items()):
            # Purge stale data
            if (now - state['ts']) > self.max_age:
                del self.sensors[sensor_key]
                continue
                
            # Skip if currently in RoC timeout
            if now < state['quarantined_until']:
                continue

            # Skip if it hasn't passed the initial Dwell time (anti-highway filter)
            if (now - state['first_seen']) < self.dwell_sec:
                continue
                
            # Skip if outside sane climate bounds
            if not (self.min_valid_temp <= state['val'] <= self.max_valid_temp):
                continue
                
            valid_readings.append(state['val'])

        # 3. Publish Median ambient candidate
        n = len(valid_readings)
        ambient = None
        
        if n == 1:
            ambient = valid_readings[0]
        elif n == 2:
            ambient = sum(valid_readings) / 2.0
        elif n >= 3:
            ambient = statistics.median(valid_readings)

        # 4. Write back safely to outTemp for WU, CWOP, and Nostr
        if ambient is not None:
            log.debug(f"TPMSTempService: Computed ambient temperature baseline -> {ambient}°C")
            packet['outTemp'] = ambient
            self.last_good_ambient = ambient
        elif self.last_good_ambient is not None:
            # All sensors are either stale, dwelling, or quarantined; ride out the gap
            packet['outTemp'] = self.last_good_ambient
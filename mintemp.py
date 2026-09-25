import time
import weewx
from weewx.engine import StdService

class MinTempService(StdService):
    def __init__(self, engine, config_dict):
        super(MinTempService, self).__init__(engine, config_dict)
        self.bind(weewx.NEW_LOOP_PACKET, self.handle_new_loop)
        
        # Operational Parameters
        service_dict = config_dict.get('MinTempService', {})
        self.max_age = int(service_dict.get('max_age', 3600))             # Purge after 1 hour
        self.quarantine_sec = int(service_dict.get('quarantine', 7200))   # 2-hour timeout for hot tires
        self.max_rate = float(service_dict.get('max_rate', 0.11))         # 0.11°C is approx 0.2°F per min
        
        # Sane bounds for the local climate of San Diego, California
        self.min_valid_temp = float(service_dict.get('min_temp', 0.0))
        self.max_valid_temp = float(service_dict.get('max_temp', 46.0))
        
        # State tracking: { 'extraTemp1': {'val': 72.5, 'ts': 1690000000, 'quarantined_until': 0} }
        self.sensors = {}
        self.last_good_ambient = None

    def handle_new_loop(self, event):
        packet = event.packet
        print(f"DEBUG: MinTempService intercepted packet! Current memory: {self.sensors}")
        now = packet.get('dateTime', time.time())
        
        # 1. Ingest data and calculate Rate of Change
        for key, value in packet.items():
            if key.startswith('extraTemp') and value is not None:
                current_val = float(value)
                
                if key in self.sensors:
                    state = self.sensors[key]
                    dt_minutes = (now - state['ts']) / 60.0
                    
                    # Calculate rate of change if enough time has passed (>1 min) to avoid micro-jitter
                    if dt_minutes > 1.0:
                        rate = (current_val - state['val']) / dt_minutes
                        
                        # If temperature spikes unnaturally fast, quarantine the sensor
                        if rate > self.max_rate:
                            state['quarantined_until'] = now + self.quarantine_sec
                            
                    state['val'] = current_val
                    state['ts'] = now
                else:
                    # First time seeing this sensor
                    self.sensors[key] = {'val': current_val, 'ts': now, 'quarantined_until': 0}

        # 2. Filter the pool of sensors
        valid_readings = []
        for key, state in list(self.sensors.items()):
            # Purge stale data
            if (now - state['ts']) > self.max_age:
                del self.sensors[key]
                continue
                
            # Skip if currently in timeout
            if now < state['quarantined_until']:
                continue
                
            # Skip if outside sane climate bounds
            if not (self.min_valid_temp <= state['val'] <= self.max_valid_temp):
                continue
                
            valid_readings.append(state['val'])

        # 3. Publish the lowest stable reading, or fallback to the last known good state
        if valid_readings:
            ambient = min(valid_readings)
            packet['outTemp'] = ambient
            self.last_good_ambient = ambient
        elif self.last_good_ambient is not None:
            # All sensors are either stale or quarantined; ride out the gap
            packet['outTemp'] = self.last_good_ambient
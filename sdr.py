#!/usr/bin/env python
# Copyright 2016-2024 Matthew Wall
# 
# Modified 2026 by TheButterZone for TPMSTempNet:
# - Replaced all weather stations with universal TPMS packet class
# - Embedded TPMS state machine with median-centered cluster consensus filtering
# - Added integer quantization bypass and dual-extremity outlier rejection
# - Configured foreground terminal stdout streaming of state machine operation
# - Centralized optimized multi-frequency rtl_433 parameters into DEFAULT_CMD for zero-config deployment
# 
# Distributed under the terms of the GNU Public License (GPLv3)

from __future__ import with_statement
from calendar import timegm
try:
    # Python 3
    import queue
except ImportError:
    # Python 2:
    import Queue as queue
import os
import re
import subprocess
import threading
import time
import copy
import statistics

try:
    import cjson as json
    setattr(json, 'dumps', json.encode)
    setattr(json, 'loads', json.decode)
except (ImportError, AttributeError):
    try:
        import simplejson as json
    except ImportError:
        import json

import weewx.drivers
import weewx.units
from weeutil.weeutil import tobool

try:
    # New-style weewx logging
    import weeutil.logger
    import logging
    log = logging.getLogger(__name__)

    def logdbg(msg):
        log.debug(msg)

    def loginf(msg):
        log.info(msg)

    def logerr(msg):
        log.error(msg)

except ImportError:
    # Old-style weewx logging
    import syslog

    def logmsg(level, msg):
        syslog.syslog(level, 'sdr: %s: %s' %
                      (threading.currentThread().getName(), msg))

    def logdbg(msg):
        logmsg(syslog.LOG_DEBUG, msg)

    def loginf(msg):
        logmsg(syslog.LOG_INFO, msg)

    def logerr(msg):
        logmsg(syslog.LOG_ERR, msg)

# --- DRIVER CONSTANTS & CONFIGURATION ---
DRIVER_NAME = 'SDR'
DRIVER_VERSION = '1.0.0-TPMS'
DEFAULT_CMD = '/usr/local/bin/rtl_433 -M utc -F json -f 315M -f 433.92M -H 15 -s 1024k -g 42.1 -R 0 -R 59 -R 60 -R 82 -R 88 -R 89 -R 90 -R 95 -R 110 -R 123 -R 140 -R 156 -R 168 -R 180 -R 186 -R 201 -R 203 -R 208 -R 212 -R 225 -R 226 -R 241 -R 248 -R 252 -R 257 -R 275 -R 295 -R 298 -R 299 -R 321 -R 322 -R 328 -R 343 -R 352 -R 354 -R 355 -R 362 -R 365 -R 378 -R 380 -R 381'

def loader(config_dict, _):
    return SDRDriver(**config_dict[DRIVER_NAME])

def confeditor_loader():
    return SDRConfigurationEditor()

class AsyncReader(threading.Thread):

    def __init__(self, fd, queue, label):
        threading.Thread.__init__(self)
        self._fd = fd
        self._queue = queue
        self._running = False
        self.daemon = True
        self.name = label

    def run(self):
        logdbg("start async reader for %s" % self.name)
        self._running = True
        for line in self._fd:
            if line:
                self._queue.put(line)
            if not self._running:
                break

    def stop_running(self):
        self._running = False


class ProcManager(object):
    TS = re.compile(r'^\d\d\d\d-\d\d-\d\d \d\d:\d\d:\d\d[\s]+')

    def __init__(self):
        self._cmd = None
        self._process = None
        self.stdout_queue = queue.Queue()
        self.stdout_reader = None
        self.stderr_queue = queue.Queue()
        self.stderr_reader = None

    def startup(self, cmd, path=None, ld_library_path=None):
        self._cmd = cmd
        loginf("startup process '%s'" % self._cmd)
        env = os.environ.copy()
        if path:
            env['PATH'] = path + ':' + env['PATH']
        if ld_library_path:
            env['LD_LIBRARY_PATH'] = ld_library_path
        try:
            self._process = subprocess.Popen(cmd.split(' '),
                                             env=env,
                                             stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE)
            self.stdout_reader = AsyncReader(
                self._process.stdout, self.stdout_queue, 'stdout-thread')
            self.stdout_reader.start()
            self.stderr_reader = AsyncReader(
                self._process.stderr, self.stderr_queue, 'stderr-thread')
            self.stderr_reader.start()
        except (OSError, ValueError) as e:
            raise weewx.WeeWxIOError("failed to start process '%s': %s" %
                                     (cmd, e))

    def shutdown(self):
        loginf('shutdown process %s' % self._cmd)
        self._process.kill()
        logdbg("close stdout")
        self._process.stdout.close()
        logdbg("close stderr")
        self._process.stderr.close()
        logdbg('shutdown %s' % self.stdout_reader.getName())
        self.stdout_reader.stop_running()
        self.stdout_reader.join(0.5)
        logdbg('shutdown %s' % self.stderr_reader.getName())
        self.stderr_reader.stop_running()
        self.stderr_reader.join(0.5)
        if self._process.poll() is None:
            logerr('process did not respond to kill, shutting down anyway')
        self._process = None
        if self.stdout_reader.is_alive():
            loginf('timed out waiting for %s' % self.stdout_reader.getName())
        self.stdout_reader = None
        if self.stderr_reader.is_alive():
            loginf('timed out waiting for %s' % self.stderr_reader.getName())
        self.stderr_reader = None
        loginf('shutdown complete')

    def running(self):
        return self._process.poll() is None

    def get_stderr(self):
        lines = []
        while not self.stderr_queue.empty():
            lines.append(self.stderr_queue.get().decode())
        return lines

    def get_stdout(self):
        lines = []
        while self.running():
            try:
                # Fetch the output line. For it to be searched, Python 3
                # requires that it be decoded to unicode. Decoding does no
                # harm under Python 2:
                line = self.stdout_queue.get(True, 3).decode()
                m = ProcManager.TS.search(line)
                if m and lines:
                    yield lines
                    lines = []
                lines.append(line)
            except queue.Empty:
                yield lines
                lines = []
        yield lines


class Packet:

    def __init__(self):
        pass

    @staticmethod
    def parse_json(obj):
        return None

    TS_PATTERN = re.compile(r'(\d\d\d\d-\d\d-\d\d \d\d:\d\d:\d\d)')

    @staticmethod
    def parse_time(line):
        ts = None
        try:
            m = Packet.TS_PATTERN.search(line)
            if m:
                utc = time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                ts = timegm(utc)
        except Exception as e:
            logerr("parse timestamp failed for '%s': %s" % (line, e))
        return ts

    @staticmethod
    def get_float(obj, key_):
        if key_ in obj:
            try:
                return float(obj[key_])
            except ValueError:
                pass
        return None

    @staticmethod
    def add_identifiers(pkt, sensor_id='', packet_type=''):
        packet = dict()
        if 'dateTime' in pkt:
            packet['dateTime'] = pkt.pop('dateTime', 0)
        if 'usUnits' in pkt:
            packet['usUnits'] = pkt.pop('usUnits', 0)
        for n in pkt:
            packet["%s.%s.%s" % (n, sensor_id, packet_type)] = pkt[n]
        return packet


class UniversalTPMSPacket(Packet):
    @classmethod
    def parse_json(cls, obj):
        # We ensure it's either explicitly marked as TPMS or it at least has temperature_C
        if obj.get('type') != 'TPMS' and 'temperature_C' not in obj:
            return None
            
        pkt = dict()
        if 'time' in obj:
            pkt['dateTime'] = Packet.parse_time(obj.get('time'))
        pkt['usUnits'] = weewx.METRICWX
        
        temp_c = Packet.get_float(obj, 'temperature_C')
        
        # Sanity Filter: Discard missing readings or values outside plausible physical bounds (-30°C to 80°C)
        if temp_c is None or not (-30.0 <= temp_c <= 80.0):
            return None
            
        pkt['temperature'] = temp_c

        # Extract metadata for ephemeral terminal inspection (not saved to logs/disk/cloud)
        sensor_id = str(obj.get('id', 'unknown'))
        model_name = str(obj.get('model', 'Unknown TPMS'))
        
        # Pass the hardware ID so TPMSTempService can isolate physical sensors
        pkt['tpms_id'] = sensor_id
        pkt['tpms_model'] = model_name
        
        # Hardcode the aliased identifier 'sane' and class name 'UniversalTPMSPacket'
        # to match `outTemp = temperature.sane.UniversalTPMSPacket` in weewx.conf
        pkt = Packet.add_identifiers(pkt, 'sane', 'UniversalTPMSPacket')
        return pkt

# List every TPMS model base identifier string rtl_433 outputs
supported_tpms_models = [
    "Steelmate", "Schrader", "Citroen", "Toyota", "Ford", "Renault", 
    "PMV-107J", "Jansite", "Elantra2012", "Abarth", "Hyundai", 
    "SolarTPMS", "Porsche", "AVE", "TyreGuard", "Kia", "EezTire", 
    "Nissan", "BMW", "Airpuxem", "TRW", "GearHive", "SmarTire", 
    "iMars", "Sprinter", "Sefis", "Jeep", "Honda"
]

# Dynamically create and register a packet class for each one
for model_name in supported_tpms_models:
    class_name = model_name.replace('-', '') + "TPMSPacket"
    globals()[class_name] = type(class_name, (UniversalTPMSPacket,), {'IDENTIFIER': model_name})


class PacketFactory(object):

    KNOWN_PACKETS = []

    @staticmethod
    def known_packets():
        if not PacketFactory.KNOWN_PACKETS:
            import sys, inspect
            objs = inspect.getmembers(sys.modules[__name__], inspect.isclass)
            for name, obj in objs:
                if hasattr(obj, 'IDENTIFIER'):
                    PacketFactory.KNOWN_PACKETS.append(obj)
        return PacketFactory.KNOWN_PACKETS

    @staticmethod
    def create(lines):
        # return a list of packets from the specified lines
        while lines:
            pkt = None
            if lines[0].startswith('{'):
                pkt = PacketFactory.parse_json(lines)
                if pkt is None:
                    logdbg("punt unrecognized line '%s'" % lines[0])
                lines.pop(0)
            else:
                lines.pop(0)
            if pkt is not None:
                yield pkt

    @staticmethod
    def parse_json(lines):
        try:
            obj = json.loads(lines[0])
            if 'model' in obj:
                for parser in PacketFactory.known_packets():
                    if obj['model'].find(parser.IDENTIFIER) >= 0:
                        return parser.parse_json(obj)
                logdbg("parse_json: unknown model %s" % obj['model'])
        except ValueError as e:
            logdbg("parse_json failed: %s" % e)
        return None


class SDRConfigurationEditor(weewx.drivers.AbstractConfEditor):
    @property
    def default_stanza(self):
        return """
[SDR]
    # This section is for the software-defined radio driver.

    # The driver to use
    driver = user.sdr

    # How to invoke the rtl_433 command
#    cmd = %s
""" % DEFAULT_CMD


class SDRDriver(weewx.drivers.AbstractDevice):

    TIMESTAMP_MATCH_THRESHHOLD = 1

    def __init__(self, **stn_dict):
        loginf('driver version is %s' % DRIVER_VERSION)
        self._model = stn_dict.get('model', 'SDR')
        loginf('model is %s' % self._model)
        self._log_lines = tobool(stn_dict.get('log_lines', False))
        self._log_unknown = tobool(stn_dict.get('log_unknown_sensors', False))
        self._log_unmapped = tobool(stn_dict.get('log_unmapped_sensors', False))
        self._sensor_map = stn_dict.get('sensor_map', {})
        loginf('sensor map is %s' % self._sensor_map)
        self._ts_delta = stn_dict.get('timestamp_match_threshhold', SDRDriver.TIMESTAMP_MATCH_THRESHHOLD)
        cmd = stn_dict.get('cmd', DEFAULT_CMD)
        path = stn_dict.get('path', None)
        ld_library_path = stn_dict.get('ld_library_path', None)
        self._last_pkt = None # avoid duplicate sequential packets
        self._mgr = ProcManager()
        self._mgr.startup(cmd, path, ld_library_path)

        # --- TPMS STATE MACHINE INIT ---
        self.max_age = int(stn_dict.get('max_age', 3600))             # Purge after 1 hour
        self.quarantine_sec = int(stn_dict.get('quarantine', 7200))   # 2-hour timeout for hot tires
        self.max_rate = float(stn_dict.get('max_rate', 0.11))         # 0.11°C is approx 0.2°F per min
        self.dwell_sec = int(stn_dict.get('dwell_sec', 600))          # NEW: 10 min wait for fresh tires

        # Sane Celsius bounds for the local climate of San Diego, California
        self.min_valid_temp = float(stn_dict.get('min_temp', 0.0))
        self.max_valid_temp = float(stn_dict.get('max_temp', 46.0))
        self.sensors = {}

    def closePort(self):
        self._mgr.shutdown()

    @property
    def hardware_name(self):
        return self._model

    def genLoopPackets(self):
        while self._mgr.running():
            for lines in self._mgr.get_stdout():
                if self._log_lines:
                    loginf("lines: %s" % lines)

                for packet in PacketFactory.create(lines):
                    if not packet:
                        if self._log_unknown:
                            loginf("unparsed: %s" % lines)
                        continue

                    pkt = self.map_to_fields(packet, self._sensor_map)

                    if not pkt:
                        if self._log_unmapped:
                            loginf("unmapped: %s" % packet)
                        continue

                    # ---------------------------------------------------------
                    # TPMS STATE MACHINE CHOKE POINT
                    # ---------------------------------------------------------
                    raw_tpms_id = packet.get('tpms_id.sane.UniversalTPMSPacket')
                    raw_tpms_model = packet.get('tpms_model.sane.UniversalTPMSPacket', 'Unknown')

                    if raw_tpms_id is not None and 'outTemp' in pkt:
                        now = pkt.get('dateTime', time.time())

                        ambient = self._compute_ambient_baseline(
                            pkt['outTemp'],
                            now,
                            raw_tpms_id,
                            model_name=raw_tpms_model
                        )

                        if ambient is None:
                            # No legitimate ambient observation yet.
                            # Drop before duplicate checking or yielding.
                            continue

                        pkt['outTemp'] = ambient

                    # ---------------------------------------------------------
                    # DUPLICATE SUPPRESSION
                    # ---------------------------------------------------------
                    if self._packets_match(pkt, self._last_pkt):
                        continue

                    self._last_pkt = pkt

                    yield pkt

            # report any errors
            for line in self._mgr.get_stderr():
                line = line.strip()
                if line:
                    print(f"[SDR HEALTH ERROR] {line}", flush=True)
        else:
            for line in self._mgr.get_stderr():
                line = line.strip()
                if line:
                    print(f"[SDR HEALTH CRITICAL] {line}", flush=True)
            print("[SDR HEALTH CRITICAL] rtl_433 process exited. Waiting 60 seconds for WeeWX auto-retry...", flush=True)
            raise weewx.WeeWxIOError("rtl_433 process is not running")

    def _compute_ambient_baseline(self, current_val, now, tpms_id, model_name='Unknown'):
        # 1. Ingest, Debounce Rapid Bursts, & Track Rate of Change
        min_burst_interval = 60  # Minimum 60 seconds between identical burst frames from the same ID
        
        dwell_remaining = 0
        if tpms_id in self.sensors:
            state = self.sensors[tpms_id]

            # Burst Check: If value is identical and under 60s, skip duplicate frame
            if current_val == state.get('last_accepted_val') and (now - state.get('last_accepted_ts', 0)) < min_burst_interval:
                print(f"[TPMS FILTER] Debouncing rapid burst duplicate frame from sensor {tpms_id}.", flush=True)
                return state['val']
                
            dt_minutes = (now - state['ts']) / 60.0
            if dt_minutes > 1.0:
                delta = abs(current_val - state['val'])
                rate = delta / dt_minutes

                # Rate-of-Change & Hopping Gap Tolerance:
                # Forgiving 2°C gaps from frequency hopping (delta < 3.0),
                # but aggressively quarantining real warming/driving spikes (delta >= 3.0).
                if rate > self.max_rate and delta >= 3:
                    state['quarantined_until'] = now + self.quarantine_sec
                    print(f"[TPMS FILTER] Sensor {tpms_id} rate spike detected (Δ{delta:.1f}°C). Quarantined.", flush=True)
                    
            state['val'] = current_val
            state['ts'] = now
            state['last_accepted_ts'] = now
            state['last_accepted_val'] = current_val
            dwell_remaining = int(max(0, self.dwell_sec - (now - state['first_seen'])))
        else:
            self.sensors[tpms_id] = {
                'val': current_val, 
                'ts': now, 
                'first_seen': now, 
                'quarantined_until': 0,
                'last_accepted_ts': now,
                'last_accepted_val': current_val
            }
            dwell_remaining = self.dwell_sec

        dwell_str = f"| Dwell: {dwell_remaining}s remaining" if dwell_remaining > 0 else "| Dwell: Expired"
        # Force-flushed print: renders live in the foreground terminal window only.
        # Bypasses the WeeWX logging system entirely, leaving zero persistent traces on disk.
        print(f"[TPMS INTERCEPT] Model: {model_name} | ID: {tpms_id} | Temp: {current_val}°C {dwell_str}", flush=True)

        # 2. Filter pool for valid candidate readings
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

            # Skip if outside sane climate bounds (governed strictly by config max_temp/min_temp)
            if not (self.min_valid_temp <= state['val'] <= self.max_valid_temp):
                continue
                
            valid_readings.append(state['val'])

        if not valid_readings:
            return None

        # 3. Median-Centered Cluster Consensus & Small-Pool Divergence Check
        valid_readings.sort()

	
        # Safeguard for small pools (n = 2): 
        # If we only have two sensors and they diverge by > 4.0°C, the higher
        # one is a cooling tire / warm outlier. Favor the lower (true ambient) reading.
        if len(valid_readings) == 2 and (valid_readings[1] - valid_readings[0] > 4.0):
            print(f"[TPMS FILTER] Small pool divergence ({valid_readings[0]:.1f}°C vs {valid_readings[1]:.1f}°C). Rejecting warm outlier.", flush=True)
            valid_readings = [valid_readings[0]]

        # Find the median of all valid candidates as our gravitational center
        med = statistics.median(valid_readings)

        # Keep only readings clustering closely around the median (±5.0°C)
        cluster_window = 5.0
        filtered_cluster = [v for v in valid_readings if abs(v - med) <= cluster_window]

        if not filtered_cluster:
            return None

        # Compute final ambient baseline from the tight consensus cluster
        n = len(filtered_cluster)
        if n == 1:
            ambient = filtered_cluster[0]
        elif n == 2:
            ambient = sum(filtered_cluster) / 2.0
        else:
            ambient = statistics.median(filtered_cluster)

        if ambient is not None:
            print(f"[TPMS FILTER] Computed median-centered ambient baseline -> {ambient:.1f}°C", flush=True)
            
        return ambient

    def _packets_match(self, pkt1, pkt2):
        # see if two packets match.  this is more than just a direct comparison
        # of packets.  if the data match, but the timestamps are different,
        # then that is considered different, but only if the timestamp is
        # bigger than the sampling period for the hardware.
        if pkt1 != pkt2:
            return False
        a = copy.deepcopy(pkt1)
        b = copy.deepcopy(pkt2)
        a_ts = a.pop('dateTime')
        b_ts = b.pop('dateTime')
        if a != b:
            return False
        if abs(a_ts - b_ts) > self._ts_delta:
            return False
        return True

    @staticmethod
    def map_to_fields(pkt, sensor_map):
        # selectively get elements from the packet using the specified sensor
        # map.  if the identifier is found, then use its value.  if not, then
        # skip it completely (it is not given a None value).  include the
        # time stamp and unit system only if we actually got data.
        packet = dict()
        for n in sensor_map.keys():
            label = SDRDriver._find_match(sensor_map[n], pkt.keys())
            if label:
                packet[n] = pkt.get(label)
        if packet:
            for k in ['dateTime', 'usUnits']:
                packet[k] = pkt[k]
        return packet

    @staticmethod
    def _find_match(pattern, keylist):
        # find the first key in pkt that matches the specified pattern.
        # the general form of a pattern is:
        #   <observation_name>.<sensor_id>.<packet_type>
        # do glob-style matching.
        if pattern in keylist:
            return pattern
        match = None
        pparts = pattern.split('.')
        if len(pparts) == 3:
            for k in keylist:
                kparts = k.split('.')
                if (len(kparts) == 3 and
                    SDRDriver._part_match(pparts[0], kparts[0]) and
                    SDRDriver._part_match(pparts[1], kparts[1]) and
                    SDRDriver._part_match(pparts[2], kparts[2])):
                    match = k
                    break
                elif pparts[0] == k:
                    match = k
                    break
        return match

    @staticmethod
    def _part_match(pattern, value):
        # use glob matching for parts of the tuple
        matches = fnmatch.filter([value], pattern)
        return True if matches else False


def main():
    import optparse
    import syslog

    usage = """%prog [--debug] [--help] [--version]
        [--action=(show-packets | show-detected | list-supported)]
        [--cmd=RTL_CMD] [--path=PATH] [--ld_library_path=LD_LIBRARY_PATH]"""

    syslog.openlog('sdr', syslog.LOG_PID | syslog.LOG_CONS)
    syslog.setlogmask(syslog.LOG_UPTO(syslog.LOG_INFO))
    parser = optparse.OptionParser(usage=usage)
    parser.add_option('--version', dest='version', action='store_true',
                      help='display driver version')
    parser.add_option('--debug', dest='debug', action='store_true',
                      help='display diagnostic information while running')
    parser.add_option('--cmd', dest='cmd', default=DEFAULT_CMD,
                      help='rtl command with options')
    parser.add_option('--path', dest='path',
                      help='value for PATH')
    parser.add_option('--ld_library_path', dest='ld_library_path',
                      help='value for LD_LIBRARY_PATH')
    parser.add_option('--config',
                      help='configuration file with sensor map')
    parser.add_option('--hide', dest='hidden', default='empty',
                      help='output to be hidden')
    parser.add_option('--action', dest='action', default='show-packets',
                      help='actions include show-packets, show-detected, list-supported')

    (options, args) = parser.parse_args()

    if options.version:
        print("sdr driver version %s" % DRIVER_VERSION)
        exit(1)

    if options.debug:
        syslog.setlogmask(syslog.LOG_UPTO(syslog.LOG_DEBUG))

    sensor_map = dict()
    if options.config:
        import weecfg
        config_path, config_dict = weecfg.read_config(options.config)
        sensor_map = config_dict.get('SDR', {}).get('sensor_map', {})

    if options.action == 'list-supported':
        pkt_names = PacketFactory.known_packets()
        print("%s known packet types" % len(pkt_names))
        for pt in pkt_names:
            print("%s '%s'" % (pt.__name__, pt.IDENTIFIER))
    elif options.action == 'show-detected':
        # display identifiers for detected sensors
        mgr = ProcManager()
        mgr.startup(options.cmd, path=options.path,
                    ld_library_path=options.ld_library_path)
        detected = dict()
        for lines in mgr.get_stdout():
            # print("out: %s" % lines)
            for p in PacketFactory.create(lines):
                if p:
                    del p['usUnits']
                    del p['dateTime']
                    keys = p.keys()
                    label = re.sub(r'^[^\.]+', '', keys[0])
                    if label not in detected:
                        detected[label] = 0
                    detected[label] += 1
                print(detected)
    else:
        hidden = [x.strip() for x in options.hidden.split(',')]
        mgr = ProcManager()
        mgr.startup(options.cmd, path=options.path,
                    ld_library_path=options.ld_library_path)
        for lines in mgr.get_stdout():
            if 'out' not in hidden and (
                    'empty' not in hidden or len(lines)):
                print("out: %s" % lines)
            for p in PacketFactory.create(lines):
                if p:
                    if 'parsed' not in hidden:
                        print('parsed: %s' % p)
                    if sensor_map:
                        m = SDRDriver.map_to_fields(p, sensor_map)
                        if m:
                            if 'mapped' not in hidden:
                                print('mapped: %s' % m)
        for line in mgr.get_stderr():
            line = line.rstrip()
            print("err: %s" % line)


if __name__ == '__main__':
    main()
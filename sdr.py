#!/usr/bin/env python
# Copyright 2016-2024 Matthew Wall
# Distributed under the terms of the GNU Public License (GPLv3)
"""
Collect data from stl-sdr.  Run rtl_433 on a thread and push the output onto
a queue.

The SDR detects many different sensors and sensor types, so this driver
includes a mechanism to filter the incoming data, and to map the filtered
data onto the weewx database schema and identify the type of data from each
sensor.

Sensors are filtered based on a tuple that identifies uniquely each sensor.
A tuple consists of the observation name, a unique identifier for the hardware,
and the packet type, separated by periods:

  <observation_name>.<hardware_id>.<packet_type>

The filter and data types are specified in a sensor_map stanza in the driver
stanza.  For example:

[SDR]
    driver = user.sdr
    [[sensor_map]]
        inTemp = temperature.25A6.AcuriteTowerPacket
        outTemp = temperature.24A4.AcuriteTowerPacket
        rain_total = rain_total.A52B.Acurite5n1Packet

If no sensor_map is specified, no data will be collected.

The deltas stanza indicates which observations are cumulative measures and
how they should be split into delta measures.

[SDR]
    ...
    [[deltas]]
        rain = rain_total

In this case, the value for rain will be a delta calculated from sequential
rain_total observations.

To identify sensors, run the driver directly.  Alternatively, use the options
log_unknown_sensors and log_unmapped_sensors to see data from the SDR that are
not yet recognized by your configuration.

[SDR]
    driver = user.sdr
    log_unknown_sensors = True
    log_unmapped_sensors = True

The default for each of these is False.

In addition, there are two options to more granularly log debugging
information that can be helpful in bringing up a new system that may not
have sensor models known to this driver.

    log_packets            = False
    log_duplicate_readings = False

The default for these is True.  When debug=1, the default behavior is to
log the assembled packets as well as any duplicate received readings.  Many
sensors send duplicate messages that rtl_433 will read.  Setting these False
will suppress those messages from being logged if you otherwise want to see
normal debug=1 messages.

Eventually we would prefer to have all rtl_433 output as json.  Unfortunately,
many of the rtl_433 decoders do not emit this format yet (as of January 2017).
So this driver is designed to look for json first, then fall back to single-
or multi-line plain text format.

Battery Status

In the weewx database, a battery status of 1 indicates low battery.  This has
origins in the original battery indicators from davis vantage stations.  Some
devices report 'battery' where a value of 1 indicates that the battery is ok,
i.e., the battery is *not* low.  The rtl_433 output has been changed recently
to make this less ambiguous, so many devices now report 'battery_ok' instead
of just 'battery'.  There were also cases where the 'battery' value was a
string, typically just 'OK'.  FWIW, user fgonza2 reports that the Acurite low
battery indicator kicks in when the voltage hits about 4V.

WARNING: Handling of units and unit systems in rtl_433 is a mess, but it is
getting better.  Although there is an option to request SI units, there is no
indicate in the decoder output whether that option is respected, nor does
rtl_433 specify exactly which SI units are used for various types of measure.
There seems to be a pattern of appending a unit label to the observation name
in the JSON data, for example 'wind_speed_mph' instead of just 'wind_speed'.
"""

# FIXME: deprecate then eliminate the V2 acurite packets - single packet def
# can recognize anything rtl_433 spits out

from __future__ import with_statement
from calendar import timegm
try:
    # Python 3
    import queue
except ImportError:
    # Python 2:
    import Queue as queue
import fnmatch
import os
import re
import subprocess
import threading
import time
import copy

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

DRIVER_NAME = 'SDR'
DRIVER_VERSION = '0.96b1'

# The default command requests json output from every decoder
# Use the -R option to indicate specific decoders

# -q      - suppress non-data messages (for older versions of rtl_433)
# -M utc  - print timestamps in UTC (-U for older versions of rtl_433)
# -F json - emit data in json format (not all rtl_433 decoders support this)
# -G      - emit data for all rtl decoders (only available in newer rtl_433)
#           as of early 2020, the syntax is '-G4', but use only for testing

# very old implmentations:
#DEFAULT_CMD = 'rtl_433 -q -U -F json -G'
# as of dec2018:
#DEFAULT_CMD = 'rtl_433 -M utc -F json -G'
# as of feb2020:
DEFAULT_CMD = 'rtl_433 -M utc -F json'

def loader(config_dict, _):
    return SDRDriver(**config_dict[DRIVER_NAME])

def confeditor_loader():
    return SDRConfigurationEditor()


# utilities for inline unit conversions.  respect the None!
def to_F(v):
    if v is not None:
        v  = v * 1.8 + 32
    return v

def to_C(v):
    if v is not None:
        v  = 5 / 9 * (v - 32)
    return v

def to_mph(v):
    if v is not None:
        v *= 0.621371
    return v

def to_in(v):
    if v is not None:
        v /= 25.4
    return v

def to_v(v):
    if v is not None:
        v /= 1000
    return v

def kmh_to_mps(v):
    if v is not None:
        v /= 3.6
    return v


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
    def parse_text(ts, payload, lines):
        return None

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
    def get_int(obj, key_):
        if key_ in obj:
            try:
                return int(obj[key_])
            except ValueError:
                pass
        return None

    @staticmethod
    def get_battery(obj):
        # deal with boolean battery status indicators.  older rtl_433 used a
        # field called 'battery' with string 'OK'.  later rtl_433 uses a field
        # called 'battery_ok' with value 1 to indicate battery is ok and 0 to
        # indicate battery is not ok.  at one point rtl_433 was emitting a
        # field called 'battery_low', so check for that too.
        #
        # for some sensors we see 'battery_ok' with a floating value.  not sure
        # what this means.
        #
        # map these to the WeeWX convention of boolean battery indicators of
        # 0=OK 1=notOK
        bs = None
        if 'battery_ok' in obj:
            level = Packet.get_float(obj, 'battery_ok')
            if level is not None:
                bs = 0 if level >= 1.0 else 1
        elif 'battery' in obj:
            bs = 0 if obj.get('battery') == 'OK' else 1
        elif 'battery_low' in obj:
            bs = Packet.get_int(obj, 'battery_low')
        return bs

    @staticmethod
    def parse_lines(lines, parseinfo=None):
        # parse each line, splitting on colon for name:value
        # tuple in parseinfo is label, pattern, lambda
        # if there is a label, use it to transform the name
        # if there is a pattern, use it to match the value
        # if there is a lamba, use it to convert the value
        if parseinfo is None:
            parseinfo = dict()
        packet = dict()
        for line in lines[1:]:
            if line.count(':') == 1:
                try:
                    (name, value) = [x.strip() for x in line.split(':')]
                    if name in parseinfo:
                        if parseinfo[name][1]:
                            m = parseinfo[name][1].search(value)
                            if m:
                                value = m.group(1)
                            else:
                                logdbg("regex failed for %s:'%s'" %
                                       (name, value))
                        if parseinfo[name][2]:
                            value = parseinfo[name][2](value)
                        if parseinfo[name][0]:
                            name = parseinfo[name][0]
                        packet[name] = value
                    else:
                        logdbg("ignoring %s:%s" % (name, value))
                except Exception as e:
                    logerr("parse failed for line '%s': %s" % (line, e))
            else:
                logdbg("skip line '%s'" % line)
        while lines:
            lines.pop(0)
        return packet

    @staticmethod
    def add_identifiers(pkt, sensor_id='', packet_type=''):
        # qualify each field name with details about the sensor.  not every
        # sensor has all three fields.
        # observation.<sensor_id>.<packet_type>
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
        sensor_id = obj.get('id', 'unknown')
        pkt['temperature'] = Packet.get_float(obj, 'temperature_C')
        
        # If the packet is missing a temperature reading, discard it
        if pkt['temperature'] is None:
            return None
            
        pkt = Packet.add_identifiers(pkt, sensor_id, cls.__name__)
        return pkt


# List every TPMS model base identifier string rtl_433 outputs
supported_tpms_models = 
[
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

    # known packets will be lazy-loaded by introspecting at first request
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
                pkt = PacketFactory.parse_text(lines)
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

    @staticmethod
    def parse_text(lines):
        ts, payload = PacketFactory.parse_firstline(lines[0])
        if ts and payload:
            logdbg("parse_text: ts=%s payload=%s" % (ts, payload))
            for parser in PacketFactory.known_packets():
                if payload.find(parser.IDENTIFIER) >= 0:
                    pkt = parser.parse_text(ts, payload, lines)
                    logdbg("pkt=%s" % pkt)
                    return pkt
            logdbg("parse_text: unknown format: ts=%s payload=%s" %
                   (ts, payload))
        logdbg("parse_text failed: ts=%s payload=%s line=%s" %
               (ts, payload, lines[0]))
        lines.pop(0)
        return None

    TS_PATTERN = re.compile(r'(\d\d\d\d-\d\d-\d\d \d\d:\d\d:\d\d)[\s]+:*(.*)')

    @staticmethod
    def parse_firstline(line):
        ts = payload = None
        try:
            m = PacketFactory.TS_PATTERN.search(line)
            if m:
                utc = time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                ts = timegm(utc)
                payload = m.group(2).strip()
        except Exception as e:
            logerr("parse timestamp failed for '%s': %s" % (line, e))
        return ts, payload


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

    # The sensor map associates observations with database fields.  Each map
    # element consists of a tuple on the left and a database field name on the
    # right.  The tuple on the left consists of:
    #
    #   <observation_name>.<sensor_identifier>.<packet_type>
    #
    # The sensor_identifier is hardware-specific.  For example, Acurite sensors
    # have a 4 character hexadecimal identifier, whereas fine offset sensor
    # clusters have a 4 digit identifier.
    #
    # glob-style pattern matching is supported for the sensor_identifier.
    #
# map data from any fine offset sensor cluster to database field names
#    [[sensor_map]]
#        windGust = wind_gust.*.FOWH1080Packet
#        outBatteryStatus = battery.*.FOWH1080Packet
#        rain_total = rain_total.*.FOWH1080Packet
#        windSpeed = wind_speed.*.FOWH1080Packet
#        windDir = wind_dir.*.FOWH1080Packet
#        outHumidity = humidity.*.FOWH1080Packet
#        outTemp = temperature.*.FOWH1080Packet

""" % DEFAULT_CMD


class SDRDriver(weewx.drivers.AbstractDevice):

    # map the counter total to the counter delta.  for example, the pair
    #   rain:rain_total
    # will result in a delta called 'rain' from the cumulative 'rain_total'.
    # these are applied to mapped packets.
    DEFAULT_DELTAS = {
        'rain': 'rain_total',
        'lightning_strike_count': 'strikes_total'}

    # what is the difference in timestamp values at which we consider two
    # data samples to be different?  some hardware emits duplicate data, and
    # sometimes the rtl_433/rtl-sdr emits duplicate data.  when all of the
    # data are identical, including timestamp, we can easily reject duplicates.
    # when the data are identical but vary only by timestamp, then we can
    # reject duplicates only if the difference in timestamp is smaller than
    # the sensor sampling period.  there is no way to know this, so we make
    # that number a configurable option.  the default is 1 second.
    TIMESTAMP_MATCH_THRESHHOLD = 1

    def __init__(self, **stn_dict):
        loginf('driver version is %s' % DRIVER_VERSION)
        self._model = stn_dict.get('model', 'SDR')
        loginf('model is %s' % self._model)
        self._log_lines = tobool(stn_dict.get('log_lines', False))
        self._log_unknown = tobool(stn_dict.get('log_unknown_sensors', False))
        self._log_unmapped = tobool(stn_dict.get('log_unmapped_sensors', False))
        self._log_packets  = tobool(stn_dict.get('log_packets', True))
        self._log_dups     = tobool(stn_dict.get('log_duplicate_readings', True))
        self._sensor_map = stn_dict.get('sensor_map', {})
        loginf('sensor map is %s' % self._sensor_map)
        self._deltas = stn_dict.get('deltas', SDRDriver.DEFAULT_DELTAS)
        loginf('deltas is %s' % self._deltas)
        self._ts_delta = stn_dict.get('timestamp_match_threshhold', SDRDriver.TIMESTAMP_MATCH_THRESHHOLD)
        self._counter_values = dict()
        cmd = stn_dict.get('cmd', DEFAULT_CMD)
        path = stn_dict.get('path', None)
        ld_library_path = stn_dict.get('ld_library_path', None)
        self._last_pkt = None # avoid duplicate sequential packets
        self._mgr = ProcManager()
        self._mgr.startup(cmd, path, ld_library_path)

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
                    if packet:
                        pkt = self.map_to_fields(packet, self._sensor_map)
                        if pkt:
                            if not self._packets_match(pkt, self._last_pkt):
                                if self._log_packets:
                                    logdbg("packet=%s" % pkt)
                                self._last_pkt = pkt
                                self._calculate_deltas(pkt)
                                yield pkt
                            else:
                                if self._log_dups:
                                    logdbg("ignoring duplicate packet %s" % pkt)
                        elif self._log_unmapped:
                            loginf("unmapped: %s" % packet)
                    elif self._log_unknown:
                        loginf("unparsed: %s" % lines)
            # report any errors
            for line in self._mgr.get_stderr():
                logerr(line)
        else:
            for line in self._mgr.get_stderr():
                logerr(line)
            raise weewx.WeeWxIOError("rtl_433 process is not running")

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

    def _calculate_deltas(self, pkt):
        for k in self._deltas:
            label = self._deltas[k]
            if label in pkt:
                pkt[k] = self._calculate_delta(
                    label, pkt[label], self._counter_values.get(label))
                self._counter_values[label] = pkt[label]

    @staticmethod
    def _calculate_delta(label, newtotal, oldtotal):
        delta = None
        if newtotal is not None and oldtotal is not None:
            if newtotal >= oldtotal:
                delta = newtotal - oldtotal
            else:
                loginf("%s decrement ignored:"
                       " new: %s old: %s" % (label, newtotal, oldtotal))
        return delta

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
        [--cmd=RTL_CMD] [--path=PATH] [--ld_library_path=LD_LIBRARY_PATH]

Actions:
  show-packets: display each packet (default)
  show-detected: display a running count of the number of each packet type
  list-supported: show a list of the supported packet types

Hide:
  This is a comma-separate list of the types of data that should not be
  displayed.  Default is to show everything."""

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
                      help='output to be hidden as comma-delimited list: out, parsed, unparsed, mapped, unmapped, empty')
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
        # display output and parsed/unparsed packets
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
                        else:
                            if 'unmapped' not in hidden:
                                print('unmapped: %s' % p)
                else:
                    if 'unparsed' not in hidden and (
                            'empty' not in hidden or len(lines)):
                        print("unparsed: %s" % lines)
        for line in mgr.get_stderr():
            line = line.rstrip()
            print("err: %s" % line)


if __name__ == '__main__':
    main()
#!/usr/bin/env python3
import os
import time
import datetime
import json
import logging
import math
import re
import http.client
import urllib.error
import urllib.request
import threading
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import paho.mqtt.client as mqtt
from prometheus_client import start_http_server, Gauge

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# --- CONFIGURATION ---
# Base station IP -> port of its Home Assistant proxy, as "ip:port,ip:port"
if not os.getenv("PROXY_PORTS"):
    raise SystemExit("PROXY_PORTS is not set, expected ip:port,ip:port")
PROXY_PORTS = {
    ip.strip(): int(port)
    for ip, port in (entry.rsplit(":", 1) for entry in os.environ["PROXY_PORTS"].split(","))
}
PROXY_BIND = os.getenv("PROXY_BIND", "0.0.0.0")
PORT = 9121
SCRAPE_INTERVAL = 60  # seconds, same pace as the Home Assistant integration
UPSTREAM_TIMEOUT = 5  # seconds
CACHE_MAX_AGE = 300  # seconds; after this the proxy answers 503 instead of stale data
WRITE_SETTLE = 2  # seconds a base gets to apply a change before it is read back
WRITE_RECHECK = 4  # seconds until a second read-back, for slow changes
WRITE_READ_WAIT = 5  # seconds a proxy GET or MQTT command may wait for the read-back after a write
TEMP_LIMITS = (5.0, 30.0)  # degrees; outer bounds of a room's own limits, shown for read-only rooms
TEMP_STEP = 0.2  # degrees
OFFSET_LIMITS = (-2.0, 2.0)  # kelvin
OFFSET_STEP = 0.1  # kelvin
MAX_COMMAND_SIZE = 32  # bytes of an MQTT command payload; HA can send 20.400000000000002
PRESETS = {"auto": 0, "day": 1, "night": 2}  # HEATAREA_MODE values
ROOM_NUMBERS = {  # MQTT setting -> (XML field, entity name), as on the base's web interface
    "heat_day": ("T_HEAT_DAY", "Heat Day"),
    "heat_night": ("T_HEAT_NIGHT", "Heat Night"),
    "cool_day": ("T_COOL_DAY", "Cool Day"),
    "cool_night": ("T_COOL_NIGHT", "Cool Night"),
    "target_min": ("T_TARGET_MIN", "Target Minimum"),
    "target_max": ("T_TARGET_MAX", "Target Maximum"),
    "offset": ("OFFSET", "Temperature Offset"),
}

# Read-only values for monitoring. These are explicit lists because static.xml also holds
# the cloud password and the lock and expert codes, which must never be published.
# (key, XML path, entity name, unit)
SYSTEM_DETAILS = (
    ("system_mode", "Device/MODE", "Operating Mode", None),
    ("cooling", "Device/COOLING", "Cooling", None),
    ("changeover", "Device/CHANGEOVER", "Changeover", None),
    ("changeover_mode", "Device/CHANGEOVER_FUNC/CHANGEOVER_FUNC_MODE", "Changeover Function", None),
    ("actor_mode", "Device/OPERATIONMODE_ACTOR", "Actor Operating Mode", None),
    ("antifreeze", "Device/ANTIFREEZE", "Antifreeze", None),
    ("antifreeze_temp", "Device/ANTIFREEZE_TEMP", "Antifreeze Temperature", "°C"),
    ("first_open_time", "Device/FIRSTOPEN_TIME", "First Open Time", "min"),
    ("smart_start", "Device/SMARTSTART", "Smart Start", None),
    ("lock_mode", "Device/LOCK_MODE", "Lock Mode", None),
    ("tps", "Device/TPS", "TPS", None),
    ("limiter", "Device/LIMITER", "Limiter", None),
    ("ntp_sync", "Device/NTPTIMESYNC", "NTP Time Sync", None),
    ("eco_diff", "Device/ECO_DIFF", "Eco Difference", "K"),
    ("eco_input_mode", "Device/ECO_INPUTMODE", "Eco Input Mode", None),
    ("eco_input_state", "Device/ECO_INPUT_STATE", "Eco Input State", None),
    ("vacation_state", "Device/VACATION/VACATION_STATE", "Vacation State", None),
    ("pump_scope", "Device/PUMP_OUTPUT/LOCALGLOBAL", "Pump Scope", None),
    ("pump_output_type", "Device/PUMP_OUTPUT/PUMP_OUTPUT_TYPE", "Pump Output Type", None),
    ("pump_lead_time", "Device/PUMP_OUTPUT/PUMP_LEADTIME", "Pump Lead Time", "min"),
    ("pump_stopping_time", "Device/PUMP_OUTPUT/PUMP_STOPPINGTIME", "Pump Stopping Time", "min"),
    ("pump_operation_mode", "Device/PUMP_OUTPUT/PUMP_OPERATIONMODE", "Pump Operation Mode", None),
    ("pump_min_runtime", "Device/PUMP_OUTPUT/MINRUNTIME", "Pump Minimum Runtime", "min"),
    ("pump_min_standstill", "Device/PUMP_OUTPUT/MINSTANDSTILL", "Pump Minimum Standstill", "min"),
    ("relais_function", "Device/RELAIS/FUNCTION", "Relay Function", None),
    ("relais_lead_time", "Device/RELAIS/RELAIS_LEADTIME", "Relay Lead Time", "min"),
    ("relais_stopping_time", "Device/RELAIS/RELAIS_STOPPINGTIME", "Relay Stopping Time", "min"),
    ("relais_operation_mode", "Device/RELAIS/RELAIS_OPERATIONMODE", "Relay Operation Mode", None),
    ("relais_boiler_mode", "Device/RELAIS/RELAIS_BOILERMODE", "Relay Boiler Mode", None),
    ("emergency_time", "Device/EMERGENCYMODE/EMERGENCYMODE_TIME", "Emergency Mode Time", "min"),
    ("pwm_cycle", "Device/EMERGENCYMODE/PWMCYCLE", "PWM Cycle", "min"),
    ("pwm_heat", "Device/EMERGENCYMODE/PWMHEAT", "PWM Heat", "%"),
    ("pwm_cool", "Device/EMERGENCYMODE/PWMCOOL", "PWM Cool", "%"),
    ("valve_protect_time", "Device/VALVEPROTECT/VALVEPROTECT_TIME", "Valve Protection Interval", "d"),
    ("valve_protect_duration", "Device/VALVEPROTECT/DURATION", "Valve Protection Duration", "min"),
    ("pump_protect_time", "Device/PUMPPROTECT/PUMPPROTECT_TIME", "Pump Protection Interval", "d"),
    ("pump_protect_duration", "Device/PUMPPROTECT/DURATION", "Pump Protection Duration", "min"),
)
SYSTEM_TEXT_DETAILS = (
    ("firmware_stm", "Device/VERS_SW_STM", "Firmware STM"),
    ("firmware_eth", "Device/VERS_SW_ETH", "Firmware Ethernet"),
    ("hardware", "Device/VERS_HW", "Hardware Version"),
    ("vacation_start_time", "Device/VACATION/START_TIME", "Vacation Start Time"),
    ("vacation_end_time", "Device/VACATION/END_TIME", "Vacation End Time"),
)
ROOM_DETAILS = (
    ("target_base", "T_TARGET_BASE", "Target Base", "°C"),
    ("temp_actual_ext", "T_ACTUAL_EXT", "External Temperature", "°C"),
    ("program_source", "PROGRAM_SOURCE", "Program Source", None),
    ("program_week", "PROGRAM_WEEK", "Program Week", None),
    ("program_weekend", "PROGRAM_WEEKEND", "Program Weekend", None),
    ("party", "PARTY", "Party", None),
    ("party_remaining", "PARTY_REMAININGTIME", "Party Remaining Time", None),
    ("rpm_motor", "RPM_MOTOR", "Motor RPM", None),
    ("floor_day", "T_FLOOR_DAY", "Floor Temperature Day", None),
    ("floor_max", "T_FLOOR_MAX", "Floor Temperature Maximum", None),
    ("heating_system", "HEATINGSYSTEM", "Heating System", None),
    ("block_hc", "BLOCK_HC", "Mode Lock", None),
    ("is_locked", "ISLOCKED", "Locked", None),
    ("lock_available", "LOCK_AVAILABLE", "Lock Available", None),
    ("light", "LIGHT", "Display Light", None),
    ("sensor_ext", "SENSOR_EXT", "External Sensor", None),
    ("target_adjustable", "T_TARGET_ADJUSTABLE", "Target Adjustable", None),
)

VACATION_COMMANDS = ("start", "end", "clear", "temp")
VACATION_DATES = (("start", "START_DATE", "Vacation Start"), ("end", "END_DATE", "Vacation End"))
VACATION_DATE_PATTERN = r"\d{4}-\d{2}-\d{2}"
VACATION_DOTTED_PATTERN = r"(\d{1,2})\.(\d{1,2})\.(\d{4})"  # DD.MM.YYYY, as on the web interface
VACATION_UNSET = "2000-00-00"  # what the base's web interface writes to remove a vacation

SECRET_FIELDS = re.compile(rb"(<(PASSWORD|EXPERT|LOCK_CODE)\b[^>]*(?<!/)>)(.*?)(</\2\s*>)", re.DOTALL)

STATIC_PATH = "/data/static.xml"
CHANGES_PATH = "/data/changes.xml"

# MQTT Configuration
MQTT_BROKER = os.getenv("MQTT_BROKER")
MQTT_PORT = 1883
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASS = os.getenv("MQTT_PASS")
DISCOVERY_PREFIX = "homeassistant"

# --- PROMETHEUS METRICS ---
error_count_gauge = Gauge('moehlenhoff_error_count', 'Number of system errors', ['ip'])
pump_active_gauge = Gauge('moehlenhoff_pump_active', 'Pump active (0/1)', ['ip'])
relais_active_gauge = Gauge('moehlenhoff_relais_active', 'Relay active (0/1)', ['ip'])
temp_actual_gauge = Gauge('moehlenhoff_temp_actual_celsius', 'Current temperature', ['ip', 'room'])
temp_target_gauge = Gauge('moehlenhoff_temp_target_celsius', 'Target temperature', ['ip', 'room'])
heatarea_state_gauge = Gauge('moehlenhoff_heatarea_state', 'Heating zone active/state', ['ip', 'room'])
presence_gauge = Gauge('moehlenhoff_presence', 'Presence in room (0/1)', ['ip', 'room'])
actor_percent_gauge = Gauge('moehlenhoff_valve_open_percent', 'Valve opening in percent', ['ip', 'room', 'heatctrl_id'])
battery_gauge = Gauge('moehlenhoff_iodevice_battery', 'Battery status', ['ip', 'room', 'device_id'])
signal_gauge = Gauge('moehlenhoff_iodevice_signal_strength', 'Signal strength', ['ip', 'room', 'device_id'])
system_detail_gauge = Gauge('moehlenhoff_system_detail', 'Read-only system setting', ['ip', 'name'])
room_detail_gauge = Gauge('moehlenhoff_room_detail', 'Read-only room setting', ['ip', 'room', 'name'])
host_up_gauge = Gauge(
    'moehlenhoff_up',
    'Whether the Moehlenhoff host responded with valid XML (1=up, 0=down)',
    ['ip'],
)

data_gauges = (
    error_count_gauge,
    pump_active_gauge,
    relais_active_gauge,
    temp_actual_gauge,
    temp_target_gauge,
    heatarea_state_gauge,
    presence_gauge,
    actor_percent_gauge,
    battery_gauge,
    signal_gauge,
    system_detail_gauge,
    room_detail_gauge,
)
metric_labels_by_ip = {gauge: {} for gauge in data_gauges}


def set_gauge(gauge, label_values, value):
    gauge.labels(*label_values).set(value)
    metric_labels_by_ip[gauge].setdefault(label_values[0], set()).add(label_values)


def clear_host_metrics(ip):
    for gauge, labels_by_ip in metric_labels_by_ip.items():
        for label_values in labels_by_ip.pop(ip, set()):
            gauge.remove(*label_values)


def mask_secrets(body):
    """Zero out secrets for proxy clients; same length, so the XML parses as before."""
    return SECRET_FIELDS.sub(lambda match: match[1] + b"0" * len(match[3]) + match[4], body)


def read_float(element, path, ip, description):
    raw_value = element.findtext(path)
    if raw_value is None or not raw_value.strip():
        logger.warning("%s: missing value for %s", ip, description)
        return None

    try:
        value = float(raw_value)
    except ValueError:
        logger.warning("%s: invalid value %r for %s", ip, raw_value, description)
        return None

    if not math.isfinite(value):
        logger.warning("%s: non-finite value %r for %s", ip, raw_value, description)
        return None
    return value


def temperature_limits(room, ip):
    """Lowest and highest target of a room, or None if the base cannot set it.

    Rooms with analog thermostats are set on the thermostat and report 0.0 for both.
    """
    name = room.findtext("HEATAREA_NAME")
    low = read_float(room, "T_TARGET_MIN", ip, f"{name} minimum target")
    high = read_float(room, "T_TARGET_MAX", ip, f"{name} maximum target")
    if low is None or high is None or low >= high:
        return None
    return low, high


def number_range(setting, limits):
    """Allowed values of a room number; the minimum has to stay below the maximum."""
    low, high = limits
    if setting == "offset":
        return OFFSET_LIMITS
    if setting == "target_min":
        return TEMP_LIMITS[0], round(high - TEMP_STEP, 1)
    if setting == "target_max":
        return round(low + TEMP_STEP, 1), TEMP_LIMITS[1]
    return limits


def checked_number(payload, value_range, ip):
    try:
        number = round(float(payload), 1) + 0.0  # adding 0.0 turns -0.0 into 0.0
    except (ValueError, OverflowError):
        logger.warning("%s: ignoring invalid number %r", ip, payload)
        return None

    low, high = value_range
    if not low <= number <= high:
        logger.warning("%s: ignoring number %r outside %s to %s", ip, payload, low, high)
        return None
    return f"{number:.1f}"


def temperature_changes(room, root, payload, ip):
    """Same fields the Home Assistant integration writes for a new target."""
    limits = temperature_limits(room, ip)
    value = limits and checked_number(payload, limits, ip)
    if value is None:
        return None

    changes = {"T_TARGET": value}
    mode = room.findtext("HEATAREA_MODE")
    if mode in ("1", "2"):
        kind = "COOL" if root.findtext("Device/COOLING") == "1" else "HEAT"
        changes[f"T_{kind}_{'DAY' if mode == '1' else 'NIGHT'}"] = value
    return changes


def number_changes(setting):
    def changes(room, root, payload, ip):
        limits = temperature_limits(room, ip)
        value = limits and checked_number(payload, number_range(setting, limits), ip)
        return None if value is None else {ROOM_NUMBERS[setting][0]: value}
    return changes


def vacation_date(value):
    """The date as YYYY-MM-DD if it is a real one; the base reports unset ones with month 00."""
    value = (value or "").strip()
    dotted = re.fullmatch(VACATION_DOTTED_PATTERN, value, re.ASCII)
    if dotted:
        value = f"{dotted[3]}-{dotted[2]:0>2}-{dotted[1]:0>2}"
    if not re.fullmatch(VACATION_DATE_PATTERN, value, re.ASCII):
        return None
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return None
    return value


def cooling_xml(payload, ip):
    """Same field the Home Assistant integration writes for the climate mode."""
    if payload not in ("heat", "cool"):
        logger.warning("%s: ignoring invalid climate mode %r", ip, payload)
        return None
    return f"<COOLING>{int(payload == 'cool')}</COOLING>"


def preset_changes(room, root, payload, ip):
    if payload not in PRESETS:
        logger.warning("%s: ignoring invalid preset %r", ip, payload)
        return None
    return {"HEATAREA_MODE": PRESETS[payload]}


COMMANDS = {"temperature": temperature_changes, "preset": preset_changes}
COMMANDS.update((setting, number_changes(setting)) for setting in ROOM_NUMBERS)

# --- MQTT SETUP ---
stations = {}  # base station IP -> Station


def on_mqtt_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        logger.warning("MQTT connection refused: %s", reason_code)
        return
    # Subscriptions do not survive a reconnect, so they are made here
    for ip in PROXY_PORTS:
        client.subscribe(f"moehlenhoff/{ip}/room/+/+/set")
        client.subscribe(f"moehlenhoff/{ip}/vacation/+/set")
        client.subscribe(f"moehlenhoff/{ip}/cooling/set")


def on_mqtt_message(client, userdata, message):
    # A retained command would be replayed on every reconnect
    if message.retain:
        logger.warning("Ignoring retained command on %r", message.topic)
        return

    # moehlenhoff/<ip>/room/<room number>/<setting>/set, moehlenhoff/<ip>/vacation/<setting>/set
    # or moehlenhoff/<ip>/cooling/set
    levels = message.topic.split("/")
    ip = levels[1]
    if levels[2] == "cooling":
        room_nr, setting = None, "cooling"
        valid = True
    elif levels[2] == "vacation":
        room_nr, setting = None, levels[3]
        valid = setting in VACATION_COMMANDS
    else:
        room_nr, setting = levels[3], levels[4]
        valid = room_nr.isascii() and room_nr.isdecimal() and setting in COMMANDS
    station = stations.get(ip)
    if station is None or not valid or len(message.payload) > MAX_COMMAND_SIZE:
        logger.warning("Ignoring command on %r with payload %r", message.topic, message.payload[:MAX_COMMAND_SIZE])
        return
    station.queue_command(room_nr, setting, message.payload.decode(errors="replace"))


mqtt_client = None
if MQTT_BROKER:
    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if MQTT_USER and MQTT_PASS:
        mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
    mqtt_client.on_connect = on_mqtt_connect
    mqtt_client.on_message = on_mqtt_message
    mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
    mqtt_client.loop_start()


def publish_host_availability(ip, state):
    if mqtt_client is not None:
        mqtt_client.publish(
            f"moehlenhoff/{ip}/availability",
            state,
            retain=True,
        )


# Tracking dict to avoid sending HA discovery payload on every iteration
discovery_sent = set()

def device_info(device_id, ip_address):
    return {
        "identifiers": [device_id],
        "name": f"Alpha2 Base {ip_address.split('.')[-1]}",
        "manufacturer": "Möhlenhoff",
        "model": "Alpha 2 Base Station",
        "configuration_url": f"http://{ip_address}/"
    }

def publish_discovery(device_id, ip_address, entity_id, name, state_topic, value_template, unit=None, dev_class=None, icon=None, category=None):
    """Standard MQTT Auto-Discovery for sensors"""
    if mqtt_client is None:
        return

    config_topic = f"{DISCOVERY_PREFIX}/sensor/{device_id}/{entity_id}/config"
    if config_topic in discovery_sent:
        return

    payload = {
        "name": name,
        "unique_id": f"{device_id}_{entity_id}",
        "state_topic": state_topic,
        "value_template": value_template,
        "availability_topic": f"moehlenhoff/{ip_address}/availability",
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device_info(device_id, ip_address),
    }
    
    if unit: payload["unit_of_measurement"] = unit
    if dev_class: payload["device_class"] = dev_class
    if icon: payload["icon"] = icon
    if category: payload["entity_category"] = category

    mqtt_client.publish(config_topic, json.dumps(payload), retain=True)
    discovery_sent.add(config_topic)

def publish_climate_discovery(device_id, ip_address, entity_id, name, state_topic, room_nr, limits):
    """MQTT Auto-Discovery for a room's climate entity, writable like the integration's if limits are known"""
    if mqtt_client is None:
        return

    config_topic = f"{DISCOVERY_PREFIX}/climate/{device_id}/{entity_id}/config"
    # Limits are part of the key, so a change of them is published again
    sent_key = (config_topic, limits)
    if sent_key in discovery_sent:
        return

    payload = {
        "name": name,
        "unique_id": f"{device_id}_{entity_id}",
        "current_temperature_topic": state_topic,
        "current_temperature_template": "{{ value_json.temp_actual }}",
        
        "temperature_state_topic": state_topic,
        "temperature_state_template": "{{ value_json.temp_target }}",

        # A room cannot be switched off; heat or cool is set for the whole base
        "mode_state_topic": state_topic,
        "mode_state_template": "{% if value_json.cooling == 1 %}cool{% else %}heat{% endif %}",
        "action_topic": state_topic,
        "action_template": "{% if value_json.heating != 1 %}idle{% elif value_json.cooling == 1 %}cooling{% else %}heating{% endif %}",
        "availability_topic": f"moehlenhoff/{ip_address}/availability",
        "payload_available": "online",
        "payload_not_available": "offline",
        
        "modes": ["heat", "cool"],
        "min_temp": (limits or TEMP_LIMITS)[0],
        "max_temp": (limits or TEMP_LIMITS)[1],
        "temp_step": TEMP_STEP,
        
        "device": device_info(device_id, ip_address),
    }

    if limits:
        command_topic = f"moehlenhoff/{ip_address}/room/{room_nr}"
        payload.update({
            "temperature_command_topic": f"{command_topic}/temperature/set",
            "mode_command_topic": f"moehlenhoff/{ip_address}/cooling/set",
            "preset_mode_state_topic": state_topic,
            "preset_mode_value_template": "{% if value_json.mode == 1 %}day{% elif value_json.mode == 2 %}night{% else %}auto{% endif %}",
            "preset_mode_command_topic": f"{command_topic}/preset/set",
            "preset_modes": list(PRESETS),
        })

    mqtt_client.publish(config_topic, json.dumps(payload), retain=True)
    discovery_sent.add(sent_key)

def publish_number_discovery(device_id, ip_address, room_safe, name, state_topic, room_nr, limits):
    """MQTT Auto-Discovery for the settings of a room that the web interface offers"""
    if mqtt_client is None:
        return

    for setting, (_, label) in ROOM_NUMBERS.items():
        entity_id = f"{room_safe}_{setting}"
        config_topic = f"{DISCOVERY_PREFIX}/number/{device_id}/{entity_id}/config"
        sent_key = (config_topic, limits)
        if sent_key in discovery_sent:
            continue

        payload = {
            "name": f"{name} {label}",
            "unique_id": f"{device_id}_{entity_id}",
            "state_topic": state_topic,
            "value_template": f"{{{{ value_json.{setting} }}}}",
            "command_topic": f"moehlenhoff/{ip_address}/room/{room_nr}/{setting}/set",
            "availability_topic": f"moehlenhoff/{ip_address}/availability",
            "payload_available": "online",
            "payload_not_available": "offline",
            "min": number_range(setting, limits)[0],
            "max": number_range(setting, limits)[1],
            "mode": "box",
            "entity_category": "config",
            "device": device_info(device_id, ip_address),
        }
        if setting == "offset":
            payload.update({"step": OFFSET_STEP, "unit_of_measurement": "K"})
        else:
            payload.update({"step": TEMP_STEP, "unit_of_measurement": "°C", "device_class": "temperature"})

        mqtt_client.publish(config_topic, json.dumps(payload), retain=True)
        discovery_sent.add(sent_key)

def publish_vacation_discovery(device_id, ip_address, state_topic):
    """MQTT Auto-Discovery for the vacation dates and for removing a vacation"""
    if mqtt_client is None:
        return

    common = {
        "availability_topic": f"moehlenhoff/{ip_address}/availability",
        "payload_available": "online",
        "payload_not_available": "offline",
        "entity_category": "config",
        "device": device_info(device_id, ip_address),
    }
    # (platform, entity, config, read-only sensor it replaces)
    entities = [
        ("button", "vacation_clear", {
            "name": "Vacation Clear",
            "command_topic": f"moehlenhoff/{ip_address}/vacation/clear/set",
            "icon": "mdi:calendar-remove",
        }, None),
        ("number", "vacation_temp", {
            "name": "Vacation Temperature",
            "state_topic": state_topic,
            "value_template": "{{ value_json.vacation_temp }}",
            "command_topic": f"moehlenhoff/{ip_address}/vacation/temp/set",
            "min": TEMP_LIMITS[0],
            "max": TEMP_LIMITS[1],
            "step": TEMP_STEP,
            "mode": "box",
            "unit_of_measurement": "°C",
            "device_class": "temperature",
        }, "vacation_temp"),
    ]
    for setting, _, name in VACATION_DATES:
        entities.append(("text", f"vacation_{setting}", {
            "name": name,
            "state_topic": state_topic,
            # Shown as DD.MM.YYYY; the state topic keeps the format of the base
            "value_template": f"{{{{ (value_json.vacation_{setting}_date | default('')).split('-') | reverse | join('.') }}}}",
            "command_topic": f"moehlenhoff/{ip_address}/vacation/{setting}/set",
            "pattern": f"^({VACATION_DATE_PATTERN}|{VACATION_DOTTED_PATTERN})$",
            "icon": "mdi:calendar",
        }, f"vacation_{setting}_date"))

    for platform, entity_id, config, replaced_sensor in entities:
        config_topic = f"{DISCOVERY_PREFIX}/{platform}/{device_id}/{entity_id}/config"
        if config_topic in discovery_sent:
            continue
        payload = {**config, **common, "unique_id": f"{device_id}_{entity_id}"}
        mqtt_client.publish(config_topic, json.dumps(payload), retain=True)
        discovery_sent.add(config_topic)
        if replaced_sensor:
            mqtt_client.publish(f"{DISCOVERY_PREFIX}/sensor/{device_id}/{replaced_sensor}/config", "", retain=True)


def read_details(element, details, ip, description, device_id, state_topic, entity_prefix, name_prefix, gauge, gauge_labels):
    """Read the listed values that exist on this base and announce them as diagnostic sensors."""
    values = {}
    for key, path, name, unit in details:
        if element.find(path) is None:
            continue
        value = read_float(element, path, ip, f"{description} {key}")
        if value is None:
            continue
        values[key] = value
        set_gauge(gauge, (*gauge_labels, key), value)
        publish_discovery(device_id, ip, f"{entity_prefix}{key}", f"{name_prefix}{name}", state_topic, f"{{{{ value_json.{key} }}}}", unit=unit, icon="mdi:cog", category="diagnostic")
    return values


def publish_host(ip, root, vacation_pending):
    ip_safe = ip.replace('.', '_')
    device_id = f"moehlenhoff_{ip_safe}"
    sys_state_topic = f"moehlenhoff/{ip}/system/state"

    # --- GLOBAL / SYSTEM DATA ---
    system_payload = {}
    system_fields = (
        ("errors", ".//ERRORCOUNT", error_count_gauge),
        ("pump", ".//PUMP_OUTPUT/PUMP_ISACTIVE", pump_active_gauge),
        ("relais", ".//RELAIS/RELAIS_ISACTIVE", relais_active_gauge),
        ("summer_winter", ".//SUMMERWINTER", None),
    )
    for key, path, gauge in system_fields:
        value = read_float(root, path, ip, key)
        if value is not None:
            system_payload[key] = value
            if gauge is not None:
                set_gauge(gauge, (ip,), value)

    # MQTT Discovery System
    publish_discovery(device_id, ip, "error_count", "System Errors", sys_state_topic, "{{ value_json.errors }}", icon="mdi:alert-circle")
    publish_discovery(device_id, ip, "pump", "Pump State", sys_state_topic, "{{ value_json.pump }}", icon="mdi:pump")
    publish_discovery(device_id, ip, "relais", "Relay State", sys_state_topic, "{{ value_json.relais }}", icon="mdi:electric-switch")
    publish_discovery(device_id, ip, "summer_winter", "Summer/Winter Mode", sys_state_topic, "{{ value_json.summer_winter }}", icon="mdi:sun-snowflake")

    system_payload.update(read_details(root, SYSTEM_DETAILS, ip, "system", device_id, sys_state_topic, "", "", system_detail_gauge, (ip,)))
    for key, path, name in SYSTEM_TEXT_DETAILS:
        value = root.findtext(path)
        if value and value.strip():
            system_payload[key] = value.strip()
            publish_discovery(device_id, ip, key, name, sys_state_topic, f"{{{{ value_json.{key} }}}}", icon="mdi:information-outline", category="diagnostic")

    # A date waiting for its counterpart shows instead of what the base still reports
    for setting, field, _ in VACATION_DATES:
        value = vacation_pending.get(setting) or root.findtext(f"Device/VACATION/{field}")
        if value and value.strip():
            system_payload[f"vacation_{setting}_date"] = value.strip()
    vacation_temp = read_float(root, "Device/T_HEAT_VACATION", ip, "vacation temperature")
    if vacation_temp is not None:
        system_payload["vacation_temp"] = vacation_temp
        set_gauge(system_detail_gauge, (ip, "vacation_temp"), vacation_temp)
    publish_vacation_discovery(device_id, ip, sys_state_topic)

    if mqtt_client is not None:
        mqtt_client.publish(sys_state_topic, json.dumps(system_payload))

    # --- ROOMS (HEATAREA) ---
    for room in root.findall(".//HEATAREA"):
        room_nr = room.get("nr")
        name = room.findtext("HEATAREA_NAME")
        if not room_nr or not name or not name.strip():
            logger.warning("%s: skipping room with missing number or name", ip)
            continue
        name = name.strip()
        room_safe = name.lower().replace(' ', '_').replace('ü', 'ue').replace('ö', 'oe').replace('ä', 'ae')
        state_topic = f"moehlenhoff/{ip}/{room_safe}/state"
        room_payload = {}

        room_fields = (
            ("temp_actual", "T_ACTUAL", temp_actual_gauge),
            ("temp_target", "T_TARGET", temp_target_gauge),
            ("state", "HEATAREA_STATE", heatarea_state_gauge),
            ("presence", "PRESENCE", presence_gauge),
            ("mode", "HEATAREA_MODE", None),
            *((setting, field, None) for setting, (field, _) in ROOM_NUMBERS.items()),
        )
        for key, path, gauge in room_fields:
            value = read_float(room, path, ip, f"{name} {key}")
            if value is not None:
                if gauge is not None:
                    set_gauge(gauge, (ip, name), value)
                room_payload[key] = value
        room_payload.update(read_details(room, ROOM_DETAILS, ip, name, device_id, state_topic, f"{room_safe}_", f"{name} ", room_detail_gauge, (ip, name)))

        # MQTT Discovery for room
        publish_discovery(device_id, ip, f"{room_safe}_temp_actual", f"{name} Current Temperatur", state_topic, "{{ value_json.temp_actual }}", unit="°C", dev_class="temperature")
        publish_discovery(device_id, ip, f"{room_safe}_temp_target", f"{name} Target Temperatur", state_topic, "{{ value_json.temp_target }}", unit="°C", dev_class="temperature")

        limits = temperature_limits(room, ip)
        publish_climate_discovery(device_id, ip, f"{room_safe}_thermostat", f"{name} Thermostat", state_topic, room_nr, limits)
        if limits:
            publish_number_discovery(device_id, ip, room_safe, name, state_topic, room_nr, limits)
        publish_discovery(device_id, ip, f"{room_safe}_state", f"{name} Heat State", state_topic, "{{ value_json.state }}", icon="mdi:radiator")
        publish_discovery(device_id, ip, f"{room_safe}_presence", f"{name} Presence", state_topic, "{{ value_json.presence }}", icon="mdi:account")

        # --- VALVES (HEATCTRL) ---
        room_payload["heating"] = 0
        room_payload["cooling"] = 1 if root.findtext("Device/COOLING") == "1" else 0
        for controller in root.findall(".//HEATCTRL"):
            if controller.findtext("INUSE") == "1" and controller.findtext("HEATAREA_NR") == room_nr:
                if controller.findtext("HEATCTRL_STATE") == "1":
                    room_payload["heating"] = 1
                valve_id = controller.get("nr")
                if not valve_id:
                    logger.warning("%s: skipping valve without an ID in room %s", ip, name)
                    continue
                valve_percent = read_float(controller, "ACTOR_PERCENT", ip, f"{name} valve {valve_id}")
                if valve_percent is None:
                    continue

                set_gauge(actor_percent_gauge, (ip, name, valve_id), valve_percent)
                room_payload[f"valve_{valve_id}"] = valve_percent
                publish_discovery(device_id, ip, f"{room_safe}_valve_{valve_id}", f"{name} Valve {valve_id}", state_topic, f"{{{{ value_json.valve_{valve_id} }}}}", unit="%", icon="mdi:valve")

        # --- THERMOSTATS (IODEVICE) ---
        for thermostat in root.findall(".//IODEVICE"):
            if thermostat.findtext("ISON") == "1" and thermostat.findtext("HEATAREA_NR") == room_nr:
                thermostat_id = thermostat.get("nr")
                if not thermostat_id:
                    logger.warning("%s: skipping thermostat without an ID in room %s", ip, name)
                    continue
                thermostat_fields = (
                    ("battery", "BATTERY", battery_gauge),
                    ("signal", "SIGNALSTRENGTH", signal_gauge),
                    ("com_error", "IODEVICE_COMERROR", None),
                )
                for key, path, gauge in thermostat_fields:
                    value = read_float(thermostat, path, ip, f"{name} thermostat {thermostat_id} {key}")
                    if value is not None:
                        if gauge is not None:
                            set_gauge(gauge, (ip, name, thermostat_id), value)
                        room_payload[f"{key}_{thermostat_id}"] = value

                publish_discovery(device_id, ip, f"{room_safe}_battery_{thermostat_id}", f"{name} Thermostat {thermostat_id} Battery", state_topic, f"{{{{ value_json.battery_{thermostat_id} }}}}", icon="mdi:battery")
                publish_discovery(device_id, ip, f"{room_safe}_signal_{thermostat_id}", f"{name} Thermostat {thermostat_id} Signal", state_topic, f"{{{{ value_json.signal_{thermostat_id} }}}}", icon="mdi:wifi")
                publish_discovery(device_id, ip, f"{room_safe}_com_error_{thermostat_id}", f"{name} Thermostat {thermostat_id} Com Error", state_topic, f"{{{{ value_json.com_error_{thermostat_id} }}}}", icon="mdi:network-strength-off-outline")

        if mqtt_client is not None:
            mqtt_client.publish(state_topic, json.dumps(room_payload))


class Station:
    """One base station: all its HTTP traffic and the last good static.xml."""

    def __init__(self, ip, proxy_port):
        self.ip = ip
        self.proxy_port = proxy_port
        self.lock = threading.Lock()  # a base only ever sees one request at a time
        self.cache = None  # (body, content type, monotonic time)
        self.write_pending = threading.Event()
        self.settled = threading.Event()
        self.settled.set()
        self.commands = {}  # (room number, setting) -> newest payload
        self.commands_lock = threading.Lock()
        self.commands_ready = threading.Event()
        # Vacation date setting -> date, until both are known. Replaced, never changed in
        # place, because the poll thread reads it while the command thread writes it.
        self.vacation_pending = {}

    def request(self, path, data=None, content_type=None):
        headers = {"Content-Type": content_type} if content_type else {}
        request = urllib.request.Request(f"http://{self.ip}{path}", data=data, headers=headers)
        with self.lock:
            with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT) as response:
                return response.status, response.read(), response.headers.get("Content-Type", "text/xml")

    def scrape(self):
        try:
            _, body, content_type = self.request(STATIC_PATH)
            root = ET.fromstring(body)
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,
            ET.ParseError,
        ) as exc:
            logger.warning("Unable to scrape Moehlenhoff host %s: %s", self.ip, exc)
            clear_host_metrics(self.ip)
            host_up_gauge.labels(ip=self.ip).set(0)
            publish_host_availability(self.ip, "offline")
            return

        self.cache = (mask_secrets(body), content_type, time.monotonic())
        clear_host_metrics(self.ip)
        host_up_gauge.labels(ip=self.ip).set(1)
        publish_host_availability(self.ip, "online")
        publish_host(self.ip, root, dict(self.vacation_pending))

    def write(self, data, content_type):
        """Forward a change to the base and get the cache re-read afterwards."""
        result = self.request(CHANGES_PATH, data, content_type)
        self.settled.clear()
        self.write_pending.set()
        return result

    def fresh_cache(self):
        cache = self.cache
        if cache is None or time.monotonic() - cache[2] > CACHE_MAX_AGE:
            return None
        return cache

    def queue_command(self, room_nr, setting, payload):
        """Called on the MQTT thread, which must not wait for a base."""
        with self.commands_lock:
            self.commands[(room_nr, setting)] = payload
            self.commands_ready.set()

    def next_command(self):
        with self.commands_lock:
            if not self.commands:
                self.commands_ready.clear()
                return None
            key = next(iter(self.commands))
            return *key, self.commands.pop(key)

    def room_xml(self, root, room_nr, setting, payload):
        room = next((area for area in root.findall(".//HEATAREA") if area.get("nr") == room_nr), None)
        if room is None:
            logger.warning("%s: dropping %s command for unknown room %s", self.ip, setting, room_nr)
            return None
        if temperature_limits(room, self.ip) is None:
            logger.warning("%s: dropping %s command, room %s is set on its thermostat", self.ip, setting, room_nr)
            return None
        changes = COMMANDS[setting](room, root, payload, self.ip)
        if changes is None:
            return None
        fields = "".join(f"<{key}>{value}</{key}>" for key, value in changes.items())
        return f'<HEATAREA nr="{room_nr}">{fields}</HEATAREA>'

    def vacation_xml(self, root, setting, payload):
        """Same bodies as the base's web interface, which always sends both dates."""
        if setting == "clear":
            self.vacation_pending = {}
            return (
                f"<VACATION><VACATION_STATE>0</VACATION_STATE><START_DATE>{VACATION_UNSET}</START_DATE>"
                f"<END_DATE>{VACATION_UNSET}</END_DATE></VACATION>"
            )
        if setting == "temp":
            value = checked_number(payload, TEMP_LIMITS, self.ip)
            return None if value is None else f"<T_HEAT_VACATION>{value}</T_HEAT_VACATION>"

        date = vacation_date(payload)
        if date is None:
            logger.warning("%s: ignoring invalid vacation %s date %r", self.ip, setting, payload)
            return None
        self.vacation_pending = {**self.vacation_pending, setting: date}

        start = self.vacation_pending.get("start") or vacation_date(root.findtext("Device/VACATION/START_DATE"))
        end = self.vacation_pending.get("end") or vacation_date(root.findtext("Device/VACATION/END_DATE"))
        if start is None or end is None or start > end:
            logger.info("%s: holding vacation dates %s to %s until they form a period", self.ip, start, end)
            self.write_pending.set()  # gets the held date published
            return None

        self.vacation_pending = {}
        return f"<VACATION><START_DATE>{start}</START_DATE><END_DATE>{end}</END_DATE></VACATION>"

    def run_command(self, room_nr, setting, payload):
        # Decide on what the previous write produced, not the state before it
        self.settled.wait(WRITE_READ_WAIT)
        cache = self.fresh_cache()
        if cache is None:
            logger.warning("%s: dropping %s command, no recent data", self.ip, setting)
            return

        root = ET.fromstring(cache[0])
        device_id = root.findtext("Device/ID")
        if not device_id:
            logger.warning("%s: dropping %s command, base reports no device ID", self.ip, setting)
            return

        if setting == "cooling":
            xml = cooling_xml(payload, self.ip)
        elif room_nr is None:
            xml = self.vacation_xml(root, setting, payload)
        else:
            xml = self.room_xml(root, room_nr, setting, payload)
        if xml is None:
            return

        body = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<Devices><Device><ID>{escape(device_id)}</ID>{xml}</Device></Devices>'
        )
        self.write(body.encode("utf-8"), "application/octet-stream")
        logger.info("%s: wrote %s", self.ip, xml)

    def command_forever(self):
        while True:
            self.commands_ready.wait()
            command = self.next_command()
            if command is None:
                continue
            try:
                self.run_command(*command)
            except Exception:
                logger.exception("MQTT command for Moehlenhoff host %s failed", self.ip)

    def poll_forever(self):
        delay = 0
        while True:
            wrote = self.write_pending.wait(delay)
            if wrote:
                self.write_pending.clear()
                time.sleep(WRITE_SETTLE)
            try:
                self.scrape()
            except Exception:
                logger.exception("Scrape of Moehlenhoff host %s failed", self.ip)
            if not self.write_pending.is_set():
                self.settled.set()
            delay = WRITE_RECHECK if wrote else SCRAPE_INTERVAL


class ProxyHandler(BaseHTTPRequestHandler):
    """Speaks just enough of the base's API for the Home Assistant integration."""

    def respond(self, status, body=b"", content_type="text/plain"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != STATIC_PATH:
            self.respond(404)
            return

        station = self.server.station
        # Serve what a just-forwarded write produced, not the state before it
        station.settled.wait(WRITE_READ_WAIT)
        cache = station.fresh_cache()
        if cache is None:
            self.respond(503)
            return
        self.respond(200, cache[0], cache[1])

    def do_POST(self):
        if self.path != CHANGES_PATH:
            self.respond(404)
            return

        station = self.server.station
        data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            status, body, content_type = station.write(data, self.headers.get("Content-Type"))
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            logger.warning("Unable to forward change to Moehlenhoff host %s: %s", station.ip, exc)
            self.respond(502)
            return
        self.respond(status, mask_secrets(body), content_type)

    def log_message(self, format, *args):
        logger.info("Proxy %s from %s: %s", self.server.station.ip, self.client_address[0], format % args)


def start_proxy(station):
    server = ThreadingHTTPServer((PROXY_BIND, station.proxy_port), ProxyHandler)
    server.daemon_threads = True
    server.station = station
    threading.Thread(target=server.serve_forever, daemon=True).start()


if __name__ == '__main__':
    start_http_server(PORT)
    stations.update((ip, Station(ip, proxy_port)) for ip, proxy_port in PROXY_PORTS.items())
    for station in stations.values():
        start_proxy(station)
    mqtt_status = "enabled" if mqtt_client is not None else "disabled (MQTT_BROKER is unset)"
    print(f"Moehlenhoff Exporter running on port {PORT}; proxy ports {list(PROXY_PORTS.values())}; MQTT {mqtt_status}...")
    workers = [threading.Thread(target=station.poll_forever, daemon=True) for station in stations.values()]
    for station in stations.values():
        threading.Thread(target=station.command_forever, daemon=True).start()
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

#!/usr/bin/env python3
import os
import time
import json
import logging
import math
import http.client
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import paho.mqtt.client as mqtt
from prometheus_client import start_http_server, Gauge

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# --- CONFIGURATION ---
IPS = ["192.168.10.71", "192.168.10.72"]
PORT = 9121
SCRAPE_INTERVAL = 120  # seconds

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
)
metric_labels_by_ip = {gauge: {} for gauge in data_gauges}


def set_gauge(gauge, label_values, value):
    gauge.labels(*label_values).set(value)
    metric_labels_by_ip[gauge].setdefault(label_values[0], set()).add(label_values)


def clear_host_metrics(ip):
    for gauge, labels_by_ip in metric_labels_by_ip.items():
        for label_values in labels_by_ip.pop(ip, set()):
            gauge.remove(*label_values)


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

# --- MQTT SETUP ---
mqtt_client = None
if MQTT_BROKER:
    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if MQTT_USER and MQTT_PASS:
        mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
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

def publish_discovery(device_id, ip_address, entity_id, name, state_topic, value_template, unit=None, dev_class=None, icon=None):
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
        "device": {
            "identifiers": [device_id],
            "name": f"Alpha2 Base {ip_address.split('.')[-1]}",
            "manufacturer": "Möhlenhoff",
            "model": "Alpha 2 Base Station",
            "configuration_url": f"http://{ip_address}/"
        }
    }
    
    if unit: payload["unit_of_measurement"] = unit
    if dev_class: payload["device_class"] = dev_class
    if icon: payload["icon"] = icon

    mqtt_client.publish(config_topic, json.dumps(payload), retain=True)
    discovery_sent.add(config_topic)

def publish_climate_discovery(device_id, ip_address, entity_id, name, state_topic):
    """Spezielle Helper-Funktion für Climate Entitäten (read-only)"""
    if mqtt_client is None:
        return

    config_topic = f"{DISCOVERY_PREFIX}/climate/{device_id}/{entity_id}/config"
    if config_topic in discovery_sent:
        return

    payload = {
        "name": name,
        "unique_id": f"{device_id}_{entity_id}",
        # Wo liegt das JSON mit allen Daten?
        "current_temperature_topic": state_topic,
        "current_temperature_template": "{{ value_json.temp_actual }}",
        
        "temperature_state_topic": state_topic,
        "temperature_state_template": "{{ value_json.temp_target }}",
        
        # Welcher Modus ist aktiv? (Dummy, da wir nur lesen)
        "mode_state_topic": state_topic,
        "mode_state_template": "{% if value_json.state == 1 %}heat{% else %}off{% endif %}",
        "availability_topic": f"moehlenhoff/{ip_address}/availability",
        "payload_available": "online",
        "payload_not_available": "offline",
        
        "modes": ["off", "heat"],
        "min_temp": 5,
        "max_temp": 30,
        "temp_step": 0.1,
        
        "device": {
            "identifiers": [device_id],
            "name": f"Alpha2 Base {ip_address.split('.')[-1]}",
            "manufacturer": "Möhlenhoff",
            "model": "Alpha 2 Base Station",
            "configuration_url": f"http://{ip_address}/"
        }
    }

    mqtt_client.publish(config_topic, json.dumps(payload), retain=True)
    discovery_sent.add(config_topic)

def fetch_data():
    for ip in IPS:
        clear_host_metrics(ip)
        host_up_gauge.labels(ip=ip).set(0)

        ip_safe = ip.replace('.', '_')
        device_id = f"moehlenhoff_{ip_safe}"
        sys_state_topic = f"moehlenhoff/{ip}/system/state"

        try:
            with urllib.request.urlopen(f"http://{ip}/data/static.xml", timeout=5) as response:
                root = ET.fromstring(response.read())
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,
            ET.ParseError,
        ) as exc:
            logger.warning("Unable to scrape Moehlenhoff host %s: %s", ip, exc)
            publish_host_availability(ip, "offline")
            continue

        host_up_gauge.labels(ip=ip).set(1)
        publish_host_availability(ip, "online")

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
            )
            for key, path, gauge in room_fields:
                value = read_float(room, path, ip, f"{name} {key}")
                if value is not None:
                    set_gauge(gauge, (ip, name), value)
                    room_payload[key] = value

            # MQTT Discovery for room
            publish_discovery(device_id, ip, f"{room_safe}_temp_actual", f"{name} Current Temperatur", state_topic, "{{ value_json.temp_actual }}", unit="°C", dev_class="temperature")
            publish_discovery(device_id, ip, f"{room_safe}_temp_target", f"{name} Target Temperatur", state_topic, "{{ value_json.temp_target }}", unit="°C", dev_class="temperature")

            publish_climate_discovery(device_id, ip, f"{room_safe}_thermostat", f"{name} Thermostat", state_topic)
            publish_discovery(device_id, ip, f"{room_safe}_state", f"{name} Heat State", state_topic, "{{ value_json.state }}", icon="mdi:radiator")
            publish_discovery(device_id, ip, f"{room_safe}_presence", f"{name} Presence", state_topic, "{{ value_json.presence }}", icon="mdi:account")

            # --- VALVES (HEATCTRL) ---
            for controller in root.findall(".//HEATCTRL"):
                if controller.findtext("INUSE") == "1" and controller.findtext("HEATAREA_NR") == room_nr:
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

if __name__ == '__main__':
    start_http_server(PORT)
    mqtt_status = "enabled" if mqtt_client is not None else "disabled (MQTT_BROKER is unset)"
    print(f"Moehlenhoff Exporter running on port {PORT}; MQTT {mqtt_status}...")
    while True:
        fetch_data()
        time.sleep(SCRAPE_INTERVAL)

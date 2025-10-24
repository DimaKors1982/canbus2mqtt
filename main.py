import time
import network
import ujson
import socket
import os
from random import randint
import _thread
from umqttsimple import MQTTClient
from src import (
    CAN,
    CAN_CLOCK,
    CAN_SPEED,
    ERROR,
)
from src import SPIESP32 as SPI
from src import CANFrame

# Speed map for CAN bitrates
speed_map = {
    '10': CAN_SPEED.CAN_10KBPS,
    '20': CAN_SPEED.CAN_20KBPS,
    '50': CAN_SPEED.CAN_50KBPS,
    '100': CAN_SPEED.CAN_100KBPS,
    '125': CAN_SPEED.CAN_125KBPS,
    '250': CAN_SPEED.CAN_250KBPS,
    '500': CAN_SPEED.CAN_500KBPS,
}

# Load or set default config
config = {}
if 'config.json' in os.listdir():
    try:
        with open('config.json', 'r') as f:
            config = ujson.load(f)
    except Exception as e:
        print('Config load error:', e)

# Defaults
config.setdefault('SSID', 'your_ssid')
config.setdefault('PASSWORD', 'your_pass')
config.setdefault('MQTT_SERVER', 'your_mqtt')
config.setdefault('MQTT_PORT', 1883)
config.setdefault('MQTT_USER', None)
config.setdefault('MQTT_PASS', None)
config.setdefault('bitrate', '125')
#for test
config.setdefault('can_id', '100')
config.setdefault('data_hex', '12 34 56 78 9A BC DE F0')
config.setdefault('can_mode', 'loopback')

# Set globals from config
SSID = config['SSID']
PASSWORD = config['PASSWORD']
MQTT_SERVER = config['MQTT_SERVER']
MQTT_PORT = config['MQTT_PORT']
MQTT_USER = config['MQTT_USER']
MQTT_PASS = config['MQTT_PASS']
try:
    data = bytes.fromhex(config['data_hex'].replace(' ', ''))
    frame = CANFrame(can_id=int(config['can_id'], 16), data=data)
except Exception as e:
    print('Frame initialization error:', e)
    data = b'\x12\x34\x56\x78\x9A\xBC\xDE\xF0'
    frame = CANFrame(can_id=0x100, data=data)

# MQTT settings
CLIENT_ID = b'esp32'
TOPIC_PUB = b'can/'

# Global variables
wlan = None
mqtt_client = None
can = None
bridge_running = False
last_reconnect = 0
reconnect_interval = 10  # Seconds

def url_decode(s):
    try:
        s = s.replace('+', ' ')
        i = 0
        result = ''
        while i < len(s):
            if s[i] == '%' and i + 2 < len(s):
                try:
                    code = int(s[i + 1:i + 3], 16)
                    result += chr(code)
                    i += 3
                except ValueError:
                    result += s[i]
                    i += 1
            else:
                result += s[i]
                i += 1
        return result
    except Exception as e:
        print('URL decode error:', e)
        return s

def save_config():
    try:
        with open('config.json', 'w') as f:
            ujson.dump(config, f)
    except Exception as e:
        print('Config save error:', e)

def connect_wifi():
    global wlan
    try:
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        wlan.connect(SSID, PASSWORD)
        for _ in range(10):  # Timeout after 10 seconds
            if wlan.isconnected():
                print('Wi-Fi connected:', wlan.ifconfig())
                return
            time.sleep(1)
        print('Wi-Fi connection timeout')
    except Exception as e:
        print('Wi-Fi connect error:', e)

def connect_mqtt():
    global mqtt_client
    try:
        if mqtt_client:
            mqtt_client.disconnect()
    except:
        pass
    try:
        mqtt_client = MQTTClient(CLIENT_ID, MQTT_SERVER, MQTT_PORT, MQTT_USER, MQTT_PASS)
        mqtt_client.connect()
        print('MQTT connected to broker')
        return True
    except Exception as e:
        print('MQTT connect error:', e)
        return False

def init_can(bitrate_str='125'):
    global can
    try:
        speed = speed_map.get(bitrate_str, CAN_SPEED.CAN_125KBPS)
        can = CAN(SPI(cs=23))
        if can.reset() != ERROR.ERROR_OK:
            print("Cannot reset MCP2515")
            return False
        if can.setBitrate(speed, CAN_CLOCK.MCP_8MHZ) != ERROR.ERROR_OK:
            print("Cannot set bitrate for MCP2515")
            return False
        can_mode = config.get('can_mode', 'loopback')
        if can_mode == 'loopback':
            if can.setLoopbackMode() != ERROR.ERROR_OK:
                print("Cannot set loopback mode for MCP2515")
                return False
            print("CAN set to Loopback Mode")
        else:
            if can.setNormalMode() != ERROR.ERROR_OK:
                print("Cannot set normal mode for MCP2515")
                return False
            print("CAN set to Normal Mode")
        return True
    except Exception as e:
        print('CAN init error:', e)
        return False

def read_can_and_publish():
    try:
        error, iframe = can.readMessage()
        if error == ERROR.ERROR_OK:
            data_dict = {f'd{i}': iframe.data[i] for i in range(iframe.dlc)}
            data_dict['time'] = time.time_ns()
            mqtt_client.publish(TOPIC_PUB + hex(iframe.can_id), ujson.dumps(data_dict))
    except Exception as e:
        print('Read CAN error:', e)

def reconnect():
    global last_reconnect
    if time.time() - last_reconnect > reconnect_interval:
        print('Reconnecting...')
        try:
            if not wlan.isconnected():
                connect_wifi()
            if not mqtt_client or not mqtt_client.sock:
                connect_mqtt()
            last_reconnect = time.time()
        except Exception as e:
            print('Reconnection error:', e)
            time.sleep(5)

def bridge_loop():
    while True:
        if not bridge_running:
            time.sleep(1)
            continue
        try:
            # In Loopback Mode, send test data before reading
            if config.get('can_mode', 'loopback') == 'loopback':
                error = can.sendMessage(frame)
                if error == ERROR.ERROR_OK:
                    print("TX  {}".format(frame))
                else:
                    print("TX failed with error code {}".format(error))
                time.sleep(1)
            # Read CAN messages in both modes
            read_can_and_publish()
            
        except OSError as e:
            print('Loop error:', e)
            reconnect()
        except Exception as e:
            print('Unexpected error:', e)
            time.sleep(1)

# Web page with DOCTYPE, minimal CSS, and CAN mode selection
def web_page(status_message=''):
    try:
        # Gather status information
        ip = wlan.ifconfig()[0] if wlan and wlan.isconnected() else 'Not connected'
        wifi_status = 'Connected' if wlan and wlan.isconnected() else 'Disconnected'
        mqtt_status = 'Connected' if mqtt_client and mqtt_client.sock else 'Disconnected'
        can_status = 'Initialized' if can else 'Not initialized'
        bridge_status = 'Running' if bridge_running else 'Stopped'
        ssid_val = config.get('SSID', '')
        pass_val = config.get('PASSWORD', '')
        server_val = config.get('MQTT_SERVER', '')
        port_val = config.get('MQTT_PORT', 1883)
        user_val = config.get('MQTT_USER', '') or ''
        pass_mqtt_val = config.get('MQTT_PASS', '') or ''
        bitrate_val = config.get('bitrate', '125')
        can_id_val = config.get('can_id', '100')
        data_hex_val = config.get('data_hex', '12 34 56 78 9A BC DE F0')
        can_mode_val = config.get('can_mode', 'loopback')

        # Generate bitrate options
        bitrate_options = ''
        try:
            for k in speed_map.keys():
                selected = 'selected' if k == bitrate_val else ''
                bitrate_options += '<option value="{}" {}>{} kbps</option>'.format(k, selected, k)
            print('Bitrate options generated, length:', len(bitrate_options))
        except Exception as e:
            print('Bitrate options error:', e)
            bitrate_options = '<option value="125" selected>125 kbps</option>'

        # Generate CAN mode options
        can_mode_options = ''
        try:
            modes = ['loopback', 'normal']
            for mode in modes:
                selected = 'selected' if mode == can_mode_val else ''
                can_mode_options += '<option value="{}" {}>{}</option>'.format(mode, selected, mode.capitalize())
            print('CAN mode options generated, length:', len(can_mode_options))
        except Exception as e:
            print('CAN mode options error:', e)
            can_mode_options = '<option value="loopback" selected>Loopback</option><option value="normal">Normal</option>'

        # Build HTML in parts
        html_parts = []
        html_parts.append('<!DOCTYPE html>\n<html>\n<head>\n')
        html_parts.append('<title>ESP32 CAN MQTT Bridge Config</title>\n')
        html_parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">\n')
        html_parts.append('<style>\n')
        html_parts.append('body { font-family: Arial; margin: 10px; padding: 5px; }\n')
        html_parts.append('h1 { font-size: 20px; }\n')
        html_parts.append('input, select, button { margin: 5px; padding: 5px; }\n')
        html_parts.append('</style>\n')
        html_parts.append('</head>\n<body>\n')
        html_parts.append('<h1>CAN to MQTT Bridge Configuration</h1>\n')
        html_parts.append('<p><b>Status:</b><br>\n')
        html_parts.append('IP Address: {}<br>\n'.format(ip))
        html_parts.append('WiFi: {}<br>\n'.format(wifi_status))
        html_parts.append('MQTT: {}<br>\n'.format(mqtt_status))
        html_parts.append('CAN: {}<br>\n'.format(can_status))
        html_parts.append('Bridge: {}<br>\n'.format(bridge_status))
        html_parts.append('</p>\n<p>{}</p>\n'.format(status_message))
        html_parts.append('<form method="GET" action="/config">\n')
        html_parts.append('WiFi SSID: <input type="text" name="ssid" value="{}" required><br>\n'.format(ssid_val))
        html_parts.append('WiFi Password: <input type="password" name="password" value="{}"><br>\n'.format(pass_val))
        html_parts.append('MQTT Server: <input type="text" name="mqtt_server" value="{}"><br>\n'.format(server_val))
        html_parts.append('MQTT Port: <input type="number" name="mqtt_port" value="{}" min="1" max="65535"><br>\n'.format(port_val))
        html_parts.append('MQTT User: <input type="text" name="mqtt_user" value="{}"><br>\n'.format(user_val))
        html_parts.append('MQTT Password: <input type="password" name="mqtt_pass" value="{}"><br>\n'.format(pass_mqtt_val))
        html_parts.append('CAN Mode: <select name="can_mode">{}</select><br>\n'.format(can_mode_options))
        html_parts.append('CAN Bitrate: <select name="bitrate">{}</select><br>\n'.format(bitrate_options))
        html_parts.append('CAN ID (hex): <input type="text" name="can_id" value="{}" placeholder="e.g. 100"><br>\n'.format(can_id_val))
        html_parts.append('Data (hex, space separated): <input type="text" name="data_hex" value="{}" placeholder="12 34 56 78 9A BC DE F0"><br>\n'.format(data_hex_val))
        html_parts.append('<input type="submit" value="Save Config">\n')
        html_parts.append('</form>\n')
        html_parts.append('<p>\n<a href="/start"><button>Start Bridge</button></a>\n')
        html_parts.append('<a href="/stop"><button>Stop Bridge</button></a>\n</p>\n')
        html_parts.append('</body>\n</html>')

        # Combine parts
        try:
            html = ''.join(html_parts)
            print('Web page generated successfully, length:', len(html))
            return html
        except Exception as e:
            print('HTML join error:', e)
            return '<!DOCTYPE html><html><body><h1>Error</h1><p>Failed to generate page: {}</p></body></html>'.format(str(e))
    except Exception as e:
        print('Web page generation error:', e)
        return '<!DOCTYPE html><html><body><h1>Error</h1><p>Failed to generate page: {}</p></body></html>'.format(str(e))

# Initial connections
try:
    connect_wifi()
    init_can(config['bitrate'])
    connect_mqtt()
except Exception as e:
    print('Initial connection error:', e)

# Start bridge thread
try:
    _thread.start_new_thread(bridge_loop, ())
except Exception as e:
    print('Thread start error:', e)

# Web server with timeout and enhanced error handling
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
#     s.settimeout(5.0)  # 5-second timeout
    s.bind(('', 8885))
    s.listen(5)
    print('Socket setup successfully')
except Exception as e:
    print('Socket setup error:', e)

while True:
    try:
        conn, addr = s.accept()
        print('Got a connection from %s' % str(addr))
        try:
#             conn.settimeout(5.0)  # 3-second timeout for client connection
            request = conn.recv(1024).decode('utf-8')
            print('Content = %s' % request)
        except OSError as e:
            print('Request receive error:', e)
            conn.close()
            continue
        except Exception as e:
            print('Unexpected request error:', e)
            conn.close()
            continue

        status_message = ''
        if 'GET /start' in request:
#             global bridge_running
            bridge_running = True
            status_message = '<p>Bridge started!</p>'
        elif 'GET /stop' in request:
            bridge_running = False
            status_message = '<p>Bridge stopped!</p>'
        elif 'GET /config' in request:
            # Parse query
            try:
                query_start = request.find('?') + 1
                query_end = request.find(' ', query_start)
                if query_end == -1:
                    query_end = len(request)
                query = request[query_start:query_end]
                params = {}
                for pair in query.split('&'):
                    if '=' in pair:
                        k, v = pair.split('=', 1)
                        params[k] = url_decode(v)
                # Update config
                old_ssid = SSID
                old_pass = PASSWORD
                changed_mqtt = False
                changed_can = False
                for k, v in params.items():
                    if k == 'ssid':
                        config['SSID'] = v
                        SSID = v
                    elif k == 'password':
                        config['PASSWORD'] = v
                        PASSWORD = v
                    elif k == 'mqtt_server':
                        config['MQTT_SERVER'] = v
                        MQTT_SERVER = v
                        changed_mqtt = True
                    elif k == 'mqtt_port':
                        try:
                            config['MQTT_PORT'] = int(v)
                            MQTT_PORT = config['MQTT_PORT']
                            changed_mqtt = True
                        except ValueError:
                            pass
                    elif k == 'mqtt_user':
                        config['MQTT_USER'] = v if v else None
                        MQTT_USER = config['MQTT_USER']
                        changed_mqtt = True
                    elif k == 'mqtt_pass':
                        config['MQTT_PASS'] = v if v else None
                        MQTT_PASS = config['MQTT_PASS']
                        changed_mqtt = True
                    elif k == 'bitrate':
                        config['bitrate'] = v
                        changed_can = True
                    elif k == 'can_id':
                        try:
                            new_id = int(v, 16)
                            frame.can_id = new_id
                            config['can_id'] = v
                        except ValueError:
                            pass
                    elif k == 'data_hex':
                        try:
                            new_data = bytes.fromhex(v.replace(' ', ''))
                            frame.data = new_data
                            config['data_hex'] = v
                        except ValueError:
                            pass
                    elif k == 'can_mode':
                        config['can_mode'] = v
                        changed_can = True
                save_config()
                # Apply changes
                if SSID != old_ssid or PASSWORD != old_pass:
                    print('WiFi config changed, reconnecting...')
                    try:
                        wlan.disconnect()
                        connect_wifi()
                    except Exception as e:
                        print('WiFi reconnect error:', e)
                        status_message = '<p>WiFi reconnect failed!</p>'
                if changed_mqtt:
                    print('MQTT config changed, reconnecting...')
                    try:
                        connect_mqtt()
                    except Exception as e:
                        print('MQTT reconnect error:', e)
                        status_message = '<p>MQTT reconnect failed!</p>'
                if changed_can:
                    print('CAN config changed, reinitializing...')
                    try:
                        init_can(config['bitrate'])
                    except Exception as e:
                        print('CAN init error:', e)
                        status_message = '<p>CAN init failed!</p>'
                if not status_message:
                    status_message = '<p>Config saved and applied!</p>'
            except Exception as e:
                print('Config processing error:', e)
                status_message = '<p>Config processing failed: {}</p>'.format(str(e))

        try:
            response = web_page(status_message)
            conn.send('HTTP/1.1 200 OK\n')
            conn.send('Content-Type: text/html; charset=utf-8\n')
            conn.send('Connection: close\n\n')
            conn.sendall(response.encode('utf-8'))
            print('Response sent successfully, length:', len(response))
        except OSError as e:
            print('Response send error:', e)
            try:
                conn.send('HTTP/1.1 500 Internal Server Error\n')
                conn.send('Content-Type: text/html; charset=utf-8\n')
                conn.send('Connection: close\n\n')
                conn.sendall('<html><body><h1>Server Error</h1><p>{}</p></body></html>'.format(str(e)).encode('utf-8'))
            except:
                pass
        except Exception as e:
            print('Unexpected response error:', e)
            try:
                conn.send('HTTP/1.1 500 Internal Server Error\n')
                conn.send('Content-Type: text/html; charset=utf-8\n')
                conn.send('Connection: close\n\n')
                conn.sendall('<html><body><h1>Server Error</h1><p>{}</p></body></html>'.format(str(e)).encode('utf-8'))
            except:
                pass
        finally:
            try:
                conn.close()
            except:
                pass
    except OSError as e:
        print('Socket accept error:', e)
        time.sleep(1)  # Prevent tight loop on error
    except Exception as e:
        print('Unexpected server error:', e)
        time.sleep(1)

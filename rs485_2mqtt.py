import paho.mqtt.client as mqtt
import re, inspect, sys, asyncio
from json import dumps as json_dumps
from functools import reduce
from collections import defaultdict
from pprint import pprint

class Device:
    def __init__(self, device_name, device_id, device_subid, device_class, child_device, mqtt_discovery, optional_info):
        self.device_name = device_name
        self.device_id = device_id
        self.device_subid = device_subid
        self.device_unique_id = 'rs485_' + self.device_id + '_' + self.device_subid
        self.device_class = device_class
        self.child_device = child_device
        self.mqtt_discovery = mqtt_discovery
        self.optional_info = optional_info

        self.__message_flag = {}            # {'power': '41'}
        self.__command_process_func = {}

        self.__status_messages_map = defaultdict(list)
        self.__command_messages_map = {}

    def register_status(self, message_flag, attr_name, regex, topic_class, device_name = None, process_func = lambda v: v):
        device_name = self.device_name if device_name == None else device_name
        self.__status_messages_map[message_flag].append({'regex': regex, 'process_func': process_func, 'device_name': device_name, 'attr_name': attr_name, 'topic_class': topic_class})

    def register_command(self, message_flag, attr_name, topic_class, process_func = lambda v: v):
        self.__command_messages_map[attr_name] = {'message_flag': message_flag, 'attr_name': attr_name, 'topic_class': topic_class, 'process_func': process_func}

    def parse_payload(self, payload_dict):
        result = {}
        device_family = [self] + self.child_device
        for device in device_family:
            for status in device.__status_messages_map[payload_dict['message_flag']]:
                topic = '/'.join([ROOT_TOPIC_NAME, device.device_class, device.device_name, status['attr_name']])
                result[topic] = status['process_func'](re.match(status['regex'], payload_dict['data'])[1])
        return result

    def get_command_payload_byte(self, attr_name, attr_value):  # command('power', 'ON')   command('speed', 'middle')
        attr_value = self.__command_messages_map[attr_name]['process_func'](attr_value)

        command_payload = ['f7', self.device_id, self.device_subid, self.__command_messages_map[attr_name]['message_flag'], '01', attr_value]
        command_payload.append(Wallpad.xor(command_payload))
        command_payload.append(Wallpad.add(command_payload))
        return bytearray.fromhex(' '.join(command_payload))

    def get_mqtt_discovery_payload(self):
        result = {
            '~': '/'.join([ROOT_TOPIC_NAME, self.device_class, self.device_name]),
            'name': self.device_name,
            'uniq_id': self.device_unique_id,
        }
        result.update(self.optional_info)
        for status_list in self.__status_messages_map.values():
            for status in status_list:
                result[status['topic_class']] = '/'.join(['~', status['attr_name']])

        for status_list in self.__command_messages_map.values():
            result[status_list['topic_class']] = '/'.join(['~', status_list['attr_name'], 'set'])

        result['device'] = {
            'identifiers': self.device_unique_id,
            'name': self.device_name
        }
        return json_dumps(result, ensure_ascii = False)

    def get_status_attr_list(self):
        return list(set([status['attr_name'] for status_list in self.__status_messages_map.values() for status in status_list]))

class Wallpad:
    _device_list = []

    def __init__(self):
        self.mqtt_client = mqtt.Client()
        self.mqtt_client.on_message = self.on_raw_message

        self.mqtt_client.connect(MQTT_SERVER, 1883)

    def listen(self):
        self.register_mqtt_discovery()
        self.mqtt_client.subscribe([(topic, 2) for topic in [ROOT_TOPIC_NAME + '/dev/raw'] + self.get_topic_list_to_listen()])
        self.mqtt_client.loop_forever()

    def register_mqtt_discovery(self):
        for device in self._device_list:
            if device.mqtt_discovery:
                topic = '/'.join([HOMEASSISTANT_ROOT_TOPIC_NAME, device.device_class, device.device_unique_id, 'config'])
                payload = device.get_mqtt_discovery_payload()
                self.mqtt_client.publish(topic, payload, qos = 2, retain = True)

    def add_device(self, device_name, device_id, device_subid, device_class, child_device = [], mqtt_discovery = True, optional_info = {}):
        device = Device(device_name, device_id, device_subid, device_class, child_device, mqtt_discovery, optional_info)
        self._device_list.append(device)
        return device

    def get_device(self, **kwargs):
        if 'device_name' in kwargs:
            return [device for device in self._device_list if device.device_name == kwargs['device_name']][0]
        else:
            return [device for device in self._device_list if device.device_id == kwargs['device_id'] and device.device_subid == kwargs['device_subid']][0]

    def get_topic_list_to_listen(self):
        return ['/'.join([ROOT_TOPIC_NAME, device.device_class, device.device_name, attr_name, 'set']) for device in self._device_list for attr_name in device.get_status_attr_list()]

    @classmethod
    def xor(cls, hexstring_array):
        return format(reduce((lambda x, y: x^y), list(map(lambda x: int(x, 16), hexstring_array))), '02x')

    @classmethod
    def add(cls, hexstring_array): # hexstring_array ['f7', '32', ...]
        return format(reduce((lambda x, y: x+y), list(map(lambda x: int(x, 16), hexstring_array))), '02x')[-2:]

    @classmethod
    def is_valid(cls, payload_hexstring):
        payload_hexstring_array = [payload_hexstring[i:i+2] for i in range(0, len(payload_hexstring), 2)] # ['f7', '0e', '1f', '81', '04', '00', '00', '00', '00', '63', '0c']
        try:
            result = int(payload_hexstring_array[4], 16) + 7 == len(payload_hexstring_array) and cls.xor(payload_hexstring_array[:-2]) == payload_hexstring_array[-2:-1][0] and cls.add(payload_hexstring_array[:-1]) == payload_hexstring_array[-1:][0]
            return result
        except:
            return False

    def on_raw_message(self, client, userdata, msg):
        if msg.topic == ROOT_TOPIC_NAME + '/dev/raw': # ew11이 MQTT에 rs485 패킷을 publish하는 경우
            for payload_raw_bytes in msg.payload.split(b'\xf7')[1:]: # payload 내에 여러 메시지가 있는 경우, \f7 disappear as delimiter here
                payload_hexstring = 'f7' + payload_raw_bytes.hex() # 'f7361f810f000001000017179817981717969896de22'
                try:
                    if self.is_valid(payload_hexstring):
                        payload_dict = re.match(r'f7(?P<device_id>0e|12|32|33|36)(?P<device_subid>[0-9a-f]{2})(?P<message_flag>[0-9a-f]{2})(?:[0-9a-f]{2})(?P<data>[0-9a-f]*)(?P<xor>[0-9a-f]{2})(?P<add>[0-9a-f]{2})', payload_hexstring).groupdict()

                        for topic, value in self.get_device(device_id = payload_dict['device_id'], device_subid = payload_dict['device_subid']).parse_payload(payload_dict).items():
                            client.publish(topic, value, qos = 1, retain = False)
                    else:
                        continue
                except Exception as e:
                    client.publish(ROOT_TOPIC_NAME + '/dev/error', payload_hexstring, qos = 1, retain = True)

        else: # homeassistant에서 명령하여 MQTT topic을 publish하는 경우
            topic_split = msg.topic.split('/') # rs485_2mqtt/light/침실등/power/set
            device = self.get_device(device_name = topic_split[2])
            payload = device.get_command_payload_byte(topic_split[3], msg.payload.decode())
            client.publish(ROOT_TOPIC_NAME + '/dev/command', payload, qos = 2, retain = False)

MQTT_SERVER = '192.168.1.1'
ROOT_TOPIC_NAME = 'rs485_2mqtt'
HOMEASSISTANT_ROOT_TOPIC_NAME = 'homeassistant'
wallpad = Wallpad()

packet_2_payload_speed = {'00': 'off', '01': 'low', '02': 'medium', '03': 'high'}
packet_2_payload_oscillation = {'03': 'oscillate_on', '00': 'oscillation_off', '01': 'oscillate_off'}

### 전열교환기 ###
optional_info = {'optimistic': 'false', 'speeds': ['low', 'medium', 'high']}
전열교환기 = wallpad.add_device(device_name = '전열교환기', device_id = '32', device_subid = '01', device_class = 'fan', optional_info = optional_info)
전열교환기.register_status(message_flag = '01', attr_name = 'availability', topic_class ='availability_topic',      regex = r'()', process_func = lambda v: 'online')
전열교환기.register_status(message_flag = '81', attr_name = 'power',        topic_class ='state_topic',             regex = r'00(0[01])0[0-3]0[013]00', process_func = lambda v: 'ON' if v == '01' else 'OFF')
전열교환기.register_status(message_flag = 'c1', attr_name = 'power',        topic_class ='state_topic',             regex = r'00(0[01])0[0-3]0[013]00', process_func = lambda v: 'ON' if v == '01' else 'OFF')
전열교환기.register_status(message_flag = '81', attr_name = 'speed',        topic_class ='speed_state_topic',       regex = r'000[01](0[0-3])0[013]00', process_func = lambda v: packet_2_payload_speed[v])
전열교환기.register_status(message_flag = 'c2', attr_name = 'speed',        topic_class ='speed_state_topic',       regex = r'000[01](0[0-3])0[013]00', process_func = lambda v: packet_2_payload_speed[v])
전열교환기.register_status(message_flag = '81', attr_name = 'heat',         topic_class ='oscillation_state_topic', regex = r'000[01]0[0-3](0[013])00', process_func = lambda v: packet_2_payload_oscillation[v])
전열교환기.register_status(message_flag = 'c3', attr_name = 'heat',         topic_class ='oscillation_state_topic', regex = r'000[01]0[0-3](0[013])00', process_func = lambda v: packet_2_payload_oscillation[v])

전열교환기.register_command(message_flag = '41', attr_name = 'power', topic_class = 'command_topic', process_func = lambda v: '01' if v =='ON' else '00')
전열교환기.register_command(message_flag = '42', attr_name = 'speed', topic_class = 'speed_command_topic', process_func = lambda v: {payload: packet for packet, payload in packet_2_payload_speed.items()}[v])
전열교환기.register_command(message_flag = '43', attr_name = 'heat',  topic_class = 'oscillation_command_topic', process_func = lambda v: {payload: packet for packet, payload in packet_2_payload_oscillation.items()}[v])

### 가스차단기 ###
optional_info = {'optimistic': 'false'}
가스 = wallpad.add_device(device_name = '가스', device_id = '12', device_subid = '01', device_class = 'switch', optional_info = optional_info)
가스.register_status(message_flag = '01', attr_name = 'availability', topic_class ='availability_topic', regex = r'()', process_func = lambda v: 'online')
가스.register_status(message_flag = '81', attr_name = 'power', topic_class ='state_topic', regex = r'00(0[12])', process_func = lambda v: 'ON' if v == '01' else 'OFF')
가스.register_status(message_flag = 'c1', attr_name = 'power', topic_class ='state_topic', regex = r'00(0[12])', process_func = lambda v: 'ON' if v == '01' else 'OFF')

가스.register_command(message_flag = '41', attr_name = 'power', topic_class = 'command_topic', process_func = lambda v: '01' if v == 'ON' else '00')

### 조명 ###
optional_info = {'optimistic': 'false'}
거실등1    = wallpad.add_device(device_name = '거실등1', device_id = '0e', device_subid = '11', device_class = 'light', optional_info = optional_info)
거실등2    = wallpad.add_device(device_name = '거실등2', device_id = '0e', device_subid = '12', device_class = 'light', optional_info = optional_info)
복도등     = wallpad.add_device(device_name = '복도등',  device_id = '0e', device_subid = '13', device_class = 'light', optional_info = optional_info)
침실등     = wallpad.add_device(device_name = '침실등',  device_id = '0e', device_subid = '21', device_class = 'light', optional_info = optional_info)
거실등전체 = wallpad.add_device(device_name = '거실등 전체', device_id = '0e', device_subid = '1f', device_class = 'light', mqtt_discovery = False, child_device = [거실등1, 거실등2, 복도등])
침실등전체 = wallpad.add_device(device_name = '침실등 전체', device_id = '0e', device_subid = '2f', device_class = 'light', mqtt_discovery = False, child_device = [침실등])

거실등전체.register_status(message_flag = '01', attr_name = 'availability', topic_class ='availability_topic', regex = r'()', process_func = lambda v: 'online')
침실등전체.register_status(message_flag = '01', attr_name = 'availability', topic_class ='availability_topic', regex = r'()', process_func = lambda v: 'online')

거실등1.register_status(message_flag = '81', attr_name = 'power', topic_class ='state_topic', regex = r'00(0[01])0[01]0[01]', process_func = lambda v: 'ON' if v == '01' else 'OFF')
거실등2.register_status(message_flag = '81', attr_name = 'power', topic_class ='state_topic', regex = r'000[01](0[01])0[01]', process_func = lambda v: 'ON' if v == '01' else 'OFF')
복도등.register_status( message_flag = '81', attr_name = 'power', topic_class ='state_topic', regex = r'000[01]0[01](0[01])', process_func = lambda v: 'ON' if v == '01' else 'OFF')
침실등.register_status( message_flag = '81', attr_name = 'power', topic_class ='state_topic', regex = r'000([01])', process_func = lambda v: 'ON' if v == '01' else 'OFF')

거실등1.register_status(message_flag = 'c1', attr_name = 'power', topic_class ='state_topic', regex = r'00(0[01])', process_func = lambda v: 'ON' if v == '01' else 'OFF')
거실등2.register_status(message_flag = 'c1', attr_name = 'power', topic_class ='state_topic', regex = r'00(0[01])', process_func = lambda v: 'ON' if v == '01' else 'OFF')
복도등.register_status( message_flag = 'c1', attr_name = 'power', topic_class ='state_topic', regex = r'00(0[01])', process_func = lambda v: 'ON' if v == '01' else 'OFF')
침실등.register_status( message_flag = 'c1', attr_name = 'power', topic_class ='state_topic', regex = r'00(0[01])', process_func = lambda v: 'ON' if v == '01' else 'OFF')

거실등1.register_command(message_flag = '41', attr_name = 'power', topic_class = 'command_topic', process_func = lambda v: '01' if v =='ON' else '00') # 'ON': '01' / 'OFF': '00'
거실등2.register_command(message_flag = '41', attr_name = 'power', topic_class = 'command_topic', process_func = lambda v: '01' if v =='ON' else '00')
복도등.register_command( message_flag = '41', attr_name = 'power', topic_class = 'command_topic', process_func = lambda v: '01' if v =='ON' else '00')
침실등.register_command( message_flag = '41', attr_name = 'power', topic_class = 'command_topic', process_func = lambda v: '01' if v =='ON' else '00')

### 난방 ###
optional_info = {'modes': ['off', 'heat'], 'temp_step': 0.5, 'precision': 0.5, 'min_temp': 5.0, 'max_temp': 40.0, 'send_if_off': 'false'}
거실난방 =  wallpad.add_device(device_name = '거실 난방',   device_id = '36', device_subid = '11', device_class = 'climate', optional_info = optional_info)
침실난방 =  wallpad.add_device(device_name = '침실 난방',   device_id = '36', device_subid = '12', device_class = 'climate', optional_info = optional_info)
서재난방 =  wallpad.add_device(device_name = '서재 난방',   device_id = '36', device_subid = '14', device_class = 'climate', optional_info = optional_info)
동굴난방 =  wallpad.add_device(device_name = '동굴 난방',   device_id = '36', device_subid = '13', device_class = 'climate', optional_info = optional_info)
알파룸난방= wallpad.add_device(device_name = '알파룸 난방', device_id = '36', device_subid = '15', device_class = 'climate', optional_info = optional_info)
난방전체 =  wallpad.add_device(device_name = '난방 전체',   device_id = '36', device_subid = '1f', device_class = 'climate', mqtt_discovery = False, child_device = [거실난방, 침실난방, 서재난방, 동굴난방, 알파룸난방])

난방전체.register_status(message_flag = '01', attr_name = 'availability', regex = r'()', topic_class ='availability_topic', process_func = lambda v: 'online')

for message_flag in ['81', 'c3', 'c4', 'c5']:
    거실난방.register_status(  message_flag = message_flag, attr_name = 'power', topic_class = 'mode_state_topic', regex = r'00(\d{2})\d{2}\d{4}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}', process_func = lambda v: 'heat' if format(int(v, 16), '05b')[4] == '1' else 'off')
    침실난방.register_status(  message_flag = message_flag, attr_name = 'power', topic_class = 'mode_state_topic', regex = r'00(\d{2})\d{2}\d{4}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}', process_func = lambda v: 'heat' if format(int(v, 16), '05b')[3] == '1' else 'off')
    동굴난방.register_status(  message_flag = message_flag, attr_name = 'power', topic_class = 'mode_state_topic', regex = r'00(\d{2})\d{2}\d{4}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}', process_func = lambda v: 'heat' if format(int(v, 16), '05b')[2] == '1' else 'off')
    서재난방.register_status(  message_flag = message_flag, attr_name = 'power', topic_class = 'mode_state_topic', regex = r'00(\d{2})\d{2}\d{4}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}', process_func = lambda v: 'heat' if format(int(v, 16), '05b')[1] == '1' else 'off')
    알파룸난방.register_status(message_flag = message_flag, attr_name = 'power', topic_class = 'mode_state_topic', regex = r'00(\d{2})\d{2}\d{4}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}', process_func = lambda v: 'heat' if format(int(v, 16), '05b')[0] == '1' else 'off')

    거실난방.register_status(  message_flag = message_flag, attr_name = 'away_mode', topic_class = 'away_mode_state_topic', regex = r'00\d{2}(\d{2})\d{4}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}', process_func = lambda v: 'ON' if format(int(v, 16), '05b')[4] == '1' else 'OFF')
    침실난방.register_status(  message_flag = message_flag, attr_name = 'away_mode', topic_class = 'away_mode_state_topic', regex = r'00\d{2}(\d{2})\d{4}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}', process_func = lambda v: 'ON' if format(int(v, 16), '05b')[3] == '1' else 'OFF')
    동굴난방.register_status(  message_flag = message_flag, attr_name = 'away_mode', topic_class = 'away_mode_state_topic', regex = r'00\d{2}(\d{2})\d{4}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}', process_func = lambda v: 'ON' if format(int(v, 16), '05b')[2] == '1' else 'OFF')
    서재난방.register_status(  message_flag = message_flag, attr_name = 'away_mode', topic_class = 'away_mode_state_topic', regex = r'00\d{2}(\d{2})\d{4}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}', process_func = lambda v: 'ON' if format(int(v, 16), '05b')[1] == '1' else 'OFF')
    알파룸난방.register_status(message_flag = message_flag, attr_name = 'away_mode', topic_class = 'away_mode_state_topic', regex = r'00\d{2}(\d{2})\d{4}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}', process_func = lambda v: 'ON' if format(int(v, 16), '05b')[0] == '1' else 'OFF')

    거실난방.register_status(  message_flag = message_flag, attr_name = 'targettemp',  topic_class ='temperature_state_topic',   regex = r'00\d{2}\d{2}\d{4}([\da-f]{2})[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}', process_func = lambda v: int(v, 16) % 128 + int(v, 16) // 128 * 0.5)
    침실난방.register_status(  message_flag = message_flag, attr_name = 'targettemp',  topic_class ='temperature_state_topic',   regex = r'00\d{2}\d{2}\d{4}[\da-f]{2}[\da-f]{2}([\da-f]{2})[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}', process_func = lambda v: int(v, 16) % 128 + int(v, 16) // 128 * 0.5)
    동굴난방.register_status(  message_flag = message_flag, attr_name = 'targettemp',  topic_class ='temperature_state_topic',   regex = r'00\d{2}\d{2}\d{4}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}([\da-f]{2})[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}', process_func = lambda v: int(v, 16) % 128 + int(v, 16) // 128 * 0.5)
    서재난방.register_status(  message_flag = message_flag, attr_name = 'targettemp',  topic_class ='temperature_state_topic',   regex = r'00\d{2}\d{2}\d{4}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}([\da-f]{2})[\da-f]{2}[\da-f]{2}[\da-f]{2}', process_func = lambda v: int(v, 16) % 128 + int(v, 16) // 128 * 0.5)
    알파룸난방.register_status(message_flag = message_flag, attr_name = 'targettemp',  topic_class ='temperature_state_topic',   regex = r'00\d{2}\d{2}\d{4}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}([\da-f]{2})[\da-f]{2}', process_func = lambda v: int(v, 16) % 128 + int(v, 16) // 128 * 0.5)

    거실난방.register_status(  message_flag = message_flag, attr_name = 'currenttemp', topic_class ='current_temperature_topic', regex = r'00\d{2}\d{2}\d{4}[\da-f]{2}([\da-f]{2})[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}', process_func = lambda v: int(v, 16) % 128 + int(v, 16) // 128 * 0.5)
    침실난방.register_status(  message_flag = message_flag, attr_name = 'currenttemp', topic_class ='current_temperature_topic', regex = r'00\d{2}\d{2}\d{4}[\da-f]{2}[\da-f]{2}[\da-f]{2}([\da-f]{2})[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}', process_func = lambda v: int(v, 16) % 128 + int(v, 16) // 128 * 0.5)
    동굴난방.register_status(  message_flag = message_flag, attr_name = 'currenttemp', topic_class ='current_temperature_topic', regex = r'00\d{2}\d{2}\d{4}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}([\da-f]{2})[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}', process_func = lambda v: int(v, 16) % 128 + int(v, 16) // 128 * 0.5)
    서재난방.register_status(  message_flag = message_flag, attr_name = 'currenttemp', topic_class ='current_temperature_topic', regex = r'00\d{2}\d{2}\d{4}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}([\da-f]{2})[\da-f]{2}[\da-f]{2}', process_func = lambda v: int(v, 16) % 128 + int(v, 16) // 128 * 0.5)
    알파룸난방.register_status(message_flag = message_flag, attr_name = 'currenttemp', topic_class ='current_temperature_topic', regex = r'00\d{2}\d{2}\d{4}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}[\da-f]{2}([\da-f]{2})', process_func = lambda v: int(v, 16) % 128 + int(v, 16) // 128 * 0.5)

난방전체.register_command(message_flag = '43', attr_name = 'power', topic_class = 'mode_command_topic', process_func = lambda v: '01' if v == 'heat' else '00')

거실난방.register_command(message_flag = '43', attr_name = 'power', topic_class = 'mode_command_topic', process_func = lambda v: '01' if v == 'heat' else '00')
거실난방.register_command(message_flag = '44', attr_name = 'targettemp', topic_class = 'temperature_command_topic', process_func = lambda v: format(int(float(v) // 1 + float(v) % 1 * 128 * 2), '02x'))
거실난방.register_command(message_flag = '45', attr_name = 'away_mode', topic_class = 'away_mode_command_topic', process_func = lambda v: '01' if v =='ON' else '00')

침실난방.register_command(message_flag = '43', attr_name = 'power', topic_class = 'mode_command_topic', process_func = lambda v: '01' if v == 'heat' else '00')
침실난방.register_command(message_flag = '44', attr_name = 'targettemp', topic_class = 'temperature_command_topic', process_func = lambda v: format(int(float(v) // 1 + float(v) % 1 * 128 * 2), '02x'))
침실난방.register_command(message_flag = '45', attr_name = 'away_mode', topic_class = 'away_mode_command_topic', process_func = lambda v: '01' if v =='ON' else '00')

서재난방.register_command(message_flag = '43', attr_name = 'power', topic_class = 'mode_command_topic', process_func = lambda v: '01' if v == 'heat' else '00')
서재난방.register_command(message_flag = '44', attr_name = 'targettemp', topic_class = 'temperature_command_topic', process_func = lambda v: format(int(float(v) // 1 + float(v) % 1 * 128 * 2), '02x'))
서재난방.register_command(message_flag = '45', attr_name = 'away_mode', topic_class = 'away_mode_command_topic', process_func = lambda v: '01' if v =='ON' else '00')

동굴난방.register_command(message_flag = '43', attr_name = 'power', topic_class = 'mode_command_topic', process_func = lambda v: '01' if v == 'heat' else '00') # { 'ON': '01', 'OFF': '00' }
동굴난방.register_command(message_flag = '44', attr_name = 'targettemp', topic_class = 'temperature_command_topic', process_func = lambda v: format(int(float(v) // 1 + float(v) % 1 * 128 * 2), '02x'))
동굴난방.register_command(message_flag = '45', attr_name = 'away_mode', topic_class = 'away_mode_command_topic', process_func = lambda v: '01' if v =='ON' else '00')

알파룸난방.register_command(message_flag = '43', attr_name = 'power', topic_class = 'mode_command_topic', process_func = lambda v: '01' if v == 'heat' else '00') # , { 'ON': '01', 'OFF': '00' }
알파룸난방.register_command(message_flag = '44', attr_name = 'targettemp', topic_class = 'temperature_command_topic', process_func = lambda v: format(int(float(v) // 1 + float(v) % 1 * 128 * 2), '02x'))
알파룸난방.register_command(message_flag = '45', attr_name = 'away_mode', topic_class = 'away_mode_command_topic', process_func = lambda v: '01' if v =='ON' else '00')

### 엘리베이터 ###
# 엘리베이터, 일괄 제어 용도의 패킷이지만 엘리베이터 호출 용도로만 사용해도 무방
optional_info = {'optimistic': 'false'}
엘리베이터 = wallpad.add_device(device_name = '엘리베이터', device_id = '33', device_subid = '01', device_class = 'switch', optional_info = optional_info)
엘리베이터.register_status(message_flag = '01', attr_name = 'power', topic_class ='state_topic', regex = r'(0[01])', process_func = lambda v: 'OFF')
엘리베이터.register_status(message_flag = '01', attr_name = 'availability', topic_class ='availability_topic', regex = r'(0[01])', process_func = lambda v: 'online')
엘리베이터.register_command(message_flag = '43', attr_name = 'power', topic_class = 'command_topic', process_func = lambda v: '10' if v == 'ON' else '10') # 엘리베이터 호출 # F7 33 01 43 01 10 97 16

wallpad.listen()

import socket
import serial
import paho.mqtt.client as paho_mqtt
import json

import sys
import time
import logging
from logging.handlers import TimedRotatingFileHandler
import os.path
import re

RS485_DEVICE = {
    # 전등 스위치
    "light": {
        "query":    { "id": 0x0E, "cmd": 0x01, },
        "state":    { "id": 0x0E, "cmd": 0x81, },
        "last":     { },

        "power":    { "id": 0x0E, "cmd": 0x41, "ack": 0xC1, },
    },
    # 각방 난방 제어
    "thermostat": {
        "query":    { "id": 0x36, "cmd": 0x01, },
        "state":    { "id": 0x36, "cmd": 0x81, },
        "last":     { },

        "away":    { "id": 0x36, "cmd": 0x45, "ack": 0x00, },
        "target":   { "id": 0x36, "cmd": 0x44, "ack": 0xC4, },
    },
}

DISCOVERY_DEVICE = {
    "ids": ["ezville_wallpad",],
    "name": "ezville_wallpad",
    "mf": "EzVille",
    "mdl": "EzVille Wallpad",
    "sw": "oopswow/ha_addons/ezville_wallpad",
}


DISCOVERY_PAYLOAD = {
    "light": [ {
        "_intg": "light",
        "~": "{prefix}/light/{grp}_{rm}_{id}",
        "name": "{prefix}_light_{grp}_{rm}_{id}",
        "opt": True,
        "stat_t": "~/power/state",
        "cmd_t": "~/power/command",
    } ],
    "thermostat": [ {
        "_intg": "climate",
        "~": "{prefix}/thermostat/{grp}_{id}",
        "name": "{prefix}_thermostat_{grp}_{id}",
        "mode_stat_t": "~/power/state",
        "temp_stat_t": "~/target/state",
        "temp_cmd_t": "~/target/command",
        "curr_temp_t": "~/current/state",
        "away_stat_t": "~/away/state",
        "away_cmd_t": "~/away/command",
        "modes": [ "off", "heat" ],
        "min_temp": 5,
        "max_temp": 40,
    } ],
}

STATE_HEADER = {
    prop["state"]["id"]: (device, prop["state"]["cmd"])
    for device, prop in RS485_DEVICE.items()
    if "state" in prop
}
QUERY_HEADER = {
    prop["query"]["id"]: (device, prop["query"]["cmd"])
    for device, prop in RS485_DEVICE.items()
    if "query" in prop
}
# 제어 명령의 ACK header만 모음
ACK_HEADER = {
    prop[cmd]["id"]: (device, prop[cmd]["ack"])
    for device, prop in RS485_DEVICE.items()
        for cmd, code in prop.items()
            if "ack" in code
}

ACK_MAP = {}
for device, prop in RS485_DEVICE.items():
    for cmd, code in prop.items():
        if "ack" in code:
            ACK_MAP[code["id"]] = {}
            ACK_MAP[code["id"]][code["cmd"]] = {}
            ACK_MAP[code["id"]][code["cmd"]] = code["ack"]

# KTDO: Ezville에서는 가스밸브 STATE Query 코드로 처리
HEADER_0_FIRST = [ [0x12, 0x01], [0x12, 0x0F] ]
# KTDO: Virtual은 Skip
#header_0_virtual = {}
# KTDO: 아래 미사용으로 코멘트 처리
#HEADER_1_SCAN = 0x5A
header_0_first_candidate = [ [[0x33, 0x01], [0x33, 0x0F]], [[0x36, 0x01], [0x36, 0x0F]] ]


serial_queue = {}
serial_ack = {}

last_query = int(0).to_bytes(2, "big")
last_topic_list = {}

mqtt = paho_mqtt.Client()
mqtt_connected = False

logger = logging.getLogger(__name__)

class EzVilleSerial:
    def __init__(self):
        self._ser = serial.Serial()
        self._ser.port = Options["serial"]["port"]
        self._ser.baudrate = Options["serial"]["baudrate"]
        self._ser.bytesize = Options["serial"]["bytesize"]
        self._ser.parity = Options["serial"]["parity"]
        self._ser.stopbits = Options["serial"]["stopbits"]

        self._ser.close()
        self._ser.open()

        self._pending_recv = 0

        # 시리얼에 뭐가 떠다니는지 확인
        self.set_timeout(5.0)
        data = self._recv_raw(1)
        self.set_timeout(None)
        if not data:
            logger.critical("no active packet at this serial port!")

    def _recv_raw(self, count=1):
        return self._ser.read(count)

    def recv(self, count=1):
        # serial은 pending count만 업데이트
        self._pending_recv = max(self._pending_recv - count, 0)
        return self._recv_raw(count)

    def send(self, a):
        self._ser.write(a)

    def set_pending_recv(self):
        self._pending_recv = self._ser.in_waiting

    def check_pending_recv(self):
        return self._pending_recv

    def check_in_waiting(self):
        return self._ser.in_waiting

    def set_timeout(self, a):
        self._ser.timeout = a

class EzVilleSocket:
    def __init__(self):
        addr = Options["socket"]["address"]
        port = Options["socket"]["port"]

        self._soc = socket.socket()
        self._soc.connect((addr, port))

        self._recv_buf = bytearray()
        self._pending_recv = 0

        # 소켓에 뭐가 떠다니는지 확인
        self.set_timeout(5.0)
        data = self._recv_raw(1)
        self.set_timeout(None)
        if not data:
            logger.critical("no active packet at this socket!")

    def _recv_raw(self, count=1):
        return self._soc.recv(count)

    def recv(self, count=1):
        # socket은 버퍼와 in_waiting 직접 관리
        if len(self._recv_buf) < count:
            new_data = self._recv_raw(256)
            self._recv_buf.extend(new_data)
        if len(self._recv_buf) < count:
            return None

        self._pending_recv = max(self._pending_recv - count, 0)

        res = self._recv_buf[0:count]
        del self._recv_buf[0:count]
        return res

    def send(self, a):
        self._soc.sendall(a)

    def set_pending_recv(self):
        self._pending_recv = len(self._recv_buf)

    def check_pending_recv(self):
        return self._pending_recv

    def check_in_waiting(self):
        if len(self._recv_buf) == 0:
            new_data = self._recv_raw(256)
            self._recv_buf.extend(new_data)
        return len(self._recv_buf)

    def set_timeout(self, a):
        self._soc.settimeout(a)

def init_logger():
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(fmt="%(asctime)s %(levelname)-8s %(message)s", datefmt="%H:%M:%S")
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logger.addHandler(handler)

def init_logger_file():
    if Options["log"]["to_file"]:
        filename = Options["log"]["filename"]
        os.makedirs(os.path.dirname(filename), exist_ok=True)

        formatter = logging.Formatter(fmt="%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        handler = TimedRotatingFileHandler(os.path.abspath(Options["log"]["filename"]), when="midnight", backupCount=7)
        handler.setFormatter(formatter)
        handler.suffix = '%Y%m%d'
        logger.addHandler(handler)

def init_option(argv):
    # option 파일 선택
    if len(argv) == 1:
        option_file = "./options_standalone.json"
    else:
        option_file = argv[1]

    # configuration이 예전 버전이어도 최대한 동작 가능하도록,
    # 기본값에 해당하는 파일을 먼저 읽고나서 설정 파일로 업데이트 한다.
    global Options

    # 기본값 파일은 .py 와 같은 경로에 있음
    default_file = os.path.join(os.path.dirname(os.path.abspath(argv[0])), "config.json")

    with open(default_file) as f:
        config = json.load(f)
        logger.info("addon version {}".format(config["version"]))
        Options = config["options"]
    with open(option_file) as f:
        Options2 = json.load(f)

    # 업데이트
    for k, v in Options.items():
        if type(v) is dict and k in Options2:
            Options[k].update(Options2[k])
            for k2 in Options[k].keys():
                if k2 not in Options2[k].keys():
                    logger.warning("no configuration value for '{}:{}'! try default value ({})...".format(k, k2, Options[k][k2]))
        else:
            if k not in Options2:
                logger.warning("no configuration value for '{}'! try default value ({})...".format(k, Options[k]))
            else:
                Options[k] = Options2[k]

    # 관용성 확보
    Options["mqtt"]["server"] = re.sub("[a-z]*://", "", Options["mqtt"]["server"])
    if Options["mqtt"]["server"] == "127.0.0.1":
        logger.warning("MQTT server address should be changed!")

    # internal options
    Options["mqtt"]["_discovery"] = Options["mqtt"]["discovery"]

def mqtt_discovery(payload):
    intg = payload.pop("_intg")

    # MQTT 통합구성요소에 등록되기 위한 추가 내용
    payload["device"] = DISCOVERY_DEVICE
    payload["uniq_id"] = payload["name"]

    # discovery에 등록
    topic = "homeassistant/{}/ezville_wallpad/{}/config".format(intg, payload["name"])
    logger.info("Add new device:  {}".format(topic))
    mqtt.publish(topic, json.dumps(payload))

def mqtt_debug(topics, payload):
    device = topics[2]
    command = topics[3]

    if (device == "packet"):
        if (command == "send"):
            # parity는 여기서 재생성
            packet = bytearray.fromhex(payload)
            packet[-2], packet[-1] = serial_generate_checksum(packet)
            packet = bytes(packet)

            logger.info("prepare packet:  {}".format(packet.hex()))
            serial_queue[packet] = time.time()

def mqtt_device(topics, payload):
    device = topics[1]
    idn = topics[2]
    cmd = topics[3]

    # HA에서 잘못 보내는 경우 체크
    if device not in RS485_DEVICE:
        logger.error("    unknown device!"); return
    if cmd not in RS485_DEVICE[device]:
        logger.error("    unknown command!"); return
    if payload == "":
        logger.error("    no payload!"); return

    # ON, OFF인 경우만 1, 0으로 변환, 복잡한 경우 (fan 등) 는 값으로 받자
    if payload == "ON": payload = "1"
    elif payload == "OFF": payload = "0"
    elif payload == "heat": payload = "1"
    elif payload == "off": payload = "0"

    # 오류 체크 끝났으면 serial 메시지 생성
    cmd = RS485_DEVICE[device][cmd]
    
    if device == "light":
        length = 10
        packet = bytearray(length)
        packet[0] = 0xF7
        packet[1] = cmd["id"]
        packet[2] = int(idn.split("_")[0]) << 4 | int(idn.split("_")[1])
        packet[3] = cmd["cmd"]
        packet[4] = 0x03
        packet[5] = int(idn.split("_")[2])
        packet[6] = int(float(payload))
        packet[7] = 0x00
        packet[8], packet[9] = serial_generate_checksum(packet)

    elif device == "thermostat":
        length = 8
        packet = bytearray(length)
        packet[0] = 0xF7
        packet[1] = cmd["id"]
        packet[2] = int(idn.split("_")[0]) << 4 | int(idn.split("_")[1])
        packet[3] = cmd["cmd"]
        packet[4] = 0x01
        packet[5] = int(float(payload))
        packet[6], packet[7] = serial_generate_checksum(packet)
    
    packet = bytes(packet)
    serial_queue[packet] = time.time()


def mqtt_init_discovery():
    # HA가 재시작됐을 때 모든 discovery를 다시 수행한다
    Options["mqtt"]["_discovery"] = Options["mqtt"]["discovery"]
    for device in RS485_DEVICE:
        RS485_DEVICE[device]["last"] = {}

    global last_topic_list
    last_topic_list = {}

    
def mqtt_on_message(mqtt, userdata, msg):
    topics = msg.topic.split("/")
    payload = msg.payload.decode()

    logger.info("recv. from HA:   {} = {}".format(msg.topic, payload))

    device = topics[1]
    if device == "status":
        if payload == "online":
            mqtt_init_discovery()
    elif device == "debug":
        mqtt_debug(topics, payload)
    else:
        mqtt_device(topics, payload)

        
def mqtt_on_connect(mqtt, userdata, flags, rc):
    if rc == 0:
        logger.info("MQTT connect successful!")
        global mqtt_connected
        mqtt_connected = True
    else:
        logger.error("MQTT connection return with:  {}".format(paho_mqtt.connack_string(rc)))

    mqtt_init_discovery()

    topic = "homeassistant/status"
    logger.info("subscribe {}".format(topic))
    mqtt.subscribe(topic, 0)

    prefix = Options["mqtt"]["prefix"]

    if Options["wallpad_mode"] != "off":
        topic = "{}/+/+/+/command".format(prefix)
        logger.info("subscribe {}".format(topic))
        mqtt.subscribe(topic, 0)

def mqtt_on_disconnect(mqtt, userdata, rc):
    logger.warning("MQTT disconnected! ({})".format(rc))
    global mqtt_connected
    mqtt_connected = False

def start_mqtt_loop():
    logger.info("initialize mqtt...")

    mqtt.on_message = mqtt_on_message
    mqtt.on_connect = mqtt_on_connect
    mqtt.on_disconnect = mqtt_on_disconnect

    if Options["mqtt"]["need_login"]:
        mqtt.username_pw_set(Options["mqtt"]["user"], Options["mqtt"]["passwd"])

    try:
        mqtt.connect(Options["mqtt"]["server"], Options["mqtt"]["port"])
    except Exception as e:
        logger.error("MQTT server address/port may be incorrect! ({})".format(str(e)))
        sys.exit(1)

    mqtt.loop_start()

    delay = 1
    while not mqtt_connected:
        logger.info("waiting MQTT connected ...")
        time.sleep(delay)
        delay = min(delay * 2, 10)

def serial_verify_checksum(packet):
    # 모든 byte를 XOR
    # KTDO: 마지막 ADD 빼고 XOR
    checksum = 0
    for b in packet[:-1]:
        checksum ^= b
        
    # KTDO: ADD 계산
    add = sum(packet[:-1]) & 0xFF

    # parity의 최상위 bit는 항상 0
    # KTDO: EzVille은 아님
    #if checksum >= 0x80: checksum -= 0x80

    # checksum이 안맞으면 로그만 찍고 무시
    # KTDO: ADD 까지 맞아야함.
    if checksum or add != packet[-1]:
        logger.warning("checksum fail! {}, {:02x}, {:02x}".format(packet.hex(), checksum, add))
        return False

    # 정상
    return True

def serial_generate_checksum(packet):
    # 마지막 제외하고 모든 byte를 XOR
    checksum = 0
    for b in packet[:-1]:
        checksum ^= b
        
    # KTDO: add 추가 생성 
    add = (sum(packet) + checksum) & 0xFF 
    
    return checksum, add

def serial_new_device(device, packet):
    prefix = Options["mqtt"]["prefix"]

    # 조명은 두 id를 조합해서 개수와 번호를 정해야 함
    if device == "light":
        # KTDO: EzVille에 맞게 수정
        grp_id = int(packet[2] >> 4)
        rm_id = int(packet[2] & 0x0F)
        light_count = int(packet[4]) - 1
        
        #id2 = last_query[3]
        #num = idn >> 4
        #idn = int("{:x}".format(idn))

        for id in range(1, light_count + 1):
            payload = DISCOVERY_PAYLOAD[device][0].copy()
            payload["~"] = payload["~"].format(prefix=prefix, grp=grp_id, rm=rm_id, id=id)
            payload["name"] = payload["name"].format(prefix=prefix, grp=grp_id, rm=rm_id, id=id)

            mqtt_discovery(payload)
            
    elif device == "thermostat":
        # KTDO: EzVille에 맞게 수정
        grp_id = int(packet[2] >> 4)
        room_count = int((int(packet[4]) - 5) / 2)
        
        for id in range(1, room_count + 1):
            payload = DISCOVERY_PAYLOAD[device][0].copy()
            payload["~"] = payload["~"].format(prefix=prefix, grp=grp_id, id=id)
            payload["name"] = payload["name"].format(prefix=prefix, grp=grp_id, id=id)

            mqtt_discovery(payload)

def serial_receive_state(device, packet):
    form = RS485_DEVICE[device]["state"]
    last = RS485_DEVICE[device]["last"]
    idn = (packet[1] << 8) | packet[2]

    # 해당 ID의 이전 상태와 같은 경우 바로 무시
    if last.get(idn) == packet:
        return

    # 처음 받은 상태인 경우, discovery 용도로 등록한다.
    if Options["mqtt"]["_discovery"] and not last.get(idn):
        
        serial_new_device(device, packet)
        last[idn] = True

        # 장치 등록 먼저 하고, 상태 등록은 그 다음 턴에 한다. (난방 상태 등록 무시되는 현상 방지)
        return

    else:
        last[idn] = packet

# KTDO: 아래 코드로 값을 바로 판별
    prefix = Options["mqtt"]["prefix"]
    
    if device == "light":
        grp_id = int(packet[2] >> 4)
        rm_id = int(packet[2] & 0x0F)
        light_count = int(packet[4]) - 1
        
        for id in range(1, light_count + 1):
            topic = "{}/{}/{}_{}_{}/power/state".format(prefix, device, grp_id, rm_id, id)
            
            if packet[5+id] & 1:
                value = "ON"
            else:
                value = "OFF"
                
            if last_topic_list.get(topic) != value:
                logger.info("publish to HA:   {} = {} ({})".format(topic, value, packet.hex()))
                mqtt.publish(topic, value)
                last_topic_list[topic] = value
            
    elif device == "thermostat":
        grp_id = int(packet[2] >> 4)
        room_count = int((int(packet[4]) - 5) / 2)
        
        for id in range(1, room_count + 1):
            topic1 = "{}/{}/{}_{}/power/state".format(prefix, device, grp_id, id)
            topic2 = "{}/{}/{}_{}/away/state".format(prefix, device, grp_id, id)
            topic3 = "{}/{}/{}_{}/target/state".format(prefix, device, grp_id, id)
            topic4 = "{}/{}/{}_{}/current/state".format(prefix, device, grp_id, id)
            
            if ((packet[6] & 0x1F) >> (room_count - id)) & 1:
                value1 = "ON"
            else:
                value1 = "OFF"
            if ((packet[7] & 0x1F) >> (room_count - id)) & 1:
                value2 = "ON"
            else:
                value2 = "OFF"
            #value3 = packet[8 + id * 2]
            if (packet[8 + id * 2] >> 7 ):
                value3 = (packet[8 + id *2] & 0x1F) + 0.5
            else:
                value3 = (packet[8 + id *2] & 0x1F)
            #value4 = packet[9 + id * 2]
            if (packet[9 + id * 2] >> 7 ):
                value4 = (packet[9 + id *2] & 0x1F) + 0.5
            else:
                value4 = (packet[9 + id *2] & 0x1F)
            
            if last_topic_list.get(topic1) != value1:
                logger.info("publish to HA:   {} = {} ({})".format(topic1, value1, packet.hex()))
                mqtt.publish(topic1, value1)
                last_topic_list[topic1] = value1
            if last_topic_list.get(topic2) != value2:
                logger.info("publish to HA:   {} = {} ({})".format(topic2, value2, packet.hex()))
                mqtt.publish(topic2, value2)
                last_topic_list[topic2] = value2
            if last_topic_list.get(topic3) != value3:
                logger.info("publish to HA:   {} = {} ({})".format(topic3, value3, packet.hex()))
                mqtt.publish(topic3, value3)
                last_topic_list[topic3] = value3
            if last_topic_list.get(topic4) != value4:
                logger.info("publish to HA:   {} = {} ({})".format(topic4, value4, packet.hex()))
                mqtt.publish(topic4, value4)
                last_topic_list[topic4] = value4

def serial_get_header():
    try:
        # 0x80보다 큰 byte가 나올 때까지 대기
        # KTDO: 시작 F7 찾기
        while 1:
            header_0 = conn.recv(1)[0]
            #if header_0 >= 0x80: break
            if header_0 == 0xF7: break

        # 중간에 corrupt되는 data가 있으므로 연속으로 0x80보다 큰 byte가 나오면 먼젓번은 무시한다
        # KTDO: 연속 0xF7 무시                                           
        while 1:
            header_1 = conn.recv(1)[0]
            #if header_1 < 0x80: break
            if header_1 != 0xF7: break
            header_0 = header_1
        
        header_2 = conn.recv(1)[0]
        header_3 = conn.recv(1)[0]
        
    except (OSError, serial.SerialException):
        logger.error("ignore exception!")
        header_0 = header_1 = header_2 = header_3 = 0

    # 헤더 반환
    return header_0, header_1, header_2, header_3


def serial_ack_command(packet):
    logger.info("ack from device: {} ({:x})".format(serial_ack[packet].hex(), packet))

    # 성공한 명령을 지움
    serial_queue.pop(serial_ack[packet], None)
    serial_ack.pop(packet)

def serial_send_command():
    # 한번에 여러개 보내면 응답이랑 꼬여서 망함
    cmd = next(iter(serial_queue))
    conn.send(cmd)

    ack = bytearray(cmd[0:4])
    ack[3] = ACK_MAP[cmd[1]][cmd[3]]
    waive_ack = False
    if ack[3] == 0x00:
        waive_ack = True
    ack = int.from_bytes(ack, "big")

    # retry time 관리, 초과했으면 제거
    elapsed = time.time() - serial_queue[cmd]
    if elapsed > Options["rs485"]["max_retry"]:
        logger.error("send to device:  {} max retry time exceeded!".format(cmd.hex()))
        serial_queue.pop(cmd)
        serial_ack.pop(ack, None)
    elif elapsed > 3:
        logger.warning("send to device:  {}, try another {:.01f} seconds...".format(cmd.hex(), Options["rs485"]["max_retry"] - elapsed))
        serial_ack[ack] = cmd
    elif waive_ack:
        logger.info("waive ack:  {}".format(cmd.hex()))
        serial_queue.pop(cmd)
        serial_ack.pop(ack, None)
    else:
        logger.info("send to device:  {}".format(cmd.hex()))
        serial_ack[ack] = cmd

def serial_loop():
    logger.info("start loop ...")
    loop_count = 0
    scan_count = 0
    send_aggressive = False

    start_time = time.time()
    while True:
        # 로그 출력
        sys.stdout.flush()
        header_0, header_1, header_2, header_3 = serial_get_header()

        # device로부터의 state 응답이면 확인해서 필요시 HA로 전송해야 함
        if header_1 in STATE_HEADER and header_3 in STATE_HEADER[header_1]:
            #packet = bytes([header_0, header_1])

            # 몇 Byte짜리 패킷인지 확인
            #device, remain = STATE_HEADER[header]
            device = STATE_HEADER[header_1][0]
            # KTDO: 데이터 길이는 다음 패킷에서 확인
            header_4 = conn.recv(1)[0]
            data_length = int(header_4)
            
            # KTDO: packet 생성 위치 변경
            packet = bytes([header_0, header_1, header_2, header_3, header_4])
            
            # 해당 길이만큼 읽음
            # KTDO: 데이터 길이 + 2 (XOR + ADD) 만큼 읽음
            packet += conn.recv(data_length + 2)

            # checksum 오류 없는지 확인
            # KTDO: checksum 및 ADD 오류 없는지 확인 
            if not serial_verify_checksum(packet):
                continue

            # 디바이스 응답 뒤에도 명령 보내봄
            if serial_queue and not conn.check_pending_recv():
                serial_send_command()
                conn.set_pending_recv()

            # 적절히 처리한다
            serial_receive_state(device, packet)

        # KTDO: 이전 명령의 ACK 경우
        ## thermostat ACK 관련 변경 작업 필요
        elif header_1 in ACK_HEADER and header_3 in ACK_HEADER[header_1]:
            header = header_0 << 24 | header_1 << 16 | header_2 << 8 | header_3

            if header in serial_ack:
                serial_ack_command(header)
        
        # KTDO: EzVille은 표준에 따라 Ack 이후 다음 Request 까지의 시간 활용하여 command 전송
        #       즉 State 확인 후에만 전달
        elif (header_3 == 0x81 or 0x8F or 0x0F) or send_aggressive:
            scan_count += 1
            if serial_queue and not conn.check_pending_recv():
                serial_send_command()
                conn.set_pending_recv()

        # 전체 루프 수 카운트
        # KTDO: 가스 밸브 쿼리로 확인
        global HEADER_0_FIRST
        # KTDO: 2번째 Header가 장치 Header임
        if header_1 == HEADER_0_FIRST[0][0] and (header_3 == HEADER_0_FIRST[0][1] or header_3 == HEADER_0_FIRST[1][1]):
            loop_count += 1

            # 돌만큼 돌았으면 상황 판단
            if loop_count == 30:
                # discovery: 가끔 비트가 튈때 이상한 장치가 등록되는걸 막기 위해, 시간제한을 둠
                if Options["mqtt"]["_discovery"]:
                    logger.info("Add new device:  All done.")
                    Options["mqtt"]["_discovery"] = False
                else:
                    logger.info("running stable...")

                # 스캔이 없거나 적으면, 명령을 내릴 타이밍을 못잡는걸로 판단, 아무때나 닥치는대로 보내봐야한다.
                if Options["serial_mode"] == "serial" and scan_count < 30:
                    logger.warning("initiate aggressive send mode!", scan_count)
                    send_aggressive = True

            # HA 재시작한 경우
            elif loop_count > 30 and Options["mqtt"]["_discovery"]:
                loop_count = 1

        # 루프 카운트 세는데 실패하면 다른 걸로 시도해봄
        if loop_count == 0 and time.time() - start_time > 6:
            print("check loop count fail: there are no F7 {:02X} ** {:02X} or F7 {:02X} ** {:02X}! try F7 {:02X} ** {:02X} or F7 {:02X} ** {:02X}...".format(HEADER_0_FIRST[0][0],HEADER_0_FIRST[0][1],HEADER_0_FIRST[1][0],HEADER_0_FIRST[1][1],header_0_first_candidate[-1][0][0],header_0_first_candidate[-1][0][1],header_0_first_candidate[-1][1][0],header_0_first_candidate[-1][1][1]))
            HEADER_0_FIRST = header_0_first_candidate.pop()
            start_time = time.time()
            scan_count = 0

def dump_loop():
    dump_time = Options["rs485"]["dump_time"]

    if dump_time > 0:
        if dump_time < 10:
            logger.warning("dump_time is too short! automatically changed to 10 seconds...")
            dump_time = 10

        start_time = time.time()
        logger.warning("packet dump for {} seconds!".format(dump_time))

        conn.set_timeout(2)
        logs = []
        while time.time() - start_time < dump_time:
            try:
                data = conn.recv(256)
            except:
                continue

            if data:
                for b in data:
                    if b == 0xF7 or len(logs) > 500:
                        logger.info("".join(logs))
                        logs = ["{:02X}".format(b)]
                    else:           logs.append(",  {:02X}".format(b))
        logger.info("".join(logs))
        logger.warning("dump done.")
        conn.set_timeout(None)


if __name__ == "__main__":
    global conn

    # configuration 로드 및 로거 설정
    init_logger()
    init_option(sys.argv)
    init_logger_file()

    if Options["serial_mode"] == "socket":
        logger.info("initialize socket...")
        conn = EzVilleSocket()
    else:
        logger.info("initialize serial...")
        conn = EzVilleSerial()

    dump_loop()

    start_mqtt_loop()

    try:
        # 무한 루프
        serial_loop()
    except:
        logger.exception("addon finished!")

import asyncio
import copy
import hmac
import json
import math
import os
import re
import secrets
import socket
import sys
import time
import traceback
import uuid
from concurrent.futures import TimeoutError as FutureTimeoutError
from functools import wraps
from pathlib import Path
from threading import Event, Thread

import yaml
from loguru import logger
from srv.advanced_chatbox_manager import AdvancedChatboxManager

from flask import Flask, jsonify, redirect, render_template, request
from websockets.asyncio.server import serve as wsserve
from werkzeug.exceptions import HTTPException

import srv
from srv.connector.coyotev3ws import DGConnection
from srv.handler.shock_handler import ShockHandler
from srv.handler.machine_handler import TuyaHandler, TuYaConnection

from pythonosc.osc_server import AsyncIOOSCUDPServer
from pythonosc.dispatcher import Dispatcher

BUNDLE_DIR = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
app = Flask(__name__, template_folder=str(BUNDLE_DIR / 'templates'))

CONFIG_FILE_VERSION  = 'v0.2'
CONFIG_FILENAME = APP_DIR / f'settings-advanced-{CONFIG_FILE_VERSION}.yaml'
CONFIG_FILENAME_BASIC = APP_DIR / f'settings-{CONFIG_FILE_VERSION}.yaml'
SETTINGS_BASIC = {
    'dglab3':{
        'channel_a': {
            'avatar_params': [
                '/avatar/parameters/pcs/contact/enterPass',
                '/avatar/parameters/Shock/TouchAreaA',
                '/avatar/parameters/Shock/TouchAreaC',
                '/avatar/parameters/Shock/wildcard/*',
            ],
            'mode': 'distance',
            'strength_limit': 100,
        },
        'channel_b': {
            'avatar_params': [
                '/avatar/parameters/pcs/contact/enterPass',
                '/avatar/parameters/lms-penis-proximityA*',
                '/avatar/parameters/Shock/TouchAreaB',
                '/avatar/parameters/Shock/TouchAreaC',
            ],
            'mode': 'distance',
            'strength_limit': 100,
        }
    },
    'version': CONFIG_FILE_VERSION,
}
SETTINGS = {
    'SERVER_IP': None,
    'dglab3': {
        'channel_a': {
            'mode_config':{
                'shock': {
                    'duration': 2,
                    'wave': '["0A0A0A0A64646464","0A0A0A0A64646464","0A0A0A0A64646464","0A0A0A0A64646464","0A0A0A0A64646464","0A0A0A0A64646464","0A0A0A0A64646464","0A0A0A0A64646464","0A0A0A0A64646464","0A0A0A0A64646464"]',
                },
                'distance': {
                    'freq_ms': 10,
                },
                'trigger_range': {
                    'bottom': 0.0,
                    'top': 1.0,
                },
            }
        },
        'channel_b': {
            'mode_config':{
                'shock': {
                    'duration': 2,
                    'wave': '["0A0A0A0A64646464","0A0A0A0A64646464","0A0A0A0A64646464","0A0A0A0A64646464","0A0A0A0A64646464","0A0A0A0A64646464","0A0A0A0A64646464","0A0A0A0A64646464","0A0A0A0A64646464","0A0A0A0A64646464"]',
                },
                'distance': {
                    'freq_ms': 10,
                },
                'trigger_range': {
                    'bottom': 0.0,
                    'top': 1.0,
                },
            }
        },
    },
    'ws':{
        'master_uuid': None,
        'listen_host': '0.0.0.0',
        'listen_port': 28846 
    },
    'osc':{
        'listen_host': '127.0.0.1',
        'listen_port': 9001,
    },
    'web_server':{
        'listen_host': '127.0.0.1',
        'listen_port': 8800
    },
    'log_level': 'INFO',
    'version': CONFIG_FILE_VERSION,
    'general': {
        'auto_open_qr_web_page': True,
        'local_ip_detect': {
            'host': '223.5.5.5',
            'port': 80,
        }
    },
    'chatbox': {
        'enable': True,
        'osc_host': '127.0.0.1',
        'osc_port': 9000,
        'update_interval': 3.0,
        'set_avatar_parameter': True,
    },
    'api': {
        # 控制接口默认关闭；启用后还必须提供 token。
        'control_enabled': False,
        'token': None,
    },
}
SERVER_IP = None
ASYNC_LOOP = None
ASYNC_STOP_EVENT = None
ASYNC_READY = Event()
ASYNC_START_ERROR = None


def configure_console_encoding():
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if reconfigure is not None:
            try:
                reconfigure(encoding='utf-8')
            except (OSError, ValueError):
                pass


configure_console_encoding()

@app.route('/get_ip')
def get_current_ip():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(2.0)
        sock.connect((
            SETTINGS['general']['local_ip_detect']['host'],
            SETTINGS['general']['local_ip_detect']['port'],
        ))
        return sock.getsockname()[0]

@app.route("/")
def web_index():
    return redirect("/qr", code=302)

@app.route("/qr")
def web_qr():
    return render_template('tiny-qr.html', content=f'https://www.dungeon-lab.com/app-download.php#DGLAB-SOCKET#ws://{SERVER_IP}:{SETTINGS["ws"]["listen_port"]}/{SETTINGS["ws"]["master_uuid"]}')

@app.after_request
def after_request_hook(response):
    if request.args.get('ret') == 'status' and response.status_code == 200:
        response = jsonify(build_status_response())
    return response

class ClientNotAllowed(Exception):
    pass

@app.errorhandler(ClientNotAllowed)
def hendle_ClientNotAllowed(e):
    return {
        "error": "Client not allowed."
    }, 401

@app.errorhandler(Exception)
def handle_Exception(e):
    if isinstance(e, HTTPException):
        return e
    logger.error(traceback.format_exc())
    return {'error': 'Internal server error.'}, 500

# Disallow (Video)
# User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.72 Safari/537.36\r\n
# User-Agent: NSPlayer/12.00.26100.2314 WMFSDK/12.00.26100.2314\r\n
# Allow (Text/Image)
# User-Agent: UnityPlayer/2022.3.22f1-DWR (UnityWebRequest/1.0, libcurl/8.5.0-DEV)\r\n
def require_control_access(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        api_settings = SETTINGS.get('api', {})
        if not api_settings.get('control_enabled', False):
            raise ClientNotAllowed
        expected_token = str(api_settings.get('token') or '')
        supplied_token = request.headers.get('X-Control-Token') or request.args.get('token') or ''
        if not expected_token or not hmac.compare_digest(expected_token, supplied_token):
            raise ClientNotAllowed
        ua = request.headers.get('User-Agent', '')
        if 'UnityPlayer' not in ua:
            raise ClientNotAllowed
        if 'NSPlayer' in ua or 'WMFSDK' in ua:
            raise ClientNotAllowed
        return func(*args, **kwargs)
    return wrapper

@app.route('/api/v1/status')
def api_v1_status():
    return build_status_response()


def build_status_response():
    return {
        'healthy': 'ok',
        'devices': [
            {
                'type': 'shock',
                'device': 'coyotev3',
                'attr': {'strength': dict(conn.strength), 'uuid': conn.uuid},
            }
            for conn in srv.get_ws_connections()
        ]
    }


def submit_to_async_loop(coroutine, timeout=10):
    loop = ASYNC_LOOP
    if loop is None or not loop.is_running():
        coroutine.close()
        raise RuntimeError('Device service is not running.')
    future = asyncio.run_coroutine_threadsafe(coroutine, loop)
    try:
        return future.result(timeout=timeout)
    except FutureTimeoutError:
        future.cancel()
        raise TimeoutError('Device operation timed out.')


def normalize_wave_request(channel, repeat, wavedata):
    channel = str(channel).upper()
    if channel not in ('A', 'B'):
        raise ValueError('channel must be A or B')
    repeat = int(repeat)
    if repeat < 1 or repeat > 100:
        raise ValueError('repeat must be between 1 and 100')
    if not re.fullmatch(r'[0-9A-F]{16}', str(wavedata)):
        raise ValueError('wavedata must be 16 uppercase hexadecimal characters')
    return channel, repeat, str(wavedata)


async def broadcast_repeated_wave(channels, repeat, wavedata):
    wavestr = json.dumps([wavedata] * repeat, separators=(',', ':'))
    for channel in channels:
        await DGConnection.broadcast_wave(channel=channel, wavestr=wavestr)

@app.route('/api/v1/shock/<channel>/<second>', methods=['GET', 'POST'])
@require_control_access
def api_v1_shock(channel, second):
    channel = str(channel).upper()
    if channel == 'ALL':
        channels = ['A', 'B']
    elif channel in ('A', 'B'):
        channels = [channel]
    else:
        return {'error': 'channel must be A, B, or all'}, 400
    try:
        second = float(second)
    except ValueError:
        return {'error': 'second must be a number'}, 400
    if not 0.1 <= second <= 10.0:
        return {'error': 'second must be between 0.1 and 10'}, 400
    repeat = math.ceil(second / 0.1)
    submit_to_async_loop(broadcast_repeated_wave(channels, repeat, '0A0A0A0A64646464'))
    return {'result': 'OK'}

@app.route('/api/v1/sendwave/<channel>/<repeat>/<wavedata>', methods=['GET', 'POST'])
@require_control_access
def api_v1_sendwave(channel, repeat, wavedata):
    """API V1 Sendwave.

    Keyword arguments:
    channel -- A or B.
    repeat -- repeat times, 1 for 100ms, 1 to 80. Max 80 for json length limit.
    wavedata -- Coyote v3 wave format, eg. 0A0A0A0A64646464.
    """
    try:
        channel, repeat, wavedata = normalize_wave_request(channel, repeat, wavedata)
    except (TypeError, ValueError) as exc:
        return {'error': str(exc)}, 400
    logger.success(f'[API][sendwave] C:{channel} R:{repeat} W:{wavedata}')
    submit_to_async_loop(broadcast_repeated_wave([channel], repeat, wavedata))
    return {'result': 'OK'}

async def wshandler(connection):
    client = DGConnection(connection, SETTINGS=SETTINGS)
    await client.serve()

async def chatbox_update_task():
    while True:
        await chatbox_manager.update_chatbox(srv.get_ws_connections(), handlers)
        await asyncio.sleep(0.5)

async def async_main():
    global ASYNC_LOOP, ASYNC_STOP_EVENT, ASYNC_START_ERROR
    ASYNC_LOOP = asyncio.get_running_loop()
    ASYNC_STOP_EVENT = asyncio.Event()
    transport = None
    chatbox_task = None
    try:
        for handler in handlers:
            handler.start_background_jobs()
        if chatbox_manager.enabled:
            chatbox_task = asyncio.create_task(chatbox_update_task())

        server = AsyncIOOSCUDPServer(
            (SETTINGS['osc']['listen_host'], SETTINGS['osc']['listen_port']),
            dispatcher,
            ASYNC_LOOP,
        )
        transport, _ = await server.create_serve_endpoint()
        async with wsserve(wshandler, SETTINGS['ws']['listen_host'], SETTINGS['ws']['listen_port']):
            ASYNC_READY.set()
            await ASYNC_STOP_EVENT.wait()
    except Exception as exc:
        ASYNC_START_ERROR = exc
        ASYNC_READY.set()
        logger.error(traceback.format_exc())
        logger.error('OSC 或 WebSocket 服务启动/运行失败，可能存在端口冲突。')
    finally:
        if chatbox_task is not None:
            chatbox_task.cancel()
            await asyncio.gather(chatbox_task, return_exceptions=True)
        for handler in handlers:
            stop = getattr(handler, 'stop_background_jobs', None)
            if stop is not None:
                await stop()
        if transport is not None:
            transport.close()

def async_main_wrapper():
    """Not async Wrapper around async_main to run it as target function of Thread"""
    asyncio.run(async_main())


def stop_async_services(thread):
    if ASYNC_LOOP is not None and ASYNC_STOP_EVENT is not None and ASYNC_LOOP.is_running():
        ASYNC_LOOP.call_soon_threadsafe(ASYNC_STOP_EVENT.set)
    thread.join(timeout=5)
    if thread.is_alive():
        logger.warning('后台服务未能在 5 秒内完全退出。')

def config_save():
    for filename, content in (
        (CONFIG_FILENAME, SETTINGS),
        (CONFIG_FILENAME_BASIC, SETTINGS_BASIC),
    ):
        temporary = filename.with_name(filename.name + '.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            yaml.safe_dump(content, stream, allow_unicode=True, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, filename)


def merge_defaults(defaults, values):
    """Recursively add missing defaults without overwriting user values."""
    if not isinstance(values, dict):
        return copy.deepcopy(values)
    merged = copy.deepcopy(defaults)
    for key, value in values.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = merge_defaults(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def validate_config(settings, basic_settings):
    def validate_port(section, name):
        port = section.get(name)
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError(f'{name} 必须是 1~65535 之间的整数。')

    for section_name in ('osc', 'web_server', 'ws'):
        section = settings[section_name]
        if not isinstance(section.get('listen_host'), str) or not section['listen_host']:
            raise ValueError(f'{section_name}.listen_host 不能为空。')
        validate_port(section, 'listen_port')

    chatbox = settings['chatbox']
    validate_port(chatbox, 'osc_port')
    if float(chatbox['update_interval']) < 1.0:
        raise ValueError('chatbox.update_interval 不能小于 1 秒。')

    for channel_name in ('channel_a', 'channel_b'):
        basic_channel = basic_settings['dglab3'][channel_name]
        if basic_channel['mode'] not in ('distance', 'shock'):
            raise ValueError(f'{channel_name}.mode 只支持 distance 或 shock。')
        if not isinstance(basic_channel['strength_limit'], int) or not 0 <= basic_channel['strength_limit'] <= 200:
            raise ValueError(f'{channel_name}.strength_limit 必须是 0~200 之间的整数。')
        avatar_params = basic_channel['avatar_params']
        if not isinstance(avatar_params, list) or not all(isinstance(item, str) for item in avatar_params):
            raise ValueError(f'{channel_name}.avatar_params 必须是字符串列表。')

        mode_config = settings['dglab3'][channel_name]['mode_config']
        trigger_range = mode_config['trigger_range']
        bottom = float(trigger_range['bottom'])
        top = float(trigger_range['top'])
        if not 0 <= bottom < top <= 1:
            raise ValueError(f'{channel_name}.trigger_range 必须满足 0 <= bottom < top <= 1。')
        frequency = mode_config['distance']['freq_ms']
        if not isinstance(frequency, int) or not 0 <= frequency <= 255:
            raise ValueError(f'{channel_name}.distance.freq_ms 必须是 0~255 之间的整数。')
        duration = float(mode_config['shock']['duration'])
        if not 0.1 <= duration <= 10:
            raise ValueError(f'{channel_name}.shock.duration 必须在 0.1~10 秒之间。')
        try:
            wave = json.loads(mode_config['shock']['wave'])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f'{channel_name}.shock.wave 必须是合法 JSON 数组。') from exc
        if not wave or not all(isinstance(item, str) and re.fullmatch(r'[0-9A-F]{16}', item) for item in wave):
            raise ValueError(f'{channel_name}.shock.wave 包含无效波形。')

    try:
        uuid.UUID(str(settings['ws']['master_uuid']))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError('ws.master_uuid 必须是有效 UUID。') from exc
    if settings['api']['control_enabled'] and not settings['api'].get('token'):
        raise ValueError('启用 API 控制时必须设置 api.token。')

class ConfigFileInited(Exception):
    pass

def config_init():
    logger.info(f'Init settings..., Config filename: {CONFIG_FILENAME_BASIC} {CONFIG_FILENAME}, Config version: {CONFIG_FILE_VERSION}.')
    global SETTINGS, SETTINGS_BASIC, SERVER_IP
    if not (os.path.exists(CONFIG_FILENAME) and os.path.exists(CONFIG_FILENAME_BASIC)):
        SETTINGS['ws']['master_uuid'] = str(uuid.uuid4())
        SETTINGS['api']['token'] = secrets.token_urlsafe(32)
        config_save()
        raise ConfigFileInited()

    with open(CONFIG_FILENAME, 'r', encoding='utf-8') as fr:
        loaded_settings = yaml.safe_load(fr) or {}
    with open(CONFIG_FILENAME_BASIC, 'r', encoding='utf-8') as fr:
        loaded_basic_settings = yaml.safe_load(fr) or {}

    if loaded_settings.get('version') != CONFIG_FILE_VERSION or loaded_basic_settings.get('version') != CONFIG_FILE_VERSION:
        logger.error(f"Configuration file version mismatch! Please delete the {CONFIG_FILENAME_BASIC} and {CONFIG_FILENAME} files and run the program again to generate the latest version of the configuration files.")
        raise Exception(f'配置文件版本不匹配！请删除 {CONFIG_FILENAME_BASIC} {CONFIG_FILENAME} 文件后再次运行程序，以生成最新版本的配置文件。')
    SETTINGS = merge_defaults(SETTINGS, loaded_settings)
    SETTINGS_BASIC = merge_defaults(SETTINGS_BASIC, loaded_basic_settings)
    config_changed = SETTINGS != loaded_settings or SETTINGS_BASIC != loaded_basic_settings
    if SETTINGS['ws']['master_uuid'] is None:
        SETTINGS['ws']['master_uuid'] = str(uuid.uuid4())
        config_changed = True
    if SETTINGS['api']['token'] is None:
        SETTINGS['api']['token'] = secrets.token_urlsafe(32)
        config_changed = True

    validate_config(SETTINGS, SETTINGS_BASIC)
    if config_changed:
        config_save()
    SERVER_IP = SETTINGS['SERVER_IP'] or get_current_ip()

    for chann in ['channel_a', 'channel_b']:
        SETTINGS['dglab3'][chann]['avatar_params'] = SETTINGS_BASIC['dglab3'][chann]['avatar_params']
        SETTINGS['dglab3'][chann]['mode'] = SETTINGS_BASIC['dglab3'][chann]['mode']
        SETTINGS['dglab3'][chann]['strength_limit'] = SETTINGS_BASIC['dglab3'][chann]['strength_limit']

    logger.remove()
    logger.add(sys.stderr, level=SETTINGS['log_level'])
    logger.success("The configuration file initialization is complete. The WebSocket service needs to listen for incoming connections. If a firewall prompt appears, please click Allow Access.")
    logger.success("配置文件初始化完成，Websocket服务需要监听外来连接，如弹出防火墙提示，请点击允许访问。")

def main():
    global dispatcher, handlers, chatbox_manager, ASYNC_START_ERROR
    chatbox_manager = AdvancedChatboxManager(SETTINGS)

    dispatcher = Dispatcher()
    handlers = []

    for chann in ['A', 'B']:
        config_chann_name = f'channel_{chann.lower()}'
        chann_mode = SETTINGS['dglab3'][config_chann_name]['mode']
        shock_handler = ShockHandler(SETTINGS=SETTINGS, DG_CONN = DGConnection, channel_name=chann)
        shock_handler.set_chatbox_manager(chatbox_manager)
        handlers.append(shock_handler)
        for param in SETTINGS['dglab3'][config_chann_name]['avatar_params']:
            logger.success(f"Channel {chann} Mode：{chann_mode} Listening：{param}")
            dispatcher.map(param, shock_handler.osc_handler)
    
    if 'machine' in SETTINGS and 'tuya' in SETTINGS['machine']:
        TuyaConn = TuYaConnection(
            access_id=SETTINGS['machine']['tuya']['access_id'],
            access_key=SETTINGS['machine']['tuya']['access_key'],
            device_ids=SETTINGS['machine']['tuya']['device_ids'],
        )
        machine_tuya_handler = TuyaHandler(SETTINGS=SETTINGS, DEV_CONN=TuyaConn)
        handlers.append(machine_tuya_handler)
        for param in SETTINGS['machine']['tuya']['avatar_params']:
            logger.success(f"Machine Listening：{param}")
            dispatcher.map(param, machine_tuya_handler.osc_handler)


    ASYNC_READY.clear()
    ASYNC_START_ERROR = None
    th = Thread(target=async_main_wrapper, daemon=True, name='device-services')
    th.start()
    if not ASYNC_READY.wait(timeout=10):
        stop_async_services(th)
        raise RuntimeError('后台设备服务启动超时。')
    if ASYNC_START_ERROR is not None:
        stop_async_services(th)
        raise RuntimeError('后台设备服务启动失败。') from ASYNC_START_ERROR

    try:
        if SETTINGS['general']['auto_open_qr_web_page']:
            import webbrowser
            webbrowser.open_new_tab(f"http://127.0.0.1:{SETTINGS['web_server']['listen_port']}")
        else:
            info_ip = SETTINGS['web_server']['listen_host']
            if info_ip == '0.0.0.0':
                info_ip = get_current_ip()
            logger.success(f"请打开浏览器访问 http://{info_ip}:{SETTINGS['web_server']['listen_port']}")
        app.run(
            SETTINGS['web_server']['listen_host'],
            SETTINGS['web_server']['listen_port'],
            debug=False,
            use_reloader=False,
        )
    finally:
        stop_async_services(th)

if __name__ == "__main__":
    try:
        config_init()
        main()
    except ConfigFileInited:
        logger.success('The configuration file initialization is complete. Please modify it as needed and restart the program.')
        logger.success('配置文件初始化完成，请按需修改后重启程序。')
    except Exception as e:
        logger.error(traceback.format_exc())
        logger.error("Unexpected Error.")
    finally:
        # 清理Chatbox
        if 'chatbox_manager' in globals():
            chatbox_manager.cleanup()
    logger.info('Exiting in 1 seconds ... Press Ctrl-C to exit immediately')
    logger.info('退出等待1秒 ... 按Ctrl-C立即退出')
    time.sleep(1)

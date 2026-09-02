"""Small dependency-free Win32 desktop shell for the service.

The only UI-specific third-party package is qrcode; its matrix is painted
directly with GDI, so no image toolkit or browser process is required.
"""

import copy
import ctypes
import queue
import socket
import sys
import threading
import traceback
from ctypes import wintypes

import qrcode
from loguru import logger

from srv.config_manager import format_endpoint, parse_endpoint, parse_parameter_lines, validate_config


if sys.platform != 'win32':
    raise RuntimeError('桌面界面仅支持 Windows。')


user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32
shell32 = ctypes.windll.shell32
comctl32 = ctypes.windll.comctl32

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

# ctypes otherwise assumes 32-bit integer return values and truncates handles
# in a 64-bit build. Declare the pointer-sized results used by this module.
kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.restype = LRESULT
user32.LoadIconW.restype = wintypes.HICON
user32.LoadCursorW.restype = wintypes.HANDLE
user32.SendMessageW.restype = LRESULT
user32.CreatePopupMenu.restype = wintypes.HMENU
gdi32.CreateFontW.restype = wintypes.HANDLE
gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
user32.BeginPaint.restype = wintypes.HDC
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]

WM_CREATE = 0x0001
WM_DESTROY = 0x0002
WM_PAINT = 0x000F
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_TIMER = 0x0113
WM_SETFONT = 0x0030
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_EVENTS = WM_APP + 2
WM_AUTOSTART = WM_APP + 3

WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_TABSTOP = 0x00010000
WS_VSCROLL = 0x00200000
WS_OVERLAPPED = 0x00000000
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_EX_CLIENTEDGE = 0x00000200
BS_PUSHBUTTON = 0x00000000
BS_AUTOCHECKBOX = 0x00000003
BS_GROUPBOX = 0x00000007
ES_LEFT = 0x0000
ES_MULTILINE = 0x0004
ES_AUTOVSCROLL = 0x0040
ES_READONLY = 0x0800
SS_LEFT = 0x0000
SW_HIDE = 0
SW_SHOW = 5
SW_RESTORE = 9
BM_GETCHECK = 0x00F0
BM_SETCHECK = 0x00F1
BST_CHECKED = 1
PBM_SETRANGE32 = 0x0406
PBM_SETPOS = 0x0402
MF_STRING = 0x0000
TPM_RIGHTBUTTON = 0x0002
NIM_ADD = 0x00000000
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
COLOR_WINDOW = 5
IDC_ARROW = 32512
IDI_APPLICATION = 32512
CW_USEDEFAULT = -2147483648

ID_NAV_GENERAL = 101
ID_NAV_PARAMS = 102
ID_NAV_DEBUG = 103
ID_START = 110
ID_STOP = 111
ID_SAVE_RESTART = 112
ID_EXIT = 113
ID_TRAY_SHOW = 201
ID_TRAY_START = 202
ID_TRAY_STOP = 203
ID_TRAY_EXIT = 204


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ('cbSize', wintypes.UINT),
        ('style', wintypes.UINT),
        ('lpfnWndProc', WNDPROC),
        ('cbClsExtra', ctypes.c_int),
        ('cbWndExtra', ctypes.c_int),
        ('hInstance', wintypes.HINSTANCE),
        ('hIcon', wintypes.HICON),
        ('hCursor', wintypes.HANDLE),
        ('hbrBackground', wintypes.HBRUSH),
        ('lpszMenuName', wintypes.LPCWSTR),
        ('lpszClassName', wintypes.LPCWSTR),
        ('hIconSm', wintypes.HICON),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ('hdc', wintypes.HDC),
        ('fErase', wintypes.BOOL),
        ('rcPaint', wintypes.RECT),
        ('fRestore', wintypes.BOOL),
        ('fIncUpdate', wintypes.BOOL),
        ('rgbReserved', ctypes.c_byte * 32),
    ]


class GUID(ctypes.Structure):
    _fields_ = [
        ('Data1', wintypes.DWORD),
        ('Data2', wintypes.WORD),
        ('Data3', wintypes.WORD),
        ('Data4', ctypes.c_byte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ('cbSize', wintypes.DWORD),
        ('hWnd', wintypes.HWND),
        ('uID', wintypes.UINT),
        ('uFlags', wintypes.UINT),
        ('uCallbackMessage', wintypes.UINT),
        ('hIcon', wintypes.HICON),
        ('szTip', wintypes.WCHAR * 128),
        ('dwState', wintypes.DWORD),
        ('dwStateMask', wintypes.DWORD),
        ('szInfo', wintypes.WCHAR * 256),
        ('uTimeoutOrVersion', wintypes.UINT),
        ('szInfoTitle', wintypes.WCHAR * 64),
        ('dwInfoFlags', wintypes.DWORD),
        ('guidItem', GUID),
        ('hBalloonIcon', wintypes.HICON),
    ]


kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.InvalidateRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT), wintypes.BOOL]
user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]


_application = None


@WNDPROC
def _window_proc(hwnd, message, wparam, lparam):
    if _application is not None:
        return _application.window_proc(hwnd, message, wparam, lparam)
    return user32.DefWindowProcW(hwnd, message, wparam, lparam)


class DesktopApplication:
    WIDTH = 1000
    HEIGHT = 680

    def __init__(self, config_manager, settings, basic_settings, controller):
        self.config_manager = config_manager
        self.settings = copy.deepcopy(settings)
        self.basic_settings = copy.deepcopy(basic_settings)
        self.controller = controller
        self.hwnd = None
        self.font = None
        self.controls = {}
        self.panels = {'general': [], 'params': [], 'debug': []}
        self.events = queue.Queue()
        self.action_running = False
        self.exiting = False
        self.pending_exit = False
        self.tray_data = None
        self.qr_matrix = []
        self._last_snapshot = None

    def post_event(self, event):
        self.events.put(event)
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_EVENTS, 0, 0)

    def _create(self, class_name, text, style, x, y, width, height, control_id=0, ex_style=0, panel=None):
        handle = user32.CreateWindowExW(
            ex_style,
            class_name,
            text,
            style | WS_CHILD | WS_VISIBLE,
            x,
            y,
            width,
            height,
            self.hwnd,
            control_id,
            kernel32.GetModuleHandleW(None),
            None,
        )
        if not handle:
            raise ctypes.WinError()
        if self.font:
            user32.SendMessageW(handle, WM_SETFONT, self.font, True)
        if panel:
            self.panels[panel].append(handle)
        return handle

    def _label(self, text, x, y, width, height=24, panel=None):
        return self._create('STATIC', text, SS_LEFT, x, y, width, height, panel=panel)

    def _edit(self, key, value, x, y, width, height=25, multiline=False, readonly=False, panel=None):
        style = WS_TABSTOP | ES_LEFT
        if multiline:
            style |= ES_MULTILINE | ES_AUTOVSCROLL | WS_VSCROLL
        if readonly:
            style |= ES_READONLY
        handle = self._create('EDIT', str(value), style, x, y, width, height, ex_style=WS_EX_CLIENTEDGE, panel=panel)
        self.controls[key] = handle
        return handle

    def _check(self, key, text, checked, x, y, width, panel=None):
        handle = self._create('BUTTON', text, WS_TABSTOP | BS_AUTOCHECKBOX, x, y, width, 25, panel=panel)
        user32.SendMessageW(handle, BM_SETCHECK, BST_CHECKED if checked else 0, 0)
        self.controls[key] = handle
        return handle

    def _button(self, text, control_id, x, y, width, height=30, panel=None):
        return self._create('BUTTON', text, WS_TABSTOP | BS_PUSHBUTTON, x, y, width, height, control_id, panel=panel)

    def _build_controls(self):
        self.font = gdi32.CreateFontW(
            -16, 0, 0, 0, 400, False, False, False, 1, 0, 0, 5, 0, 'Segoe UI'
        )
        self._label('服务状态：', 20, 20, 75)
        self.controls['service_status'] = self._label('正在启动…', 95, 20, 210)
        self._button('启动服务', ID_START, 320, 14, 95, 32)
        self._button('停止服务', ID_STOP, 425, 14, 95, 32)
        self._button('保存并重启服务', ID_SAVE_RESTART, 530, 14, 130, 32)

        self._button('基本设置', ID_NAV_GENERAL, 20, 75, 100, 38)
        self._button('A/B 参数', ID_NAV_PARAMS, 20, 123, 100, 38)
        self._button('运行调试', ID_NAV_DEBUG, 20, 171, 100, 38)

        self._build_general_panel()
        self._build_parameter_panel()
        self._build_debug_panel()

        self._label('手机连接二维码', 700, 72, 240, 28)
        self.controls['device_status'] = self._label('郊狼：未连接', 700, 355, 260, 25)
        self._label('连接地址', 700, 393, 260, 22)
        self._edit('qr_content', '', 700, 418, 260, 90, multiline=True, readonly=True)
        self._label('配置文件', 700, 526, 260, 22)
        self._edit('config_path', str(self.config_manager.path), 700, 550, 260, 50, multiline=True, readonly=True)
        self._label('关闭窗口时可按设置隐藏到系统托盘。', 700, 615, 270, 25)

        self._show_panel('general')
        self._refresh_qr()

    def _build_general_panel(self):
        panel = 'general'
        self._create('BUTTON', '网络监听', BS_GROUPBOX, 140, 70, 520, 100, panel=panel)
        self._label('OSC / 分流入口', 160, 102, 125, panel=panel)
        relay = self.settings['relay']
        endpoint = (
            format_endpoint(relay['listen_host'], relay['listen_port'])
            if relay['enabled']
            else format_endpoint(self.settings['osc']['listen_host'], self.settings['osc']['listen_port'])
        )
        self._edit('listen_endpoint', endpoint, 300, 98, 330, panel=panel)

        self._create('BUTTON', '安全与显示', BS_GROUPBOX, 140, 180, 520, 145, panel=panel)
        self._label('A 通道最大强度（0~200）', 160, 212, 200, panel=panel)
        self._edit('strength_a', self.basic_settings['dglab3']['channel_a']['strength_limit'], 370, 208, 75, panel=panel)
        self._label('B 通道最大强度（0~200）', 160, 247, 200, panel=panel)
        self._edit('strength_b', self.basic_settings['dglab3']['channel_b']['strength_limit'], 370, 243, 75, panel=panel)
        self._check('chatbox', '启用 VRChat Chatbox 状态消息', self.settings['chatbox']['enable'], 160, 280, 270, panel=panel)
        self._check(
            'background',
            '关闭主窗口后继续在系统托盘运行',
            self.settings['general']['run_in_background'],
            160,
            305,
            300,
            panel=panel,
        )

        self._create('BUTTON', 'UDP 端口分流', BS_GROUPBOX, 140, 338, 520, 170, panel=panel)
        self._check('relay', '启用端口分流', relay['enabled'], 160, 370, 180, panel=panel)
        self._label('VRCFT 目标', 160, 410, 125, panel=panel)
        self._edit('vrcft_endpoint', format_endpoint(relay['vrcft_host'], relay['vrcft_port']), 300, 406, 330, panel=panel)
        self._label('本程序内部目标', 160, 449, 125, panel=panel)
        self._edit('internal_endpoint', format_endpoint(relay['internal_host'], relay['internal_port']), 300, 445, 330, panel=panel)
        self._label('启用后，每个 UDP 数据包会原样发送到以上两个目标。', 160, 480, 450, panel=panel)

    def _build_parameter_panel(self):
        panel = 'params'
        self._label('每行一个 /avatar/parameters/... 参数；支持通配符 * 和直接批量粘贴。', 140, 72, 520, 28, panel=panel)
        self._label('A 通道监听参数', 140, 108, 245, 25, panel=panel)
        self._label('B 通道监听参数', 405, 108, 245, 25, panel=panel)
        self._edit(
            'params_a',
            '\r\n'.join(self.basic_settings['dglab3']['channel_a']['avatar_params']),
            140,
            136,
            245,
            405,
            multiline=True,
            panel=panel,
        )
        self._edit(
            'params_b',
            '\r\n'.join(self.basic_settings['dglab3']['channel_b']['avatar_params']),
            405,
            136,
            245,
            405,
            multiline=True,
            panel=panel,
        )
        self._label('修改后点击顶部“保存并重启服务”才会生效。', 140, 555, 500, 25, panel=panel)

    def _build_debug_panel(self):
        panel = 'debug'
        self._label('单台郊狼设备实时状态（只读）', 140, 72, 500, 28, panel=panel)
        self._build_channel_debug('A', 140, 110, panel)
        self._build_channel_debug('B', 140, 315, panel)
        self.controls['relay_debug'] = self._label('UDP 分流包数：0', 155, 540, 480, 25, panel=panel)

    def _build_channel_debug(self, channel, x, y, panel):
        self._create('BUTTON', f'{channel} 通道', BS_GROUPBOX, x, y, 520, 185, panel=panel)
        self._label('正在触发的参数', x + 15, y + 32, 125, panel=panel)
        self.controls[f'debug_param_{channel}'] = self._label('—', x + 145, y + 32, 355, 25, panel=panel)
        self._label('原始值', x + 15, y + 67, 125, panel=panel)
        self.controls[f'debug_raw_{channel}'] = self._label('0.000', x + 145, y + 67, 120, 25, panel=panel)
        self._label('映射强度', x + 15, y + 102, 125, panel=panel)
        self.controls[f'debug_mapped_{channel}'] = self._label('0.0%', x + 145, y + 102, 120, 25, panel=panel)
        self._label('实际发送强度', x + 275, y + 102, 120, panel=panel)
        self.controls[f'debug_actual_{channel}'] = self._label('0 / 0', x + 400, y + 102, 95, 25, panel=panel)
        progress = self._create('msctls_progress32', '', 0, x + 15, y + 140, 485, 22, panel=panel)
        user32.SendMessageW(progress, PBM_SETRANGE32, 0, 1000)
        self.controls[f'debug_progress_{channel}'] = progress

    def _show_panel(self, selected):
        for name, handles in self.panels.items():
            command = SW_SHOW if name == selected else SW_HIDE
            for handle in handles:
                user32.ShowWindow(handle, command)
        user32.InvalidateRect(self.hwnd, None, True)

    def _get_text(self, key):
        handle = self.controls[key]
        length = user32.GetWindowTextLengthW(handle)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, length + 1)
        return buffer.value

    def _set_text(self, key, value):
        user32.SetWindowTextW(self.controls[key], str(value))

    def _is_checked(self, key):
        return user32.SendMessageW(self.controls[key], BM_GETCHECK, 0, 0) == BST_CHECKED

    def _read_form(self):
        settings = copy.deepcopy(self.settings)
        basic = copy.deepcopy(self.basic_settings)
        listen_host, listen_port = parse_endpoint(self._get_text('listen_endpoint'))
        vrcft_host, vrcft_port = parse_endpoint(self._get_text('vrcft_endpoint'))
        internal_host, internal_port = parse_endpoint(self._get_text('internal_endpoint'))
        settings['osc'].update(listen_host=listen_host, listen_port=listen_port)
        settings['relay'].update(
            enabled=self._is_checked('relay'),
            listen_host=listen_host,
            listen_port=listen_port,
            vrcft_host=vrcft_host,
            vrcft_port=vrcft_port,
            internal_host=internal_host,
            internal_port=internal_port,
        )
        settings['chatbox']['enable'] = self._is_checked('chatbox')
        settings['general']['run_in_background'] = self._is_checked('background')
        basic['dglab3']['channel_a']['strength_limit'] = int(self._get_text('strength_a'))
        basic['dglab3']['channel_b']['strength_limit'] = int(self._get_text('strength_b'))
        basic['dglab3']['channel_a']['avatar_params'] = parse_parameter_lines(self._get_text('params_a'))
        basic['dglab3']['channel_b']['avatar_params'] = parse_parameter_lines(self._get_text('params_b'))
        validate_config(settings, basic)
        return settings, basic

    def _start_action(self, action, settings=None, basic=None):
        if self.action_running:
            self._message('当前操作尚未完成，请稍候。', error=False)
            return
        self.action_running = True

        def worker():
            error = None
            try:
                if action == 'start':
                    self.controller.start(self.settings, self.basic_settings)
                elif action == 'stop':
                    self.controller.stop()
                elif action == 'restart':
                    self.controller.restart(settings, basic)
                elif action == 'exit':
                    self.controller.stop()
            except Exception as exc:
                logger.error(traceback.format_exc())
                error = str(exc)
            self.post_event({'type': 'action_done', 'action': action, 'error': error})

        threading.Thread(target=worker, daemon=True, name=f'ui-{action}').start()

    def _save_and_restart(self):
        try:
            settings, basic = self._read_form()
            self.config_manager.save(settings, basic)
            self.settings = settings
            self.basic_settings = basic
            self._refresh_qr()
            self._start_action('restart', settings, basic)
        except (ValueError, OSError) as exc:
            self._message(str(exc), error=True)

    def _process_events(self):
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            event_type = event.get('type')
            if event_type == 'service':
                state = event.get('state', 'stopped')
                labels = {
                    'starting': '正在启动…',
                    'running': '运行中',
                    'stopped': '已停止',
                    'error': '启动失败',
                }
                self._set_text('service_status', labels.get(state, state))
            elif event_type == 'action_done':
                self.action_running = False
                if event.get('error'):
                    self._message(f'操作失败：{event["error"]}', error=True)
                if event.get('action') == 'exit':
                    user32.DestroyWindow(self.hwnd)
                elif self.pending_exit:
                    self.pending_exit = False
                    self._start_action('exit')
            elif event_type == 'device':
                self._set_text('device_status', '郊狼：已连接' if event.get('connected') else '郊狼：未连接')

    def _refresh_snapshot(self):
        snapshot = self.controller.snapshot()
        if snapshot == self._last_snapshot:
            return
        self._last_snapshot = snapshot
        device = '郊狼：已连接' if snapshot['connected'] else '郊狼：未连接'
        if snapshot['connected'] and snapshot['device_id']:
            device += f'  ({snapshot["device_id"][:8]})'
        self._set_text('device_status', device)
        self._set_text('relay_debug', f'UDP 分流包数：{snapshot["relay_packets"]}')
        for channel in ('A', 'B'):
            info = snapshot['channels'].get(channel, {})
            percentage = float(info.get('strength_percentage', 0.0))
            self._set_text(f'debug_param_{channel}', info.get('parameter') or '—')
            self._set_text(f'debug_raw_{channel}', f'{float(info.get("raw_value", 0.0)):.3f}')
            self._set_text(f'debug_mapped_{channel}', f'{percentage * 100:.1f}%')
            self._set_text(
                f'debug_actual_{channel}',
                f'{info.get("actual_strength", 0)} / {info.get("upper_strength", 0)}',
            )
            user32.SendMessageW(self.controls[f'debug_progress_{channel}'], PBM_SETPOS, int(percentage * 1000), 0)

    def _refresh_qr(self):
        server_ip = self.settings.get('SERVER_IP')
        if not server_ip:
            try:
                target = self.settings['general']['local_ip_detect']
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.settimeout(2.0)
                    sock.connect((target['host'], target['port']))
                    server_ip = sock.getsockname()[0]
            except OSError:
                server_ip = '127.0.0.1'
        content = (
            'https://www.dungeon-lab.com/app-download.php#DGLAB-SOCKET#'
            f'ws://{server_ip}:{self.settings["ws"]["listen_port"]}/{self.settings["ws"]["master_uuid"]}'
        )
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, border=2, box_size=1)
        qr.add_data(content)
        qr.make(fit=True)
        self.qr_matrix = qr.get_matrix()
        if 'qr_content' in self.controls:
            self._set_text('qr_content', content)
        if self.hwnd:
            user32.InvalidateRect(self.hwnd, None, False)

    def _paint_qr(self):
        paint = PAINTSTRUCT()
        hdc = user32.BeginPaint(self.hwnd, ctypes.byref(paint))
        try:
            if not self.qr_matrix:
                return
            left, top, size = 700, 105, 260
            white = gdi32.CreateSolidBrush(0x00FFFFFF)
            black = gdi32.CreateSolidBrush(0x00000000)
            rect = wintypes.RECT(left, top, left + size, top + size)
            user32.FillRect(hdc, ctypes.byref(rect), white)
            count = len(self.qr_matrix)
            scale = max(1, size // count)
            actual = scale * count
            offset_x = left + (size - actual) // 2
            offset_y = top + (size - actual) // 2
            for row_index, row in enumerate(self.qr_matrix):
                for column_index, value in enumerate(row):
                    if not value:
                        continue
                    cell = wintypes.RECT(
                        offset_x + column_index * scale,
                        offset_y + row_index * scale,
                        offset_x + (column_index + 1) * scale,
                        offset_y + (row_index + 1) * scale,
                    )
                    user32.FillRect(hdc, ctypes.byref(cell), black)
            gdi32.DeleteObject(white)
            gdi32.DeleteObject(black)
        finally:
            user32.EndPaint(self.hwnd, ctypes.byref(paint))

    def _add_tray_icon(self):
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(data)
        data.hWnd = self.hwnd
        data.uID = 1
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = WM_TRAY
        data.hIcon = user32.LoadIconW(None, IDI_APPLICATION)
        data.szTip = 'ShockingVRChat'
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data))
        self.tray_data = data

    def _remove_tray_icon(self):
        if self.tray_data is not None:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.tray_data))
            self.tray_data = None

    def _show_tray_menu(self):
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, ID_TRAY_SHOW, '打开主窗口')
        user32.AppendMenuW(menu, MF_STRING, ID_TRAY_START, '启动服务')
        user32.AppendMenuW(menu, MF_STRING, ID_TRAY_STOP, '停止服务')
        user32.AppendMenuW(menu, MF_STRING, ID_TRAY_EXIT, '退出')
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.SetForegroundWindow(self.hwnd)
        user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON, point.x, point.y, 0, self.hwnd, None)
        user32.DestroyMenu(menu)

    def _restore(self):
        user32.ShowWindow(self.hwnd, SW_RESTORE)
        user32.SetForegroundWindow(self.hwnd)

    def _request_exit(self):
        if self.exiting:
            return
        self.exiting = True
        user32.ShowWindow(self.hwnd, SW_HIDE)
        if self.action_running:
            self.pending_exit = True
        else:
            self._start_action('exit')

    def _message(self, text, error=False):
        user32.MessageBoxW(self.hwnd, str(text), 'ShockingVRChat', 0x10 if error else 0x40)

    def window_proc(self, hwnd, message, wparam, lparam):
        if message == WM_CREATE:
            self.hwnd = hwnd
            self._build_controls()
            self._add_tray_icon()
            user32.SetTimer(hwnd, 1, 250, None)
            user32.PostMessageW(hwnd, WM_AUTOSTART, 0, 0)
            return 0
        if message == WM_AUTOSTART:
            self._start_action('start')
            return 0
        if message == WM_COMMAND:
            command = int(wparam) & 0xFFFF
            if command == ID_NAV_GENERAL:
                self._show_panel('general')
            elif command == ID_NAV_PARAMS:
                self._show_panel('params')
            elif command == ID_NAV_DEBUG:
                self._show_panel('debug')
            elif command in (ID_START, ID_TRAY_START):
                self._start_action('start')
            elif command in (ID_STOP, ID_TRAY_STOP):
                self._start_action('stop')
            elif command == ID_SAVE_RESTART:
                self._save_and_restart()
            elif command in (ID_EXIT, ID_TRAY_EXIT):
                self._request_exit()
            elif command == ID_TRAY_SHOW:
                self._restore()
            return 0
        if message == WM_EVENTS:
            self._process_events()
            return 0
        if message == WM_TIMER:
            self._process_events()
            self._refresh_snapshot()
            return 0
        if message == WM_PAINT:
            self._paint_qr()
            return 0
        if message == WM_TRAY:
            mouse_message = int(lparam) & 0xFFFF
            if mouse_message in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self._restore()
            elif mouse_message == WM_RBUTTONUP:
                self._show_tray_menu()
            return 0
        if message == WM_CLOSE:
            if not self.exiting and self._is_checked('background'):
                user32.ShowWindow(hwnd, SW_HIDE)
            else:
                self._request_exit()
            return 0
        if message == WM_DESTROY:
            user32.KillTimer(hwnd, 1)
            self._remove_tray_icon()
            if self.font:
                gdi32.DeleteObject(self.font)
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def run(self):
        global _application
        _application = self
        user32.SetProcessDPIAware()
        comctl32.InitCommonControls()
        instance = kernel32.GetModuleHandleW(None)
        class_name = 'ShockingVRChatDesktopWindow'
        icon = user32.LoadIconW(None, IDI_APPLICATION)
        window_class = WNDCLASSEXW(
            ctypes.sizeof(WNDCLASSEXW),
            0,
            _window_proc,
            0,
            0,
            instance,
            icon,
            user32.LoadCursorW(None, IDC_ARROW),
            COLOR_WINDOW + 1,
            None,
            class_name,
            icon,
        )
        if not user32.RegisterClassExW(ctypes.byref(window_class)):
            error = ctypes.get_last_error()
            if error != 1410:
                raise ctypes.WinError(error)
        style = WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX
        hwnd = user32.CreateWindowExW(
            0,
            class_name,
            'ShockingVRChat',
            style,
            CW_USEDEFAULT,
            CW_USEDEFAULT,
            self.WIDTH,
            self.HEIGHT,
            None,
            None,
            instance,
            None,
        )
        if not hwnd:
            raise ctypes.WinError()
        self.hwnd = hwnd
        user32.ShowWindow(hwnd, SW_SHOW)
        user32.UpdateWindow(hwnd)
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        _application = None
        return int(message.wParam)

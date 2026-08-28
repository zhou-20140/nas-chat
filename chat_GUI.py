# -*- coding: utf-8 -*-
"""NAS 多人在线聊天室 —— 图形界面版（tkinter）

基于 NAS 共享文件夹实现，聊天记录以纯文本文件存储在 NAS 上，
后台线程轮询文件，通过队列 + 定时器在主线程安全刷新界面。
支持：多聊天室 / 实时聊天 / 文件上传/下载/列表/删除（仅上传者）。
"""
import os
import json
import random
import shutil
import sys
import threading
import queue
import time

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ============ 配置加载（与 chat.py 一致） ============
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')

DEFAULT_CONFIG = {
    'nas_path': r'\\192.168.31.1\共享',
    'sub_path': 'chat-text',
    'default_user': '',
    'poll_interval': 1.0,
    'chat_rooms': [
        {'id': 'chat-room1', 'name': '闲聊大厅'},
        {'id': 'chat-room2', 'name': '技术讨论'},
        {'id': 'chat-room3', 'name': '游戏交流'},
        {'id': 'chat-room4', 'name': '生活分享'},
        {'id': 'chat-room5', 'name': '工作协作'},
    ],
}

def load_config():
    config = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config.update(json.load(f))
        except Exception:
            pass
    return config

config = load_config()
nas_path = config['nas_path']
sub_path = config['sub_path']
files_sub_path = config.get('files_sub_path', 'files')
chat_rooms = config['chat_rooms']
poll_interval = float(config.get('poll_interval', 1.0))

# 文件锁：保护对共享文件/NAS 的并发读写
file_lock = threading.Lock()


# ============ NAS 文件逻辑（复用 chat.py） ============
def get_chat_file_path(room_id):
    return os.path.join(nas_path, sub_path, f'{room_id}.txt')


def ensure_chat_file(room_id):
    path = get_chat_file_path(room_id)
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('')


def read_chat_file(room_id):
    """读取聊天文件，返回 [(用户名, 消息), ...]"""
    result = []
    path = get_chat_file_path(room_id)
    if not os.path.exists(path):
        return result
    with file_lock:
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    for line in lines:
        line = line.strip()
        if not line or ':' not in line:
            continue
        other_user, message = line.split(':', 1)
        result.append((other_user.strip(), message.strip()))
    return result


def send_message(user, message, room_id):
    path = get_chat_file_path(room_id)
    with file_lock:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(f'{user}: {message}\n')


def get_files_dir():
    return os.path.join(nas_path, sub_path, files_sub_path)


def get_index_file():
    return os.path.join(get_files_dir(), 'files_index.json')


def normalize_index(index):
    normalized = {}
    for number, value in index.items():
        if isinstance(value, dict):
            normalized[number] = {
                'name': value.get('name', ''),
                'owner': value.get('owner', ''),
            }
        else:
            normalized[number] = {'name': str(value), 'owner': ''}
    return normalized


def load_files_index():
    idx_file = get_index_file()
    if os.path.exists(idx_file):
        try:
            with open(idx_file, 'r', encoding='utf-8') as f:
                return normalize_index(json.load(f))
        except Exception:
            return {}
    return {}


def save_files_index(index):
    idx_file = get_index_file()
    os.makedirs(os.path.dirname(idx_file), exist_ok=True)
    with open(idx_file, 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


def next_file_number(index):
    nums = [int(k) for k in index.keys() if k.isdigit()]
    return max(nums) + 1 if nums else 1


def send_file(user, file_path, room_id):
    """上传文件到共享目录并分配编号。返回 (是否成功, 提示消息)"""
    file_path = file_path.strip().strip('"').strip("'")
    if not os.path.isfile(file_path):
        return False, f'文件不存在：{file_path}'

    files_dir = get_files_dir()
    os.makedirs(files_dir, exist_ok=True)
    filename = os.path.basename(file_path)
    dest = os.path.join(files_dir, filename)

    base, ext = os.path.splitext(filename)
    counter = 1
    while os.path.exists(dest):
        dest = os.path.join(files_dir, f'{base}({counter}){ext}')
        counter += 1

    try:
        shutil.copy2(file_path, dest)
    except Exception as e:
        return False, f'文件发送失败：{e}'

    index = load_files_index()
    number = next_file_number(index)
    index[str(number)] = {'name': os.path.basename(dest), 'owner': user}
    save_files_index(index)

    size = os.path.getsize(dest)
    tip = f'[文件 #{number}] {user} 上传了文件：{os.path.basename(dest)} ({size/1024:.1f}KB)'
    send_message(user, tip, room_id)
    return True, f'已上传 {os.path.basename(dest)}（编号 #{number}）'


def download_file(number_str, download_dir=None):
    """下载文件到本地目录。返回 (是否成功, 提示消息)"""
    number_str = str(number_str).strip()
    if not number_str.isdigit():
        return False, f'无效编号：{number_str}'

    index = load_files_index()
    if number_str not in index:
        return False, f'编号 {number_str} 不存在，请先刷新文件列表'

    filename = index[number_str]['name']
    src = os.path.join(get_files_dir(), filename)
    if not os.path.isfile(src):
        return False, f'文件已丢失：{filename}'

    if download_dir is None:
        download_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '下载')
    os.makedirs(download_dir, exist_ok=True)
    dest = os.path.join(download_dir, filename)

    try:
        shutil.copy2(src, dest)
    except Exception as e:
        return False, f'下载失败：{e}'
    return True, f'已下载 {filename} 到 {dest}'


def list_files_text():
    """返回文件列表文本"""
    index = load_files_index()
    if not index:
        return '当前没有可下载的文件。'
    lines = ['================ 文件列表 ================']
    for number in sorted(index.keys(), key=lambda x: int(x)):
        info = index[number]
        owner = info.get('owner', '')
        owner_txt = f'（{owner}）' if owner else ''
        lines.append(f'  #{number}  {info["name"]}{owner_txt}')
    lines.append('=========================================')
    return '\n'.join(lines)


def delete_file(user, number_str, room_id=None):
    """删除文件，仅上传者本人。删除成功后向聊天室广播提示。
    返回 (是否成功, 提示消息)"""
    number_str = str(number_str).strip()
    if not number_str.isdigit():
        return False, f'无效编号：{number_str}'

    index = load_files_index()
    if number_str not in index:
        return False, f'编号 {number_str} 不存在'

    info = index[number_str]
    owner = info.get('owner', '')
    if owner and owner != user:
        return False, f'无权删除：文件 #{number_str} 是 {owner} 上传的，只能由上传者删除'

    filename = info['name']
    src = os.path.join(get_files_dir(), filename)
    try:
        if os.path.isfile(src):
            os.remove(src)
        del index[number_str]
        save_files_index(index)
    except Exception as e:
        return False, f'删除失败：{e}'

    # 删除成功后向聊天室广播提示（与上传提示对称）
    if room_id:
        tip = f'[文件 #{number_str}] {user} 删除了文件：{filename}'
        send_message(user, tip, room_id)
    return True, f'已删除文件 #{number_str}：{filename}'


# ============ GUI 界面 ============
class ChatApp:
    def __init__(self, root, user):
        self.root = root
        self.user = user
        self.room = None
        self.room_id = None
        self.known_messages = []   # 当前聊天室已显示的消息
        self.msg_queue = queue.Queue()  # 后台线程 -> 主线程的消息通知
        self.nas_error = False

        root.title('NAS 聊天室')
        root.geometry('680x520')
        root.minsize(560, 420)

        self._build_header()
        self._build_chat_area()
        self._build_file_bar()
        self._build_input_bar()

        # 先选择聊天室
        self.choose_room_dialog()

        # 启动后台轮询线程 + 定时器
        self._start_poller()
        self.root.after(int(poll_interval * 1000), self._poll_queue)

    # ---- 界面构建 ----
    def _build_header(self):
        self.header = ttk.Frame(self.root, padding=(8, 6))
        self.header.pack(side=tk.TOP, fill=tk.X)
        self.room_label = ttk.Label(self.header, text='聊天室：未选择', font=('', 10, 'bold'))
        self.room_label.pack(side=tk.LEFT)
        ttk.Button(self.header, text='切换聊天室', command=self.choose_room_dialog).pack(side=tk.RIGHT)
        ttk.Button(self.header, text='刷新文件', command=self.refresh_files).pack(side=tk.RIGHT, padx=4)

    def _build_chat_area(self):
        # 消息显示区（只读）
        self.chat_text = tk.Text(self.root, wrap='word', state='disabled',
                                 bg='#fafafa', fg='#222', font=('Microsoft YaHei', 10))
        self.chat_text.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=(4, 0))
        # 滚动条
        scroll = ttk.Scrollbar(self.chat_text, command=self.chat_text.yview)
        self.chat_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        # 文本标签样式
        self.chat_text.tag_configure('bold', font=('Microsoft YaHei', 10, 'bold'))
        self.chat_text.tag_configure('mine', foreground='#1a73e8')
        self.chat_text.tag_configure('other', foreground='#222')
        self.chat_text.tag_configure('sys', foreground='#b45309')

    def _build_file_bar(self):
        bar = ttk.Frame(self.root, padding=(8, 6))
        bar.pack(side=tk.TOP, fill=tk.X)
        ttk.Button(bar, text='上传文件', command=self.upload_file).pack(side=tk.LEFT)
        ttk.Button(bar, text='文件管理', command=self.open_file_manager).pack(side=tk.LEFT, padx=4)

    def _build_input_bar(self):
        frame = ttk.Frame(self.root, padding=(8, 6))
        frame.pack(side=tk.BOTTOM, fill=tk.X)
        self.input_var = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=self.input_var, font=('Microsoft YaHei', 10))
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        entry.bind('<Return>', lambda e: self.send_input())
        ttk.Button(frame, text='表情', command=self.open_emoji_picker).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(frame, text='发送', command=self.send_input).pack(side=tk.RIGHT)

    # ---- 表情包窗口 ----
    def open_emoji_picker(self):
        """弹出表情包窗口：点击任意表情插入到输入框"""
        win = tk.Toplevel(self.root)
        win.title('表情包')
        win.resizable(False, False)
        win.transient(self.root)

        tk.Label(win, text='点击表情插入到输入框', font=('', 9), fg='#888').pack(padx=10, pady=(8, 2))

        # 表情包网格
        EMOJIS = [
            '😀', '😁', '😂', '🤣', '😊', '😇', '🙂', '😉',
            '😍', '🥰', '😘', '😜', '🤪', '😎', '🤩', '🥳',
            '😢', '😭', '😅', '😳', '😡', '🤬', '😱', '😴',
            '👍', '👎', '👏', '🙌', '🤝', '💪', '👌', '✌️',
            '❤️', '💔', '💯', '🔥', '✨', '🎉', '🎊', '🎈',
            '🌹', '🌻', '🍀', '🍉', '🍔', '🍕', '☕', '🍺',
            '🐱', '🐶', '🐼', '🦊', '🐸', '🐵', '🦄', '🐷',
            '🚀', '✈️', '🌟', '🌈', '⚡', '❄️', '☀️', '🌙',
        ]
        grid = ttk.Frame(win, padding=(10, 4))
        grid.pack()
        for i, emoji in enumerate(EMOJIS):
            btn = tk.Button(grid, text=emoji, font=('Segoe UI Emoji', 16),
                            width=3, relief='flat',
                            command=lambda e=emoji: self._insert_emoji(e))
            btn.grid(row=i // 8, column=i % 8, padx=2, pady=2)

    def _insert_emoji(self, emoji):
        """把表情插入到输入框当前文本末尾"""
        self.input_var.set(self.input_var.get() + emoji)

    # ---- 聊天室选择 ----
    def choose_room_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title('选择聊天室')
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)

        tk.Label(dlg, text=f'当前用户：{self.user}', font=('', 10, 'bold')).pack(padx=20, pady=(12, 6))
        tk.Label(dlg, text='请选择聊天室：').pack(padx=20)

        var = tk.StringVar(value=str(chat_rooms[0]['id']) if chat_rooms else '')
        for room in chat_rooms:
            tk.Radiobutton(dlg, text=f"{room['name']}", value=room['id'],
                           variable=var).pack(anchor='w', padx=30)

        def confirm():
            rid = var.get()
            room = next((r for r in chat_rooms if r['id'] == rid), chat_rooms[0])
            self.switch_room(room)
            dlg.destroy()

        ttk.Button(dlg, text='进入', command=confirm).pack(pady=(6, 12))

    def switch_room(self, room):
        self.room = room
        self.room_id = room['id']
        # 进入聊天室：先显示"连接存储服务器中..."，强制刷新界面后再访问 NAS
        self.room_label.config(text=f'连接存储服务器中...（{room["name"]}）')
        self.root.update_idletasks()
        try:
            # 确保聊天文件存在并加载现有消息
            ensure_chat_file(self.room_id)
            self.known_messages = read_chat_file(self.room_id)
            self.room_label.config(text=f"聊天室：{room['name']}  （用户：{self.user}）")
            self._render_all_messages()
            self._show_message(f'已连接存储服务器，进入聊天室：{room["name"]}')
        except Exception as e:
            self.room_label.config(text=f"聊天室：{room['name']}  （用户：{self.user}）")
            self._show_message(f'连接存储服务器失败：{e}')

    # ---- 消息渲染 ----
    def _render_all_messages(self):
        self.chat_text.configure(state='normal')
        self.chat_text.delete('1.0', tk.END)
        for other_user, message in self.known_messages:
            self._append_line(other_user, message)
        self.chat_text.configure(state='disabled')
        self.chat_text.see(tk.END)

    def _append_line(self, other_user, message):
        """在消息区追加一行。other_user == 当前用户时高亮。"""
        tag = 'mine' if other_user == self.user else 'other'
        self.chat_text.insert(tk.END, f'{other_user}: ', (tag, 'bold'))
        self.chat_text.insert(tk.END, f'{message}\n', tag)

    def _show_message(self, text):
        """在消息区以系统提示追加一行"""
        self.chat_text.configure(state='normal')
        self.chat_text.insert(tk.END, f'—— {text}\n', 'sys')
        self.chat_text.configure(state='disabled')
        self.chat_text.see(tk.END)

    def _clear_chat_view(self):
        """清空当前聊天区的本地显示（不影响聊天文件，刷新后消息会回来）"""
        self.chat_text.configure(state='normal')
        self.chat_text.delete('1.0', tk.END)
        self.chat_text.configure(state='disabled')
        self._show_message('已清空本地聊天显示（/help 查看命令）')

    def _refresh_chat_view(self, room_id):
        """重读当前聊天室消息并更新到界面（后台线程调用前需放到主线程）"""
        new_msgs = read_chat_file(room_id)
        # 只渲染比已知更新的部分
        start = len(self.known_messages)
        added = new_msgs[start:]
        if added:
            self.chat_text.configure(state='normal')
            for other_user, message in added:
                self._append_line(other_user, message)
            self.chat_text.configure(state='disabled')
            self.chat_text.see(tk.END)
        self.known_messages = new_msgs

    # ---- 发送 ----
    def send_input(self):
        text = self.input_var.get().strip()
        if not text:
            return
        if self.room_id is None:
            return
        self.input_var.set('')
        if text.startswith('/'):
            self._handle_command(text)
        else:
            try:
                send_message(self.user, text, self.room_id)
            except Exception as e:
                self._show_message(f'发送失败：{e}')
                return
            self._refresh_chat_view(self.room_id)

    def _handle_command(self, text):
        if text.startswith('/file'):
            # GUI 里 /file 改为调用文件选择框
            self.upload_file()
        elif text.startswith('/ls'):
            self.show_files()
        elif text.startswith('/dl'):
            parts = text.split(None, 1)
            number = parts[1].strip() if len(parts) > 1 else ''
            if not number:
                self._show_message('用法：/dl <编号>')
            else:
                self.download_by_number(number)
        elif text.startswith('/rm'):
            parts = text.split(None, 1)
            number = parts[1].strip() if len(parts) > 1 else ''
            if not number:
                self._show_message('用法：/rm <编号>')
            else:
                self.delete_by_number(number)
        elif text.startswith('/em'):
            self.open_emoji_picker()
        elif text.startswith('/time'):
            self._show_message(f'当前时间：{time.strftime("%Y-%m-%d %H:%M:%S")}')
        elif text.startswith('/snake') or text.startswith('/game'):
            # 彩蛋：贪吃蛇小游戏（需输入彩蛋密码）
            self._open_snake_game()
        elif text.startswith('/clear'):
            self._clear_chat_view()
        elif text.startswith('/rooms'):
            self.choose_room_dialog()
        elif text.startswith('/help'):
            self._show_help()
        else:
            self._show_message(f'未知命令：{text}')

    def _show_help(self):
        help_text = (
            '可用命令：\n'
            '  /em      打开表情包窗口（或用输入框旁"表情"按钮）\n'
            '  /file    选择文件上传（或用上方"上传文件"按钮）\n'
            '  /ls      查看文件列表\n'
            '  /dl <编号>  下载文件\n'
            '  /rm <编号>  删除文件（仅上传者）\n'
            '  /time    显示当前时间\n'
            '  /clear   清空本地聊天显示\n'
            '  /rooms   切换聊天室\n'
            '  /help    显示帮助\n'
            '  /snake   🐍 隐藏的小惊喜\n'
            '  直接输入文字发送聊天消息'
        )
        messagebox.showinfo('帮助', help_text, parent=self.root)

    # ---- 彩蛋入口 ----
    def _open_snake_game(self):
        """彩蛋小游戏入口：输入正确密码后才能进入贪吃蛇"""
        import tkinter.simpledialog as simpledialog
        pwd = simpledialog.askstring('彩蛋', '输入彩蛋密码：', show='*', parent=self.root)
        if pwd is None:
            return  # 用户取消
        if pwd.strip() != 'sga666':
            self._show_message('彩蛋密码错误，无法进入。')
            return
        SnakeGame(self.root)

    # ---- 文件功能 ----
    def upload_file(self):
        path = filedialog.askopenfilename(title='选择要上传的文件', parent=self.root)
        if not path:
            return
        ok, tip = send_file(self.user, path, self.room_id)
        self._show_message(tip if ok else f'上传失败：{tip}')
        if ok:
            self._refresh_chat_view(self.room_id)

    def refresh_files(self):
        self._show_message('文件列表已刷新')
        self.show_files()

    def show_files(self):
        text = list_files_text()
        self._show_message(text)

    def _get_file_entries(self):
        """返回 [(编号, 文件名, 大小KB, 上传者), ...] 按编号排序"""
        index = load_files_index()
        entries = []
        for number in sorted(index.keys(), key=lambda x: int(x)):
            info = index[number]
            name = info.get('name', '')
            owner = info.get('owner', '')
            size_kb = 0.0
            path = os.path.join(get_files_dir(), name) if name else ''
            if path and os.path.isfile(path):
                size_kb = os.path.getsize(path) / 1024.0
            entries.append((number, name, size_kb, owner))
        return entries

    def open_file_manager(self):
        """打开文件管理窗口：文件列表 + 每行下载/删除按钮（仅自己的文件显示删除）"""
        if self.room_id is None:
            self._show_message('请先进入聊天室')
            return

        win = tk.Toplevel(self.root)
        win.title('文件管理')
        win.geometry('560x420')
        win.minsize(480, 320)
        win.transient(self.root)

        # 顶部工具栏：上传 + 刷新
        top = ttk.Frame(win, padding=(8, 6))
        top.pack(side=tk.TOP, fill=tk.X)
        ttk.Button(top, text='上传文件', command=lambda: self._manager_upload(win)).pack(side=tk.LEFT)
        ttk.Button(top, text='刷新', command=lambda: self._manager_reload(win)).pack(side=tk.LEFT, padx=4)
        ttk.Label(top, text='提示：删除按钮仅对你自己上传的文件显示', foreground='#888').pack(side=tk.RIGHT)

        # 列表区域：Canvas + 内部 Frame 实现滚动
        canvas = tk.Canvas(win, highlightthickness=0)
        scroll = ttk.Scrollbar(win, orient='vertical', command=canvas.yview)
        list_frame = ttk.Frame(canvas)
        list_frame.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.create_window((0, 0), window=list_frame, anchor='nw')
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0), pady=(0, 8))
        scroll.pack(side=tk.RIGHT, fill=tk.Y, pady=(0, 8))

        # 鼠标滚轮支持
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-event.delta / 120), 'units')
        canvas.bind_all('<MouseWheel>', _on_mousewheel)
        win.bind('<Destroy>', lambda e: canvas.unbind_all('<MouseWheel>'))

        win._manager_canvas = canvas
        win._manager_list_frame = list_frame
        self._manager_reload(win)

    def _manager_upload(self, win):
        path = filedialog.askopenfilename(title='选择要上传的文件', parent=win)
        if not path:
            return
        ok, tip = send_file(self.user, path, self.room_id)
        if not ok:
            messagebox.showerror('上传失败', tip, parent=win)
            return
        self._show_message(tip)
        self._refresh_chat_view(self.room_id)
        self._manager_reload(win)

    def _manager_reload(self, win):
        """刷新文件管理窗口的列表内容"""
        list_frame = win._manager_list_frame
        # 清空旧内容
        for child in list_frame.winfo_children():
            child.destroy()

        entries = self._get_file_entries()
        if not entries:
            ttk.Label(list_frame, text='当前没有可下载的文件。', foreground='#888').pack(anchor='w', padx=12, pady=12)
            return

        # 表头
        header = ttk.Frame(list_frame)
        header.pack(fill=tk.X, padx=4, pady=(4, 2))
        ttk.Label(header, text='编号', width=6, font=('', 9, 'bold')).pack(side=tk.LEFT)
        ttk.Label(header, text='文件名', width=28, font=('', 9, 'bold'), anchor='w').pack(side=tk.LEFT)
        ttk.Label(header, text='大小', width=10, font=('', 9, 'bold'), anchor='e').pack(side=tk.LEFT)
        ttk.Label(header, text='上传者', width=12, font=('', 9, 'bold'), anchor='w').pack(side=tk.LEFT)
        ttk.Label(header, text='操作', width=14, font=('', 9, 'bold'), anchor='w').pack(side=tk.LEFT)

        for number, name, size_kb, owner in entries:
            row = ttk.Frame(list_frame)
            row.pack(fill=tk.X, padx=4, pady=2)
            ttk.Label(row, text=f'#{number}', width=6, foreground='#1a73e8').pack(side=tk.LEFT)
            ttk.Label(row, text=name, width=28, anchor='w').pack(side=tk.LEFT)
            ttk.Label(row, text=f'{size_kb:.1f}KB', width=10, anchor='e').pack(side=tk.LEFT)
            owner_txt = owner if owner else '未知'
            ttk.Label(row, text=owner_txt, width=12, anchor='w').pack(side=tk.LEFT)
            # 操作列：下载按钮 + 删除按钮（仅上传者本人可见）
            ttk.Button(row, text='下载', width=5,
                       command=lambda n=number: self._manager_download(win, n)).pack(side=tk.LEFT, padx=2)
            if owner and owner == self.user:
                ttk.Button(row, text='删除', width=5,
                           command=lambda n=number: self._manager_delete(win, n)).pack(side=tk.LEFT, padx=2)

    def _manager_download(self, win, number):
        # 选择保存目录
        save_dir = filedialog.askdirectory(title='选择保存位置', parent=win)
        if not save_dir:
            return
        ok, tip = download_file(number, download_dir=save_dir)
        if not ok:
            messagebox.showerror('下载失败', tip, parent=win)
            return
        messagebox.showinfo('下载成功', tip, parent=win)

    def _manager_delete(self, win, number):
        if not messagebox.askyesno('确认删除', f'确定删除文件 #{number} 吗？', parent=win):
            return
        ok, tip = delete_file(self.user, number, room_id=self.room_id)
        if not ok:
            messagebox.showerror('删除失败', tip, parent=win)
            return
        self._show_message(tip)
        self._manager_reload(win)

    def download_by_number(self, number):
        """按编号下载文件到本地（/dl 命令使用）"""
        if not number:
            self._show_message('请先输入要下载的文件编号')
            return
        ok, tip = download_file(number)
        self._show_message(tip if ok else f'下载失败：{tip}')

    def delete_by_number(self, number):
        """按编号删除文件，仅上传者可删（/rm 命令使用）"""
        if not number:
            self._show_message('请先输入要删除的文件编号')
            return
        if not messagebox.askyesno('确认删除', f'确定删除文件 #{number} 吗？', parent=self.root):
            return
        ok, tip = delete_file(self.user, number, room_id=self.room_id)
        self._show_message(tip if ok else f'删除失败：{tip}')

    # ---- 后台轮询 ----
    def _start_poller(self):
        # 后台线程：轮询聊天文件，把"有新消息"通知放入队列
        def worker():
            last_len = 0
            while True:
                time.sleep(poll_interval)
                if self.room_id is None:
                    continue
                try:
                    msgs = read_chat_file(self.room_id)
                    if len(msgs) != last_len:
                        self.msg_queue.put(True)
                        last_len = len(msgs)
                except Exception:
                    self.msg_queue.put('nas_error')
        threading.Thread(target=worker, daemon=True).start()

    def _poll_queue(self):
        # 主线程定时器：处理队列中的通知
        try:
            while True:
                item = self.msg_queue.get_nowait()
                if item == 'nas_error':
                    if not self.nas_error:
                        self.nas_error = True
                        self._show_message('NAS 连接失败，正在重试...')
                else:
                    if self.nas_error:
                        self.nas_error = False
                        self._show_message('已恢复与 NAS 的连接')
                    self._refresh_chat_view(self.room_id)
        except queue.Empty:
            pass
        self.root.after(int(poll_interval * 1000), self._poll_queue)


# ============ 彩蛋小游戏：贪吃蛇 ============
class SnakeGame:
    """隐藏彩蛋：贪吃蛇小游戏。
    触发方式：在聊天输入框输入 /snake 或 /game。
    操作：方向键移动 · 空格暂停/继续 · 回车重新开始 · 关闭窗口退出。
    """
    CELL = 20                       # 每格边长（像素）
    COLS, ROWS = 24, 16             # 网格尺寸
    SPEED_MS = 130                  # 移动速度（毫秒/格）

    def __init__(self, master):
        self.master = master
        self.win = tk.Toplevel(master)
        self.win.title('🐍 贪吃蛇 · 彩蛋')
        self.win.resizable(False, False)
        self.win.transient(master)
        self.win.protocol('WM_DELETE_WINDOW', self._on_close)

        canvas_w = self.COLS * self.CELL
        canvas_h = self.ROWS * self.CELL
        self.canvas = tk.Canvas(self.win, width=canvas_w, height=canvas_h,
                                bg='#101418', highlightthickness=0)
        self.canvas.pack(padx=10, pady=(10, 0))

        bar = ttk.Frame(self.win, padding=(10, 6))
        bar.pack(fill=tk.X)
        self.score_var = tk.StringVar(value='得分：0')
        ttk.Label(bar, textvariable=self.score_var, font=('', 11, 'bold')).pack(side=tk.LEFT)
        ttk.Label(bar, text='方向键移动 · 空格暂停 · 回车重来', foreground='#888').pack(side=tk.RIGHT)

        self._reset()
        self.win.bind('<KeyPress>', self._on_key)
        self.win.focus_force()
        self._timer = None
        self._tick()

    # ---- 游戏状态 ----
    def _reset(self):
        cx, cy = self.COLS // 2, self.ROWS // 2
        self.snake = [(cx, cy), (cx - 1, cy), (cx - 2, cy)]
        self.dir = (1, 0)                    # 当前方向
        self.pending_dir = (1, 0)            # 待生效方向（防止快速连按反转）
        self.score = 0
        self.paused = False
        self.over = False
        self.food = self._spawn_food()
        self.score_var.set('得分：0')
        self._draw()

    def _spawn_food(self):
        # 在所有非蛇身格子中随机生成食物
        free = [(x, y) for x in range(self.COLS) for y in range(self.ROWS)
                if (x, y) not in self.snake]
        return random.choice(free) if free else None

    def _on_key(self, event):
        key = event.keysym.lower()
        if key in ('up', 'down', 'left', 'right'):
            d = {'up': (0, -1), 'down': (0, 1), 'left': (-1, 0), 'right': (1, 0)}[key]
            if (d[0] * -1, d[1] * -1) != self.dir:   # 禁止 180° 掉头
                self.pending_dir = d
        elif key == 'space':
            if not self.over:
                self.paused = not self.paused
        elif key in ('return', 'r'):
            self._reset()

    def _tick(self):
        try:
            if not self.over and not self.paused:
                self._step()
            self._draw()
            self._timer = self.win.after(self.SPEED_MS, self._tick)
        except tk.TclError:
            pass    # 窗口已关闭，静默停止

    def _step(self):
        self.dir = self.pending_dir
        hx, hy = self.snake[0]
        nh = (hx + self.dir[0], hy + self.dir[1])
        if not (0 <= nh[0] < self.COLS and 0 <= nh[1] < self.ROWS):   # 撞墙
            self.over = True
            return
        if nh in self.snake[:-1]:                                      # 撞自己
            self.over = True
            return
        self.snake.insert(0, nh)
        if nh == self.food:
            self.score += 1
            self.score_var.set(f'得分：{self.score}')
            self.food = self._spawn_food()
        else:
            self.snake.pop()

    def _draw(self):
        c = self.canvas
        c.delete('all')
        # 食物
        if self.food:
            fx, fy = self.food
            c.create_oval(fx * self.CELL + 3, fy * self.CELL + 3,
                          fx * self.CELL + self.CELL - 3, fy * self.CELL + self.CELL - 3,
                          fill='#f87171', outline='')
        # 蛇身（头为深绿色）
        for i, (x, y) in enumerate(self.snake):
            color = '#22c55e' if i == 0 else '#4ade80'
            c.create_rectangle(x * self.CELL + 1, y * self.CELL + 1,
                               x * self.CELL + self.CELL - 1, y * self.CELL + self.CELL - 1,
                               fill=color, outline='')
        # 状态提示
        cx, cy = self.COLS * self.CELL / 2, self.ROWS * self.CELL / 2
        if self.over:
            c.create_text(cx, cy, text=f'游戏结束！得分：{self.score}\n回车重新开始',
                          fill='#ffffff', font=('Microsoft YaHei', 13, 'bold'), justify='center')
        elif self.paused:
            c.create_text(cx, cy, text='已暂停 · 按空格继续',
                          fill='#ffffff', font=('Microsoft YaHei', 12, 'bold'))

    def _on_close(self):
        # 关闭窗口时取消定时器，避免后台空转
        try:
            if self._timer is not None:
                self.win.after_cancel(self._timer)
        except tk.TclError:
            pass
        self.win.destroy()


def main():
    # 用户名：优先 config，否则弹窗输入
    user = config.get('default_user', '').strip()
    if not user:
        # 用简单的输入对话框获取用户名
        import tkinter.simpledialog as simpledialog
        tmp = tk.Tk()
        tmp.withdraw()
        user = simpledialog.askstring('用户名', '请输入你的用户名：', parent=tmp)
        tmp.destroy()
        if not user:
            print('未输入用户名，程序退出。')
            sys.exit(0)
        user = user.strip()

    root = tk.Tk()
    app = ChatApp(root, user)
    root.mainloop()


if __name__ == '__main__':
    main()

import os
import json
import shutil
import sys
import threading
import time

# 全局锁：保护对共享文件/NAS 的并发读写
file_lock = threading.Lock()

# ---- 输入：主线程使用 input() 整行输入（完美兼容中文输入法 IME）----
# 后台刷新与 input() 的冲突通过"输入区固定 + 新消息上滚"的界面方案解决

# ============ 配置加载 ============
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
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config.update(json.load(f))
    return config

config = load_config()
nas_path = config['nas_path']
sub_path = config['sub_path']
files_sub_path = config.get('files_sub_path', 'files')
chat_rooms = config['chat_rooms']
poll_interval = float(config.get('poll_interval', 1.0))

messages = []

# ============ 界面函数 ============
# ANSI 转义码
CSI = '\033['          # Control Sequence Introducer
HOME = CSI + 'H'       # 光标移到左上角
CLEAR_BELOW = CSI + 'J'  # 清除光标位置到屏幕末尾

# Windows 上启用 ANSI 转义支持（Windows 10+ 的终端才支持）
if os.name == 'nt':
    os.system('')

def clear_screen():
    # 光标归位到左上角，并清除从光标到屏幕末尾的内容（整屏重绘用）
    print(HOME + CLEAR_BELOW, end='', flush=True)

def get_chat_file_path(room_id):
    # 每个聊天室一个独立文件：chat-text/<room_id>.txt
    return os.path.join(nas_path, sub_path, f'{room_id}.txt')

def ensure_chat_file(room_id):
    # 确保某个聊天室的文件存在
    path = get_chat_file_path(room_id)
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('')

def read_chat_file(room_id):
    global messages
    new_messages = []
    path = get_chat_file_path(room_id)
    with file_lock:
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        other_user, message = line.split(':', 1)
        new_messages.append((other_user.strip(), message.strip()))
    messages = new_messages

# ANSI 光标控制序列
SAVE_CURSOR = '\033[s'   # 保存光标位置
RESTORE_CURSOR = '\033[u'  # 恢复光标位置
ERASE_LINE = '\033[2K'   # 清除整行
MOVE_UP = '\033[1A'      # 光标上移一行

def print_messages():
    global messages
    print('------------------chat-------------------')
    for other_user, message in messages:
        print(f'{other_user}: {message}')

def print_message_lines(lines):
    """把多行文本打印到当前光标处（用于后台插入新消息）"""
    for line in lines:
        print(line)

def send_message(user, message, room_id):
    global messages
    path = get_chat_file_path(room_id)
    with file_lock:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(f'{user}: {message}\n')

def get_files_dir():
    # 文件共享目录：nas_path/sub_path/files
    return os.path.join(nas_path, sub_path, files_sub_path)

def get_index_file():
    # 文件索引文件，记录编号 -> {name, owner}
    return os.path.join(get_files_dir(), 'files_index.json')

def normalize_index(index):
    """把索引规范成 {编号: {'name':..., 'owner':...}}，兼容旧版 {编号: 文件名}"""
    normalized = {}
    for number, value in index.items():
        if isinstance(value, dict):
            normalized[number] = {
                'name': value.get('name', ''),
                'owner': value.get('owner', ''),
            }
        else:
            # 旧版索引：直接是文件名，owner 未知
            normalized[number] = {'name': str(value), 'owner': ''}
    return normalized

def load_files_index():
    """读取文件索引，返回 {编号: {'name':..., 'owner':...}}"""
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
    # 生成下一个编号（取现有编号最大值 + 1）
    nums = [int(k) for k in index.keys() if k.isdigit()]
    return max(nums) + 1 if nums else 1

def send_file(user, file_path, room_id):
    """把本地文件复制到 NAS 共享目录，分配编号并广播提示。
    返回 (是否成功, 提示消息)"""
    global messages
    file_path = file_path.strip().strip('"').strip("'")
    if not os.path.isfile(file_path):
        return False, f'文件不存在：{file_path}'

    files_dir = get_files_dir()
    os.makedirs(files_dir, exist_ok=True)
    filename = os.path.basename(file_path)
    dest = os.path.join(files_dir, filename)

    # 避免重名覆盖，若已存在则加序号
    base, ext = os.path.splitext(filename)
    counter = 1
    while os.path.exists(dest):
        dest = os.path.join(files_dir, f'{base}({counter}){ext}')
        counter += 1

    try:
        shutil.copy2(file_path, dest)
    except Exception as e:
        return False, f'文件发送失败：{e}'

    # 分配编号并更新索引（记录上传者，用于权限校验）
    index = load_files_index()
    number = next_file_number(index)
    index[str(number)] = {'name': os.path.basename(dest), 'owner': user}
    save_files_index(index)

    size = os.path.getsize(dest)
    tip = f'[文件 #{number}] {user} 上传了文件：{os.path.basename(dest)} ({size/1024:.1f}KB)，输入 /dl {number} 下载'
    send_message(user, tip, room_id)
    return True, tip

def download_file(number_str):
    """根据编号把共享目录里的文件下载到本地 '下载' 文件夹。
    返回 (是否成功, 提示消息)"""
    number_str = number_str.strip()
    if not number_str.isdigit():
        return False, f'无效编号：{number_str}，请输入数字编号'

    index = load_files_index()
    if number_str not in index:
        return False, f'编号 {number_str} 不存在，请先用 /ls 查看可用文件'

    filename = index[number_str]['name']
    src = os.path.join(get_files_dir(), filename)
    if not os.path.isfile(src):
        return False, f'文件已丢失：{filename}（索引中编号 {number_str}）'

    # 下载到脚本所在目录的 '下载' 文件夹
    download_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '下载')
    os.makedirs(download_dir, exist_ok=True)
    dest = os.path.join(download_dir, filename)

    try:
        shutil.copy2(src, dest)
    except Exception as e:
        return False, f'下载失败：{e}'

    return True, f'已下载 {filename} 到 {dest}'

def list_files():
    """列出共享目录所有文件（带编号和上传者），返回提示消息"""
    index = load_files_index()
    if not index:
        return '当前没有可下载的文件。'
    lines = ['============== 文件列表 ==============']
    for number in sorted(index.keys(), key=lambda x: int(x)):
        info = index[number]
        owner = info.get('owner', '')
        owner_txt = f'（{owner}）' if owner else ''
        lines.append(f'  #{number}  {info["name"]}{owner_txt}')
    lines.append('=====================================')
    return '\n'.join(lines)

def delete_file(user, number_str):
    """根据编号删除文件。仅上传者本人可删。
    返回 (是否成功, 提示消息)"""
    number_str = number_str.strip()
    if not number_str.isdigit():
        return False, f'无效编号：{number_str}，请输入数字编号'

    index = load_files_index()
    if number_str not in index:
        return False, f'编号 {number_str} 不存在，请先用 /ls 查看可用文件'

    info = index[number_str]
    owner = info.get('owner', '')
    if owner and owner != user:
        return False, f'无权删除：文件 #{number_str} 是 {owner} 上传的，只能由上传者删除'

    filename = info['name']
    src = os.path.join(get_files_dir(), filename)
    try:
        if os.path.isfile(src):
            os.remove(src)
        # 从索引中移除
        del index[number_str]
        save_files_index(index)
    except Exception as e:
        return False, f'删除失败：{e}'

    return True, f'已删除文件 #{number_str}：{filename}'

def refresh_display():
    # 整屏重绘：清屏后重新打印全部消息 + 底部输入提示
    clear_screen()
    print_messages()
    print('> ', end='', flush=True)

def append_new_messages(room_id, shown_count):
    """把新增消息追加到输入提示上方，并返回新的已显示消息数。
    使用 ANSI 光标保存/恢复，不打断输入。"""
    global messages
    read_chat_file(room_id)
    new_lines = messages[shown_count:]
    if not new_lines:
        return shown_count
    # 保存当前光标（input 行末尾）→ 移到消息区 → 打印新消息 → 恢复光标
    sys.stdout.write(SAVE_CURSOR)
    # 清掉输入提示行，避免被新消息覆盖时残留
    sys.stdout.write(MOVE_UP + ERASE_LINE + '\r')
    for other_user, message in new_lines:
        sys.stdout.write(f'{other_user}: {message}\n')
    # 恢复光标到输入行末尾并重绘提示
    sys.stdout.write(RESTORE_CURSOR)
    sys.stdout.write(ERASE_LINE + '> ')
    sys.stdout.flush()
    return len(messages)

def handle_command_or_message(user, text, room_id):
    """处理一条完整输入：斜杠命令或普通消息"""
    if text.startswith('/'):
        if text.startswith('/file'):
            ok, tip = send_file(user, text[5:], room_id)
            read_chat_file(room_id)
            if ok:
                refresh_display()
            else:
                print(tip)
                print('> ', end='', flush=True)
        elif text.startswith('/dl'):
            parts = text.split(None, 1)
            if len(parts) < 2:
                print('用法：/dl <编号>')
                print('> ', end='', flush=True)
            else:
                ok, tip = download_file(parts[1])
                print(tip)
                print('> ', end='', flush=True)
        elif text.startswith('/ls'):
            print(list_files())
            print('> ', end='', flush=True)
        elif text.startswith('/rm'):
            parts = text.split(None, 1)
            if len(parts) < 2:
                print('用法：/rm <编号>（仅上传者可删除）')
                print('> ', end='', flush=True)
            else:
                ok, tip = delete_file(user, parts[1])
                if ok:
                    read_chat_file(room_id)
                    refresh_display()
                else:
                    print(tip)
                    print('> ', end='', flush=True)
        elif text.startswith('/help'):
            clear_screen()
            print('============== 可用命令 ==============')
            print('  /file <路径>   上传文件到共享目录')
            print('  /ls            查看可下载的文件列表')
            print('  /dl <编号>     下载编号对应的文件到"下载"文件夹')
            print('  /rm <编号>     删除编号对应的文件（仅上传者）')
            print('  /help          显示帮助')
            print('  直接输入文字   发送聊天消息')
            print('======================================')
            print('> ', end='', flush=True)
        else:
            print(f'未知命令：{text}')
            print('> ', end='', flush=True)
        return

    # 普通消息
    send_message(user, text, room_id)
    read_chat_file(room_id)
    refresh_display()

def choose_room():
    # 显示聊天室列表并让用户选择
    clear_screen()
    print('============== 请选择聊天室 ==============')
    for idx, room in enumerate(chat_rooms, 1):
        print(f'  {idx}. {room["name"]}')
    print('==========================================')
    while True:
        choice = input('请输入编号(1-{})：'.format(len(chat_rooms))).strip()
        if choice.isdigit() and 1 <= int(choice) <= len(chat_rooms):
            room = chat_rooms[int(choice) - 1]
            print(f'已进入聊天室：{room["name"]}')
            return room
        print(f'无效输入，请输入 1-{len(chat_rooms)} 之间的数字。')

# ============ 后台线程 ============
def poll_chat_file(room_id, shown_count_holder):
    """后台线程：监测聊天文件是否有新消息。
    检测到变化时，把新增消息追加显示到输入提示上方（不打断 input）。"""
    while True:
        time.sleep(poll_interval)
        try:
            shown = append_new_messages(room_id, shown_count_holder[0])
        except Exception:
            continue
        if shown != shown_count_holder[0]:
            shown_count_holder[0] = shown

# ============ 主程序 ============
# 直接使用 config.json 里的 default_user，不再手动输入
user = config.get('default_user', '')
if not user.strip():
    print('错误：config.json 中未设置 default_user 用户名！')
    exit(1)

# 选择聊天室
room = choose_room()
room_id = room['id']
print("连接存储服务器中...")

ensure_chat_file(room_id)
read_chat_file(room_id)

# 显示消息区 + 底部输入提示
refresh_display()

# 已显示的消息条数（后台线程以此判断并追加新消息）
shown_count_holder = [len(messages)]
threading.Thread(target=poll_chat_file, args=(room_id, shown_count_holder), daemon=True).start()

# 主循环：使用 input() 整行输入，完美兼容中文输入法。
# 后台新消息由 poll_chat_file 追加到输入提示上方，不会打断这里的 input()。
while True:
    send_text = input()
    text = send_text.strip()

    # 后台可能已追加了新消息，同步已显示计数
    shown_count_holder[0] = len(messages)

    if not text:
        # 空回车：不发送，重新显示输入提示
        print('> ', end='', flush=True)
        continue

    # 处理命令或消息（发送后统一整屏重绘，避免输入行与追加消息错乱）
    handle_command_or_message(user, text, room_id)
    shown_count_holder[0] = len(messages)

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

def clear_screen_lines(rows):
    """逐行清屏：光标归位后，对前 rows 行逐行清除再归位。
    比 \033[J 更可靠，能确保旧的顶部栏等内容被彻底清除。"""
    out = [HOME]
    for i in range(rows):
        out.append(ERASE_LINE)
        if i < rows - 1:
            out.append('\n')
    out.append(HOME)
    sys.stdout.write(''.join(out))
    sys.stdout.flush()

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
        # 容错解析：某行格式异常（缺冒号）时跳过，避免整批消息读取失败
        if ':' not in line:
            continue
        other_user, message = line.split(':', 1)
        new_messages.append((other_user.strip(), message.strip()))
    messages = new_messages

# ANSI 光标控制序列
SAVE_CURSOR = '\033[s'   # 保存光标位置
RESTORE_CURSOR = '\033[u'  # 恢复光标位置
ERASE_LINE = '\033[2K'   # 清除整行
MOVE_UP = '\033[1A'      # 光标上移一行

# 消息区最大高度（行数），限制顶部栏与输入栏的间距，避免过大
MAX_MSG_AREA = 15

def get_term_height():
    """获取终端可用的行数；获取失败时回退到 24"""
    try:
        return os.get_terminal_size().lines
    except Exception:
        return 24

def render_screen(show_prompt=True):
    """整屏重绘固定布局：
       - 顶部栏（标题行）固定在第 1 行
       - 消息区固定高度（最多 MAX_MSG_AREA 行），超出只显示最新几条
       - 输入栏固定在消息区之后一行
       顶部栏与输入栏的距离始终一致，且不会因终端过高而间距过大。
       清屏采用逐行清除，避免旧的顶部栏残留。"""
    term_h = get_term_height()
    msg_area = max(min(term_h - 2, MAX_MSG_AREA), 1)  # 消息区行数
    # 逐行清除，确保旧的顶部栏等内容被彻底清掉
    clear_screen_lines(term_h)
    print('-------------------chat-------------------')
    # 只显示最新 msg_area 条消息，保证输入栏位置固定
    for other_user, message in messages[-msg_area:]:
        print(f'{other_user}: {message}')
    # 用空行把输入栏推到固定位置（标题 1 行 + 消息区 msg_area 行）
    filled = 1 + min(len(messages), msg_area)
    for _ in range(filled, msg_area + 1):
        print('')
    if show_prompt:
        print('> ', end='', flush=True)
    else:
        sys.stdout.flush()

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
    # 整屏重绘固定布局（标题 + 固定消息区 + 底部输入栏）
    render_screen(show_prompt=True)

def append_new_messages(room_id, shown_count):
    """把新增消息按固定布局重绘屏幕，并返回新的已显示消息数。
    使用 ANSI 光标保存/恢复，尽量不打断 input() 输入。"""
    global messages
    read_chat_file(room_id)
    new_lines = messages[shown_count:]
    if not new_lines:
        return shown_count
    # 保存当前 input 光标位置 → 整屏重绘 → 恢复光标位置
    sys.stdout.write(SAVE_CURSOR)
    sys.stdout.flush()
    render_screen(show_prompt=True)
    sys.stdout.write(RESTORE_CURSOR)
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
    nas_error_reported = False
    while True:
        time.sleep(poll_interval)
        try:
            shown = append_new_messages(room_id, shown_count_holder[0])
            if shown != shown_count_holder[0]:
                shown_count_holder[0] = shown
            # NAS 恢复后清除报错标记
            if nas_error_reported:
                nas_error_reported = False
                sys.stdout.write(MOVE_UP + ERASE_LINE + '\r')
                sys.stdout.write('[提示] 已恢复与 NAS 的连接\n')
                sys.stdout.write(RESTORE_CURSOR)
                sys.stdout.write(ERASE_LINE + '> ')
                sys.stdout.flush()
        except Exception:
            # NAS 不可用：只提示一次，避免刷屏；持续重试直至恢复
            if not nas_error_reported:
                nas_error_reported = True
                try:
                    sys.stdout.write(MOVE_UP + ERASE_LINE + '\r')
                    sys.stdout.write('[警告] NAS 连接失败，正在重试...（请检查网络或共享路径）\n')
                    sys.stdout.write(RESTORE_CURSOR)
                    sys.stdout.write(ERASE_LINE + '> ')
                    sys.stdout.flush()
                except Exception:
                    pass

# ============ 主程序 ============
# 优先使用 config.json 里的 default_user；若未设置，则启动时手动输入
user = config.get('default_user', '').strip()
if not user:
    while True:
        user = input('请输入你的用户名：').strip()
        if user:
            break
        print('用户名不能为空，请重新输入。')

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

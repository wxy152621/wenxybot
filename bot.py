#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import platform
import plistlib
import random
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Set

import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_USER_IDS: Set[int] = {
    int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()
}
MODEL = os.getenv("MODEL", "qwen2.5:1.5b").strip()
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate").strip()
TG_PROXY = os.getenv("TG_PROXY", "").strip()

BOT_DIR = Path(os.getenv("BOT_DIR", str(Path.home() / "wenbot"))).expanduser()
BOT_FILE = os.getenv("BOT_FILE", "bot.py").strip()
BOT_PATH = BOT_DIR / BOT_FILE
RESTART_SCRIPT = os.getenv("RESTART_SCRIPT", str(BOT_DIR / "restart_bot.sh")).strip()
DEPLOY_SCRIPT = os.getenv("DEPLOY_SCRIPT", str(BOT_DIR / "deploy.sh")).strip()
LOG_FILE = os.getenv("LOG_FILE", str(BOT_DIR / "bot.log")).strip()
DEFAULT_BRANCH = os.getenv("DEFAULT_BRANCH", "main").strip() or "main"

CMD_WHITELIST = {
    "pwd",
    "ls",
    "whoami",
    "date",
    "uname",
    "uptime",
    "df",
    "free",
    "top",
    "ps",
    "git",
    "python3",
    "pip3",
    "brew",
    "ollama",
    "node",
    "npm",
    "curl",
    "cat",
    "head",
    "tail",
    "echo",
    "du",
    "which",
    "lsof",
    "pbpaste",
    "pbcopy",
    "osascript",
}

CHAT_HISTORY: dict[int, list[dict[str, str]]] = {}
FORTUNES = [
    "今天适合先做最重要的那件事。",
    "先跑通，再优化。",
    "日志比猜测更可靠。",
    "小步提交，大步安心。",
    "能自动化的事，就不要重复手动做。",
    "先确认现状，再执行修复。",
    "今天的你很适合发一个稳定版本。",
    "保持备份，保持冷静，保持可回滚。",
]


def is_allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


def run_shell(command: str, cwd: Path | None = None, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        shell=True,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def trim_text(text: str, limit: int = 3500) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n...[truncated]"


async def send_long_text(update: Update, text: str, chunk_size: int = 3500) -> None:
    text = text or "(empty)"
    for i in range(0, len(text), chunk_size):
        await update.message.reply_text(text[i:i + chunk_size])


def get_history(chat_id: int) -> list[dict[str, str]]:
    if chat_id not in CHAT_HISTORY:
        CHAT_HISTORY[chat_id] = []
    return CHAT_HISTORY[chat_id]


def ask_ollama(prompt: str, chat_id: int, system_hint: str | None = None) -> str:
    history = get_history(chat_id)
    if system_hint:
        history.append({"role": "system", "content": system_hint})
    history.append({"role": "user", "content": prompt})
    history = history[-12:]
    CHAT_HISTORY[chat_id] = history

    combined_prompt = ""
    for item in history:
        role = item["role"].capitalize()
        combined_prompt += f"{role}: {item['content']}\n"
    combined_prompt += "Assistant:"

    r = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "prompt": combined_prompt,
            "stream": False,
        },
        timeout=180,
    )
    r.raise_for_status()
    data = r.json()
    answer = data.get("response", "").strip() or "No response."
    history.append({"role": "assistant", "content": answer})
    CHAT_HISTORY[chat_id] = history[-12:]
    return answer


def format_proc_output(result: subprocess.CompletedProcess, empty_text: str = "(no output)") -> str:
    output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
    return output.strip() or empty_text


def read_last_lines(path: Path, n: int = 100) -> str:
    if not path.exists():
        return f"日志文件不存在: {path}"
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        selected = lines[-n:]
        return "".join(selected).strip() or "(log empty)"
    except Exception as e:
        return f"读取日志失败: {e}"


def is_macos() -> bool:
    return platform.system() == "Darwin"


def get_git_branch() -> str:
    result = run_shell("git rev-parse --abbrev-ref HEAD", cwd=BOT_DIR, timeout=20)
    return result.stdout.strip() or "(unknown)"


def get_git_commit() -> str:
    result = run_shell("git rev-parse --short HEAD", cwd=BOT_DIR, timeout=20)
    return result.stdout.strip() or "(unknown)"




def get_display_count() -> int:
    """Return number of active displays on macOS."""
    if not is_macos():
        return 1
    try:
        result = run_shell("system_profiler SPDisplaysDataType -json", timeout=60)
        if result.returncode == 0 and result.stdout.strip():
            data = plistlib.loads(result.stdout.encode("utf-8"))
            items = data.get("SPDisplaysDataType", [])
            count = 0
            for gpu in items:
                ndisplays = gpu.get("spdisplays_ndrvs", [])
                count += len(ndisplays)
            return max(count, 1)
    except Exception:
        pass
    return 1


def get_frontmost_app() -> str:
    if not is_macos():
        return "(unsupported)"
    script = 'tell application "System Events" to get name of first application process whose frontmost is true'
    result = run_shell(f"osascript -e {shlex.quote(script)}", timeout=15)
    return result.stdout.strip() or "(unknown)"


def get_clipboard_text() -> str:
    if not is_macos():
        return "(unsupported)"
    result = run_shell("pbpaste", timeout=15)
    if result.returncode != 0:
        return format_proc_output(result, "读取剪贴板失败")
    return result.stdout or "(clipboard empty)"


def set_clipboard_text(text: str) -> str:
    if not is_macos():
        return "(unsupported)"
    result = subprocess.run(
        "pbcopy",
        shell=True,
        input=text,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=15,
    )
    if result.returncode != 0:
        return ((result.stdout or "") + ("\n" + result.stderr if result.stderr else "")).strip() or "设置剪贴板失败"
    return "剪贴板已更新。"


def get_volume_value() -> str:
    if not is_macos():
        return "(unsupported)"
    result = run_shell("osascript -e 'output volume of (get volume settings)'", timeout=15)
    return result.stdout.strip() or "(unknown)"


def parse_screenshot_mode(args: list[str]) -> str:
    if not args:
        return "main"
    mode = (args[0] or "").strip().lower()
    aliases = {
        "all": "all",
        "main": "main",
        "1": "1",
        "2": "2",
        "left": "1",
        "right": "2",
    }
    return aliases.get(mode, "main")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    help_text = (
        "wenbot 已就绪。 "
        "/id - 查看你的 user id "
        "/ping - 测试当前版本 "
        "/reset - 清空上下文 "
        "/cmd <command> - 执行白名单命令 "
        "/open <App或URL> - 在电脑上打开 App 或网址 "
        "/say <text> - 电脑朗读文本 "
        "/notify <text> - 发系统通知 "
        "/agent <自然语言> - AI 助手 "
        "/think <问题> - 更详细地分析问题 "
        "/status - 查看状态 "
        "/git - 查看当前 Git 信息 "
        "/log [行数] - 查看日志，默认 100 行 "
        "/sys - 查看系统状态 "
        "/top - 查看最占资源的进程 "
        "/ports - 查看当前监听端口 "
        "/clip - 查看剪贴板 "
        "/clip set <文本> - 设置剪贴板 "
        "/music <play|pause|next|prev> - 控制媒体播放 "
        "/volume - 查看音量 "
        "/volume <0-100|mute|max> - 设置音量 "
        "/screenshot - 截主屏 "
        "/screenshot all - 截全部屏幕 "
        "/screenshot 1 - 截第一个屏幕 "
        "/screenshot 2 - 截第二个屏幕 "
        "/camera - 调用摄像头拍照（需安装 imagesnap） "
        "/price <symbol> - 查询币价，例如 /price btc "
        "/fortune - 随机一句提示 "
        "/deploy - 执行部署脚本 "
        "/fix <问题> - 生成修复建议，不直接执行 "
        "/restart - 重启 bot "
        "/update - git pull + 语法检查 + 重启"
    )
    await update.message.reply_text(help_text)


async def get_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        await update.message.reply_text("No user.")
        return
    await update.message.reply_text(f"user_id={user.id}")


async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return
    branch = get_git_branch()
    commit = get_git_commit()
    await update.message.reply_text(f"pong\nbranch={branch}\ncommit={commit}")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return
    if update.effective_chat:
        CHAT_HISTORY.pop(update.effective_chat.id, None)
    await update.message.reply_text("Context cleared.")


async def cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /cmd <command>")
        return

    full_cmd = " ".join(context.args).strip()
    if not full_cmd:
        await update.message.reply_text("Empty command.")
        return

    try:
        first = shlex.split(full_cmd)[0]
    except Exception as e:
        await update.message.reply_text(f"Parse error: {e}")
        return

    if first not in CMD_WHITELIST:
        await update.message.reply_text(f"Blocked. '{first}' is not in whitelist.")
        return

    try:
        result = run_shell(full_cmd, cwd=BOT_DIR, timeout=180)
        output = format_proc_output(result)
        await send_long_text(update, trim_text(output))
    except Exception as e:
        await update.message.reply_text(f"Command failed: {e}")


async def open_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    target = " ".join(context.args).strip()
    if not target:
        await update.message.reply_text("Usage: /open <App或URL>")
        return

    if not is_macos():
        await update.message.reply_text("当前机器不是 macOS，/open 不可用。")
        return

    try:
        result = run_shell(f"open {shlex.quote(target)}", timeout=30)
        if result.returncode == 0:
            await update.message.reply_text(f"Opened: {target}")
        else:
            await update.message.reply_text(trim_text(format_proc_output(result, "open failed")))
    except Exception as e:
        await update.message.reply_text(f"Open failed: {e}")


async def say_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Usage: /say <text>")
        return

    if not is_macos():
        await update.message.reply_text("当前机器不是 macOS，/say 不可用。")
        return

    try:
        result = run_shell(f"say {shlex.quote(text)}", timeout=60)
        if result.returncode == 0:
            await update.message.reply_text("Said it.")
        else:
            await update.message.reply_text(trim_text(format_proc_output(result, "say failed")))
    except Exception as e:
        await update.message.reply_text(f"Say failed: {e}")


async def notify_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Usage: /notify <text>")
        return

    if not is_macos():
        await update.message.reply_text("当前机器不是 macOS，/notify 不可用。")
        return

    escaped = text.replace('"', '\\"')
    script = f'display notification "{escaped}" with title "wenbot"'
    try:
        result = run_shell(f"osascript -e {shlex.quote(script)}", timeout=30)
        if result.returncode == 0:
            await update.message.reply_text("Notification sent.")
        else:
            await update.message.reply_text(trim_text(format_proc_output(result, "notify failed")))
    except Exception as e:
        await update.message.reply_text(f"Notify failed: {e}")


async def agent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    prompt = " ".join(context.args).strip()
    if not prompt:
        await update.message.reply_text("Usage: /agent <自然语言>")
        return

    await update.message.reply_text("Thinking...")
    try:
        answer = ask_ollama(prompt, update.effective_chat.id)
        await send_long_text(update, trim_text(answer))
    except Exception as e:
        await update.message.reply_text(f"Agent failed: {e}")


async def think_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    prompt = " ".join(context.args).strip()
    if not prompt:
        await update.message.reply_text("Usage: /think <问题>")
        return

    await update.message.reply_text("Thinking deeper...")
    try:
        answer = ask_ollama(
            prompt,
            update.effective_chat.id,
            system_hint="请按步骤分析，先判断问题，再给出结论和建议，尽量清晰、具体、可执行。",
        )
        await send_long_text(update, trim_text(answer))
    except Exception as e:
        await update.message.reply_text(f"Think failed: {e}")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    parts = []
    parts.append(f"BOT_DIR={BOT_DIR}")
    parts.append(f"BOT_PATH={BOT_PATH}")
    parts.append(f"MODEL={MODEL}")
    parts.append(f"OLLAMA_URL={OLLAMA_URL}")
    parts.append(f"TG_PROXY={TG_PROXY or '(empty)'}")
    parts.append(f"Platform={platform.platform()}")

    try:
        branch = run_shell("git rev-parse --abbrev-ref HEAD", cwd=BOT_DIR, timeout=20)
        commit = run_shell("git rev-parse --short HEAD", cwd=BOT_DIR, timeout=20)
        status = run_shell("git status --short", cwd=BOT_DIR, timeout=20)
        remote = run_shell("git remote get-url origin", cwd=BOT_DIR, timeout=20)

        parts.append(f"git_branch={branch.stdout.strip() or '(unknown)'}")
        parts.append(f"git_commit={commit.stdout.strip() or '(unknown)'}")
        parts.append(f"git_remote={remote.stdout.strip() or '(unknown)'}")
        parts.append("git_status=" + (status.stdout.strip() or "clean"))
    except Exception as e:
        parts.append(f"git_error={e}")

    try:
        proc = run_shell("pgrep -fl 'python.*bot|python3.*bot'", timeout=20)
        parts.append("bot_processes=" + (proc.stdout.strip() or "(none)"))
    except Exception as e:
        parts.append(f"process_error={e}")

    try:
        ollama = requests.get("http://localhost:11434/api/tags", timeout=15)
        if ollama.ok:
            data = ollama.json()
            models = [m.get("name", "") for m in data.get("models", [])]
            parts.append("ollama_models=" + (", ".join(models) if models else "(none)"))
        else:
            parts.append(f"ollama_http={ollama.status_code}")
    except Exception as e:
        parts.append(f"ollama_error={e}")

    await send_long_text(update, trim_text("\n".join(parts)))


async def git_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    try:
        outputs = []
        outputs.append("$ git rev-parse --abbrev-ref HEAD")
        outputs.append(format_proc_output(run_shell("git rev-parse --abbrev-ref HEAD", cwd=BOT_DIR, timeout=20)))
        outputs.append("")
        outputs.append("$ git rev-parse --short HEAD")
        outputs.append(format_proc_output(run_shell("git rev-parse --short HEAD", cwd=BOT_DIR, timeout=20)))
        outputs.append("")
        outputs.append("$ git log -1 --oneline")
        outputs.append(format_proc_output(run_shell("git log -1 --oneline", cwd=BOT_DIR, timeout=20)))
        outputs.append("")
        outputs.append("$ git status --short")
        outputs.append(format_proc_output(run_shell("git status --short", cwd=BOT_DIR, timeout=20), "clean"))
        await send_long_text(update, trim_text("\n".join(outputs)))
    except Exception as e:
        await update.message.reply_text(f"git failed: {e}")


async def log_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    n = 100
    if context.args:
        try:
            n = max(1, min(1000, int(context.args[0])))
        except ValueError:
            await update.message.reply_text("Usage: /log [行数]，例如 /log 200")
            return

    text = f"--- last {n} lines ---\n" + read_last_lines(Path(LOG_FILE), n)
    await send_long_text(update, trim_text(text))


async def sys_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    cmds = [
        ("系统信息", "uname -a"),
        ("运行时长", "uptime"),
        ("磁盘", "df -h"),
    ]
    if is_macos():
        cmds.append(("内存", "vm_stat"))
        cmds.append(("CPU概览", "top -l 1 | head -n 15"))
    else:
        cmds.append(("内存", "free -h"))
        cmds.append(("CPU概览", "top -bn1 | head -n 15"))

    parts = []
    for title, command in cmds:
        parts.append(f"[{title}]")
        result = run_shell(command, timeout=30)
        parts.append(format_proc_output(result))
        parts.append("")

    await send_long_text(update, trim_text("\n".join(parts)))




async def top_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    if is_macos():
        command = "ps -Ao pid,pcpu,pmem,comm -r | head -n 12"
    else:
        command = "ps -eo pid,pcpu,pmem,comm --sort=-pcpu | head -n 12"
    result = run_shell(command, timeout=30)
    await send_long_text(update, trim_text("[Top processes]\n" + format_proc_output(result)))


async def ports_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    command = "lsof -nP -iTCP -sTCP:LISTEN | head -n 40"
    result = run_shell(command, timeout=30)
    await send_long_text(update, trim_text("[Listening ports]\n" + format_proc_output(result)))


async def clip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    if not is_macos():
        await update.message.reply_text("当前机器不是 macOS，/clip 不可用。")
        return

    if context.args and context.args[0].lower() == "set":
        text = " ".join(context.args[1:]).strip()
        if not text:
            await update.message.reply_text("Usage: /clip set <文本>")
            return
        msg = set_clipboard_text(text)
        await update.message.reply_text(msg)
        return

    text = get_clipboard_text()
    await send_long_text(update, trim_text("[Clipboard]\n" + text))


async def music_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    if not is_macos():
        await update.message.reply_text("当前机器不是 macOS，/music 不可用。")
        return

    if not context.args:
        await update.message.reply_text("Usage: /music <play|pause|next|prev>")
        return

    action = context.args[0].strip().lower()
    script_map = {
        "play": 'tell application "Music" to play',
        "pause": 'tell application "Music" to pause',
        "next": 'tell application "Music" to next track',
        "prev": 'tell application "Music" to previous track',
    }
    script = script_map.get(action)
    if not script:
        await update.message.reply_text("Usage: /music <play|pause|next|prev>")
        return

    result = run_shell(f"osascript -e {shlex.quote(script)}", timeout=20)
    if result.returncode == 0:
        await update.message.reply_text(f"music {action}: ok")
    else:
        await update.message.reply_text(trim_text(format_proc_output(result, "music command failed")))


async def volume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    if not is_macos():
        await update.message.reply_text("当前机器不是 macOS，/volume 不可用。")
        return

    if not context.args:
        await update.message.reply_text(f"当前音量: {get_volume_value()}")
        return

    arg = context.args[0].strip().lower()
    if arg == "mute":
        script = 'set volume with output muted'
    elif arg == "max":
        script = 'set volume without output muted\nset volume output volume 100'
    else:
        try:
            value = max(0, min(100, int(arg)))
        except ValueError:
            await update.message.reply_text("Usage: /volume <0-100|mute|max>")
            return
        script = f'set volume without output muted\nset volume output volume {value}'

    result = run_shell(f"osascript -e {shlex.quote(script)}", timeout=20)
    if result.returncode == 0:
        await update.message.reply_text(f"音量已设置，当前约为: {get_volume_value()}")
    else:
        await update.message.reply_text(trim_text(format_proc_output(result, "set volume failed")))
async def screenshot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    if not is_macos():
        await update.message.reply_text("当前机器不是 macOS，/screenshot 不可用。")
        return

    tool = shutil.which("screencapture")
    if not tool:
        await update.message.reply_text("未找到 screencapture。")
        return

    mode = parse_screenshot_mode(context.args)
    display_count = get_display_count()

    if mode == "2" and display_count < 2:
        await update.message.reply_text("当前没有检测到第二块屏幕。")
        return

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        image_path = Path(tmp.name)

    try:
        # all: 全部屏幕拼成一张
        # main/1: 主屏
        # 2: 先把屏幕放到第二块，再截选中窗口（作为兼容方案）
        if mode == "all":
            cmd = f"{shlex.quote(tool)} -x -C -t jpg {shlex.quote(str(image_path))}"
        elif mode == "2":
            # macOS CLI 没有稳定的“按索引截某一块屏幕”参数，这里采用交互选择单屏模式的兼容方式：
            # 先提示用户如需更精准，可把窗口拖到第二屏再使用 /screenshot 2
            cmd = f"{shlex.quote(tool)} -x -C -t jpg {shlex.quote(str(image_path))}"
        else:
            cmd = f"{shlex.quote(tool)} -x -C -t jpg {shlex.quote(str(image_path))}"

        result = run_shell(cmd, timeout=30)
        if result.returncode != 0 or not image_path.exists():
            await update.message.reply_text(trim_text(format_proc_output(result, "截图失败")))
            return

        file_size = image_path.stat().st_size
        caption_map = {
            "all": f"全部屏幕截图（检测到 {display_count} 块屏幕）",
            "1": "第一个屏幕截图",
            "2": "截图结果（双屏环境下建议把目标窗口拖到第二屏后再试）",
            "main": "主屏截图",
        }
        caption = caption_map.get(mode, "当前屏幕截图")

        if file_size <= 9 * 1024 * 1024:
            with image_path.open("rb") as f:
                await update.message.reply_photo(photo=f, caption=caption)
        else:
            with image_path.open("rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename="screenshot.jpg",
                    caption=f"{caption}，文件较大，已按文件发送（{file_size / 1024 / 1024:.2f} MB）",
                )
    except Exception as e:
        await update.message.reply_text(f"screenshot failed: {e}")
    finally:
        try:
            image_path.unlink(missing_ok=True)
        except Exception:
            pass


async def camera_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    if not is_macos():
        await update.message.reply_text("当前机器不是 macOS，/camera 不可用。")
        return

    tool = shutil.which("imagesnap")
    if not tool:
        await update.message.reply_text("未安装 imagesnap。先执行: brew install imagesnap")
        return

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        image_path = Path(tmp.name)

    try:
        result = run_shell(f"{shlex.quote(tool)} {shlex.quote(str(image_path))}", timeout=40)
        if result.returncode != 0 or not image_path.exists():
            await update.message.reply_text(trim_text(format_proc_output(result, "拍照失败")))
            return

        with image_path.open("rb") as f:
            await update.message.reply_photo(photo=f, caption="当前摄像头拍照")
    except Exception as e:
        await update.message.reply_text(f"camera failed: {e}")
    finally:
        try:
            image_path.unlink(missing_ok=True)
        except Exception:
            pass


async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /price <symbol>，例如 /price btc")
        return

    symbol = context.args[0].strip().lower()
    symbol_map = {
        "btc": "bitcoin",
        "eth": "ethereum",
        "sol": "solana",
        "bnb": "binancecoin",
        "doge": "dogecoin",
        "xrp": "ripple",
        "ada": "cardano",
        "trx": "tron",
    }
    coin_id = symbol_map.get(symbol, symbol)

    try:
        url = "https://api.coingecko.com/api/v3/simple/price"
        r = requests.get(
            url,
            params={"ids": coin_id, "vs_currencies": "usd,cny", "include_24hr_change": "true"},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        if coin_id not in data:
            await update.message.reply_text(f"未找到币种: {symbol}")
            return

        info = data[coin_id]
        usd = info.get("usd")
        cny = info.get("cny")
        change = info.get("usd_24h_change")
        text = (
            f"币种: {coin_id}\n"
            f"USD: {usd}\n"
            f"CNY: {cny}\n"
            f"24h: {round(change, 2) if change is not None else 'N/A'}%"
        )
        await update.message.reply_text(text)
    except Exception as e:
        await update.message.reply_text(f"price failed: {e}")


async def fortune_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return
    await update.message.reply_text(random.choice(FORTUNES))


async def deploy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    script_path = Path(DEPLOY_SCRIPT)
    if not script_path.exists():
        await update.message.reply_text(f"部署脚本不存在: {script_path}")
        return

    await update.message.reply_text("开始执行部署脚本...")
    try:
        result = run_shell(f"bash {shlex.quote(str(script_path))}", cwd=BOT_DIR, timeout=600)
        await send_long_text(update, trim_text(format_proc_output(result, "deploy finished")))
    except Exception as e:
        await update.message.reply_text(f"deploy failed: {e}")


async def fix_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    problem = " ".join(context.args).strip()
    if not problem:
        await update.message.reply_text("Usage: /fix <问题描述>")
        return

    await update.message.reply_text("正在生成修复建议（不会自动执行）...")
    try:
        branch = get_git_branch()
        commit = get_git_commit()
        prompt = (
            f"你是一个运维助手。当前项目目录是 {BOT_DIR}，分支 {branch}，提交 {commit}。\n"
            f"问题：{problem}\n\n"
            "请输出：\n"
            "1. 可能原因\n"
            "2. 建议先检查什么\n"
            "3. 可执行的 shell 命令（仅建议，不执行）\n"
            "4. 风险提示\n"
        )
        answer = ask_ollama(prompt, update.effective_chat.id)
        await send_long_text(update, trim_text(answer))
    except Exception as e:
        await update.message.reply_text(f"fix failed: {e}")


async def restart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    script_path = Path(RESTART_SCRIPT)
    if not script_path.exists():
        await update.message.reply_text(f"重启脚本不存在: {script_path}")
        return

    await update.message.reply_text("Restarting...")
    try:
        result = run_shell(f"bash {shlex.quote(str(script_path))}", cwd=BOT_DIR, timeout=60)
        output = format_proc_output(result, "restart command sent")
        await send_long_text(update, trim_text(output))
    except Exception as e:
        await update.message.reply_text(f"Restart failed: {e}")


async def update_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    await update.message.reply_text("Updating from GitHub...")

    try:
        steps = []

        pull_cmd = f"git pull origin {shlex.quote(DEFAULT_BRANCH)}"
        pull = run_shell(pull_cmd, cwd=BOT_DIR, timeout=180)
        steps.append(f"$ {pull_cmd}")
        steps.append(format_proc_output(pull))

        compile_cmd = f"python3 -m py_compile {shlex.quote(BOT_FILE)}"
        comp = run_shell(compile_cmd, cwd=BOT_DIR, timeout=60)
        steps.append("")
        steps.append(f"$ {compile_cmd}")
        steps.append(format_proc_output(comp, "py_compile ok"))

        if pull.returncode != 0:
            await send_long_text(update, trim_text("\n".join(steps)))
            return

        if comp.returncode != 0:
            await send_long_text(update, trim_text("\n".join(steps)))
            return

        script_path = Path(RESTART_SCRIPT)
        if script_path.exists():
            restart = run_shell(f"bash {shlex.quote(str(script_path))}", cwd=BOT_DIR, timeout=60)
            steps.append("")
            steps.append(f"$ bash {script_path}")
            steps.append(format_proc_output(restart, "restart sent"))
        else:
            steps.append("")
            steps.append(f"restart script not found: {script_path}")

        await send_long_text(update, trim_text("\n".join(steps)))
    except Exception as e:
        await update.message.reply_text(f"Update failed: {e}")


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    try:
        answer = ask_ollama(text, update.effective_chat.id)
        await send_long_text(update, trim_text(answer))
    except Exception as e:
        await update.message.reply_text(f"Chat failed: {e}")


def main() -> None:
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is empty. Please set it in .env")
    if not ALLOWED_USER_IDS:
        raise RuntimeError("ALLOWED_USER_IDS is empty. Please set it in .env")

    request = HTTPXRequest(proxy=TG_PROXY) if TG_PROXY else HTTPXRequest()
    app = ApplicationBuilder().token(TOKEN).request(request).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", get_id))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("cmd", cmd))
    app.add_handler(CommandHandler("open", open_cmd))
    app.add_handler(CommandHandler("say", say_cmd))
    app.add_handler(CommandHandler("notify", notify_cmd))
    app.add_handler(CommandHandler("agent", agent_cmd))
    app.add_handler(CommandHandler("think", think_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("git", git_cmd))
    app.add_handler(CommandHandler("log", log_cmd))
    app.add_handler(CommandHandler("sys", sys_cmd))
    app.add_handler(CommandHandler("top", top_cmd))
    app.add_handler(CommandHandler("ports", ports_cmd))
    app.add_handler(CommandHandler("clip", clip_cmd))
    app.add_handler(CommandHandler("music", music_cmd))
    app.add_handler(CommandHandler("volume", volume_cmd))
    app.add_handler(CommandHandler("screenshot", screenshot_cmd))
    app.add_handler(CommandHandler("camera", camera_cmd))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("fortune", fortune_cmd))
    app.add_handler(CommandHandler("deploy", deploy_cmd))
    app.add_handler(CommandHandler("fix", fix_cmd))
    app.add_handler(CommandHandler("restart", restart_cmd))
    app.add_handler(CommandHandler("update", update_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

    print("Bot started...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
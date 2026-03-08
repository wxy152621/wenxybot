import json
import os
import shlex
import subprocess
from typing import Dict, List

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

TOKEN = os.getenv("BOT_TOKEN", "")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
MODEL = os.getenv("MODEL", "qwen2.5:1.5b")
TG_PROXY = os.getenv("TG_PROXY", "socks5://127.0.0.1:7890")

BOT_DIR = os.getenv("BOT_DIR", os.getcwd())
BOT_MAIN = os.getenv("BOT_MAIN", "bot.py")
BOT_LOG = os.getenv("BOT_LOG", "bot.log")
GIT_BRANCH = os.getenv("GIT_BRANCH", "main")

raw_ids = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS = {int(x.strip()) for x in raw_ids.split(",") if x.strip()}

MAX_HISTORY_TURNS = 8
CHAT_MEMORY: Dict[int, List[Dict[str, str]]] = {}


def is_allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


def split_text(text: str, max_len: int = 3500) -> List[str]:
    if not text:
        return ["(empty)"]
    return [text[i:i + max_len] for i in range(0, len(text), max_len)]


async def reply_long(update: Update, text: str) -> None:
    for chunk in split_text(text):
        await update.message.reply_text(chunk)


def get_user_history(user_id: int) -> List[Dict[str, str]]:
    return CHAT_MEMORY.setdefault(user_id, [])


def reset_user_history(user_id: int) -> None:
    CHAT_MEMORY[user_id] = []


def build_prompt(user_id: int, user_text: str) -> str:
    history = get_user_history(user_id)
    system_prompt = (
        "You are a helpful assistant running locally on a Mac. "
        "Answer clearly and briefly unless the user asks for detail."
    )

    lines = [f"System: {system_prompt}"]
    for item in history[-MAX_HISTORY_TURNS * 2:]:
        role = item["role"].capitalize()
        lines.append(f"{role}: {item['content']}")
    lines.append(f"User: {user_text}")
    lines.append("Assistant:")
    return "\n".join(lines)


def ask_ollama(prompt: str) -> str:
    r = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "prompt": prompt,
            "stream": False,
        },
        timeout=180,
    )
    r.raise_for_status()
    data = r.json()
    return data.get("response", "").strip() or "模型没有返回内容。"


def append_history(user_id: int, user_text: str, assistant_text: str) -> None:
    history = get_user_history(user_id)
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": assistant_text})
    if len(history) > MAX_HISTORY_TURNS * 2:
        CHAT_MEMORY[user_id] = history[-MAX_HISTORY_TURNS * 2:]


def run_shell_command(command: str, timeout: int = 20, cwd: str | None = None) -> str:
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        return output[:7000] if output else "Done."
    except subprocess.TimeoutExpired:
        return "Error: command timed out."
    except Exception as e:
        return f"Error: {e}"


def run_shell_command_result(command: str, timeout: int = 20, cwd: str | None = None):
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        return result
    except Exception as e:
        class Dummy:
            returncode = 1
            stdout = ""
            stderr = str(e)
        return Dummy()


def run_command_safe(command: str) -> str:
    blocked_keywords = [
        "rm ",
        "sudo ",
        "shutdown",
        "reboot",
        "mkfs",
        "dd ",
        "chmod 777",
        "chown ",
        "killall",
        "pkill",
        "launchctl",
        "passwd",
        "su ",
        "curl ",
        "wget ",
        "scp ",
        "ssh ",
        "python -c",
        "python3 -c",
        "osascript -e 'do shell script",
    ]
    lowered = command.lower()
    for bad in blocked_keywords:
        if bad in lowered:
            return "Blocked: risky command."

    allowed_prefixes = [
        "pwd",
        "ls",
        "whoami",
        "date",
        "uname",
        "open ",
        "say ",
        "osascript ",
        "python ",
        "python3 ",
        "cat ",
        "head ",
        "tail ",
        "grep ",
        "find ",
    ]

    if not any(command == p.strip() or command.startswith(p) for p in allowed_prefixes):
        return "Command not allowed."

    return run_shell_command(command)


def notify_mac(title: str, message: str) -> str:
    safe_title = title.replace('"', '\\"')
    safe_msg = message.replace('"', '\\"')
    cmd = f'osascript -e \'display notification "{safe_msg}" with title "{safe_title}"\''
    return run_shell_command(cmd)


def open_target(target: str) -> str:
    target = target.strip()
    if not target:
        return "Usage: /open Safari  或  /open https://example.com"

    if target.startswith("http://") or target.startswith("https://"):
        return run_shell_command(f"open {shlex.quote(target)}")

    return run_shell_command(f"open -a {shlex.quote(target)}")


def open_url_in_safari(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        return "Blocked: invalid URL."
    return run_shell_command(f"open -a Safari {shlex.quote(url)}")


def speak_text(text: str) -> str:
    text = text.strip()
    if not text:
        return "Usage: /say 你好"
    return run_shell_command(f"say {shlex.quote(text)}")


def normalize_common_url(text: str) -> str:
    t = text.strip().lower()

    mapping = {
        "百度": "https://www.baidu.com",
        "baidu": "https://www.baidu.com",
        "baidu.com": "https://www.baidu.com",
        "谷歌": "https://www.google.com",
        "google": "https://www.google.com",
        "google.com": "https://www.google.com",
        "github": "https://github.com",
        "github.com": "https://github.com",
        "bilibili": "https://www.bilibili.com",
        "bilibili.com": "https://www.bilibili.com",
        "知乎": "https://www.zhihu.com",
        "zhihu": "https://www.zhihu.com",
        "zhihu.com": "https://www.zhihu.com",
    }

    if text in mapping:
        return mapping[text]
    if t in mapping:
        return mapping[t]
    if t.startswith("http://") or t.startswith("https://"):
        return text.strip()
    if "." in t and " " not in t:
        return "https://" + t
    return text.strip()


def parse_agent_actions(agent_output: str):
    try:
        data = json.loads(agent_output)
        if isinstance(data, list):
            good = []
            for item in data:
                if isinstance(item, dict):
                    act = str(item.get("action", "")).strip()
                    val = str(item.get("value", "")).strip()
                    good.append({"action": act, "value": val})
            if good:
                return good
    except Exception:
        pass
    return [{"action": "notify", "value": "unsupported"}]


def ask_agent_plan(user_text: str):
    prompt = f"""
You are a command planner for a Mac assistant.
Convert the user's request into a JSON array of safe actions.

Allowed actions:
- open_app
- open_url
- open_url_in_safari
- say
- notify
- list_dir
- pwd

Rules:
- Output JSON only.
- Output must be an array.
- No markdown.
- No explanation.
- Use multiple actions when needed.
- Prefer open_url_in_safari when the user explicitly asks to open Safari and visit a site.
- For open_app, value should be app name like "Safari".
- For open_url and open_url_in_safari, value should be a full URL.
- Convert common sites:
  百度 -> https://www.baidu.com
  谷歌 -> https://www.google.com
  github -> https://github.com
  bilibili -> https://www.bilibili.com
  知乎 -> https://www.zhihu.com
- If request is unsafe or unsupported, return:
  [{{"action":"notify","value":"unsupported"}}]

User request: {user_text}
"""
    result = ask_ollama(prompt)
    actions = parse_agent_actions(result)
    for item in actions:
        if item["action"] in {"open_url", "open_url_in_safari"}:
            item["value"] = normalize_common_url(item["value"])
    return actions


def execute_agent_action(action):
    act = action.get("action", "none")
    value = action.get("value", "")

    if act == "open_app":
        return open_target(value)
    if act == "open_url":
        url = normalize_common_url(value)
        if not url.startswith(("http://", "https://")):
            return "Blocked: invalid URL."
        return open_target(url)
    if act == "open_url_in_safari":
        return open_url_in_safari(normalize_common_url(value))
    if act == "say":
        return speak_text(value)
    if act == "notify":
        return notify_mac("wenbot", value)
    if act == "list_dir":
        path = value.strip() or "~"
        return run_command_safe(f"ls {shlex.quote(path)}")
    if act == "pwd":
        return run_command_safe("pwd")
    return f"Unsupported action: {act}"


def get_git_info() -> str:
    branch = run_shell_command("git rev-parse --abbrev-ref HEAD", cwd=BOT_DIR)
    commit = run_shell_command("git rev-parse --short HEAD", cwd=BOT_DIR)
    status = run_shell_command("git status --short", cwd=BOT_DIR)
    if status == "Done.":
        status = "(clean)"
    return f"Branch: {branch}\nCommit: {commit}\nStatus:\n{status}"


def get_process_info() -> str:
    cmd = f"pgrep -af 'python.*{shlex.quote(BOT_MAIN)}' || true"
    return run_shell_command(cmd)


def get_ollama_info() -> str:
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=10)
        r.raise_for_status()
        data = r.json()
        names = [m.get("name", "") for m in data.get("models", [])][:20]
        return "Ollama OK\nModels:\n" + ("\n".join(names) if names else "(none)")
    except Exception as e:
        return f"Ollama error: {e}"


def run_update_flow() -> str:
    pull = run_shell_command_result(f"git pull origin {shlex.quote(GIT_BRANCH)}", timeout=60, cwd=BOT_DIR)
    pull_output = ((pull.stdout or "") + (pull.stderr or "")).strip()

    if pull.returncode != 0:
        return f"[git pull failed]\n{pull_output}"

    compile_cmd = f"python3 -m py_compile {shlex.quote(os.path.join(BOT_DIR, BOT_MAIN))}"
    comp = run_shell_command_result(compile_cmd, timeout=30, cwd=BOT_DIR)
    comp_output = ((comp.stdout or "") + (comp.stderr or "")).strip()

    if comp.returncode != 0:
        return f"[syntax check failed]\n{comp_output}\n\n[pull output]\n{pull_output}"

    restart_script = os.path.join(BOT_DIR, "restart_bot.sh")
    res = run_shell_command_result(f"bash {shlex.quote(restart_script)}", timeout=20, cwd=BOT_DIR)
    restart_output = ((res.stdout or "") + (res.stderr or "")).strip()

    if res.returncode != 0:
        return f"[restart failed]\n{restart_output}\n\n[pull output]\n{pull_output}"

    return f"[update ok]\n{pull_output}\n\n[restart]\n{restart_output or 'Done.'}"


# ===== Telegram handlers =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    help_text = (
        "Ready.\n\n"
        "/id - 查看你的 user id\n"
        "/reset - 清空上下文\n"
        "/cmd <command> - 执行白名单命令\n"
        "/open <App或URL>\n"
        "/say <text>\n"
        "/notify <text>\n"
        "/agent <自然语言>\n"
        "/status - 查看状态\n"
        "/restart - 重启 bot\n"
        "/update - git pull + 语法检查 + 重启"
    )
    await update.message.reply_text(help_text)


async def get_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user:
        await update.message.reply_text(f"Your Telegram user id is: {user.id}")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return
    reset_user_history(update.effective_user.id)
    await update.message.reply_text("Context cleared.")


async def cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return
    text = update.message.text.removeprefix("/cmd").strip()
    if not text:
        await update.message.reply_text("Usage: /cmd pwd")
        return
    await reply_long(update, run_command_safe(text))


async def open_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return
    text = update.message.text.removeprefix("/open").strip()
    await reply_long(update, open_target(text))


async def say_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return
    text = update.message.text.removeprefix("/say").strip()
    await reply_long(update, speak_text(text))


async def notify_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return
    text = update.message.text.removeprefix("/notify").strip()
    if not text:
        await update.message.reply_text("Usage: /notify 任务完成")
        return
    await reply_long(update, notify_mac("wenbot", text))


async def agent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return
    text = update.message.text.removeprefix("/agent").strip()
    if not text:
        await update.message.reply_text("Usage: /agent 打开 Safari 并访问 github.com")
        return

    try:
        actions = ask_agent_plan(text)
        results = []
        for i, action in enumerate(actions, 1):
            result = execute_agent_action(action)
            results.append(
                f"Step {i}\nAction: {json.dumps(action, ensure_ascii=False)}\nResult: {result}"
            )
        await reply_long(update, "\n\n".join(results))
    except Exception as e:
        await update.message.reply_text(f"Agent error: {e}")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    text = (
        f"BOT_DIR: {BOT_DIR}\n"
        f"BOT_MAIN: {BOT_MAIN}\n"
        f"MODEL: {MODEL}\n"
        f"TG_PROXY: {TG_PROXY}\n\n"
        f"[git]\n{get_git_info()}\n\n"
        f"[process]\n{get_process_info()}\n\n"
        f"[ollama]\n{get_ollama_info()}"
    )
    await reply_long(update, text)


async def restart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    await update.message.reply_text("Restarting...")
    restart_script = os.path.join(BOT_DIR, "restart_bot.sh")
    result = run_shell_command(f"bash {shlex.quote(restart_script)}", timeout=20, cwd=BOT_DIR)
    # 这里可能来不及回最后一条，因为进程会被重启
    try:
        await reply_long(update, result)
    except Exception:
        pass


async def update_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    await update.message.reply_text("Updating...")
    result = run_update_flow()
    try:
        await reply_long(update, result)
    except Exception:
        pass


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    text = update.message.text.strip()
    if text.startswith("/"):
        await update.message.reply_text("Unknown command or wrong format. Try /start")
        return

    user_id = update.effective_user.id
    try:
        prompt = build_prompt(user_id, text)
        answer = ask_ollama(prompt)
        append_history(user_id, text, answer)
        await reply_long(update, answer)
    except Exception as e:
        await update.message.reply_text(f"Chat error: {e}")


def main() -> None:
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is empty. Please set it in .env")
    if not ALLOWED_USER_IDS:
        raise RuntimeError("ALLOWED_USER_IDS is empty. Please set it in .env")

    request = HTTPXRequest(proxy=TG_PROXY)
    app = ApplicationBuilder().token(TOKEN).request(request).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", get_id))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("cmd", cmd))
    app.add_handler(CommandHandler("open", open_cmd))
    app.add_handler(CommandHandler("say", say_cmd))
    app.add_handler(CommandHandler("notify", notify_cmd))
    app.add_handler(CommandHandler("agent", agent_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("restart", restart_cmd))
    app.add_handler(CommandHandler("update", update_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

    print("Bot started...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()


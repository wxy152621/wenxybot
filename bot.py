import os
import shlex
import subprocess
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
}

CHAT_HISTORY: dict[int, list[dict[str, str]]] = {}


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
        timeout=timeout,
    )


def trim_text(text: str, limit: int = 3500) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n...[truncated]"


def get_history(chat_id: int) -> list[dict[str, str]]:
    if chat_id not in CHAT_HISTORY:
        CHAT_HISTORY[chat_id] = []
    return CHAT_HISTORY[chat_id]


def ask_ollama(prompt: str, chat_id: int) -> str:
    history = get_history(chat_id)
    history.append({"role": "user", "content": prompt})
    history = history[-10:]
    CHAT_HISTORY[chat_id] = history

    combined_prompt = ""
    for item in history:
        role = "User" if item["role"] == "user" else "Assistant"
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
    CHAT_HISTORY[chat_id] = history[-10:]
    return answer


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    help_text = (
        "Ready.\n\n"
        "/id - 查看你的 user id\n"
        "/ping - 测试当前版本\n"
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
    if not user:
        await update.message.reply_text("No user.")
        return
    await update.message.reply_text(f"user_id={user.id}")


async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return
    await update.message.reply_text("pong from GitHub v1")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return
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
        output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
        output = output.strip() or "(no output)"
        await update.message.reply_text(trim_text(output))
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

    try:
        result = run_shell(f"open {shlex.quote(target)}", timeout=30)
        if result.returncode == 0:
            await update.message.reply_text(f"Opened: {target}")
        else:
            msg = (result.stderr or result.stdout or "open failed").strip()
            await update.message.reply_text(trim_text(msg))
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

    try:
        result = run_shell(f"say {shlex.quote(text)}", timeout=60)
        if result.returncode == 0:
            await update.message.reply_text("Said it.")
        else:
            msg = (result.stderr or result.stdout or "say failed").strip()
            await update.message.reply_text(trim_text(msg))
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

    escaped = text.replace('"', '\\"')
    script = f'display notification "{escaped}" with title "wenbot"'
    try:
        result = run_shell(f"osascript -e {shlex.quote(script)}", timeout=30)
        if result.returncode == 0:
            await update.message.reply_text("Notification sent.")
        else:
            msg = (result.stderr or result.stdout or "notify failed").strip()
            await update.message.reply_text(trim_text(msg))
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
        await update.message.reply_text(trim_text(answer))
    except Exception as e:
        await update.message.reply_text(f"Agent failed: {e}")


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

    try:
        branch = run_shell("git rev-parse --abbrev-ref HEAD", cwd=BOT_DIR, timeout=20)
        commit = run_shell("git rev-parse --short HEAD", cwd=BOT_DIR, timeout=20)
        status = run_shell("git status --short", cwd=BOT_DIR, timeout=20)

        parts.append(f"git_branch={branch.stdout.strip() or '(unknown)'}")
        parts.append(f"git_commit={commit.stdout.strip() or '(unknown)'}")
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

    await update.message.reply_text(trim_text("\n".join(parts)))


async def restart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    await update.message.reply_text("Restarting...")
    try:
        result = run_shell(f"bash {shlex.quote(RESTART_SCRIPT)}", cwd=BOT_DIR, timeout=60)
        output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
        await update.message.reply_text(trim_text(output or "restart command sent"))
    except Exception as e:
        await update.message.reply_text(f"Restart failed: {e}")


async def update_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Unauthorized.")
        return

    await update.message.reply_text("Updating from GitHub...")

    try:
        steps = []
        pull = run_shell("git pull origin main", cwd=BOT_DIR, timeout=180)
        steps.append("$ git pull origin main")
        steps.append((pull.stdout or "") + (pull.stderr or ""))

        compile_cmd = f"python3 -m py_compile {shlex.quote(BOT_FILE)}"
        comp = run_shell(compile_cmd, cwd=BOT_DIR, timeout=60)
        steps.append(f"$ {compile_cmd}")
        steps.append((comp.stdout or "") + (comp.stderr or "") or "py_compile ok")

        if pull.returncode != 0:
            await update.message.reply_text(trim_text("\n".join(steps)))
            return

        if comp.returncode != 0:
            await update.message.reply_text(trim_text("\n".join(steps)))
            return

        restart = run_shell(f"bash {shlex.quote(RESTART_SCRIPT)}", cwd=BOT_DIR, timeout=60)
        steps.append(f"$ bash {RESTART_SCRIPT}")
        steps.append((restart.stdout or "") + (restart.stderr or "") or "restart sent")

        await update.message.reply_text(trim_text("\n".join(steps)))
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
        await update.message.reply_text(trim_text(answer))
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
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("restart", restart_cmd))
    app.add_handler(CommandHandler("update", update_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

    print("Bot started...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
"""
BotForge — Vercel Python Backend
=================================
ONLY job: receive Telegram webhook updates and reply to users.

Flow:
  Telegram → POST /webhook/{token}
    → calls your PHP host to get bot commands
    → executes command handler code
    → replies to Telegram user

Does NOT touch MySQL directly.
Does NOT handle users, payments, plans or any admin logic.
Those all live in your PHP + MySQL frontend.
"""

from http.server import BaseHTTPRequestHandler
import json
import urllib.request
import urllib.parse
import urllib.error
import os
import traceback

# ── Config ────────────────────────────────────────────────────────────────────
# Set these in Vercel Environment Variables dashboard:
#   PHP_HOST  = https://yoursite.aeonscope.net   (your AeonFree subdomain, no trailing slash)
PHP_HOST = os.environ.get("PHP_HOST", "").rstrip("/")

# ── Helpers ───────────────────────────────────────────────────────────────────

def telegram_send(token: str, chat_id: int, text: str) -> dict:
    """Send a message back to the Telegram user."""
    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode()
    req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_bot_commands(token: str) -> list:
    """
    Ask the PHP host for this bot's commands.
    PHP file: /api/get-bot-commands.php?token=<token>
    Returns a list of command dicts with keys:
      command_name, handler_type, handler_code
    """
    if not PHP_HOST:
        return []
    url = f"{PHP_HOST}/api/get-bot-commands.php?token={urllib.parse.quote(token)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BotForge/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
            return data.get("commands", [])
    except Exception:
        return []


def log_message(token: str, telegram_user_id: int, username: str, message: str, response: str) -> None:
    """Tell the PHP host to log this message."""
    if not PHP_HOST:
        return
    url  = f"{PHP_HOST}/api/log-message.php"
    body = json.dumps({
        "token":            token,
        "telegram_user_id": telegram_user_id,
        "username":         username,
        "message":          message,
        "response":         response,
    }).encode()
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def log_error(token: str, command_name: str, error_message: str, telegram_user_id: int) -> None:
    """Tell the PHP host to log a command error."""
    if not PHP_HOST:
        return
    url  = f"{PHP_HOST}/api/log-error.php"
    body = json.dumps({
        "token":            token,
        "command_name":     command_name,
        "error_message":    error_message,
        "telegram_user_id": telegram_user_id,
    }).encode()
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def call_ai(token: str, user_message: str, system_prompt: str = "") -> str:
    """
    Ask PHP host to run an AI completion using the admin-configured
    Gemini / OpenAI / Anthropic key.
    PHP file: /api/ai-chat.php
    """
    if not PHP_HOST:
        return "AI is not configured."
    url  = f"{PHP_HOST}/api/ai-chat.php"
    body = json.dumps({
        "token":         token,
        "message":       user_message,
        "system_prompt": system_prompt,
    }).encode()
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
            return data.get("reply", "AI error")
    except Exception as e:
        return f"AI unavailable: {str(e)}"


def execute_handler(handler_code: str, user_message: str, update: dict) -> str:
    """
    Safely execute a command's Python handler_code.
    The code can set `reply_text` to control the response.
    Available variables: user_message, update, chat_id, username
    """
    msg      = update.get("message", {})
    chat_id  = msg.get("chat", {}).get("id", 0)
    username = msg.get("from", {}).get("username", "user")
    first    = msg.get("from", {}).get("first_name", "there")

    local_vars = {
        "user_message": user_message,
        "chat_id":      chat_id,
        "username":     username,
        "first_name":   first,
        "update":       update,
        "reply_text":   "Command executed.",
    }
    try:
        exec(handler_code, {"__builtins__": {}}, local_vars)
        return str(local_vars.get("reply_text", "Done."))
    except Exception as e:
        raise RuntimeError(f"Handler error: {e}")


def handle_update(token: str, update: dict) -> None:
    """Process one Telegram update."""
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return

    chat_id      = msg.get("chat", {}).get("id")
    user_id      = msg.get("from", {}).get("id", 0)
    username     = msg.get("from", {}).get("username", "")
    text         = msg.get("text", "").strip()

    if not chat_id or not text:
        return

    # Extract command (e.g. /start or /start@BotName)
    cmd_name = None
    if text.startswith("/"):
        parts    = text.split()
        cmd_name = parts[0].split("@")[0].lower()

    # Fetch commands from PHP
    commands = get_bot_commands(token)

    # Build a map: /command_name → handler info
    cmd_map = {}
    is_ai_bot = False
    ai_system_prompt = ""
    for c in commands:
        if isinstance(c, dict):
            cname = c.get("command_name", "").lower()
            cmd_map[cname] = c
            if c.get("is_ai_assistant"):
                is_ai_bot = True
                ai_system_prompt = c.get("ai_system_prompt", "")

    reply = None

    if cmd_name and cmd_name in cmd_map:
        cmd = cmd_map[cmd_name]
        try:
            reply = execute_handler(cmd.get("handler_code", "reply_text = 'Hi!'"), text, update)
        except RuntimeError as e:
            reply = "Sorry, something went wrong with that command."
            log_error(token, cmd_name, str(e), user_id)

    elif is_ai_bot and text and not text.startswith("/"):
        # AI assistant mode — pass message to AI via PHP
        reply = call_ai(token, text, ai_system_prompt)

    elif text == "/start":
        reply = "👋 Hello! I'm up and running. Use /help to see available commands."

    elif text == "/help":
        if cmd_map:
            lines = ["*Available Commands:*\n"]
            for name in cmd_map:
                lines.append(f"`{name}`")
            reply = "\n".join(lines)
        else:
            reply = "No commands configured for this bot yet."

    if reply:
        res = telegram_send(token, chat_id, reply)
        log_message(token, user_id, username, text, reply if (res.get("ok")) else "[send failed]")


# ── Vercel Serverless Handler ─────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # Suppress default access logs on Vercel

    def send_json(self, code: int, body: dict):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")

        if path in ("/health", "/api/health"):
            self.send_json(200, {
                "status":   "ok",
                "platform": "BotForge",
                "php_host": PHP_HOST or "NOT SET",
            })
            return

        if path == "/":
            self.send_json(200, {
                "message":  "BotForge Webhook Backend",
                "webhook":  "/webhook/{your_bot_token}",
                "health":   "/health",
            })
            return

        self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")

        # ── Telegram Webhook ──────────────────────────────────────────────────
        if path.startswith("/webhook/"):
            token = path[len("/webhook/"):]
            if not token:
                self.send_json(400, {"error": "Missing token"})
                return

            try:
                length = int(self.headers.get("Content-Length", 0))
                body   = self.rfile.read(length)
                update = json.loads(body)
            except Exception as e:
                self.send_json(400, {"error": f"Bad JSON: {e}"})
                return

            try:
                handle_update(token, update)
            except Exception:
                # Never let exceptions break the 200 response to Telegram
                pass

            # Telegram requires a 200 response, always
            self.send_json(200, {"ok": True})
            return

        self.send_json(404, {"error": "Not found"})

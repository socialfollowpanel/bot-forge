"""
BotForge — Vercel Python Backend
=================================
Jobs:
  1. GET  /validate-token?token=xxx  — validate bot token (CORS-safe fallback for browser)
  2. POST /webhook/{token}           — receive Telegram updates, execute commands, reply
  3. GET  /health                    — health check

Token validation flow:
  dashboard.php JS → POST /api/validate-token.php  (PHP same server, no CORS)
    If PHP can't reach Telegram → JS → GET https://your-vercel.app/validate-token?token=xxx
      → Vercel calls Telegram → returns result to JS

Webhook flow:
  Telegram → POST /webhook/{token}
    → GET PHP /api/get-bot-commands.php → execute code → reply to user
"""

from http.server import BaseHTTPRequestHandler
import json
import urllib.request
import urllib.parse
import os
import re

PHP_HOST = os.environ.get("PHP_HOST", "").rstrip("/")


# ── Telegram ──────────────────────────────────────────────────────────────────

def tg_get(token: str, method: str) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    req = urllib.request.Request(url, headers={"User-Agent": "BotForge/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def tg_send(token: str, chat_id: int, text: str) -> dict:
    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode()
    req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── PHP Bridge ────────────────────────────────────────────────────────────────

def php_get(path: str) -> dict:
    if not PHP_HOST:
        return {}
    try:
        req = urllib.request.Request(f"{PHP_HOST}{path}", headers={"User-Agent": "BotForge/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except Exception:
        return {}


def php_post(path: str, body: dict) -> dict:
    if not PHP_HOST:
        return {}
    try:
        data = json.dumps(body).encode()
        req  = urllib.request.Request(f"{PHP_HOST}{path}", data=data, headers={"Content-Type": "application/json", "User-Agent": "BotForge/1.0"}, method="POST")
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except Exception:
        return {}


# ── Bot Logic ─────────────────────────────────────────────────────────────────

def execute_handler(code: str, text: str, update: dict) -> str:
    msg        = update.get("message", {})
    chat_id    = msg.get("chat", {}).get("id", 0)
    username   = msg.get("from", {}).get("username", "")
    first_name = msg.get("from", {}).get("first_name", "there")
    safe_builtins = {
        "len": len, "str": str, "int": int, "float": float, "bool": bool,
        "list": list, "dict": dict, "range": range, "round": round,
        "min": min, "max": max, "abs": abs, "True": True, "False": False, "None": None,
    }
    local_vars = {
        "user_message": text, "chat_id": chat_id, "username": username,
        "first_name": first_name, "update": update, "reply_text": "Done.",
    }
    exec(code, {"__builtins__": safe_builtins}, local_vars)
    return str(local_vars.get("reply_text", "Done."))


def handle_update(token: str, update: dict) -> None:
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return

    chat_id    = msg.get("chat", {}).get("id")
    user_id    = msg.get("from", {}).get("id", 0)
    username   = msg.get("from", {}).get("username", "")
    first_name = msg.get("from", {}).get("first_name", "there")
    text       = msg.get("text", "").strip()

    if not chat_id or not text:
        return

    cmd_name = text.split()[0].split("@")[0].lower() if text.startswith("/") else None

    bot_data      = php_get(f"/api/get-bot-commands.php?token={urllib.parse.quote(token)}")
    commands      = bot_data.get("commands", [])
    is_ai         = bot_data.get("is_ai_assistant", False)
    ai_prompt     = bot_data.get("ai_system_prompt", "")

    cmd_map = {}
    for c in commands:
        if isinstance(c, dict) and c.get("command_name"):
            cmd_map[c["command_name"].lower()] = c
            if c.get("is_ai_assistant"):
                is_ai     = True
                ai_prompt = c.get("ai_system_prompt", ai_prompt)

    reply = None

    if cmd_name and cmd_name in cmd_map:
        try:
            reply = execute_handler(cmd_map[cmd_name].get("handler_code", "reply_text='Hi!'"), text, update)
        except Exception as e:
            reply = "⚠️ Command error. Please try again."
            php_post("/api/log-error.php", {"token": token, "command_name": cmd_name, "error_message": str(e), "telegram_user_id": user_id})

    elif is_ai and not text.startswith("/"):
        data  = php_post("/api/ai-chat.php", {"token": token, "message": text, "system_prompt": ai_prompt})
        reply = data.get("reply", "AI unavailable.")

    elif cmd_name == "/start" and "/start" not in cmd_map:
        reply = f"👋 Hello {first_name}! I'm online. Send /help for available commands."

    elif cmd_name == "/help" and "/help" not in cmd_map:
        if cmd_map:
            reply = "*Available Commands:*\n" + "\n".join(f"`{n}`" for n in sorted(cmd_map))
        else:
            reply = "No commands configured for this bot yet."

    if reply:
        res = tg_send(token, chat_id, reply)
        php_post("/api/log-message.php", {
            "token": token, "telegram_user_id": user_id, "username": username,
            "message": text, "response": reply if res.get("ok") else "[send failed]",
        })


# ── Vercel Handler ────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass

    def cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def send_json(self, code: int, body: dict):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.cors_headers()
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):
        self.send_response(200)
        self.cors_headers()
        self.end_headers()

    def do_GET(self):
        path  = self.path.split("?")[0].rstrip("/") or "/"
        query = dict(urllib.parse.parse_qsl(self.path.split("?", 1)[1])) if "?" in self.path else {}

        # Health
        if path in ("/health", "/api/health", "/"):
            self.send_json(200, {"status": "ok", "platform": "BotForge", "php_host": PHP_HOST or "NOT SET"})
            return

        # ── Token validation fallback ─────────────────────────────────────────
        # Browser JS calls this when PHP can't reach Telegram directly
        if path == "/validate-token":
            token = query.get("token", "").strip()
            if not token:
                self.send_json(400, {"ok": False, "error": "No token"})
                return
            if not re.match(r'^\d+:[A-Za-z0-9_-]{30,}$', token):
                self.send_json(400, {"ok": False, "error": "Invalid token format"})
                return
            result = tg_get(token, "getMe")
            if result.get("ok"):
                self.send_json(200, {
                    "ok":         True,
                    "username":   result["result"].get("username", ""),
                    "first_name": result["result"].get("first_name", ""),
                    "bot_id":     result["result"].get("id", 0),
                    "source":     "vercel",
                })
            else:
                self.send_json(200, {"ok": False, "error": result.get("description", "Invalid token")})
            return

        self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")

        # Webhook
        if path.startswith("/webhook/"):
            token = path[len("/webhook/"):]
            if not token:
                self.send_json(400, {"error": "Missing token"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                update = json.loads(self.rfile.read(length))
            except Exception as e:
                self.send_json(400, {"error": str(e)})
                return
            try:
                handle_update(token, update)
            except Exception:
                pass
            self.send_json(200, {"ok": True})
            return

        self.send_json(404, {"error": "Not found"})

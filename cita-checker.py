import json
import logging
import os
import random
import smtplib
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from email.message import EmailMessage
from time import sleep

from seleniumbase import SB
from selenium.webdriver.common.by import By


CONFIG_PATH = "values.json"
SCREENSHOT_PATH = "/tmp/cita_disponible.png"
LOG_PATH = "/tmp/events.log"


def load_config():
    with open(CONFIG_PATH) as config_file:
        return json.load(config_file)


config = load_config()

CHECK_INTERVAL_SECONDS = int(config.get("check_interval_seconds", 600))
APPOINTMENT_HOLD_SECONDS = int(config.get("appointment_hold_seconds", 600))
TELEGRAM_POLL_TIMEOUT = int(config.get("telegram_poll_timeout_seconds", 30))

telegram_bot_token = config.get("telegram_bot_token", "").strip()
telegram_default_chat_id = str(config.get("telegram_chat_id", "")).strip()
allowed_chat_ids = set(str(chat_id) for chat_id in config.get("telegram_allowed_chat_ids", []) if str(chat_id).strip())
if telegram_default_chat_id:
    allowed_chat_ids.add(telegram_default_chat_id)

state = {
    "checker_enabled": bool(config.get("checker_enabled_on_startup", True)),
    "next_check_at": 0.0,
    "last_result": "never_run",
    "last_check_at": 0.0,
    "is_check_running": False,
}
state_lock = threading.Lock()
check_now_event = threading.Event()


def ensure_display_env():
    # Allow docker exec sessions (without inherited DISPLAY) to still launch browser.
    if os.environ.get("DISPLAY"):
        return
    for candidate in (":99", ":1", ":0"):
        os.environ["DISPLAY"] = candidate
        logging.warning("DISPLAY was not set. Falling back to DISPLAY=%s", candidate)
        return


def validate_config():
    warnings = []

    required_checker_keys = ["url", "region", "tramiteOptionText", "idCitadoValue", "desCitadoValue"]
    missing_checker_keys = [key for key in required_checker_keys if not str(config.get(key, "")).strip()]
    if missing_checker_keys:
        warnings.append(
            "Missing checker fields in values.json: " + ", ".join(missing_checker_keys)
        )

    if not telegram_bot_token:
        warnings.append("Telegram disabled: telegram_bot_token is empty.")
    elif not allowed_chat_ids:
        warnings.append("Telegram access control disabled: telegram_allowed_chat_ids is empty.")
    elif not telegram_default_chat_id:
        warnings.append("telegram_chat_id is empty; direct outbound bot messages may be skipped.")

    email_keys = ["sender_email", "receiver_email", "password", "smtp_server", "smtp_port"]
    if not all(config.get(key) for key in email_keys):
        warnings.append("Email notifications disabled: SMTP configuration is incomplete.")

    if CHECK_INTERVAL_SECONDS < 30:
        warnings.append("check_interval_seconds is very low (<30); this may increase blocking risk.")

    if APPOINTMENT_HOLD_SECONDS < 30:
        warnings.append("appointment_hold_seconds is very low (<30); manual completion window may be too short.")

    return warnings


def find_and_kill():
    try:
        pids = subprocess.check_output("pgrep brave", shell=True).decode().strip().split()
        for pid in pids:
            subprocess.call(f"kill {pid}", shell=True, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        logging.info("No Brave process is currently running.")


def set_keyboard_layout():
    keyboard_layout = config.get("keyboard_layout", "").strip()
    if not keyboard_layout:
        logging.info("No keyboard layout set in config.")
        return
    try:
        subprocess.run(["setxkbmap", "-layout", keyboard_layout], check=True)
        logging.info("Keyboard layout set to %s.", keyboard_layout)
    except subprocess.CalledProcessError as error:
        logging.warning("Skipping keyboard layout change (setxkbmap failed): %s", error)


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()],
    )


def send_email(subject, message, attach_screenshot=False):
    sender_email = config.get("sender_email", "").strip()
    receiver_email = config.get("receiver_email", "").strip()
    password = config.get("password", "")
    smtp_server = config.get("smtp_server", "").strip()
    smtp_port = config.get("smtp_port")

    if not all([sender_email, receiver_email, password, smtp_server, smtp_port]):
        logging.info("Email config missing. Skipping email notification.")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender_email
    msg["To"] = receiver_email
    msg.set_content(message)

    if attach_screenshot and os.path.exists(SCREENSHOT_PATH):
        with open(SCREENSHOT_PATH, "rb") as screenshot_file:
            file_data = screenshot_file.read()
            msg.add_attachment(file_data, maintype="image", subtype="png", filename=os.path.basename(SCREENSHOT_PATH))

    try:
        with smtplib.SMTP(smtp_server, smtp_port) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(sender_email, password)
            smtp.send_message(msg)
            logging.info("Email sent successfully.")
    except Exception as error:
        logging.error("Error sending email: %s", error)


def telegram_api_call(method, params=None, file_field_name=None, file_path=None):
    if not telegram_bot_token:
        return None

    url = f"https://api.telegram.org/bot{telegram_bot_token}/{method}"
    params = params or {}

    try:
        if file_field_name and file_path and os.path.exists(file_path):
            boundary = f"----CitaBoundary{uuid.uuid4().hex}"
            body = bytearray()
            for key, value in params.items():
                body.extend(f"--{boundary}\r\n".encode())
                body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
                body.extend(f"{value}\r\n".encode())

            filename = os.path.basename(file_path)
            with open(file_path, "rb") as file_handle:
                file_bytes = file_handle.read()
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(
                f'Content-Disposition: form-data; name="{file_field_name}"; filename="{filename}"\r\n'.encode()
            )
            body.extend(b"Content-Type: image/png\r\n\r\n")
            body.extend(file_bytes)
            body.extend(b"\r\n")
            body.extend(f"--{boundary}--\r\n".encode())
            request = urllib.request.Request(
                url,
                data=bytes(body),
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                method="POST",
            )
        else:
            encoded = urllib.parse.urlencode(params).encode()
            request = urllib.request.Request(url, data=encoded, method="POST")

        with urllib.request.urlopen(request, timeout=TELEGRAM_POLL_TIMEOUT + 10) as response:
            response_data = response.read().decode("utf-8")
            return json.loads(response_data)
    except urllib.error.HTTPError as error:
        logging.error("Telegram API HTTP error on %s: %s", method, error)
    except Exception as error:
        logging.error("Telegram API error on %s: %s", method, error)
    return None


def send_telegram_message(message, chat_id=None):
    if not telegram_bot_token:
        return
    target_chat_id = str(chat_id or telegram_default_chat_id).strip()
    if not target_chat_id:
        logging.info("No telegram_chat_id configured. Skipping Telegram message.")
        return
    telegram_api_call("sendMessage", {"chat_id": target_chat_id, "text": message})


def send_telegram_photo(caption, file_path, chat_id=None):
    if not telegram_bot_token:
        return
    target_chat_id = str(chat_id or telegram_default_chat_id).strip()
    if not target_chat_id:
        logging.info("No telegram_chat_id configured. Skipping Telegram photo.")
        return
    if not os.path.exists(file_path):
        logging.info("Screenshot not found at %s. Skipping Telegram photo.", file_path)
        return
    telegram_api_call(
        "sendPhoto",
        {"chat_id": target_chat_id, "caption": caption},
        file_field_name="photo",
        file_path=file_path,
    )


def notify_appointment_found():
    text = "Cita disponible. Open localhost:6080 to complete the process."
    send_email("Cita Disponible Alert", text, attach_screenshot=True)
    send_telegram_message(text)
    send_telegram_photo("Possible appointment found.", SCREENSHOT_PATH)


def set_random_window_size(sb):
    width = random.randint(800, 1600)
    height = (width * 2) // 3
    sb.set_window_size(width, height)


def check_for_appointments():
    try:
        with SB(
            browser="chrome",
            binary_location="/usr/bin/brave-browser",
            headed=True,
            use_auto_ext=True,
            slow=True,
            demo=True,
            incognito=True,
            chromium_arg="--no-sandbox,--disable-dev-shm-usage,--disable-gpu,--remote-debugging-port=9222",
        ) as sb:
            set_random_window_size(sb)
            sleep(2)
            sb.open(config["url"])
            sleep(2)
            sb.click("#form")
            sleep(2)
            sb.select_option_by_text("#form", config["region"])
            sleep(2)
            sb.click("#btnAceptar")
            sb.select_option_by_text("#tramiteGrupo\\[0\\]", config["tramiteOptionText"])
            sb.click("#btnAceptar")
            sleep(2)
            sb.click("#btnEntrar")
            sb.find_element(By.ID, "rdbTipoDocPas").click()
            sb.type("#txtIdCitado", config["idCitadoValue"])
            sleep(2)
            sb.type("#txtDesCitado", config["desCitadoValue"])
            sleep(2)
            sb.click("#btnEnviar")
            sleep(2)
            sb.click("#btnEnviar")
            sleep(2)

            if sb.is_text_visible("En este momento no hay citas disponibles"):
                logging.info("No available appointments. Next check in %s seconds.", CHECK_INTERVAL_SECONDS)
                find_and_kill()
                return "retry"

            sb.set_window_size(1280, 1024)
            sb.save_screenshot(SCREENSHOT_PATH)
            notify_appointment_found()
            logging.info("Appointments might be available. Holding browser for %s seconds.", APPOINTMENT_HOLD_SECONDS)
            time.sleep(APPOINTMENT_HOLD_SECONDS)
            return "manual_check_needed"
    except Exception as error:
        logging.error("Encountered an error during the check: %s. Retrying later.", error)
        find_and_kill()
        return "error"


def format_status():
    with state_lock:
        enabled = state["checker_enabled"]
        next_check_at = state["next_check_at"]
        last_result = state["last_result"]
        last_check_at = state["last_check_at"]
        is_running = state["is_check_running"]

    now = time.time()
    if next_check_at and next_check_at > now:
        seconds_until_next = int(next_check_at - now)
    else:
        seconds_until_next = 0

    last_check_text = "never"
    if last_check_at:
        last_check_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_check_at))

    return (
        f"checker_enabled={enabled}\n"
        f"is_check_running={is_running}\n"
        f"last_result={last_result}\n"
        f"last_check_at={last_check_text}\n"
        f"next_check_in_seconds={seconds_until_next}"
    )


def read_last_log_lines(max_lines=20):
    if not os.path.exists(LOG_PATH):
        return "Log file not found yet."
    with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as log_file:
        lines = log_file.readlines()
    tail = lines[-max_lines:]
    return "".join(tail).strip() or "Log is empty."


def handle_telegram_command(text, chat_id):
    command = text.strip().split()[0].lower()

    if command == "/ping":
        send_telegram_message("pong", chat_id=chat_id)
        return

    if command in ("/start", "/start_checker"):
        with state_lock:
            state["checker_enabled"] = True
            state["next_check_at"] = 0.0
        check_now_event.set()
        send_telegram_message("Checker started. I will run a check now.", chat_id=chat_id)
        return

    if command == "/stop":
        with state_lock:
            state["checker_enabled"] = False
        send_telegram_message("Checker stopped. No automatic checks will run.", chat_id=chat_id)
        return

    if command == "/check_now":
        check_now_event.set()
        send_telegram_message("Manual check requested. It will run shortly.", chat_id=chat_id)
        return

    if command == "/status":
        send_telegram_message(format_status(), chat_id=chat_id)
        return

    if command == "/last_log":
        log_text = read_last_log_lines()
        max_length = 3500
        if len(log_text) > max_length:
            log_text = log_text[-max_length:]
        send_telegram_message(f"Last log lines:\n{log_text}", chat_id=chat_id)
        return

    if command == "/screenshot":
        if os.path.exists(SCREENSHOT_PATH):
            send_telegram_photo("Latest screenshot.", SCREENSHOT_PATH, chat_id=chat_id)
        else:
            send_telegram_message("No screenshot available yet.", chat_id=chat_id)
        return

    if command in ("/help", "/commands"):
        send_telegram_message(
            "Commands:\n"
            "/ping - health check\n"
            "/start or /start_checker - enable checks\n"
            "/stop - disable automatic checks\n"
            "/check_now - run one check now\n"
            "/status - current state\n"
            "/last_log - show last log lines\n"
            "/screenshot - send latest screenshot",
            chat_id=chat_id,
        )
        return

    send_telegram_message("Unknown command. Use /help.", chat_id=chat_id)


def run_telegram_bot_loop():
    if not telegram_bot_token:
        logging.info("Telegram bot token not configured. Telegram control is disabled.")
        return

    logging.info("Telegram control loop started.")
    offset = None
    while True:
        params = {"timeout": TELEGRAM_POLL_TIMEOUT}
        if offset is not None:
            params["offset"] = offset

        response = telegram_api_call("getUpdates", params)
        if not response or not response.get("ok"):
            time.sleep(2)
            continue

        updates = response.get("result", [])
        for update in updates:
            update_id = update.get("update_id")
            if update_id is not None:
                offset = update_id + 1

            message = update.get("message") or {}
            text = message.get("text", "")
            chat_id = str((message.get("chat") or {}).get("id", "")).strip()
            if not text or not chat_id:
                continue

            if allowed_chat_ids and chat_id not in allowed_chat_ids:
                logging.warning("Rejected Telegram command from unauthorized chat_id=%s", chat_id)
                continue

            logging.info("Received Telegram command from chat_id=%s: %s", chat_id, text)
            handle_telegram_command(text, chat_id)


def run_checker_loop():
    while True:
        now = time.time()
        with state_lock:
            checker_enabled = state["checker_enabled"]
            next_check_at = state["next_check_at"]
            is_running = state["is_check_running"]

        should_run = False
        manual_triggered = False
        if check_now_event.is_set() and not is_running:
            check_now_event.clear()
            should_run = True
            manual_triggered = True
        elif checker_enabled and not is_running and now >= next_check_at:
            should_run = True

        if not should_run:
            time.sleep(1)
            continue

        with state_lock:
            state["is_check_running"] = True

        if manual_triggered:
            logging.info("Running manual check triggered from Telegram/local event.")
        else:
            logging.info("Running scheduled appointment check.")

        result = check_for_appointments()
        finished_at = time.time()

        with state_lock:
            state["last_result"] = result
            state["last_check_at"] = finished_at
            state["next_check_at"] = finished_at + CHECK_INTERVAL_SECONDS
            state["is_check_running"] = False


def main():
    setup_logging()
    ensure_display_env()
    set_keyboard_layout()
    config_warnings = validate_config()
    if config_warnings:
        for warning in config_warnings:
            logging.warning("Startup sanity check: %s", warning)
    else:
        logging.info("Startup sanity check: configuration looks good.")

    with state_lock:
        state["next_check_at"] = time.time()

    if telegram_bot_token:
        bot_thread = threading.Thread(target=run_telegram_bot_loop, daemon=True)
        bot_thread.start()
        send_telegram_message(
            "Cita Checker bot is online. Use /help for commands.",
            chat_id=telegram_default_chat_id if telegram_default_chat_id else None,
        )

    run_checker_loop()


if __name__ == "__main__":
    main()

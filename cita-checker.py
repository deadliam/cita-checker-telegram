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
import pwd
import re
import unicodedata
from email.message import EmailMessage
from time import sleep

from seleniumbase import SB
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select


CONFIG_PATH = "values.json"
SCREENSHOT_PATH = "/tmp/cita_disponible.png"
LOG_PATH = "/tmp/events.log"


def load_config():
    env_config = os.environ.get("CITA_CONFIG_PATH", "").strip()
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        env_config,
        CONFIG_PATH,
        os.path.join(script_dir, "values.json"),
        "/home/nonroot/values.json",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            with open(path) as config_file:
                return json.load(config_file)
    raise FileNotFoundError(
        "Could not find values.json. Checked: "
        + ", ".join([path for path in candidates if path])
    )


def ensure_runtime_home():
    uid = os.getuid()
    try:
        system_home = pwd.getpwuid(uid).pw_dir
    except Exception:
        system_home = "/home/nonroot"

    preferred_home = system_home if os.path.isdir(system_home) else "/home/nonroot"
    os.environ["HOME"] = preferred_home
    os.environ["XDG_CONFIG_HOME"] = os.path.join(preferred_home, ".config")
    os.environ["XDG_CACHE_HOME"] = os.path.join(preferred_home, ".cache")
    os.environ["XDG_DATA_HOME"] = os.path.join(preferred_home, ".local", "share")

    for path in (
        os.environ["XDG_CONFIG_HOME"],
        os.environ["XDG_CACHE_HOME"],
        os.environ["XDG_DATA_HOME"],
        os.path.join(os.environ["XDG_CACHE_HOME"], "selenium"),
    ):
        os.makedirs(path, exist_ok=True)


config = load_config()

CHECK_INTERVAL_SECONDS = int(config.get("check_interval_seconds", 600))
APPOINTMENT_HOLD_SECONDS = int(config.get("appointment_hold_seconds", 600))
TELEGRAM_POLL_TIMEOUT = int(config.get("telegram_poll_timeout_seconds", 30))
HEADLESS = bool(config.get("headless", True))
CHROMEDRIVER_VERSION = str(config.get("chromedriver_version", "latest")).strip() or "latest"
BRAVE_BINARY_LOCATION = str(config.get("brave_binary_location", "/usr/bin/brave-browser")).strip()
SB_USE_AUTO_EXT = bool(config.get("sb_use_auto_ext", False))
SB_SLOW = bool(config.get("sb_slow", False))
SB_DEMO = bool(config.get("sb_demo", False))
TELEGRAM_STEP_SCREENSHOTS = bool(config.get("telegram_step_screenshots", True))
STEP_SCREENSHOTS_DIR = str(config.get("step_screenshots_dir", "/tmp/cita_steps")).strip() or "/tmp/cita_steps"
MAX_BACKOFF_SECONDS = 3600
DEFAULT_BACKOFF_SECONDS = [120, 300, 900, 1800, 3600]
configured_backoff = config.get("backoff_seconds", DEFAULT_BACKOFF_SECONDS)
if isinstance(configured_backoff, list) and configured_backoff:
    BACKOFF_SECONDS = []
    for value in configured_backoff:
        try:
            seconds = int(value)
            if seconds > 0:
                BACKOFF_SECONDS.append(min(seconds, MAX_BACKOFF_SECONDS))
        except Exception:
            continue
    if not BACKOFF_SECONDS:
        BACKOFF_SECONDS = DEFAULT_BACKOFF_SECONDS
else:
    BACKOFF_SECONDS = DEFAULT_BACKOFF_SECONDS
BLOCK_COOLDOWN_SECONDS = min(max(int(config.get("block_cooldown_seconds", 900)), 60), MAX_BACKOFF_SECONDS)

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
    "consecutive_failures": 0,
    "blocked_until": 0.0,
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


def detect_browser_version(binary_path):
    try:
        env = os.environ.copy()
        version_output = subprocess.check_output(
            [binary_path, "--version"], stderr=subprocess.STDOUT, env=env
        ).decode("utf-8", errors="replace")
        lines = [line.strip() for line in version_output.splitlines() if line.strip()]
        for line in reversed(lines):
            if "Brave Browser" in line or "Google Chrome" in line or "Chromium" in line:
                return line
        return lines[-1] if lines else ""
    except Exception:
        return ""


def get_effective_browser_binary():
    configured_binary = BRAVE_BINARY_LOCATION
    configured_version = detect_browser_version(configured_binary)
    google_chrome_path = "/usr/bin/google-chrome"

    if configured_version and "Brave Browser 72." in configured_version and os.path.exists(google_chrome_path):
        google_version = detect_browser_version(google_chrome_path)
        if google_version:
            logging.warning(
                "Configured browser is legacy (%s). Switching to %s (%s).",
                configured_version,
                google_chrome_path,
                google_version,
            )
            return google_chrome_path, google_version

    if configured_version:
        return configured_binary, configured_version

    for candidate in [google_chrome_path, "/usr/bin/chromium-browser", "/usr/bin/chromium", "/usr/bin/brave-browser"]:
        if os.path.exists(candidate):
            candidate_version = detect_browser_version(candidate)
            if candidate_version:
                logging.warning("Configured browser not usable. Falling back to %s (%s).", candidate, candidate_version)
                return candidate, candidate_version

    return configured_binary, ""


def get_effective_driver_version(browser_version_text):
    # Auto-pick compatible driver unless user explicitly configures one.
    if CHROMEDRIVER_VERSION.lower() != "latest":
        return CHROMEDRIVER_VERSION
    try:
        if "89." in browser_version_text:
            logging.warning(
                "Detected Chrome version (%s). "
                "Using chromedriver 89.0.4389.23 for compatibility.",
                browser_version_text,
            )
            return "89.0.4389.23"
        if "72." in browser_version_text:
            logging.warning(
                "Detected legacy browser version (%s). "
                "Using chromedriver 72.0.3626.69 for compatibility.",
                browser_version_text,
            )
            return "72.0.3626.69"
    except Exception as error:
        logging.warning("Could not detect browser version (%s). Using chromedriver=%s", error, CHROMEDRIVER_VERSION)
    return CHROMEDRIVER_VERSION


def get_rotating_proxy():
    """Get a rotating proxy URL for IP rotation."""
    proxy_config = config.get("proxy_config", {})

    if not proxy_config:
        return None

    # Support single proxy URL
    proxy_url = proxy_config.get("proxy_url", "").strip()
    if proxy_url:
        return proxy_url

    # Support list of proxies to rotate through
    proxy_list = proxy_config.get("proxy_list", [])
    if proxy_list:
        selected_proxy = random.choice(proxy_list)
        return selected_proxy.strip()

    return None


def build_chromium_args(browser_version_text, proxy_url=None):
    is_legacy_72 = "72." in browser_version_text
    base = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--disable-extensions",
        "--disable-infobars",
        "--remote-debugging-port=9222",
        "--user-data-dir=/tmp/chrome-user-data",
        "--data-path=/tmp/chrome-data",
        "--disk-cache-dir=/tmp/chrome-cache",
    ]

    # Add proxy if configured
    if proxy_url:
        base.append(f"--proxy-server={proxy_url}")

    if is_legacy_72:
        # Only use these aggressive flags for old Chromium/Brave 72.
        base.extend(["--no-zygote", "--single-process"])
    else:
        # More stable for Chrome 89 in containers.
        base.append("--disable-features=VizDisplayCompositor")
    if HEADLESS:
        base.extend(["--headless", "--window-size=1366,768"])
    return ",".join(base)


def normalize_text(value):
    text = (value or "").strip()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.upper()
    text = re.sub(r"\s+", " ", text)
    return text


def select_option_by_text_resilient(sb, selector, desired_text):
    desired_normalized = normalize_text(desired_text)
    element = sb.find_element(selector)
    select = Select(element)
    options = [opt.text.strip() for opt in select.options if opt.text.strip()]

    if not options:
        raise Exception(f"No options found for selector {selector}")

    # 1) Exact match first.
    for option in options:
        if option == desired_text:
            sb.select_option_by_text(selector, option)
            return option

    # 2) Normalized full-text match (accent/case/spacing tolerant).
    for option in options:
        if normalize_text(option) == desired_normalized:
            sb.select_option_by_text(selector, option)
            return option

    # 3) Handle values that include extra description after ":".
    desired_head = desired_text.split(":")[0].strip()
    desired_head_normalized = normalize_text(desired_head)
    if desired_head_normalized:
        for option in options:
            if normalize_text(option) == desired_head_normalized:
                sb.select_option_by_text(selector, option)
                return option
        for option in options:
            if normalize_text(option).startswith(desired_head_normalized):
                sb.select_option_by_text(selector, option)
                return option

    # 4) Contains/startswith fallback.
    for option in options:
        norm_option = normalize_text(option)
        if desired_normalized in norm_option or norm_option in desired_normalized:
            sb.select_option_by_text(selector, option)
            return option

    raise Exception(
        f"Could not match option '{desired_text}' for selector {selector}. "
        f"Available options: {options}"
    )


def select_tramite_option(sb, desired_text):
    selectors = []
    try:
        elements = sb.find_elements(By.CSS_SELECTOR, "select[id^='tramiteGrupo']")
        selectors = []
        for element in elements:
            element_id = element.get_attribute("id")
            escaped_id = element_id.replace("[", "\\[").replace("]", "\\]")
            selectors.append("#" + escaped_id)
    except Exception:
        selectors = []

    # Fallback to known default if dynamic discovery fails.
    if not selectors:
        selectors = ["#tramiteGrupo\\[0\\]"]

    errors = []
    for selector in selectors:
        try:
            selected = select_option_by_text_resilient(sb, selector, desired_text)
            return selector, selected
        except Exception as error:
            errors.append(f"{selector}: {error}")

    raise Exception(
        f"Could not match option '{desired_text}' in any tramite dropdown. "
        f"Tried selectors: {selectors}. Errors: {errors}"
    )


def select_document_type(sb):
    requested_type = str(config.get("TypeID", "")).strip().upper()
    mapping = {
        "NIE": ["#rdbTipoDocNie", "#rdbTipoDocNIE"],
        "PASAPORTE": ["#rdbTipoDocPas"],
        "PASSPORT": ["#rdbTipoDocPas"],
        "DNI": ["#rdbTipoDocDni", "#rdbTipoDocDNI"],
    }

    # Build candidate selectors in priority order.
    candidates = []
    if requested_type in mapping:
        candidates.extend(mapping[requested_type])
    # Generic fallbacks.
    candidates.extend(["#rdbTipoDocNie", "#rdbTipoDocNIE", "#rdbTipoDocPas", "#rdbTipoDocDni", "#rdbTipoDocDNI"])

    # Try known IDs first.
    for selector in candidates:
        try:
            if sb.is_element_visible(selector):
                sb.click(selector)
                return selector
        except Exception:
            pass

    # Fallback: click first visible radio button in document type section.
    radio_selectors = [
        "input[type='radio'][id*='TipoDoc']",
        "input[type='radio'][name*='TipoDoc']",
        "input[type='radio']",
    ]
    for css in radio_selectors:
        try:
            radios = sb.find_elements(By.CSS_SELECTOR, css)
            for radio in radios:
                if radio.is_displayed() and radio.is_enabled():
                    radio.click()
                    rid = radio.get_attribute("id") or css
                    return f"css:{rid}"
        except Exception:
            pass

    raise Exception(
        f"Could not select document type. Requested TypeID='{requested_type}'. "
        "No known document type radio buttons were found."
    )


class BlockedPageException(Exception):
    def __init__(self, support_id="", stage=""):
        self.support_id = support_id
        self.stage = stage
        support_fragment = f", support_id={support_id}" if support_id else ""
        super().__init__(f"Blocked page detected at stage='{stage}'{support_fragment}")


def parse_support_id(page_text):
    match = re.search(r"support ID is:\s*<?([0-9]+)>?", page_text, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    return ""


def detect_block_page(sb):
    try:
        page_text = sb.driver.page_source
    except Exception:
        return ""
    normalized = normalize_text(page_text)
    if "THE REQUESTED URL WAS REJECTED" in normalized or "YOUR SUPPORT ID IS" in normalized:
        return parse_support_id(page_text)
    return ""


def ensure_not_blocked(sb, stage):
    support_id = detect_block_page(sb)
    if not support_id and not sb.is_text_visible("The requested URL was rejected"):
        return

    blocked_path = "/tmp/cita_blocked.png"
    try:
        sb.save_screenshot(blocked_path)
    except Exception:
        pass

    text = (
        f"Block page detected during '{stage}'. "
        f"Cooldown for {BLOCK_COOLDOWN_SECONDS} seconds."
    )
    if support_id:
        text += f" Support ID: {support_id}"
    send_telegram_message(text)
    send_telegram_photo("Block page detected", blocked_path)
    raise BlockedPageException(support_id=support_id, stage=stage)


def run_check_steps(sb):
    set_random_window_size(sb)
    sleep(2)
    capture_step_screenshot(sb, "browser_started")
    sb.open(config["url"])
    ensure_not_blocked(sb, "open_url")
    sleep(2)
    capture_step_screenshot(sb, "page_opened")
    sb.click("#form")
    ensure_not_blocked(sb, "open_region_dropdown")
    sleep(2)
    capture_step_screenshot(sb, "province_dropdown_opened")
    sb.select_option_by_text("#form", config["region"])
    ensure_not_blocked(sb, "region_selected")
    sleep(2)
    capture_step_screenshot(sb, "province_selected")
    sb.click("#btnAceptar")
    ensure_not_blocked(sb, "after_region_accept")
    capture_step_screenshot(sb, "province_confirmed")
    matched_selector, matched_option = select_tramite_option(sb, config["tramiteOptionText"])
    logging.info("Selected tramite option from %s: %s", matched_selector, matched_option)
    capture_step_screenshot(sb, "tramite_selected")
    sb.click("#btnAceptar")
    ensure_not_blocked(sb, "after_tramite_accept")
    sleep(2)
    capture_step_screenshot(sb, "tramite_confirmed")
    sb.click("#btnEntrar")
    ensure_not_blocked(sb, "enter_form")
    capture_step_screenshot(sb, "entered_form")
    selected_doc_selector = select_document_type(sb)
    logging.info("Selected document type using selector: %s", selected_doc_selector)
    capture_step_screenshot(sb, "document_type_selected")
    sb.type("#txtIdCitado", config["idCitadoValue"])
    sleep(2)
    capture_step_screenshot(sb, "id_entered")
    sb.type("#txtDesCitado", config["desCitadoValue"])
    sleep(2)
    capture_step_screenshot(sb, "name_entered")
    sb.click("#btnEnviar")
    sleep(2)
    capture_step_screenshot(sb, "first_submit")
    sb.click("#btnEnviar")
    sleep(2)
    capture_step_screenshot(sb, "second_submit")

    if sb.is_text_visible("En este momento no hay citas disponibles"):
        capture_step_screenshot(sb, "no_appointments_message")
        logging.info("No available appointments. Next check in %s seconds.", CHECK_INTERVAL_SECONDS)
        find_and_kill()
        return "retry"

    sb.set_window_size(1280, 1024)
    sb.save_screenshot(SCREENSHOT_PATH)
    notify_appointment_found()
    logging.info("Appointments might be available. Holding browser for %s seconds.", APPOINTMENT_HOLD_SECONDS)
    time.sleep(APPOINTMENT_HOLD_SECONDS)
    return "manual_check_needed"


def check_for_appointments():
    effective_browser_binary, browser_version_text = get_effective_browser_binary()
    effective_driver_version = get_effective_driver_version(browser_version_text)
    logging.info("Using browser binary: %s", effective_browser_binary)
    if browser_version_text:
        logging.info("Detected browser version: %s", browser_version_text)

    # Get rotating proxy for this run (returns None if not configured)
    proxy_url = get_rotating_proxy()
    if proxy_url:
        logging.info("Using rotating proxy: %s", proxy_url)
    else:
        logging.info("No proxy configured. Running in regular mode.")

    # Helper function to build fallback args with proxy
    def build_fallback_args(proxy=None):
        args = "--no-sandbox,--disable-dev-shm-usage,--disable-gpu,--headless,--window-size=1366,768"
        if proxy:
            args += f",--proxy-server={proxy}"
        return args

    launch_profiles = [
        {
            "name": "primary",
            "headed": not HEADLESS,
            "headless": HEADLESS,
            "xvfb": False,
            "chromium_arg": build_chromium_args(browser_version_text, proxy_url),
        },
        {
            "name": "minimal_headless",
            "headed": False,
            "headless": True,
            "xvfb": False,
            "chromium_arg": build_fallback_args(proxy_url),
        },
        {
            "name": "xvfb_headed",
            "headed": True,
            "headless": False,
            "xvfb": True,
            "chromium_arg": "--no-sandbox,--disable-dev-shm-usage,--disable-gpu,--window-size=1366,768" + (f",--proxy-server={proxy_url}" if proxy_url else ""),
        },
    ]

    last_error = None
    for profile in launch_profiles:
        try:
            logging.info("Trying browser launch profile: %s", profile["name"])
            with SB(
                browser="chrome",
                binary_location=effective_browser_binary,
                headed=profile["headed"],
                headless=profile["headless"],
                xvfb=profile["xvfb"],
                use_auto_ext=SB_USE_AUTO_EXT,
                slow=SB_SLOW,
                demo=SB_DEMO,
                incognito=True,
                driver_version=effective_driver_version,
                chromium_arg=profile["chromium_arg"],
            ) as sb:
                return run_check_steps(sb)
        except BlockedPageException as blocked_error:
            logging.warning("Launch profile '%s' blocked: %s", profile["name"], blocked_error)
            find_and_kill()
            return "blocked"
        except Exception as error:
            last_error = error
            logging.warning("Launch profile '%s' failed: %s", profile["name"], error)
            find_and_kill()
            time.sleep(1)

    logging.error("Encountered an error during the check: %s. Retrying later.", last_error)
    return "error"


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
    response = telegram_api_call("sendMessage", {"chat_id": target_chat_id, "text": message})
    if response and not response.get("ok", False):
        logging.error("Telegram sendMessage failed: %s", response)


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
    response = telegram_api_call(
        "sendPhoto",
        {"chat_id": target_chat_id, "caption": caption},
        file_field_name="photo",
        file_path=file_path,
    )
    if response and not response.get("ok", False):
        logging.error("Telegram sendPhoto failed: %s", response)


def capture_step_screenshot(sb, step_name):
    if not TELEGRAM_STEP_SCREENSHOTS:
        return
    try:
        os.makedirs(STEP_SCREENSHOTS_DIR, exist_ok=True)
        safe_step = re.sub(r"[^a-zA-Z0-9._-]+", "_", step_name).strip("_") or "step"
        timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        screenshot_path = os.path.join(STEP_SCREENSHOTS_DIR, f"{timestamp}_{safe_step}.png")
        sb.save_screenshot(screenshot_path)
        logging.info("Saved step screenshot: %s", screenshot_path)
        send_telegram_photo(f"Step: {step_name}", screenshot_path)
    except Exception as error:
        logging.warning("Could not capture/send step screenshot for '%s': %s", step_name, error)


def initialize_telegram():
    if not telegram_bot_token:
        return

    me = telegram_api_call("getMe")
    if not me or not me.get("ok"):
        logging.error("Telegram getMe failed. Check telegram_bot_token.")
        return

    bot_info = me.get("result", {})
    logging.info(
        "Telegram bot authenticated: username=@%s id=%s",
        bot_info.get("username", "unknown"),
        bot_info.get("id", "unknown"),
    )

    # Ensure long polling works even if a webhook was configured before.
    delete_webhook = telegram_api_call("deleteWebhook", {"drop_pending_updates": False})
    if delete_webhook and delete_webhook.get("ok"):
        logging.info("Telegram webhook cleared for long polling mode.")
    else:
        logging.warning("Could not clear Telegram webhook. Long polling may fail: %s", delete_webhook)


def notify_appointment_found():
    text = "Cita disponible. Open localhost:6080 to complete the process."
    send_email("Cita Disponible Alert", text, attach_screenshot=True)
    send_telegram_message(text)
    send_telegram_photo("Possible appointment found.", SCREENSHOT_PATH)


def set_random_window_size(sb):
    width = random.randint(800, 1600)
    height = (width * 2) // 3
    sb.set_window_size(width, height)




def format_status():
    with state_lock:
        enabled = state["checker_enabled"]
        next_check_at = state["next_check_at"]
        last_result = state["last_result"]
        last_check_at = state["last_check_at"]
        is_running = state["is_check_running"]
        consecutive_failures = state.get("consecutive_failures", 0)
        blocked_until = state.get("blocked_until", 0.0)

    now = time.time()
    if next_check_at and next_check_at > now:
        seconds_until_next = int(next_check_at - now)
    else:
        seconds_until_next = 0

    last_check_text = "never"
    if last_check_at:
        last_check_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_check_at))
    blocked_until_text = "no"
    if blocked_until and blocked_until > now:
        blocked_until_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(blocked_until))

    return (
        f"checker_enabled={enabled}\n"
        f"is_check_running={is_running}\n"
        f"last_result={last_result}\n"
        f"last_check_at={last_check_text}\n"
        f"consecutive_failures={consecutive_failures}\n"
        f"blocked_until={blocked_until_text}\n"
        f"next_check_in_seconds={seconds_until_next}"
    )


def get_backoff_delay(failure_count):
    if failure_count <= 0:
        return CHECK_INTERVAL_SECONDS
    index = min(failure_count - 1, len(BACKOFF_SECONDS) - 1)
    return min(BACKOFF_SECONDS[index], MAX_BACKOFF_SECONDS)


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

    if command == "/id":
        send_telegram_message(f"chat_id={chat_id}", chat_id=chat_id)
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
            "/id - show current chat id\n"
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
        if not response:
            time.sleep(2)
            continue
        if not response.get("ok"):
            logging.error("Telegram getUpdates failed: %s", response)
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
            if result in ("retry", "manual_check_needed"):
                state["consecutive_failures"] = 0
                state["blocked_until"] = 0.0
                next_delay = CHECK_INTERVAL_SECONDS
            elif result == "blocked":
                state["consecutive_failures"] += 1
                next_delay = BLOCK_COOLDOWN_SECONDS
                state["blocked_until"] = finished_at + next_delay
            else:
                state["consecutive_failures"] += 1
                state["blocked_until"] = 0.0
                next_delay = get_backoff_delay(state["consecutive_failures"])

            state["next_check_at"] = finished_at + next_delay
            state["is_check_running"] = False

        logging.info(
            "Check finished with result=%s. Next run in %s seconds (failures=%s).",
            result,
            next_delay,
            state.get("consecutive_failures", 0),
        )


def main():
    setup_logging()
    ensure_runtime_home()
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
        initialize_telegram()
        bot_thread = threading.Thread(target=run_telegram_bot_loop, daemon=True)
        bot_thread.start()
        send_telegram_message(
            "Cita Checker bot is online. Use /help for commands.",
            chat_id=telegram_default_chat_id if telegram_default_chat_id else None,
        )

    run_checker_loop()


if __name__ == "__main__":
    main()

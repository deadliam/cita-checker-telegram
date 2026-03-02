# Cita Checker: Automated Appointment Availability Script 🚀

Cita Checker is a Python tool that automates the process of checking appointment availability on the [cita previa platform](https://icp.administracionelectronica.gob.es/icpplus/index.html) for various services in Spain, such as police services, asylum applications, or TIE card renewals.


### Buy Me A Coffee
If you think this saved you time, you can buy be a coffee if you'd like!

[!["Buy Me A Coffee"](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/nikhedonias)

## Features 🌟
- Uses SeleniumBase to interact with the web interface.
- Randomizes window size to avoid fingerprinting.
- Sends appointment alerts by email and/or Telegram.
- Telegram bot commands to start, stop, trigger checks, and inspect status/logs.
- Configurable via JSON (`values.json`) for ease of use and customization.
- Uses VNC for manual follow-up if needed.

## Setup 🛠️
This project uses Docker Compose to simplify running the application. A noVNC server is created, allowing you to check in either through a web browser or a VNC client.

### Step 1: Clone the Repository
```sh
$ git clone https://github.com/TiagoCortinhal/cita-checker.git
$ cd cita-checker
```

### Step 2: Run Docker Compose 🐳
To start the Docker container, simply run:
```sh
$ docker-compose up
```
This command will set up a noVNC server that you can access via your web browser or a VNC client to monitor the script in real-time.

### Step 3: Access the VNC
Once the container is running, you can view the interface using:
- Browser: `http://localhost:6080` to access noVNC.
- VNC Client: Connect to `localhost:5901`.
- password for the VNC and user is `root`.

## Step 4: Start the Script
After accessing the noVNC interface, start the `cita-checker.py` script located in the home folder. This will initiate the appointment-checking process.

From personal experience Brave browser is the only one that did not trigger any problems from the platform.

## Configuration 📄
Copy `values.example.json` to `values.json` and update it with your personal details:

```sh
cp values.example.json values.json
```

`values.json` is ignored by git to keep your local credentials out of version control.

Then fill this structure:
```json
{
  "url": "https://icp.administracionelectronica.gob.es/icpplus/index.html",
  "idCitadoValue": "YOUR_ID_NUMBER",
  "desCitadoValue": "YOUR_FULL_NAME",
  "TypeID": "YOUR_ID_TYPE",
  "paisNacValue": "YOUR_COUNTRY",
  "tramiteOptionText": "YOUR_SERVICE_OPTION_TEXT",
  "receiver_email": "your_receiver@example.com",
  "sender_email": "your_sender@example.com",
  "password": "your_email_password_or_app_password",
  "smtp_server": "mail.gmx.com",
  "smtp_port": 587,
  "keyboard_layout": "us",
  "region": "Madrid",
  "checker_enabled_on_startup": true,
  "schedule_enabled": false,
  "schedule_days": [0, 1, 2, 3, 4],
  "schedule_mode": "times",
  "schedule_times": ["09:00", "10:00", "12:30"],
  "schedule_interval_start": "08:00",
  "schedule_interval_minutes": 60,
  "schedule_start": "09:00",
  "schedule_end": "18:00",
  "check_interval_seconds": 600,
  "backoff_seconds": [120, 300, 900, 1800, 3600],
  "block_cooldown_seconds": 900,
  "appointment_hold_seconds": 600,
  "telegram_poll_timeout_seconds": 30,
  "telegram_step_screenshots": true,
  "step_screenshots_dir": "/tmp/cita_steps",
  "telegram_bot_token": "123456789:YOUR_TELEGRAM_BOT_TOKEN",
  "telegram_chat_id": "123456789",
  "telegram_allowed_chat_ids": ["123456789"]
}
```

### Telegram Setup 🤖
1. Open Telegram and create a bot via `@BotFather` (`/newbot`).
2. Copy the bot token and place it in `telegram_bot_token`.
3. Send one message to your bot from your account.
4. Get your chat id:
   - Call: `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   - Use the `message.chat.id` value as `telegram_chat_id`.
5. Add the same id to `telegram_allowed_chat_ids` to restrict control access.

If `telegram_bot_token` is empty, Telegram control is disabled.
On startup, the script logs a configuration sanity check and warns about missing checker/Telegram/SMTP settings.

### Telegram Commands
- `/ping`: health check (bot replies with `pong`).
- `/start` or `/start_checker`: enable automatic checks and trigger an immediate run.
- `/stop`: pause automatic checks.
- `/check_now`: trigger one immediate check.
- `/status`: show checker state and timing.
- `/menu`: show checker/schedule menu.
- `/schedule_show`: show current weekly schedule.
- `/schedule_on`: enable schedule window enforcement.
- `/schedule_off`: disable schedule window enforcement.
- `/schedule_mode window|times|interval`: set schedule mode.
- `/schedule_days`: update days (reply with `Mon,Tue` or `1,2` ... `7`).
- `/schedule_time`:
  - `window` mode: reply `HH:MM-HH:MM`
  - `times` mode: reply `09:00,10:00,12:30`
  - `interval` mode: reply `every:60` (minutes)
- `/schedule_interval_start`: set interval anchor time (example `08:00`).
- `/last_log`: return latest lines from `/tmp/events.log`.
- `/screenshot`: send the latest screenshot (`/tmp/cita_disponible.png`) if present.
- `/help`: show command list.

## Trámite Options 📝
Here are all the possible trámite options you can choose from:

- 🌍 **ASILO - PRIMERA CITA**: The start of your asylum journey in Madrid.
- 🏛️ **ASILO - OFICINA DE ASILO Y REFUGIO**: Pradillo 40 is where the magic happens for asylum and document renewals.
- ✈️ **AUTORIZACIÓN DE REGRESO**: Need to leave and come back? Get your authorization here.
- 🆔 **POLICIA - RECOGIDA DE TARJETA DE IDENTIDAD DE EXTRANJERO (TIE)**: Time to pick up that shiny new TIE card.
- 🔢 **POLICIA-ASIGNACIÓN DE N.I.E.**: Need an NIE? This is your stop.
- 📜 **POLICIA-CARTA DE INVITACIÓN**: Hosting someone? Invite them officially.
- 💚 **POLICIA-CERTIFICADO DE REGISTRO DE CIUDADANO DE LA U.E.**: For our beloved EU citizens.
- 🏠 **POLICIA-CERTIFICADOS (DE RESIDENCIA, DE NO RESIDENCIA Y DE CONCORDANCIA)**: Certificates galore – residence, non-residence, or concordance.
- 🖐️ **POLICIA-TOMA DE HUELLA (EXPEDICIÓN DE TARJETA)**: Fingerprinting and card issuance (including renewals and duplicates).
- 🎫 **POLICÍA - RECOGIDA DE LA T.I.E. CUYA AUTORIZACIÓN RESUELVE LA DIRECCIÓN GENERAL DE MIGRACIONES**: Picking up TIE cards resolved by Migration.
- 🇺🇦 **POLICÍA TARJETA CONFLICTO UCRANIA**: For those displaced due to the conflict in Ukraine.
- 🇬🇧 **POLICÍA-EXP.TARJETA ASOCIADA AL ACUERDO DE RETIRADA CIUDADANOS BRITÁNICOS (BREXIT)**: Brits and Brexit – get your cards sorted here.
- 🌐 **POLICÍA-EXPEDICIÓN DE TARJETAS CUYA AUTORIZACIÓN RESUELVE LA DIRECCIÓN GENERAL DE MIGRACIONES**: Issuing cards as per Migration resolutions.

## Using GMX for Easy SMTP Setup ✉️
You can use GMX as an SMTP server, which is easy to set up and works seamlessly for sending notification emails.
- **Server**: `mail.gmx.com`
- **Port**: `587`
- **TLS**: Enabled

Simply add your sender email, password, and SMTP configuration to `values.json`.

## Logging and Notifications 🔔
- Logs are saved in `/tmp/events.log`.
- When an appointment is available, the script sends:
  - Email alert with screenshot attachment (if SMTP config is valid).
  - Telegram text alert and screenshot (if Telegram config is valid).

## Contributing 💡
Contributions are welcome! This project was inspired by [https://github.com/tbalza/cita-checker](https://github.com/tbalza/cita-checker), but some bugs were present that have been improved in this version. Please create a pull request if you'd like to enhance the codebase or add new features.


## License 📜
This project is licensed under the MIT License.

# SplitWatch Bot ⏱️

A Telegram bot 🤖 designed to control a split-flap display, primarily functioning as a stopwatch or countdown timer. It communicates with the display via MQTT 📨 and optionally integrates with Home Assistant 🏠 for advanced control.

## ✨ Features

*   ⏱️ **Stopwatch Mode:** Start, stop, reset, and record split times.
*   ⏳ **Timer Mode:** Start countdown timers, add/subtract time on the fly.
*   📟 **MQTT Display:** Sends formatted time updates to a configured MQTT topic for the split-flap display.
*   🏠 **Home Assistant Integration (Optional):**
    *   Toggle a specified HA automation OFF/ON when the stopwatch starts/stops (using HA REST API).
    *   Power cycle a Shelly switch 🔌 connected to the display controller via a Telegram command (`/pw`) (using HA REST API).
*   🔑 **Authorization:** Restrict bot usage to specific Telegram user IDs.

## 🐳 Setup with Docker

1.  **Prerequisites:**
    *   Docker Engine
    *   Docker Compose

2.  **Clone the Repository:** 📂
    ```bash
    git clone <your-repository-url>
    cd <repository-directory>
    ```

3.  **Configure Environment Variables:** ⚙️
    *   Copy the template file:
        ```bash
        cp .env.template .env
        ```
    *   Edit the `.env` file with your specific details:
        *   `TELEGRAM_BOT_TOKEN`: Your token from Telegram's BotFather.
        *   `MQTT_BROKER_ADDRESS`, `MQTT_BROKER_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD`: Your MQTT broker connection details.
        *   `MQTT_TOPIC`: The MQTT topic your split-flap display listens to for text updates.
        *   `DISPLAY_WIDTH`, `DISPLAY_JUSTIFY`: Configure the appearance on your display.
        *   `AUTHORIZED_USER_IDS`: A comma-separated list of numeric Telegram User IDs allowed to use the bot.
        *   **Home Assistant Integration (Optional):**
            *   `HA_URL`: Your Home Assistant instance URL (e.g., `http://homeassistant.local:8123`).
            *   `HA_LLAT`: A Long-Lived Access Token generated from your HA profile page.
            *   `HA_AUTOMATION_ENTITY_ID`: The `entity_id` of the HA automation to control (e.g., `automation.your_display_automation`).
            *   `HA_SHELLY_SWITCH_ENTITY_ID`: The `entity_id` of the Shelly switch controlling the display's power (e.g., `switch.your_display_plug`).

4.  **Build and Run the Container:** ▶️
    ```bash
    docker compose up --build -d
    ```
    The bot should now be running and connected to Telegram and MQTT.

## 🚀 Usage

Interact with the bot via Telegram using the following commands:

*   `/start`, `/help`: Show the help message.
*   `/sw start`: ▶️ Start/resume the stopwatch.
*   `/timer MM:SS` or `/timer SS`:  Start a countdown timer.
*   `/stop`: ⏸️ Pause the stopwatch or timer.
*   `/reset`: ⏹️ Stop and reset to `00:00:00`.
*   `/split`:  Record a split time (stopwatch only).
*   `/add MM:SS` or `/add SS`: ➕ Add time to a running timer.
*   `/sub MM:SS` or `/sub SS`: ➖ Subtract time from a running timer.
*   `/status`:  Show the current timer/stopwatch status.
*   `/pw`:  Power cycle the display via the configured Shelly switch (requires HA integration).
*   `/quit`:  Stop/reset, clear the display, and restore HA automation state (if applicable).

## 🏠 Home Assistant Integration Details

The bot uses the Home Assistant REST API and a Long-Lived Access Token (LLAT) for integration.

*   **Automation Control:** When `/sw start` is used, the bot calls the `automation.turn_off` service for the `HA_AUTOMATION_ENTITY_ID`. When `/stop` or `/quit` is used (after a stopwatch run), it calls `automation.turn_on`.
*   **Shelly Power Cycle:** The `/pw` command calls `switch.turn_off` and `switch.turn_on` services for the `HA_SHELLY_SWITCH_ENTITY_ID`.

Ensure the LLAT you generate has permissions to call these services. 🔑

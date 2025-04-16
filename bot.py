import logging
import os
import signal
import asyncio
from typing import Optional
from dotenv import load_dotenv
import httpx # Added for webhooks

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from mqtt_handler import MQTTHandler
from timer import Timer, TimerMode # Import TimerMode here

# --- Logging Setup ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# Reduce verbosity of httpx logger used by telegram-python-bot
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Global Variables ---
mqtt_handler: MQTTHandler = None
timer: Timer = None
telegram_app: Application = None
DISPLAY_WIDTH: int = 12 # Default display width
DISPLAY_JUSTIFY: str = "center" # Default justification
AUTHORIZED_USER_IDS: set[int] = set() # Set of authorized user IDs
# Home Assistant Integration State
HA_URL: Optional[str] = None # URL for Home Assistant instance
HA_LLAT: Optional[str] = None # Long-Lived Access Token
HA_AUTOMATION_ENTITY_ID: Optional[str] = None # Automation to control via API
HA_SHELLY_SWITCH_ENTITY_ID: Optional[str] = None # Shelly switch to control via API
initial_blanking_sent: bool = False # Flag to track if initial blanking message was sent

# --- Constants ---
HELP_MESSAGE = (
    "SplitWatch Bot Started!\n"
    "Commands:\n"
    "/sw start - Start or resume stopwatch\n"
    "/timer MM:SS [or SS] - Start a NEW timer (must be stopped/reset first)\n"
    "/stop - Pause the current timer/stopwatch\n"
    "/reset - Stop and reset timer/stopwatch to 00:00:00\n"
    "/split - Record split time (stopwatch)\n"
    "/add MM:SS [or SS] - Add time (timer)\n"
    "/sub MM:SS [or SS] - Subtract time (timer)\n"
    "/status - Show current status\n"
    "/help - Show this help message\n"
    "/pw - Power cycle the display controller\n"
    "/quit - Stop timer/stopwatch, restore HA automation, clear display"
)

# --- MQTT Update Callback ---
async def update_display(formatted_time: str): # Make async
    """Callback function passed to the Timer to format and update MQTT."""
    global DISPLAY_WIDTH, DISPLAY_JUSTIFY # Access global config
    if mqtt_handler:
        # Format the time string using the configured width and justification
        payload = format_for_display(formatted_time)
        logger.debug(f"Sending to MQTT: '{payload}' (Justify: {DISPLAY_JUSTIFY}, Width: {DISPLAY_WIDTH})")
        await mqtt_handler.publish(payload) # Await async publish
    else:
        logger.warning("MQTT handler not initialized, cannot update display.")


# --- HA Status Update Callback ---
async def _status_update_callback(new_mode: TimerMode):
    """Publishes the new status to the HA status topic."""
    if HA_MQTT_DISCOVERY_ENABLED and HA_STATUS_STATE_TOPIC and mqtt_handler:
        status_payload = new_mode.name # e.g., "STOPPED", "RUNNING"
        logger.info(f"Publishing HA Status: {status_payload} to {HA_STATUS_STATE_TOPIC}")
        await mqtt_handler.publish(status_payload, topic=HA_STATUS_STATE_TOPIC)
    else:
        logger.debug("HA MQTT Discovery disabled or status topic/handler not available, skipping status publish.")


# --- Display Formatting Logic ---
def format_for_display(text: str) -> str:
    """Formats the text according to DISPLAY_WIDTH and DISPLAY_JUSTIFY."""
    width = DISPLAY_WIDTH
    justify = DISPLAY_JUSTIFY.lower()

    if justify == 'left':
        return text.ljust(width)
    elif justify == 'right':
        return text.rjust(width)
    elif justify == 'center':
        return text.center(width)
    else:
        logger.warning(f"Invalid DISPLAY_JUSTIFY value '{DISPLAY_JUSTIFY}'. Defaulting to center.")
        return text.center(width)

async def send_initial_blanking_message(): # Make async
    """Sends a blank message to clear the display upon connection."""
    if mqtt_handler:
        blank_message = " " * DISPLAY_WIDTH
        logger.info(f"Sending initial blanking message ({DISPLAY_WIDTH} spaces) to MQTT.")
        await mqtt_handler.publish(blank_message) # Await async publish
    else:
        logger.error("Cannot send initial blanking message: MQTT handler not ready.")

# --- Home Assistant Service Call ---
async def call_ha_service(domain: str, service: str, target_entity_id: Optional[str] = None):
    """Calls a Home Assistant service via the REST API using LLAT."""
    if not HA_URL or not HA_LLAT:
        logger.debug("HA_URL or HA_LLAT not set, skipping HA service call.")
        return
    if not target_entity_id: # Check if a target entity was provided
        logger.error(f"No target_entity_id provided for service call {domain}.{service}. Skipping.")
        return

    service_url = f"{HA_URL.rstrip('/')}/api/services/{domain}/{service}"
    headers = {
        "Authorization": f"Bearer {HA_LLAT}",
        "Content-Type": "application/json",
    }
    # Use the provided entity ID for the payload
    payload = {"entity_id": target_entity_id}

    try:
        async with httpx.AsyncClient() as client:
            logger.info(f"Calling HA service: {domain}.{service} for entity: {target_entity_id}")
            response = await client.post(service_url, headers=headers, json=payload)
            response.raise_for_status() # Raise an exception for bad status codes (4xx or 5xx)
            logger.info(f"Successfully called HA service {domain}.{service} for {target_entity_id}. Response: {response.status_code}")
            # Optionally log response body for debugging (can be large)
            # logger.debug(f"HA Service Response Body: {response.text}")
    except httpx.RequestError as exc:
        logger.error(f"Error calling HA service {service_url}: {exc}")
    except httpx.HTTPStatusError as exc:
        logger.error(f"Error response {exc.response.status_code} calling HA service {service_url}: {exc.response.text}")
    except Exception as e:
        logger.error(f"An unexpected error occurred while calling HA service {service_url}: {e}")


# --- Home Assistant MQTT Discovery ---
async def publish_ha_discovery():
    """Publishes MQTT discovery messages for HA entities."""
    if not HA_MQTT_DISCOVERY_ENABLED or not mqtt_handler:
        logger.info("HA MQTT Discovery is disabled or MQTT handler not ready.")
        return

    logger.info("Publishing Home Assistant MQTT Discovery messages...")

    base_topic = f"{HA_MQTT_DISCOVERY_PREFIX}"
    node_id = HA_MQTT_NODE_ID

    # Define entities
    entities = {
        "status_sensor": {
            "component": "sensor",
            "object_id": "status",
            "config": {
                "name": "SplitWatch Status",
                "state_topic": HA_STATUS_STATE_TOPIC,
                "icon": "mdi:state-machine",
                "unique_id": f"{node_id}_status",
                "device": HA_DEVICE_INFO,
            }
        },
        "time_sensor": {
            "component": "sensor",
            "object_id": "time",
            "config": {
                "name": "SplitWatch Time",
                "state_topic": HA_TIME_STATE_TOPIC,
                "icon": "mdi:timer-outline",
                "unique_id": f"{node_id}_time",
                "device": HA_DEVICE_INFO,
            }
        },
        "start_button": {
            "component": "button",
            "object_id": "start_resume",
            "config": {
                "name": "SplitWatch Start/Resume",
                "command_topic": HA_COMMAND_TOPIC,
                "payload_press": CMD_START,
                "icon": "mdi:play",
                "unique_id": f"{node_id}_start_resume",
                "device": HA_DEVICE_INFO,
            }
        },
        "stop_button": {
            "component": "button",
            "object_id": "stop_pause",
            "config": {
                "name": "SplitWatch Stop/Pause",
                "command_topic": HA_COMMAND_TOPIC,
                "payload_press": CMD_STOP,
                "icon": "mdi:pause",
                "unique_id": f"{node_id}_stop_pause",
                "device": HA_DEVICE_INFO,
            }
        },
        "reset_button": {
            "component": "button",
            "object_id": "reset",
            "config": {
                "name": "SplitWatch Reset",
                "command_topic": HA_COMMAND_TOPIC,
                "payload_press": CMD_RESET,
                "icon": "mdi:stop",
                "unique_id": f"{node_id}_reset",
                "device": HA_DEVICE_INFO,
            }
        },
        "split_button": {
            "component": "button",
            "object_id": "split",
            "config": {
                "name": "SplitWatch Split",
                "command_topic": HA_COMMAND_TOPIC,
                "payload_press": CMD_SPLIT,
                "icon": "mdi:flag-checkered",
                "unique_id": f"{node_id}_split",
                "device": HA_DEVICE_INFO,
            }
        },
    }

    # Add power cycle button only if Shelly is configured via REST API
    if HA_SHELLY_SWITCH_ENTITY_ID:
         entities["power_cycle_button"] = {
            "component": "button",
            "object_id": "power_cycle",
            "config": {
                "name": "SplitWatch Power Cycle",
                "command_topic": HA_COMMAND_TOPIC,
                "payload_press": CMD_PW_CYCLE,
                "icon": "mdi:power-cycle",
                "unique_id": f"{node_id}_power_cycle",
                "device": HA_DEVICE_INFO,
            }
        }

    publish_tasks = []
    for entity_key, entity_data in entities.items():
        component = entity_data["component"]
        object_id = entity_data["object_id"]
        config_topic = f"{base_topic}/{component}/{node_id}/{object_id}/config"
        config_payload = json.dumps(entity_data["config"])

        logger.info(f"Publishing config for {entity_key} to {config_topic}")
        # Publish with retain=True
        publish_tasks.append(
            mqtt_handler.publish(config_payload, topic=config_topic, retain=True)
        )

    await asyncio.gather(*publish_tasks)
    logger.info("Finished publishing HA MQTT Discovery messages.")


# --- Authorization Check ---
def is_authorized(update: Update) -> bool:
    """Checks if the user sending the update is authorized."""
    if not AUTHORIZED_USER_IDS: # If the list is empty, allow everyone (optional, could default to deny)
        # logger.warning("AUTHORIZED_USER_IDS is empty. Allowing all users.")
        # return True
        # Let's default to denying if the list is empty or not set correctly.
        logger.warning("AUTHORIZED_USER_IDS is not configured. Denying access.")
        return False
    user_id = update.effective_user.id
    if user_id not in AUTHORIZED_USER_IDS:
        logger.warning(f"Unauthorized access attempt by user ID: {user_id}")
        return False
    return True

async def unauthorized_reply(update: Update):
    """Sends a standard message to unauthorized users."""
    await update.message.reply_text("Sorry, you are not authorized to use this bot.")

# --- Telegram Command Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends explanation on how to use the bot."""
    global initial_blanking_sent
    if not is_authorized(update):
        await unauthorized_reply(update)
        return

    # Blanking message is now sent only on /sw start
    # if not initial_blanking_sent:
    #     send_initial_blanking_message()
    #     initial_blanking_sent = True

    # Also update the help message constant if commands change
    await update.message.reply_text(HELP_MESSAGE)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends the help message."""
    global initial_blanking_sent
    if not is_authorized(update):
        await unauthorized_reply(update)
        return

    # Blanking message is now sent only on /sw start
    # if not initial_blanking_sent:
    #     send_initial_blanking_message()
    #     initial_blanking_sent = True

    await update.message.reply_text(HELP_MESSAGE)

async def sw_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts the stopwatch."""
    if not is_authorized(update):
        await unauthorized_reply(update)
        return

    global initial_blanking_sent

    # Send initial blanking message on first authorized /sw start command
    if not initial_blanking_sent:
        await send_initial_blanking_message() # Await async call
        initial_blanking_sent = True

    # --- HA Integration: Call turn_off service ---
    if HA_AUTOMATION_ENTITY_ID: # Check if automation control is configured
        logger.info(f"Stopwatch starting. Calling automation.turn_off for {HA_AUTOMATION_ENTITY_ID}")
        await call_ha_service("automation", "turn_off", target_entity_id=HA_AUTOMATION_ENTITY_ID)
    # --- End HA Integration ---

    if timer:
        message = timer.start_stopwatch()
        await update.message.reply_text(message)
    else:
        await update.message.reply_text("Timer not initialized.")

async def timer_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts a countdown timer."""
    if not is_authorized(update):
        await unauthorized_reply(update)
        return
    if not timer:
        await update.message.reply_text("Timer not initialized.")
        return

    if not context.args:
        await update.message.reply_text("Please provide a duration. Usage: /timer MM:SS or /timer SS")
        return

    duration_str = context.args[0]
    try:
        parts = list(map(int, duration_str.split(':')))
        if len(parts) == 1:
            duration_seconds = parts[0]
        elif len(parts) == 2:
            duration_seconds = parts[0] * 60 + parts[1]
        elif len(parts) == 3:
             duration_seconds = parts[0] * 3600 + parts[1] * 60 + parts[2]
        else:
             raise ValueError("Invalid time format")

        if duration_seconds <= 0:
            await update.message.reply_text("Duration must be positive.")
            return

        message = timer.start_timer(duration_seconds)
        await update.message.reply_text(message)

    except ValueError:
        await update.message.reply_text("Invalid time format. Use MM:SS or SS.")

async def power_cycle_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Power cycles the Shelly switch controlling the display."""
    if not is_authorized(update):
        await unauthorized_reply(update)
        return

    if not HA_URL or not HA_LLAT or not HA_SHELLY_SWITCH_ENTITY_ID:
        await update.message.reply_text("Power cycle feature not configured (HA_URL, HA_LLAT, or HA_SHELLY_SWITCH_ENTITY_ID missing).")
        logger.warning("Attempted power cycle, but HA_URL, HA_LLAT, or HA_SHELLY_SWITCH_ENTITY_ID is not set.")
        return

    try:
        await update.message.reply_text("Attempting power cycle: Calling switch.turn_off...")
        logger.info(f"Calling switch.turn_off for Shelly: {HA_SHELLY_SWITCH_ENTITY_ID}")
        await call_ha_service("switch", "turn_off", target_entity_id=HA_SHELLY_SWITCH_ENTITY_ID)

        await asyncio.sleep(3) # Wait for 3 seconds

        await update.message.reply_text("Power cycle: Calling switch.turn_on...")
        logger.info(f"Calling switch.turn_on for Shelly: {HA_SHELLY_SWITCH_ENTITY_ID}")
        await call_ha_service("switch", "turn_on", target_entity_id=HA_SHELLY_SWITCH_ENTITY_ID)

        await update.message.reply_text("Power cycle sequence initiated via REST API.")
        logger.info("Power cycle sequence completed via REST API.")

    except Exception as e: # Catch potential errors from call_ha_service or sleep
        logger.error(f"Error during power cycle sequence: {e}")
        await update.message.reply_text(f"An error occurred during power cycle: {e}")


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stops the current timer or stopwatch."""
    if not is_authorized(update):
        await unauthorized_reply(update)
        return

    was_stopwatch = timer and timer.mode == TimerMode.STOPWATCH # Check *before* stopping

    if timer:
        message = timer.stop() # This stops the timer/stopwatch

        # --- HA Integration: Call turn_on service ---
        # Call turn_on only if the timer was actually a stopwatch
        if was_stopwatch and HA_AUTOMATION_ENTITY_ID: # Check if automation control is configured
            logger.info(f"Stopwatch stopped. Calling automation.turn_on for {HA_AUTOMATION_ENTITY_ID}")
            await call_ha_service("automation", "turn_on", target_entity_id=HA_AUTOMATION_ENTITY_ID)
        # --- End HA Integration ---

        await update.message.reply_text(message)
    else:
        await update.message.reply_text("Timer not initialized.")

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resets the timer or stopwatch."""
    if not is_authorized(update):
        await unauthorized_reply(update)
        return

    # Reset command should only affect the internal timer state.
    # HA automation state restoration happens only on /stop.

    if timer:
        message = timer.reset() # This resets the timer/stopwatch
        await update.message.reply_text(message)
    else:
        await update.message.reply_text("Timer not initialized.")

async def quit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stops the timer/stopwatch completely, restores HA state, and clears display."""
    if not is_authorized(update):
        await unauthorized_reply(update)
        return

    was_stopwatch = timer and timer.mode == TimerMode.STOPWATCH # Check if it was running as stopwatch before reset

    if timer:
        # Reset the timer first (stops job, clears internal state)
        reset_message = timer.reset() # This also sends 00:00:00 to display via callback

        # --- HA Integration: Call turn_on service ---
        # Call turn_on only if the timer was actually a stopwatch before reset/quit
        if was_stopwatch and HA_AUTOMATION_ENTITY_ID: # Check if automation control is configured
            logger.info(f"Quit command: Calling automation.turn_on for {HA_AUTOMATION_ENTITY_ID}")
            await call_ha_service("automation", "turn_on", target_entity_id=HA_AUTOMATION_ENTITY_ID)
        # --- End HA Integration ---

        # Explicitly clear the display after quitting
        logger.info("Quit command: Clearing display.")
        await send_initial_blanking_message() # Send blank message

        await update.message.reply_text(f"{reset_message}\nQuit successful. Display cleared and HA automation restored (if applicable).")

    else:
        await update.message.reply_text("Timer not initialized.")


async def split_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Records a split time."""
    if not is_authorized(update):
        await unauthorized_reply(update)
        return
    if timer:
        message = timer.split()
        await update.message.reply_text(message)
    else:
        await update.message.reply_text("Timer not initialized.")

async def add_time_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Adds time to the timer."""
    if not is_authorized(update):
        await unauthorized_reply(update)
        return
    if not timer:
        await update.message.reply_text("Timer not initialized.")
        return
    if not context.args:
        await update.message.reply_text("Please provide time to add. Usage: /add MM:SS or /add SS")
        return

    time_str = context.args[0]
    try:
        parts = list(map(int, time_str.split(':')))
        if len(parts) == 1:
            seconds_to_add = parts[0]
        elif len(parts) == 2:
            seconds_to_add = parts[0] * 60 + parts[1]
        elif len(parts) == 3:
             seconds_to_add = parts[0] * 3600 + parts[1] * 60 + parts[2]
        else:
             raise ValueError("Invalid time format")

        if seconds_to_add <= 0:
            await update.message.reply_text("Time to add must be positive.")
            return

        message = timer.add_time(seconds_to_add)
        await update.message.reply_text(message)
    except ValueError:
        await update.message.reply_text("Invalid time format. Use MM:SS or SS.")


async def sub_time_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Subtracts time from the timer."""
    if not is_authorized(update):
        await unauthorized_reply(update)
        return
    if not timer:
        await update.message.reply_text("Timer not initialized.")
        return
    if not context.args:
        await update.message.reply_text("Please provide time to subtract. Usage: /sub MM:SS or /sub SS")
        return

    time_str = context.args[0]
    try:
        parts = list(map(int, time_str.split(':')))
        if len(parts) == 1:
            seconds_to_subtract = parts[0]
        elif len(parts) == 2:
            seconds_to_subtract = parts[0] * 60 + parts[1]
        elif len(parts) == 3:
             seconds_to_subtract = parts[0] * 3600 + parts[1] * 60 + parts[2]
        else:
             raise ValueError("Invalid time format")

        if seconds_to_subtract <= 0:
            await update.message.reply_text("Time to subtract must be positive.")
            return

        message = timer.subtract_time(seconds_to_subtract)
        await update.message.reply_text(message)
    except ValueError:
        await update.message.reply_text("Invalid time format. Use MM:SS or SS.")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows the current status."""
    if not is_authorized(update):
        await unauthorized_reply(update)
        return
    if timer:
        message = timer.get_status()
        await update.message.reply_text(message)
    else:
        await update.message.reply_text("Timer not initialized.")

async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles unknown commands."""
    # No authorization check here, just inform the user (authorized or not)
    await update.message.reply_text("Sorry, I didn't understand that command.")

# --- Graceful Shutdown ---
async def shutdown(signal_num):
    """Handles graceful shutdown on receiving SIGINT or SIGTERM."""
    logger.info(f"Received signal {signal_num}. Shutting down...")

    global telegram_app, mqtt_handler, timer

    # Stop the timer first to prevent further MQTT updates
    if timer:
        logger.info("Stopping timer...")
        timer.reset() # Resets and stops any running jobs

    # Stop the Telegram application
    if telegram_app:
        logger.info("Shutting down Telegram application...")
        await telegram_app.shutdown()
        # Ensure the job queue is stopped (should happen during app.shutdown)
        # await telegram_app.job_queue.stop() # Usually not needed explicitly

    # Disconnect MQTT
    if mqtt_handler:
        logger.info("Disconnecting MQTT client...")
        mqtt_handler.disconnect()

    logger.info("Shutdown complete.")

# --- Main Execution ---
def main() -> None:
    """Start the bot."""
    global mqtt_handler, timer, telegram_app, DISPLAY_WIDTH, DISPLAY_JUSTIFY, AUTHORIZED_USER_IDS
    global HA_URL, HA_LLAT, HA_AUTOMATION_ENTITY_ID # REST API globals for Automation
    global HA_SHELLY_SWITCH_ENTITY_ID # REST API global for Shelly

    # --- Load Environment Variables ---
    load_dotenv()
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    MQTT_BROKER = os.getenv("MQTT_BROKER_ADDRESS")
    MQTT_PORT = os.getenv("MQTT_BROKER_PORT", 1883) # Default port
    MQTT_USER = os.getenv("MQTT_USERNAME")
    MQTT_PASS = os.getenv("MQTT_PASSWORD")
    MQTT_TOPIC = os.getenv("MQTT_TOPIC")
    # Load publish delay
    try:
        MQTT_PUB_DELAY = float(os.getenv("MQTT_PUBLISH_DELAY", "0.1"))
        if MQTT_PUB_DELAY < 0:
             logger.warning("MQTT_PUBLISH_DELAY cannot be negative. Using default 0.1s.")
             MQTT_PUB_DELAY = 0.1
    except ValueError:
        logger.warning("Invalid MQTT_PUBLISH_DELAY value. Using default 0.1s.")
        MQTT_PUB_DELAY = 0.1
    # Load display config with defaults
    try:
        DISPLAY_WIDTH = int(os.getenv("DISPLAY_WIDTH", "12"))
        if DISPLAY_WIDTH <= 0:
            logger.warning("DISPLAY_WIDTH must be positive. Using default 12.")
            DISPLAY_WIDTH = 12
    except ValueError:
        logger.warning("Invalid DISPLAY_WIDTH value. Using default 12.")
        DISPLAY_WIDTH = 12
    DISPLAY_JUSTIFY = os.getenv("DISPLAY_JUSTIFY", "center").lower()
    if DISPLAY_JUSTIFY not in ["left", "center", "right"]:
        logger.warning(f"Invalid DISPLAY_JUSTIFY value '{DISPLAY_JUSTIFY}'. Using default 'center'.")
        DISPLAY_JUSTIFY = "center"
    # Load authorized user IDs
    auth_users_str = os.getenv("AUTHORIZED_USER_IDS", "")
    if auth_users_str:
        try:
            AUTHORIZED_USER_IDS = {int(user_id.strip()) for user_id in auth_users_str.split(',') if user_id.strip()}
            logger.info(f"Loaded {len(AUTHORIZED_USER_IDS)} authorized user IDs.")
        except ValueError:
            logger.error("Invalid format in AUTHORIZED_USER_IDS. Please use comma-separated numbers. No users authorized.")
            AUTHORIZED_USER_IDS = set()
    else:
        logger.warning("AUTHORIZED_USER_IDS is not set in the environment. No users will be authorized.")
        AUTHORIZED_USER_IDS = set()

    # Load HA REST API config
    HA_URL = os.getenv("HA_URL")
    HA_LLAT = os.getenv("HA_LLAT")
    HA_AUTOMATION_ENTITY_ID = os.getenv("HA_AUTOMATION_ENTITY_ID")
    if HA_URL and HA_LLAT and HA_AUTOMATION_ENTITY_ID:
        logger.info("HA REST API Integration Enabled.")
        logger.info(f"  HA URL: {HA_URL}")
        logger.info(f"  Automation Entity ID: {HA_AUTOMATION_ENTITY_ID}")
        # Avoid logging the full LLAT for security
        logger.info(f"  LLAT Loaded: {'Yes' if HA_LLAT else 'No'}")
    else:
        logger.info("HA REST API Integration Disabled (HA_URL, HA_LLAT, or HA_AUTOMATION_ENTITY_ID not set).")
        HA_URL = None # Ensure all are None if not fully configured
        HA_LLAT = None
        HA_AUTOMATION_ENTITY_ID = None

    # Load HA Shelly Switch REST API config
    HA_SHELLY_SWITCH_ENTITY_ID = os.getenv("HA_SHELLY_SWITCH_ENTITY_ID")
    if HA_URL and HA_LLAT and HA_SHELLY_SWITCH_ENTITY_ID:
        logger.info("HA Shelly Switch REST API Control Enabled.")
        logger.info(f"  Shelly Entity ID: {HA_SHELLY_SWITCH_ENTITY_ID}")
    else:
        logger.info("HA Shelly Switch REST API Control Disabled (HA_URL, HA_LLAT, or HA_SHELLY_SWITCH_ENTITY_ID not set).")
        # Ensure Shelly ID is None if not fully configured
        HA_SHELLY_SWITCH_ENTITY_ID = None

    # Load HA MQTT Discovery config
    HA_MQTT_DISCOVERY_ENABLED = os.getenv("HA_MQTT_DISCOVERY_ENABLED", "false").lower() == "true"
    HA_MQTT_DISCOVERY_PREFIX = os.getenv("HA_MQTT_DISCOVERY_PREFIX", "homeassistant")
    HA_MQTT_NODE_ID = os.getenv("HA_MQTT_NODE_ID", "splitwatch_bot")

    if HA_MQTT_DISCOVERY_ENABLED:
        logger.info("HA MQTT Discovery Enabled.")
        logger.info(f"  Discovery Prefix: {HA_MQTT_DISCOVERY_PREFIX}")
        logger.info(f"  Node ID: {HA_MQTT_NODE_ID}")
        # Define base topics for state and commands relative to the node ID
        base_topic_path = f"splitwatch/{HA_MQTT_NODE_ID}" # e.g., splitwatch/splitwatch_bot
        HA_STATUS_STATE_TOPIC = f"{base_topic_path}/status"
        HA_TIME_STATE_TOPIC = f"{base_topic_path}/time"
        HA_COMMAND_TOPIC = f"{base_topic_path}/command"
        logger.info(f"  Status Topic: {HA_STATUS_STATE_TOPIC}")
        logger.info(f"  Time Topic: {HA_TIME_STATE_TOPIC}")
        logger.info(f"  Command Topic: {HA_COMMAND_TOPIC}")

        # Define the Device Info for HA Discovery
        HA_DEVICE_INFO = {
            "identifiers": [HA_MQTT_NODE_ID],
            "name": "SplitWatch Bot",
            "manufacturer": "SplitWatch Project", # Or your name/handle
            "model": "Telegram MQTT Bot",
            "sw_version": "1.0", # Consider making this dynamic later
            # "via_device": "MQTT Broker" # Optional: if it's connected via another HA device
        }
    else:
        logger.info("HA MQTT Discovery Disabled.")


    # --- Validate Environment Variables ---
    if not TELEGRAM_TOKEN:
        logger.critical("TELEGRAM_BOT_TOKEN not found in environment variables.")
        exit(1)
    if not MQTT_BROKER:
        logger.critical("MQTT_BROKER_ADDRESS not found in environment variables.")
        exit(1)
    if not MQTT_TOPIC:
        logger.critical("MQTT_TOPIC not found in environment variables.")
        exit(1)

    # --- Initialize Components ---
    logger.info("Initializing MQTT Handler...")
    # Removed on_connect_callback argument
    mqtt_handler = MQTTHandler(
        MQTT_BROKER,
        MQTT_PORT,
        MQTT_TOPIC,
        MQTT_USER,
        MQTT_PASS,
        publish_delay=MQTT_PUB_DELAY # Pass delay to handler
    )
    mqtt_handler.connect() # Connect MQTT

    # --- Subscribe to HA State Topic (if enabled) ---
    # Removed HA automation state subscription

    logger.info("Initializing Timer...")
    timer = Timer(update_callback=update_display)

    logger.info("Initializing Telegram Bot Application...")
    telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()

    # --- Link Timer and Job Queue ---
    timer.set_job_queue(telegram_app.job_queue)

    # --- Register Telegram Handlers ---
    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CommandHandler("help", help_command))
    telegram_app.add_handler(CommandHandler("sw", sw_start_command)) # Stopwatch start
    telegram_app.add_handler(CommandHandler("timer", timer_start_command))
    telegram_app.add_handler(CommandHandler("stop", stop_command))
    telegram_app.add_handler(CommandHandler("reset", reset_command))
    telegram_app.add_handler(CommandHandler("split", split_command))
    telegram_app.add_handler(CommandHandler("add", add_time_command))
    telegram_app.add_handler(CommandHandler("sub", sub_time_command))
    telegram_app.add_handler(CommandHandler("status", status_command))
    telegram_app.add_handler(CommandHandler("pw", power_cycle_command))
    telegram_app.add_handler(CommandHandler("quit", quit_command)) # Add quit command

    # Handler for unknown commands - must be last
    telegram_app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    # --- Setup Signal Handlers for Graceful Shutdown ---
    # Get the current event loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # 'RuntimeError: There is no current event loop...'
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    for sig in [signal.SIGINT, signal.SIGTERM]:
        loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(shutdown(s)))

    # --- Schedule Post-Connection Tasks (Discovery, Initial State) ---
    async def run_post_connection_tasks(status_callback_func, discovery_func): # Add discovery parameter
        # Add a delay to allow MQTT connection to establish
        # Adjust delay as needed based on network/broker speed
        connection_delay = 5
        logger.info(f"Waiting {connection_delay} seconds for MQTT connection before publishing discovery...")
        await asyncio.sleep(connection_delay)

        # Check if MQTT seems connected (basic check)
        if mqtt_handler and mqtt_handler.client.is_connected():
             logger.info("MQTT likely connected. Publishing initial states and discovery...")
             # Publish initial states first
             if timer:
                 await status_callback_func(timer.mode) # Use parameter
                 await update_display(timer._format_time(0)) # Publish initial time "00:00:00"
             # Publish discovery messages
             await discovery_func() # Use parameter
        else:
             logger.error("MQTT client not connected after delay. Skipping discovery publishing.")

    # Schedule the post-connection tasks to run in the background
    if HA_MQTT_DISCOVERY_ENABLED:
        # Pass the actual callback and discovery functions when creating the task
        loop.create_task(run_post_connection_tasks(_status_update_callback, publish_ha_discovery))

    # --- Run the Bot ---
    logger.info("Starting Telegram Bot Polling...")
    # Use run_until_complete on the polling task if not using run_polling directly
    # Or simply run polling which blocks until shutdown
    telegram_app.run_polling(allowed_updates=Update.ALL_TYPES)


    # Code here will run after the bot stops (e.g., after shutdown)
    logger.info("Bot has stopped.")


if __name__ == "__main__":
    main()

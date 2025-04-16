import logging
import os
import signal
import asyncio
from typing import Optional # Add this import
from dotenv import load_dotenv

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
HA_AUTOMATION_ENTITY_ID: Optional[str] = None
HA_AUTOMATION_COMMAND_TOPIC: Optional[str] = None
HA_SHELLY_SWITCH_ENTITY_ID: Optional[str] = None
HA_SHELLY_SWITCH_COMMAND_TOPIC: Optional[str] = None
ha_automation_last_state: Optional[str] = None # Store 'on' or 'off'
ha_automation_was_on_before_stopwatch: Optional[bool] = None # Store True/False if it was on
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
    "/pw - Power cycle the display controller"
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

# --- Home Assistant MQTT Message Handler ---
def _on_ha_automation_state(topic: str, payload: str):
    """Handles state updates from the HA automation."""
    global ha_automation_last_state
    state = payload.lower()
    if state in ['on', 'off']:
        if ha_automation_last_state != state:
            logger.info(f"HA Automation ({HA_AUTOMATION_ENTITY_ID}) state changed to: {state}")
            ha_automation_last_state = state
        else:
             logger.debug(f"HA Automation ({HA_AUTOMATION_ENTITY_ID}) state update received: {state} (no change)")
    else:
        logger.warning(f"Received unexpected state payload for HA Automation: {payload}")

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

    global initial_blanking_sent, ha_automation_was_on_before_stopwatch

    # Send initial blanking message on first authorized /sw start command
    if not initial_blanking_sent:
        await send_initial_blanking_message() # Await async call
        initial_blanking_sent = True

    # --- HA Integration: Turn OFF automation ---
    if HA_AUTOMATION_COMMAND_TOPIC and mqtt_handler:
        # Record current state *before* turning it off
        ha_automation_was_on_before_stopwatch = (ha_automation_last_state == 'on')
        logger.info(f"Stopwatch starting. HA Automation ({HA_AUTOMATION_ENTITY_ID}) was: {'ON' if ha_automation_was_on_before_stopwatch else 'OFF'}. Turning OFF.")
        await mqtt_handler.publish(HA_AUTOMATION_COMMAND_TOPIC, "OFF") # Await async publish
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

    if not HA_SHELLY_SWITCH_COMMAND_TOPIC:
        await update.message.reply_text("Power cycle feature not configured (Shelly switch entity ID missing).")
        logger.warning("Attempted power cycle, but HA_SHELLY_SWITCH_COMMAND_TOPIC is not set.")
        return

    if not mqtt_handler or not mqtt_handler.client.is_connected():
        await update.message.reply_text("Cannot power cycle: MQTT client not connected.")
        logger.warning("Attempted power cycle, but MQTT client is not connected.")
        return

    try:
        await update.message.reply_text("Attempting power cycle: Turning OFF...")
        logger.info(f"Sending OFF command to Shelly switch: {HA_SHELLY_SWITCH_COMMAND_TOPIC}")
        await mqtt_handler.publish(HA_SHELLY_SWITCH_COMMAND_TOPIC, "OFF") # Await async publish

        # The publish method now includes a delay, so this extra sleep might
        # make the total OFF time longer than 3 seconds (3s + publish_delay).
        # Consider if the 3s should include the publish delay or be on top of it.
        # Let's keep it simple for now: the total delay will be 3s + publish_delay.
        await asyncio.sleep(3) # Wait for 3 seconds

        await update.message.reply_text("Power cycle: Turning ON...")
        logger.info(f"Sending ON command to Shelly switch: {HA_SHELLY_SWITCH_COMMAND_TOPIC}")
        await mqtt_handler.publish(HA_SHELLY_SWITCH_COMMAND_TOPIC, "ON") # Await async publish

        # The final message might appear slightly delayed due to the publish delay after ON command.
        await update.message.reply_text("Power cycle sequence initiated.")
        logger.info("Power cycle sequence completed.")

    except Exception as e:
        logger.error(f"Error during power cycle sequence: {e}")
        await update.message.reply_text(f"An error occurred during power cycle: {e}")


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stops the current timer or stopwatch."""
    if not is_authorized(update):
        await unauthorized_reply(update)
        return

    global ha_automation_was_on_before_stopwatch
    was_stopwatch = timer and timer.mode == TimerMode.STOPWATCH # Check *before* stopping

    if timer:
        message = timer.stop() # This stops the timer/stopwatch

        # --- HA Integration: Restore automation state ---
        if was_stopwatch and HA_AUTOMATION_COMMAND_TOPIC and mqtt_handler:
            if ha_automation_was_on_before_stopwatch is True:
                logger.info(f"Stopwatch stopped. HA Automation ({HA_AUTOMATION_ENTITY_ID}) was ON before. Turning back ON.")
                await mqtt_handler.publish(HA_AUTOMATION_COMMAND_TOPIC, "ON") # Await async publish
            elif ha_automation_was_on_before_stopwatch is False:
                 logger.info(f"Stopwatch stopped. HA Automation ({HA_AUTOMATION_ENTITY_ID}) was OFF before. Leaving OFF.")
            else:
                 # State was unknown when stopwatch started
                 logger.warning(f"Stopwatch stopped. Previous HA Automation state ({HA_AUTOMATION_ENTITY_ID}) was unknown. Taking no action.")
            ha_automation_was_on_before_stopwatch = None # Reset tracker
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
    global HA_AUTOMATION_ENTITY_ID, HA_AUTOMATION_COMMAND_TOPIC, HA_SHELLY_SWITCH_ENTITY_ID, HA_SHELLY_SWITCH_COMMAND_TOPIC # Add HA globals

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
    # Load HA config
    HA_AUTOMATION_ENTITY_ID = os.getenv("HA_AUTOMATION_ENTITY_ID")
    HA_SHELLY_SWITCH_ENTITY_ID = os.getenv("HA_SHELLY_SWITCH_ENTITY_ID")
    HA_MQTT_DISCOVERY_PREFIX = os.getenv("HA_MQTT_DISCOVERY_PREFIX", "homeassistant") # Default prefix

    ha_automation_state_topic = None
    # Derive Automation topics
    if HA_AUTOMATION_ENTITY_ID:
        # Derive topics assuming standard HA structure: <prefix>/<component>/<node_id>/<object_id>/<suffix>
        # For automation: <prefix>/automation/<entity_id without domain>/state or /set
        entity_id_part = HA_AUTOMATION_ENTITY_ID.split('.')[-1] # Get 'timesplitters_2' from 'automation.timesplitters_2'
        HA_AUTOMATION_COMMAND_TOPIC = f"{HA_MQTT_DISCOVERY_PREFIX}/automation/{entity_id_part}/set"
        ha_automation_state_topic = f"{HA_MQTT_DISCOVERY_PREFIX}/automation/{entity_id_part}/state"
        logger.info(f"HA Integration Enabled for: {HA_AUTOMATION_ENTITY_ID}")
        logger.info(f"  Command Topic: {HA_AUTOMATION_COMMAND_TOPIC}")
        logger.info(f"  State Topic: {ha_automation_state_topic}")
    else:
        logger.info("HA Automation Integration Disabled (HA_AUTOMATION_ENTITY_ID not set).")

    # Derive Shelly Switch topics
    if HA_SHELLY_SWITCH_ENTITY_ID:
         # Derive topics assuming standard HA structure: <prefix>/<component>/<node_id>/<object_id>/<suffix>
         # For switch: <prefix>/switch/<entity_id without domain>/set
         entity_id_part = HA_SHELLY_SWITCH_ENTITY_ID.split('.')[-1]
         HA_SHELLY_SWITCH_COMMAND_TOPIC = f"{HA_MQTT_DISCOVERY_PREFIX}/switch/{entity_id_part}/set"
         logger.info(f"HA Shelly Switch Control Enabled for: {HA_SHELLY_SWITCH_ENTITY_ID}")
         logger.info(f"  Command Topic: {HA_SHELLY_SWITCH_COMMAND_TOPIC}")
         # We don't need to subscribe to the Shelly state for this command
    else:
        logger.info("HA Shelly Switch Control Disabled (HA_SHELLY_SWITCH_ENTITY_ID not set).")


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
    if ha_automation_state_topic and mqtt_handler:
        mqtt_handler.subscribe(ha_automation_state_topic, _on_ha_automation_state)

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
    telegram_app.add_handler(CommandHandler("pw", power_cycle_command)) # Add power cycle command

    # Handler for unknown commands - must be last
    telegram_app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    # --- Setup Signal Handlers for Graceful Shutdown ---
    loop = asyncio.get_event_loop()
    for sig in [signal.SIGINT, signal.SIGTERM]:
        loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(shutdown(s)))

    # --- Run the Bot ---
    logger.info("Starting Telegram Bot Polling...")
    telegram_app.run_polling(allowed_updates=Update.ALL_TYPES)

    # Code here will run after the bot stops (e.g., after shutdown)
    logger.info("Bot has stopped.")


if __name__ == "__main__":
    main()

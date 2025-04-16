import logging
import os
import signal
import asyncio
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from mqtt_handler import MQTTHandler
from timer import Timer

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

# --- Constants ---
HELP_MESSAGE = (
    "SplitWatch Bot Started!\n"
    "Commands:\n"
    "/sw start - Start stopwatch\n"
    "/timer MM:SS [or SS] - Start timer\n"
    "/stop - Stop current timer/stopwatch\n"
    "/reset - Reset timer/stopwatch\n"
    "/split - Record split time (stopwatch)\n"
    "/add MM:SS [or SS] - Add time (timer)\n"
    "/sub MM:SS [or SS] - Subtract time (timer)\n"
    "/status - Show current status\n"
    "/help - Show this help message"
)

# --- MQTT Update Callback ---
def update_display(formatted_time: str):
    """Callback function passed to the Timer to update MQTT."""
    if mqtt_handler:
        # Add two spaces prefix for the split-flap display formatting
        payload = f"  {formatted_time}"
        logger.debug(f"Sending to MQTT: '{payload}'") # Log the actual payload being sent
        mqtt_handler.publish(payload)
    else:
        logger.warning("MQTT handler not initialized, cannot update display.")

# --- Telegram Command Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends explanation on how to use the bot."""
    # Also update the help message constant if commands change
    await update.message.reply_text(HELP_MESSAGE)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends the help message."""
    await update.message.reply_text(HELP_MESSAGE)

async def sw_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts the stopwatch."""
    if timer:
        message = timer.start_stopwatch()
        await update.message.reply_text(message)
    else:
        await update.message.reply_text("Timer not initialized.")

async def timer_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts a countdown timer."""
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


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stops the current timer or stopwatch."""
    if timer:
        message = timer.stop()
        await update.message.reply_text(message)
    else:
        await update.message.reply_text("Timer not initialized.")

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resets the timer or stopwatch."""
    if timer:
        message = timer.reset()
        await update.message.reply_text(message)
    else:
        await update.message.reply_text("Timer not initialized.")

async def split_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Records a split time."""
    if timer:
        message = timer.split()
        await update.message.reply_text(message)
    else:
        await update.message.reply_text("Timer not initialized.")

async def add_time_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Adds time to the timer."""
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
    if timer:
        message = timer.get_status()
        await update.message.reply_text(message)
    else:
        await update.message.reply_text("Timer not initialized.")

async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles unknown commands."""
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
    global mqtt_handler, timer, telegram_app

    # --- Load Environment Variables ---
    load_dotenv()
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    MQTT_BROKER = os.getenv("MQTT_BROKER_ADDRESS")
    MQTT_PORT = os.getenv("MQTT_BROKER_PORT", 1883) # Default port
    MQTT_USER = os.getenv("MQTT_USERNAME")
    MQTT_PASS = os.getenv("MQTT_PASSWORD")
    MQTT_TOPIC = os.getenv("MQTT_TOPIC")

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
    mqtt_handler = MQTTHandler(MQTT_BROKER, MQTT_PORT, MQTT_TOPIC, MQTT_USER, MQTT_PASS)
    mqtt_handler.connect() # Connect MQTT

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

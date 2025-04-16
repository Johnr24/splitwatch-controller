import time
import logging
from enum import Enum, auto
from typing import Callable, Optional

# Use the same logger as the main bot for consistency
logger = logging.getLogger(__name__)

class TimerMode(Enum):
    STOPPED = auto()
    STOPWATCH = auto()
    TIMER = auto()
    PAUSED = auto() # Add PAUSED state

class Timer:
    """Manages stopwatch and timer functionality."""

    def __init__(self,
                 update_callback: Optional[Callable[[str], None]] = None,
                 status_update_callback: Optional[Callable[[TimerMode], None]] = None):
        """
        Initializes the Timer.

        Args:
            update_callback (callable, optional): Async function called each second with the formatted time string.
                                                  Signature: async callback(formatted_time: str). Defaults to None.
            status_update_callback (callable, optional): Async function called when the timer mode changes.
                                                         Signature: async callback(new_mode: TimerMode). Defaults to None.
        """
        self.mode = TimerMode.STOPPED
        self.start_time = 0.0
        self.elapsed_time = 0.0
        self.target_duration = 0.0 # For timer mode
        self.paused_time = 0.0 # Time elapsed when paused
        self.update_callback = update_callback
        self.status_update_callback = status_update_callback # Store the new callback
        self.job_queue = None # Will be set by the bot to schedule ticks (from telegram.ext.JobQueue)
        self.timer_job = None # Holds the Job instance from JobQueue

    async def _publish_status_update(self, new_mode: TimerMode):
        """Safely calls the status update callback if it exists."""
        if self.status_update_callback:
            try:
                # status_update_callback is expected to be async
                await self.status_update_callback(new_mode)
            except Exception as e:
                logger.error(f"Error in async status_update_callback: {e}")

    def _format_time(self, seconds: float) -> str:
        """Formats seconds into HH:MM:SS."""
        if seconds < 0:
            seconds = 0
        # Ensure we handle potential large numbers gracefully, though unlikely for a timer
        total_seconds = int(round(seconds))
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        secs = total_seconds % 60
        # Format as HH:MM:SS
        return f"{hours:02}:{minutes:02}:{secs:02}"

    async def _tick(self, context) -> None: # context is required by JobQueue
        """Called periodically (e.g., every second) by the JobQueue to update the timer/stopwatch."""
        if self.mode == TimerMode.STOPPED:
            # Ensure job is stopped if mode is stopped unexpectedly
            self._stop_timer_job()
            return

        current_time = time.time()
        display_time = 0.0

        if self.mode == TimerMode.STOPWATCH:
            # Calculate elapsed time since start, adding any previously paused duration
            self.elapsed_time = self.paused_time + (current_time - self.start_time)
            display_time = self.elapsed_time
        elif self.mode == TimerMode.TIMER:
            # Calculate elapsed time since start, adding any previously paused duration
            self.elapsed_time = self.paused_time + (current_time - self.start_time)
            remaining_time = self.target_duration - self.elapsed_time
            display_time = remaining_time
            if remaining_time <= 0:
                logger.info("Timer finished!")
                self.stop() # Stop the timer when it reaches zero
                display_time = 0 # Show 00:00:00 when done
        else:
            # Should not happen in normal operation
            logger.error(f"Timer in unexpected mode during tick: {self.mode}")
            self._stop_timer_job()
            return

        formatted_time = self._format_time(display_time)
        logger.debug(f"Tick: Mode={self.mode.name}, Elapsed={self.elapsed_time:.2f}, Display={formatted_time}")

        if self.update_callback:
            try:
                # update_callback (update_display in bot.py) is now async
                await self.update_callback(formatted_time)
            except Exception as e:
                logger.error(f"Error in async update_callback: {e}")

        # If timer finished during this tick, self.stop() was called,
        # which already called _stop_timer_job, so no need to check again.

    def _start_timer_job(self) -> None:
        """Starts the periodic tick job using the JobQueue."""
        if not self.job_queue:
            logger.error("JobQueue not set. Cannot start timer job.")
            return
        if self.timer_job:
            logger.warning("Timer job already running or not cleaned up properly.")
            # Attempt to remove existing job before starting a new one
            self._stop_timer_job()

        # Run immediately and then every second
        # Use the async _tick method directly
        self.timer_job = self.job_queue.run_repeating(
            self._tick, interval=1, first=0.1, name="timer_tick" # Start slightly delayed
        )
        if self.timer_job:
            logger.info(f"Timer job scheduled: {self.timer_job.name}")
        else:
             logger.error("Failed to schedule timer job.")


    def _stop_timer_job(self) -> None:
        """Stops the periodic tick job."""
        if self.timer_job:
            logger.debug(f"Attempting to remove timer job: {self.timer_job.name}")
            self.timer_job.schedule_removal()
            self.timer_job = None
            logger.info("Timer job stopped.")
        else:
            logger.debug("No active timer job to stop.")


    def set_job_queue(self, job_queue) -> None:
        """Sets the Telegram JobQueue instance."""
        logger.info("JobQueue instance received.")
        self.job_queue = job_queue

    async def start_stopwatch(self) -> str: # Add async
        """Starts or resumes the stopwatch."""
        if self.mode == TimerMode.STOPWATCH or self.mode == TimerMode.TIMER:
             logger.warning(f"Cannot start/resume stopwatch, already running in mode: {self.mode.name}")
             return f"Already running as {self.mode.name}. Use /stop first."

        if self.mode == TimerMode.PAUSED:
            # Resume logic
            new_mode = TimerMode.STOPWATCH
            self.mode = new_mode
            self.start_time = time.time() # Reset start time for current segment
            # Keep self.paused_time
            self._start_timer_job()
            logger.info(f"Stopwatch resumed. Current elapsed: {self.paused_time:.2f}s")
            # Update display with current paused time
            if self.update_callback:
                 # This callback is now async
                 await self.update_callback(self._format_time(self.paused_time))
            # Publish status update
            await self._publish_status_update(new_mode)
            return f"Stopwatch resumed at {self._format_time(self.paused_time)}."
        elif self.mode == TimerMode.STOPPED:
             # Start fresh logic
             new_mode = TimerMode.STOPWATCH
             self.mode = new_mode
             self.start_time = time.time()
             self.paused_time = 0.0 # Reset paused time
             self.elapsed_time = 0.0
             self.target_duration = 0.0 # Not used in stopwatch mode
             self._start_timer_job()
             logger.info("Stopwatch started.")
             # Initial display update
             if self.update_callback:
                 await self.update_callback(self._format_time(0)) # Await async callback
             # Publish status update
             await self._publish_status_update(new_mode)
             return "Stopwatch started."
        else:
             # Should not happen, but handle defensively
             logger.error(f"Stopwatch start called in unexpected state: {self.mode.name}")
             return "Cannot start stopwatch due to unexpected state."


    async def start_timer(self, duration_seconds: float) -> str: # Add async
        """Starts a *new* countdown timer. Requires timer to be stopped."""
        if self.mode != TimerMode.STOPPED:
            logger.warning(f"Cannot start timer, current mode is: {self.mode.name}. Use /reset first.")
            return f"Please /reset before starting a new timer. Current mode: {self.mode.name}."
        if duration_seconds <= 0:
            return "Timer duration must be positive."

        # Start fresh logic (only runs if mode was STOPPED)
        new_mode = TimerMode.TIMER
        self.mode = new_mode
        self.target_duration = duration_seconds
        self.start_time = time.time()
        self.paused_time = 0.0 # Reset paused time
        self.elapsed_time = 0.0
        self._start_timer_job()
        logger.info(f"Timer started for {duration_seconds} seconds.")
         # Initial display update
        if self.update_callback:
            await self.update_callback(self._format_time(self.target_duration)) # Await async callback
        # Publish status update
        await self._publish_status_update(new_mode)
        return f"Timer started for {self._format_time(self.target_duration)}."

    async def stop(self) -> str: # This now functions as PAUSE and needs to be async
        """Pauses the current stopwatch or timer and holds the current time."""
        if self.mode == TimerMode.STOPPED or self.mode == TimerMode.PAUSED:
            logger.info(f"Timer/Stopwatch is already {self.mode.name}.")
            return f"Already {self.mode.name}."

        # Calculate elapsed time up to the point of pausing
        current_time = time.time()
        self.elapsed_time = self.paused_time + (current_time - self.start_time)
        self.paused_time = self.elapsed_time # Store the total elapsed time when stopped

        # Stop the job *after* calculating final time
        self._stop_timer_job()

        original_mode = self.mode # Store original running mode (STOPWATCH or TIMER)
        new_mode = TimerMode.PAUSED
        self.mode = new_mode # Set mode to PAUSED

        logger.info(f"{original_mode.name} paused. Current elapsed time: {self.elapsed_time:.2f}")

        # Publish status update *before* returning
        await self._publish_status_update(new_mode)

        # Determine the time to display upon pausing
        if original_mode == TimerMode.STOPWATCH:
            display_value = self.elapsed_time
            return f"Stopwatch paused at {self._format_time(display_value)}."
        elif original_mode == TimerMode.TIMER:
            remaining_time = max(0, self.target_duration - self.elapsed_time)
            # Update display one last time after pausing
            if self.update_callback:
                await self.update_callback(self._format_time(remaining_time)) # Await async callback
            return f"Timer paused with {self._format_time(remaining_time)} remaining."
        else:
            # Should not happen
             return "Stopped."


    async def reset(self) -> str: # Needs to be async
        """Resets the timer/stopwatch to zero and stopped state."""
        was_running = self.mode != TimerMode.STOPPED
        self._stop_timer_job() # Stop job if running

        new_mode = TimerMode.STOPPED
        self.mode = new_mode
        self.start_time = 0.0
        self.elapsed_time = 0.0
        self.target_duration = 0.0
        self.paused_time = 0.0
        logger.info("Timer/Stopwatch reset.")
        # Update display to 00:00:00
        if self.update_callback:
            await self.update_callback(self._format_time(0)) # Await async callback
        # Publish status update
        await self._publish_status_update(new_mode)
        return "Timer/Stopwatch reset."

    # TODO: Implement split persistence if needed (e.g., store splits in a list)
    def split(self) -> str:
        """Records a split time (stopwatch mode only)."""
        if self.mode != TimerMode.STOPWATCH:
            return "Split function only available in stopwatch mode."
        # Calculate current elapsed time accurately at the moment of split
        current_split_time = self.paused_time + (time.time() - self.start_time)
        formatted_split = self._format_time(current_split_time)
        logger.info(f"Split recorded: {formatted_split}")
        # Just return the current split time for now
        return f"Split: {formatted_split}"

    async def add_time(self, seconds_to_add: float) -> str: # Add async
        """Adds time to the timer's target duration (timer mode only)."""
        if self.mode != TimerMode.TIMER:
            return "Can only add time in timer mode."
        if seconds_to_add <= 0:
            return "Please provide a positive number of seconds to add."

        self.target_duration += seconds_to_add
        logger.info(f"Added {seconds_to_add}s to timer. New target: {self.target_duration}s")

        # Update display immediately if timer is running or paused
        remaining_time = max(0, self.target_duration - self.elapsed_time)
        if self.update_callback:
            await self.update_callback(self._format_time(remaining_time)) # Await async callback

        return f"Added {self._format_time(seconds_to_add)}. New target duration: {self._format_time(self.target_duration)}. Remaining: {self._format_time(remaining_time)}"

    async def subtract_time(self, seconds_to_subtract: float) -> str: # Needs to be async
        """Subtracts time from the timer's target duration (timer mode only)."""
        if self.mode != TimerMode.TIMER:
            return "Can only subtract time in timer mode."
        if seconds_to_subtract <= 0:
            return "Please provide a positive number of seconds to subtract."

        self.target_duration -= seconds_to_subtract
        if self.target_duration < 0:
            self.target_duration = 0 # Prevent negative duration

        logger.info(f"Subtracted {seconds_to_subtract}s from timer. New target: {self.target_duration}s")

        # Update display immediately if timer is running or paused
        remaining_time = max(0, self.target_duration - self.elapsed_time)
        if self.update_callback:
            await self.update_callback(self._format_time(remaining_time)) # Await async callback

        # Check if subtracting time caused the timer to finish
        if remaining_time <= 0 and self.mode == TimerMode.TIMER:
             logger.info("Timer finished due to time subtraction.")
             await self.stop() # Stop the timer properly (now async)
             return f"Subtracted {self._format_time(seconds_to_subtract)}. Timer finished."
        else:
             return f"Subtracted {self._format_time(seconds_to_subtract)}. New target duration: {self._format_time(self.target_duration)}. Remaining: {self._format_time(remaining_time)}"


    def get_status(self) -> str:
        """Returns the current status and time."""
        current_mode = self.mode.name
        display_value = 0.0

        # Calculate the current time accurately for status display
        if self.mode == TimerMode.STOPWATCH:
             # If running, calculate current elapsed time
             current_elapsed = self.paused_time + (time.time() - self.start_time)
             display_value = current_elapsed
        elif self.mode == TimerMode.TIMER:
             # If running, calculate current elapsed time
             current_elapsed = self.paused_time + (time.time() - self.start_time)
             remaining_time = max(0, self.target_duration - current_elapsed)
             display_value = remaining_time
        elif self.mode == TimerMode.STOPPED:
             # If stopped, show the time it was stopped at (stored in paused_time)
             # Need to know if it *was* a timer or stopwatch to display correctly
             # For simplicity, let's just show elapsed time when stopped
             display_value = self.paused_time # Shows elapsed time when stopped

        current_time_str = self._format_time(display_value)

        status = f"Mode: {current_mode}\nTime: {current_time_str}"
        if self.mode == TimerMode.TIMER or (self.mode == TimerMode.STOPPED and self.target_duration > 0): # Show target if it was a timer
            status += f"\nTarget: {self._format_time(self.target_duration)}"

        return status

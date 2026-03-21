# progress_logger.py
import collections
from queue import Queue

# A thread-safe dictionary of queues to hold progress messages for different SSE streams
progress_queues = collections.defaultdict(Queue)

def get_queue(channel: str = "default") -> Queue:
    """Gets the queue for a specific channel."""
    return progress_queues[channel]

def log_progress(message: str, channel: str = "default"):
    """
    Logs a message to the console and adds it to the progress queue
    for real-time streaming to the frontend.
    """
    # Print to the backend console for debugging
    print(f"INFO [{channel}]: {message}")
    
    # Put the message into the channel's queue
    get_queue(channel).put(message)

def log_final_message(message: str = "__END__", channel: str = "default"):
    """
    Puts a special sentinel value into the queue to signal the end of the process.
    """
    get_queue(channel).put(message)
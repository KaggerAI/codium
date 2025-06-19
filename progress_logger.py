# progress_logger.py
from queue import Queue

# A thread-safe queue to hold progress messages for the SSE stream
progress_queue = Queue()

def log_progress(message: str):
    """
    Logs a message to the console and adds it to the progress queue
    for real-time streaming to the frontend.
    """
    # Print to the backend console for debugging (as before)
    print(f"INFO: {message}")
    
    # Put the message into the queue for the client
    progress_queue.put(message)

def log_final_message(message: str = "__END__"):
    """
    Puts a special sentinel value into the queue to signal the end of the process.
    """
    progress_queue.put(message)
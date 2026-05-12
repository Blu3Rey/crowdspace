import logging
from pathlib import Path

def configure_logging(save_path: str, min_level: str = "DEBUG", log_name: str = "Script", program_name: str = "MyApp"):
    """
    Configures the base logger. Only call this once at the entry point of program.

    Args:
        save_path: The directory where log files should be saved.
        min_level: The minimum logging level to capture (e.g., 'DEBUG', 'INFO').
        log_name: The name of the log file.
        program_name: The base name of the program running logger.
    """
    # 1. Create directory safely using pathlib
    log_path = Path(save_path)
    log_path.mkdir(parents=True, exist_ok=True)

    # 2. Get the base logger for program
    logger = logging.getLogger(program_name)

    # Passing the string directly to setLevel
    logger.setLevel(min_level.upper())

    # 3. Add handlers if they don't exist to prevent duplicate logging
    if not logger.handlers:
        # Define a consistent format for the logs
        formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

        # File Handler (Always specify utf-8 encoding to prevent Unicode errors)
        file_handler = logging.FileHandler(log_path / f"{log_name}.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        # Stream/Console Handler
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
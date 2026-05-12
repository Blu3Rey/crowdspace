import os
import argparse
import logging
from dotenv import load_dotenv

from modules.logger import configure_logging
from modules.utils import single_select_input

import features.ble_chat.main

# Grab a loger named after this specific file (__name__).
logger = logging.getLogger(f"BLE_DEV.{__name__}")

features = {
    "ble_chat": features.ble_chat.main.main
}

def main(argv: list[str] | None = None):
    # Initialize the argument parser
    parser = argparse.ArgumentParser(description="BLE Feature Testing Harness")
    # Add optional flags and arguments

    # Add optional flags and arguments for direct feature selection
    parser.add_argument(
        "--feature", "-f",
        choices=features.keys(),
        help="Select a ble feature to run"
    )

    # Add optional flags and arguments for environment variables
    parser.add_argument(
        "--env", "-e",
        type=str,
        default="../env/.env.example",
        help="Path to the .env file"
    )

    # Add optional flags and arguments for program logger
    parser.add_argument(
        "--log", "-l",
        type=str.upper,
        choices=logging._levelToName.values(),
        default="INFO",
        help="Minimum level for logging"
    )

    # Parse the arguments
    args = parser.parse_args(argv)

    # Configure Environment Variables
    load_dotenv(args.env)

    # Configure Program Logger
    configure_logging(save_path="./logs", min_level=args.log, program_name="BLE_DEV")
    logger.info("Configured Program Logger")

    try:
        selection = args.feature
        if not selection:
            while True:
                selection = single_select_input("Select a feature", [*features.keys(), "Exit Program"])
                if selection == "Exit Program":
                    logger.info("Exiting program via menu selection.")
                    break

                features.get(selection)(argv=[])
        else:
            features.get(selection)(argv=[])
    except Exception as e:
        logger.critical(f"Fatal error: {e}")

if __name__ == "__main__":
    main()
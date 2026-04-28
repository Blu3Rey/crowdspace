import os
import asyncio
import argparse
import logging
from dotenv import load_dotenv

from modules.program_logger import configure_logging
from modules.utils import single_select_input

from peripheral import run_peripheral_mode
from central import run_central_mode

# Grab a loger named after this specific file (__name__).
logger = logging.getLogger(f"BLE_Test_Suite.{__name__}")

test_cases = {
    "Host-Mode": run_peripheral_mode,
    "Join-Mode": run_central_mode
}

def main(argv: list[str] | None = None):
    # Initialize the argument parser
    parser = argparse.ArgumentParser(description="Parser handles command-line flags")
    # Add optional flags and arguments
    parser.add_argument(
        "--case", "-c",
        choices=test_cases.keys(),
        help="Select a test case to run"
    )
    # Parse the arguments
    args = parser.parse_args(argv)

    chosen_case = args.case
    try:
        if chosen_case:
            case_fn = test_cases.get(chosen_case)
            asyncio.run(case_fn())
        else:
            while True:
                chosen_action = single_select_input("Select an action", [*test_cases.keys(), "Exit Program"])
                if chosen_action == "Exit Program":
                    break

                action_fn = test_cases.get(chosen_action)
                asyncio.run(action_fn())
    except Exception as e:
        logger.critical(f"Fatal error: {e}")

if __name__ == "__main__":
    # Configure Program Logger
    configure_logging(save_path="./logs", min_level="INFO", program_name="BLE_Test_Suite")
    logger.info("Configured Program Logger")

    # Configure Environment Variables
    load_dotenv("../env/.env.dev")

    main()
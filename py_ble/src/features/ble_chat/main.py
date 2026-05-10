import asyncio
import argparse
import logging

from .orchestrator import BLEMessenger


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        description="BLE Peer-to-Peer Chat — run the same script on two machines",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python ble_messenger.py --name Alice
  python ble_messenger.py --name Bob --debug
        """,
    )
    parser.add_argument(
        "--name", default=None,
        help="Your display name (prompted interactively if omitted)"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable verbose BLE stack logging (bleak / bless)"
    )
    args = parser.parse_args(argv)
 
    if args.debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        )
    else:
        logging.disable(logging.CRITICAL)
 
    if args.name:
        name = args.name.strip() or "Anonymous"
    else:
        try:
            name = input("Your display name: ").strip() or "Anonymous"
        except EOFError:
            name = "Anonymous"
 
    try:
        asyncio.run(BLEMessenger(name).run())
    except KeyboardInterrupt:
        pass
 
 
if __name__ == "__main__":
    main()

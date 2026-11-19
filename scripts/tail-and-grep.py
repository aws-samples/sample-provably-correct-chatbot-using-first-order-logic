"""
A command-line tool to "tail -f" a file and 
grep for a pattern. Useful for monitoring log files.
"""

import argparse, re, time, sys
from typing import Generator

def follow_file(filepath: str, interval: float) -> Generator[str, None, None]:
    """
    Continuously reads and yields new lines from a file as they are appended.
    """
    with open(filepath, "r", encoding="utf-8") as f:
        # Go to the end of the file initially
        f.seek(0, 2)
        while True:
            line = f.readline()
            if not line:
                # No new line, wait a bit and try again
                time.sleep(interval)
                continue
            yield line.strip()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Tail a file and grep for a pattern."
    )
    parser.add_argument("file", type=str,
        help="Path to the log file to monitor."
    )
    parser.add_argument("pattern", type=str,
        help="Regex pattern to grep for."
    )
    parser.add_argument(
        "--interval", type=float, default=0.1,
        help="Interval in seconds between checks."
    )

    args = parser.parse_args()
    log_file_path: str = args.file
    pattern = re.compile(args.pattern)
    interval: float = args.interval

    try:
        for line in follow_file(log_file_path, interval):
            if pattern.search(line):
                print(line)
    except KeyboardInterrupt:
        print("\nExiting...")
        sys.exit(0)
    except Exception as ex:
        print(f"Error: {ex}")
        sys.exit(1)

import subprocess
import sys

BASE_URL = "https://raw.githubusercontent.com/c4rma/Quantasset/refs/heads/main"


def main():
    if len(sys.argv) != 2:
        print("Usage: grabber.py [FILENAME]")
        sys.exit(1)

    filename = sys.argv[1].strip()

    if not filename or not filename.replace("_", "").replace("-", "").isalnum():
        print("Invalid filename. Use only letters, numbers, hyphens, and underscores.")
        sys.exit(1)

    url = f"{BASE_URL}/{filename}.py"

    result = subprocess.run(["wget", url], capture_output=False)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()

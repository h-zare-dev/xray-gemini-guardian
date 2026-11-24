"""Run as python -m guardian inside the Docker container."""
import logging
from .bot import GuardianBot


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    GuardianBot().run()


if __name__ == "__main__":
    main()

"""Run the bot in the background: restart it after crashes, stop when it exits cleanly, log to bot.log.

Used by the settings app and by start-at-login on Windows, macOS and Linux.
"""

import sys

import botctl

if __name__ == "__main__":
    sys.exit(botctl.launcher_main())

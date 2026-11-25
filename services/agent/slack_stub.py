"""Local Slack bot over Socket Mode, for development only.

Production runs inside the Cloud Run API over the HTTP Events API (cypress/slack.py). Slack sends
events to one place: Socket Mode on = this script, Socket Mode off = the Request URL. Use this
only with Socket Mode turned on in the Slack app settings.

Run:  CYPRESS_ENV=dev .venv/bin/python slack_stub.py
"""

import logging
import os

from google.cloud import secretmanager

PROJECT = os.getenv("GCP_PROJECT", "lhca-cypress")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def secret(name: str) -> str:
    path = f"projects/{PROJECT}/secrets/{name}/versions/latest"
    return secretmanager.SecretManagerServiceClient().access_secret_version(name=path).payload.data.decode().strip()


os.environ.setdefault("SLACK_BOT_TOKEN", secret("slack-bot-token"))
os.environ.setdefault("SLACK_SIGNING_SECRET", secret("slack-signing-secret"))

from slack_bolt.adapter.socket_mode import SocketModeHandler  # noqa: E402

from cypress import slack  # noqa: E402
from cypress.db import pool  # noqa: E402

if __name__ == "__main__":
    pool.open()
    logging.info("Cypress Slack bot (Socket Mode) starting as %s", slack.bot_user_id())
    SocketModeHandler(slack.bolt(), secret("slack-app-token")).start()

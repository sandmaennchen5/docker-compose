#!/usr/bin/env python3
"""
Configuration loader for Mail Gateway
"""

from __future__ import annotations

from pathlib import Path
import yaml


CONFIG_FILE = Path("/config/config.yaml")
ACCOUNTS_FILE = Path("/config/accounts.yaml")


class ConfigError(Exception):
    """Raised when configuration is invalid."""


class Config:

    def __init__(self):

        self.config = {}
        self.accounts = []

    def load(self):

        self.config = self._load_yaml(CONFIG_FILE)
        account_cfg = self._load_yaml(ACCOUNTS_FILE)

        if not isinstance(account_cfg, dict):
            raise ConfigError("accounts.yaml must contain a dictionary.")

        self.accounts = account_cfg.get("accounts", [])

        self.validate()

        return self

    def _load_yaml(self, filename: Path):

        if not filename.exists():
            raise ConfigError(f"{filename} does not exist.")

        with filename.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if data is None:
            data = {}

        return data

    def validate(self):

        if "smtp" not in self.config:
            raise ConfigError("Missing smtp section in config.yaml.")

        smtp = self.config["smtp"]

        required = [
            "host",
            "port"
        ]

        for key in required:
            if key not in smtp:
                raise ConfigError(f"Missing smtp.{key}")

        if len(self.accounts) == 0:
            raise ConfigError("No accounts configured.")

        names = set()

        for account in self.accounts:

            required = [
                "name",
                "protocol",
                "host",
                "port",
                "username",
                "password"
            ]

            for key in required:
                if key not in account:
                    raise ConfigError(
                        f"Account '{account.get('name','?')}' missing '{key}'."
                    )

            protocol = account["protocol"].lower()

            if protocol not in (
                "imap",
                "imaps",
                "pop3",
                "pop3s"
            ):
                raise ConfigError(
                    f"Unsupported protocol '{protocol}'"
                )

            if account["name"] in names:
                raise ConfigError(
                    f"Duplicate account name '{account['name']}'"
                )

            names.add(account["name"])

    @property
    def smtp(self):
        return self.config["smtp"]

    @property
    def poll_interval(self):
        return self.config.get("poll_interval", 60)

    @property
    def web_port(self):
        return self.config.get("web", {}).get("port", 8080)
        
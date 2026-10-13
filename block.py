from __future__ import annotations

import configparser
import logging
import os
import time
from dataclasses import dataclass


CONFIG_FILE = "config.ini"
SCAN_INTERVAL_SECONDS = 60
STATUS_KEYS = ["name", "files", "file_priorities"]

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised when config.ini is missing or incomplete."""


@dataclass
class BlockConfig:
    host: str
    port: int
    user: str
    password: str
    forbidden_extensions: list[str]
    unwanted_extensions: list[str]
    delete_downloaded_data: bool = False
    scan_interval_seconds: int = SCAN_INTERVAL_SECONDS


def decode_rpc_value(value, default=""):
    if value is None:
        return default
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value)


def rpc_get(mapping, key, default=None):
    if key in mapping:
        return mapping[key]
    byte_key = key.encode("utf-8")
    if byte_key in mapping:
        return mapping[byte_key]
    return default


def normalize_extensions(configured_extensions):
    return [
        extension.strip().lower()
        for extension in configured_extensions.split(",")
        if extension.strip()
    ]


def load_config(path=CONFIG_FILE):
    if not os.path.exists(path):
        raise ConfigError(
            f"Missing {path}. Copy config-sample.ini to {path} and update the values."
        )

    parser = configparser.ConfigParser()
    parser.read(path)

    required_settings = {
        "login": ["host", "port", "user", "password"],
        "lists": ["forbidden", "unwanted"],
    }
    missing = []
    for section, keys in required_settings.items():
        if not parser.has_section(section):
            missing.append(f"[{section}]")
            continue
        for key in keys:
            if not parser.has_option(section, key):
                missing.append(f"[{section}] {key}")

    if missing:
        raise ConfigError(f"Missing required configuration: {', '.join(missing)}")

    try:
        port = parser.getint("login", "port")
    except ValueError as error:
        raise ConfigError("[login] port must be a number.") from error

    if port < 1 or port > 65535:
        raise ConfigError("[login] port must be between 1 and 65535.")

    try:
        delete_downloaded_data = parser.getboolean(
            "behavior", "delete_downloaded_data", fallback=False
        )
    except ValueError as error:
        raise ConfigError("[behavior] delete_downloaded_data must be true or false.") from error

    host = parser.get("login", "host").strip()
    user = parser.get("login", "user").strip()
    password = parser.get("login", "password")

    if not host:
        raise ConfigError("[login] host must not be empty.")
    if not user:
        raise ConfigError("[login] user must not be empty.")
    if not password:
        raise ConfigError("[login] password must not be empty.")

    return BlockConfig(
        host=host,
        port=port,
        user=user,
        password=password,
        forbidden_extensions=normalize_extensions(parser.get("lists", "forbidden")),
        unwanted_extensions=normalize_extensions(parser.get("lists", "unwanted")),
        delete_downloaded_data=delete_downloaded_data,
    )


def create_deluge_client(settings):
    from deluge_client import DelugeRPCClient

    client = DelugeRPCClient(
        settings.host,
        settings.port,
        settings.user,
        settings.password,
    )
    client.connect()
    return client


class TorrentBlocker:
    def __init__(self, client, forbidden_extensions, unwanted_extensions, delete_data):
        self.client = client
        self.forbidden_extensions = forbidden_extensions
        self.unwanted_extensions = unwanted_extensions
        self.delete_data = delete_data
        self.checked_torrent_ids = set()

    def scan_once(self):
        try:
            torrents = self.client.call(
                "core.get_torrents_status", {}, STATUS_KEYS
            )
        except Exception:
            logger.exception("Unable to fetch torrent status from Deluge.")
            return False

        for raw_torrent_id, torrent_data in torrents.items():
            torrent_id = decode_rpc_value(raw_torrent_id)
            if torrent_id in self.checked_torrent_ids:
                continue

            try:
                if self.process_torrent(torrent_id, torrent_data):
                    self.checked_torrent_ids.add(torrent_id)
            except Exception:
                torrent_name = decode_rpc_value(
                    rpc_get(torrent_data, "name", "Unknown Name"),
                    default="Unknown Name",
                )
                logger.exception(
                    "Unable to process torrent '%s' (ID: %s). It will be retried.",
                    torrent_name,
                    torrent_id,
                )

        return True

    def process_torrent(self, torrent_id, torrent_data):
        torrent_name = decode_rpc_value(
            rpc_get(torrent_data, "name", "Unknown Name"), default="Unknown Name"
        )
        files = rpc_get(torrent_data, "files", None)
        file_priorities = rpc_get(torrent_data, "file_priorities", None)

        logger.info("Validating %s", torrent_name)

        if not files:
            logger.warning(
                "No files found for torrent '%s' (ID: %s); will retry later.",
                torrent_name,
                torrent_id,
            )
            return False
        if file_priorities is None:
            logger.warning(
                "No file priorities found for torrent '%s' (ID: %s); will retry later.",
                torrent_name,
                torrent_id,
            )
            return False
        if len(files) != len(file_priorities):
            logger.error(
                "File count (%s) and priority count (%s) differ for torrent '%s' "
                "(ID: %s); will retry later.",
                len(files),
                len(file_priorities),
                torrent_name,
                torrent_id,
            )
            return False

        file_names = [
            decode_rpc_value(rpc_get(file_info, "path", ""), default="")
            for file_info in files
        ]

        # Forbidden files win even when Deluge already has them set to Skip.
        for file_name in file_names:
            if self.has_extension(file_name, self.forbidden_extensions):
                logger.warning(
                    "Removing torrent '%s' due to forbidden file '%s'.",
                    torrent_name,
                    file_name,
                )
                self.client.call("core.remove_torrent", torrent_id, self.delete_data)
                return True

        new_priorities = []
        for file_name, file_priority in zip(file_names, file_priorities):
            if self.has_extension(file_name, self.unwanted_extensions):
                if file_priority != 0:
                    logger.info("Skipping %s", file_name)
                new_priorities.append(0)
            else:
                new_priorities.append(file_priority)

        self.client.call(
            "core.set_torrent_options",
            [torrent_id],
            {"file_priorities": new_priorities},
        )
        return True

    @staticmethod
    def has_extension(file_name, extensions):
        lower_file_name = file_name.lower()
        return any(lower_file_name.endswith(extension) for extension in extensions)


def run_forever(settings, client_factory=create_deluge_client):
    blocker = None
    while True:
        logger.info(
            "==> Beginning check [%s]...",
            time.strftime("%Y-%m-%d %H:%M:%S"),
        )

        if blocker is None:
            try:
                client = client_factory(settings)
                blocker = TorrentBlocker(
                    client,
                    settings.forbidden_extensions,
                    settings.unwanted_extensions,
                    settings.delete_downloaded_data,
                )
            except Exception:
                logger.exception("Unable to connect to Deluge. Will retry later.")
                time.sleep(settings.scan_interval_seconds)
                continue

            logger.info("Scanning..")
            logger.info(" -> %i forbidden extensions", len(settings.forbidden_extensions))
            logger.info(" -> %i unwanted extensions", len(settings.unwanted_extensions))

        if not blocker.scan_once():
            blocker = None

        time.sleep(settings.scan_interval_seconds)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        settings = load_config()
    except ConfigError as error:
        logger.error("%s", error)
        return 1

    run_forever(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

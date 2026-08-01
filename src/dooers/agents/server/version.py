"""Installed package version (connect ack telemetry)."""

from importlib.metadata import PackageNotFoundError, version

try:
    PACKAGE_VERSION = version("dooers-agents-server")
except PackageNotFoundError:
    PACKAGE_VERSION = "0.0.0"

SERVER_NAME = "dooers-agents-server"

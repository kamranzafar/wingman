# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""Exceptions with user-facing messages. The CLI prints these without a traceback."""


class WingmanError(Exception):
    """Base class: something the user can fix or should know about."""


class ConfigError(WingmanError):
    """wingman.toml / .env is missing something or has an invalid value."""


class NotReadyError(WingmanError):
    """A prerequisite step hasn't been run yet (e.g. no profile)."""


class LLMError(WingmanError):
    """A Claude API call failed or returned something unusable."""

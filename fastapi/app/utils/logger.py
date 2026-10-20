"""
Logging utilities
"""
import logging
import sys
from typing import Optional
from ..config import settings


def setup_logger(
    name: str,
    level: Optional[str] = None,
    format_string: Optional[str] = None
) -> logging.Logger:
    """
    Setup logger with custom configuration

    Args:
        name: Logger name
        level: Log level (default from settings)
        format_string: Log format (default from settings)

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)

    # Set log level
    log_level = level or settings.LOG_LEVEL
    logger.setLevel(getattr(logging, log_level.upper()))

    # Create console handler
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(getattr(logging, log_level.upper()))

    # Set format
    formatter = logging.Formatter(
        format_string or settings.LOG_FORMAT
    )
    handler.setFormatter(formatter)

    # Add handler
    if not logger.handlers:
        logger.addHandler(handler)

    return logger


def get_logger(name: str) -> logging.Logger:
    """
    Get or create logger

    Args:
        name: Logger name

    Returns:
        Logger instance
    """
    return logging.getLogger(name)
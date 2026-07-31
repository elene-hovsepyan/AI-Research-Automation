import logging
from loguru import logger

def setup_logging():
    logger.remove()
    logger.add(logging.StreamHandler(), format="{time} {level} {message}", level="INFO")
    return logger

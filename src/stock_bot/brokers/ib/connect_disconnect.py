# \src\stock_bot\brokers\ib\connect_disconnect.py
 
import logging
from ib_insync import IB
from stock_bot.config.settings import ib_settings

logger = logging.getLogger(__name__)
_ib = IB()


def connect_ib() -> IB:
    if not _ib.isConnected():
        logger.info(
            "Connecting to IBKR at %s:%s (client_id=%s)",
            ib_settings.host,
            ib_settings.port,
            ib_settings.client_id,
        )
        _ib.connect(
            ib_settings.host,
            ib_settings.port,
            clientId=ib_settings.client_id,
        )
    else:
        logger.debug("IBKR already connected")

    return _ib


def disconnect_ib() -> None:
    if _ib.isConnected():
        logger.info("Disconnecting from IBKR")
        _ib.disconnect()
    else:
        logger.debug("disconnect_ib called but IBKR was not connected")

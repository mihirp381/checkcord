from checkcord.api.push_router import router
from checkcord.connectors.base import Connector

CONNECTORS = (
    Connector(
        name="custom",
        title="Push API",
        ingestion="push API",
        status="experimental",
        routers=((router, None),),
    ),
)

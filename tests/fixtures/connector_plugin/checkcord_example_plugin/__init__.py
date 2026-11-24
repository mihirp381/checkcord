from fastapi import APIRouter

from checkcord.connectors import Connector, Poll

router = APIRouter()


@router.get("/api/v1/webhooks/example/ping")
async def ping() -> dict[str, str]:
    return {"status": "ok"}


async def poll_example(engine: object, bus: object) -> int:
    return 0


CONNECTORS = [
    Connector(
        name="example",
        title="Example",
        ingestion="poller",
        status="experimental",
        scope="example folders",
        routers=((router, "webhooks"),),
        poll=Poll("poll_example", poll_example, lambda settings: 30),
    )
]

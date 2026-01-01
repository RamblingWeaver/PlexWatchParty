import uvicorn
from .api import app
from .config import settings


def run():
    uvicorn.run("server.api:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    run()

import uvicorn
from .api import app
from . import config


def run():
    settings = config.get_settings()
    uvicorn.run("server.api:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    run()

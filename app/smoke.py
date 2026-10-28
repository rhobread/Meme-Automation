"""Run inside Compose to verify HTTP, database, Redis and real worker delivery."""
import json
from urllib.request import Request, urlopen

from app.config import get_settings
from app.worker import smoke
from app.main import schema_head


def main() -> None:
    request = Request("http://api:8000/api/v1/health", headers={"X-API-Key": get_settings().internal_api_key.get_secret_value()})
    with urlopen(request, timeout=15) as response:
        assert json.load(response)["status"] == "ok"
    result = smoke.delay().get(timeout=30)
    assert result == {"database": "ok", "redis": "ok", "schema": schema_head()}
    print("HTTP, schema, Redis and Celery smoke checks passed")


if __name__ == "__main__":
    main()

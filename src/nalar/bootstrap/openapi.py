import json

from nalar.bootstrap.app import create_app
from nalar.bootstrap.settings import Settings


def render_openapi() -> str:
    schema = create_app(Settings()).openapi()
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

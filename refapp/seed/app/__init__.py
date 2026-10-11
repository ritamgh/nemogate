from flask import Flask

from app.api import bp
from app.db import init_db


def create_app(db_path: str) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    init_db(db_path)
    app.register_blueprint(bp)
    return app

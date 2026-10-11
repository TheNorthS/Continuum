"""Optional isolated API server for evaluation; never uses the live workspace database."""

from app.evaluation.configuration import load_settings


def create_eval_app():
    from app.main import create_app

    return create_app(load_settings())

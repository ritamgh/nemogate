import importlib

EXPORTERS = {"csv": "app.export_csv"}


def get_exporter(name: str):
    """Return the export(rows) function registered under `name`."""
    try:
        module_name = EXPORTERS[name]
    except KeyError:
        raise ValueError(f"unknown export format: {name!r}") from None
    return importlib.import_module(module_name).export

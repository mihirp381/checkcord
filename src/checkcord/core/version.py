from importlib.metadata import PackageNotFoundError, version


def checkcord_version() -> str:
    try:
        return version("checkcord")
    except PackageNotFoundError:
        return "0+unknown"

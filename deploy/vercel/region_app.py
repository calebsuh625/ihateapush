"""One county's API process, started by index.py with REGION=<fips> (and the county's file paths)."""
import runtime

runtime.setup()

from api.main import app  # noqa: E402,F401

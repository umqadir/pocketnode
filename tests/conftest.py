import os

# Progress lines go to the terminal running the tests; keep the suite's output as it was.
# (tests/test_progress.py turns them on for the reporters it checks.)
os.environ["POCKETNODE_QUIET"] = "1"

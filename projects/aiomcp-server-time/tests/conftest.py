import sys
from pathlib import Path

SOURCE_DIR = Path(__file__).parents[1] / "src"
source_path = str(SOURCE_DIR)

if source_path not in sys.path:
    sys.path.insert(0, source_path)

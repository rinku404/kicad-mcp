import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# never download the embedding model in tests; test_rag supplies its own
os.environ["KICAD_MCP_EMBEDDINGS"] = "off"

DATA = Path(__file__).parent / "data"

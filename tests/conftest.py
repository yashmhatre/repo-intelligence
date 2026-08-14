import sys
from pathlib import Path

# Tests import the packages by path, the same way graph/neo4j_loader.py does.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

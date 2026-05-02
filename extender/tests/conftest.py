import sys
from pathlib import Path

# Ajoute le dossier parent (extender/) au PYTHONPATH
sys.path.insert(0, str(Path(__file__).parent.parent))
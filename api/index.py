import sys
from pathlib import Path

# A Vercel executa este arquivo isoladamente - garante que a raiz do
# projeto (onde ficam app.py, db.py, consulta_inpi_*.py) esteja no path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import app  # noqa: E402

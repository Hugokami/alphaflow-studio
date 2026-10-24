import sys
from pathlib import Path

# Add project root to sys.path so backend imports work seamlessly in Vercel Serverless Functions
root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from backend.app import app

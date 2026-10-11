import os
import sys

# Ensure the parent directory is in sys.path so 'anki_import_diff' is recognized as a package
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, parent_dir)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

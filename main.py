"""Convenience entry point: python main.py <folder> [-o database.db]"""

from dbcreator.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

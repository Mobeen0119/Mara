from core.database import init_db, DB_PATH
import os

init_db()
print(f"Database ready at {DB_PATH}, size {os.path.getsize(DB_PATH)} bytes")

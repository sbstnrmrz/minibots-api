from app.database import engine
from sqlalchemy import text

with engine.connect() as conn:
    conn.execute(text("""
        ALTER TABLE bots
            ADD COLUMN IF NOT EXISTS bot_type      VARCHAR NOT NULL DEFAULT 'zen_coach',
            ADD COLUMN IF NOT EXISTS spreadsheet_id VARCHAR;
    """))
    conn.commit()
    print("Migration complete.")

import asyncio
import asyncpg

async def setup():
    conn = await asyncpg.connect(
        'postgresql://boardspell_db_user:Gin0IToR1r4TpwdpjHwaexGZbMZh4C3l@dpg-d82mrmt0lvsc738kckh0-a.oregon-postgres.render.com/boardspell_db'
    )

    await conn.execute('''
        CREATE TABLE IF NOT EXISTS workspaces (
            workspace_id      TEXT PRIMARY KEY,
            monday_account_id TEXT,
            access_token      TEXT NOT NULL,
            created_at        TIMESTAMP DEFAULT NOW(),
            plan_tier         TEXT DEFAULT 'free'
        )
    ''')
    print('✅ workspaces table ready')

    await conn.execute('''
        CREATE TABLE IF NOT EXISTS automations (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id     TEXT REFERENCES workspaces(workspace_id),
            name             TEXT NOT NULL,
            trigger_type     TEXT NOT NULL,
            trigger_board_id TEXT NOT NULL,
            trigger_config   JSONB NOT NULL,
            condition_config JSONB,
            action_type      TEXT NOT NULL,
            action_board_id  TEXT,
            action_config    JSONB NOT NULL,
            is_active        BOOLEAN DEFAULT TRUE,
            created_at       TIMESTAMP DEFAULT NOW()
        )
    ''')
    print('✅ automations table ready')

    await conn.execute('''
        CREATE TABLE IF NOT EXISTS webhook_subscriptions (
            id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            automation_id      UUID REFERENCES automations(id) ON DELETE CASCADE,
            monday_webhook_id  TEXT NOT NULL,
            board_id           TEXT NOT NULL,
            event_type         TEXT NOT NULL,
            created_at         TIMESTAMP DEFAULT NOW()
        )
    ''')
    print('✅ webhook_subscriptions table ready')

    await conn.execute('''
        CREATE TABLE IF NOT EXISTS execution_logs (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            automation_id   UUID REFERENCES automations(id) ON DELETE CASCADE,
            triggered_at    TIMESTAMP DEFAULT NOW(),
            trigger_payload JSONB,
            action_taken    JSONB,
            status          TEXT NOT NULL,
            error_message   TEXT
        )
    ''')
    print('✅ execution_logs table ready')

    print('🎉 All tables created successfully!')
    await conn.close()

asyncio.run(setup())
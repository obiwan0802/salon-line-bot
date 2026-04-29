-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- サロン・美容室向け LINE予約システム データベーススキーマ
-- Supabase (PostgreSQL) 用
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

-- 1. スタッフテーブル
CREATE TABLE IF NOT EXISTS staff (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT DEFAULT 'スタイリスト',  -- スタイリスト / ネイリスト / アイリスト 等
    emoji TEXT DEFAULT '💇',
    is_active BOOLEAN DEFAULT TRUE,
    sort_order INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 2. 顧客テーブル
CREATE TABLE IF NOT EXISTS customers (
    id BIGSERIAL PRIMARY KEY,
    line_user_id TEXT UNIQUE NOT NULL,
    display_name TEXT,
    phone TEXT,
    visit_count INTEGER DEFAULT 0,
    last_visit_date DATE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 3. 予約テーブル
CREATE TABLE IF NOT EXISTS reservations (
    id BIGSERIAL PRIMARY KEY,
    customer_id BIGINT REFERENCES customers(id),
    line_user_id TEXT NOT NULL,
    guest_name TEXT NOT NULL,
    phone TEXT,
    menu_id TEXT NOT NULL,
    menu_name TEXT NOT NULL,
    staff_id BIGINT REFERENCES staff(id),
    staff_name TEXT,               -- 指名なしの場合はNULL
    reservation_date DATE NOT NULL,
    reservation_time TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL,
    total_price INTEGER DEFAULT 0,
    status TEXT DEFAULT 'confirmed',  -- confirmed / cancelled / completed
    calendar_event_id TEXT,
    reminded BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 4. 定休日テーブル
CREATE TABLE IF NOT EXISTS closed_days (
    id BIGSERIAL PRIMARY KEY,
    closed_date DATE,
    day_of_week INTEGER,           -- 0=月〜6=日
    reason TEXT DEFAULT '定休日',
    is_recurring BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 5. スタッフ休日テーブル（スタッフ個別の休み）
CREATE TABLE IF NOT EXISTS staff_days_off (
    id BIGSERIAL PRIMARY KEY,
    staff_id BIGINT REFERENCES staff(id) ON DELETE CASCADE,
    off_date DATE,                 -- 特定日の休み
    day_of_week INTEGER,           -- 曜日（0=月〜6=日）
    is_recurring BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 6. 予約セッション（予約フロー途中のデータを永続化）
CREATE TABLE IF NOT EXISTS reservation_sessions (
    line_user_id TEXT PRIMARY KEY,
    session_data JSONB NOT NULL DEFAULT '{}',
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '30 minutes'),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rsessions_expires ON reservation_sessions(expires_at);

-- 期限切れセッションの自動削除（PostgreSQL拡張 pg_cron があれば設定可、なければアプリ側で対応）
-- SELECT cron.schedule('cleanup-sessions', '*/10 * * * *', $$DELETE FROM reservation_sessions WHERE expires_at < NOW()$$);

-- 7. 店舗設定テーブル
CREATE TABLE IF NOT EXISTS store_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 予約締切時間の初期値（1時間前）
INSERT INTO store_settings (key, value) VALUES ('booking_deadline_hours', '1')
ON CONFLICT (key) DO NOTHING;

-- インデックス
CREATE INDEX IF NOT EXISTS idx_reservations_date ON reservations(reservation_date);
CREATE INDEX IF NOT EXISTS idx_reservations_status ON reservations(status);
CREATE INDEX IF NOT EXISTS idx_reservations_line_user ON reservations(line_user_id);
CREATE INDEX IF NOT EXISTS idx_reservations_staff ON reservations(staff_id);
CREATE INDEX IF NOT EXISTS idx_customers_line_user ON customers(line_user_id);
CREATE INDEX IF NOT EXISTS idx_closed_days_date ON closed_days(closed_date);
CREATE INDEX IF NOT EXISTS idx_closed_days_dow ON closed_days(day_of_week);
CREATE INDEX IF NOT EXISTS idx_staff_days_off_staff ON staff_days_off(staff_id);
CREATE INDEX IF NOT EXISTS idx_staff_days_off_date ON staff_days_off(off_date);

-- updated_atを自動更新するトリガー
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tr_customers_updated
    BEFORE UPDATE ON customers
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER tr_reservations_updated
    BEFORE UPDATE ON reservations
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER tr_staff_updated
    BEFORE UPDATE ON staff
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- サンプルデータ（初期スタッフ — 導入時に変更）
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INSERT INTO staff (name, role, emoji, sort_order) VALUES
    ('田中 美咲', 'スタイリスト', '💇‍♀️', 1),
    ('佐藤 健太', 'スタイリスト', '💇‍♂️', 2),
    ('鈴木 あかり', 'ネイリスト', '💅', 3)
ON CONFLICT DO NOTHING;

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- RLSポリシー
-- サーバーは service_role key を使うためRLSをバイパス。
-- anon / authenticated による直接アクセスは全て拒否。
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ALTER TABLE staff ENABLE ROW LEVEL SECURITY;
ALTER TABLE customers ENABLE ROW LEVEL SECURITY;
ALTER TABLE reservations ENABLE ROW LEVEL SECURITY;
ALTER TABLE closed_days ENABLE ROW LEVEL SECURITY;
ALTER TABLE staff_days_off ENABLE ROW LEVEL SECURITY;
ALTER TABLE store_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE reservation_sessions ENABLE ROW LEVEL SECURITY;

-- RLSが有効 + ポリシーなし = anon/authenticatedからのアクセスは全拒否
-- service_role keyはRLSをバイパスするため、サーバーからは通常通りアクセス可能

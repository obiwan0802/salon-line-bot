"""
サロン・美容室向け LINE予約Bot
- スタッフ指名予約（指名なし＝自動割り当て）
- スタッフ別の空き枠管理
- Supabaseデータベースで予約・顧客・スタッフデータを永続化
- キャンセル機能
- 定休日・スタッフ個別休日対応
- Googleカレンダー連携
- オーナーLINE通知 / 前日リマインド
- Webダッシュボード（予約管理・スタッフ管理・定休日設定・顧客リスト）
"""

import os
import json
import re
import datetime
import logging
import calendar as cal_module
from zoneinfo import ZoneInfo

from flask import Flask, request, abort, render_template, jsonify, session as flask_session, redirect, url_for
from markupsafe import escape as html_escape
from functools import wraps
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent,
    PostbackEvent,
    FollowEvent,
    TextMessage,
    TextSendMessage,
    FlexSendMessage,
)

import requests as http_requests

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from apscheduler.schedulers.background import BackgroundScheduler

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 設定
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(32).hex())
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# LINE API
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
OWNER_LINE_USER_ID = os.environ.get("OWNER_LINE_USER_ID", "")

# Supabase（service_role keyを使用 — RLSをバイパスしてサーバーから全操作可能）
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", os.environ.get("SUPABASE_KEY", ""))

# Google Calendar
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

# 店舗設定
STORE_NAME = os.environ.get("STORE_NAME", "サンプルサロン")
STORE_OPEN_HOUR = int(os.environ.get("STORE_OPEN_HOUR", "9"))
STORE_CLOSE_HOUR = int(os.environ.get("STORE_CLOSE_HOUR", "20"))
SLOT_INTERVAL_MINUTES = int(os.environ.get("SLOT_INTERVAL_MINUTES", "30"))
BOOKING_DEADLINE_HOURS = int(os.environ.get("BOOKING_DEADLINE_HOURS", "1"))

# ダッシュボード認証
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
if not DASHBOARD_PASSWORD:
    logger.warning("⚠️ DASHBOARD_PASSWORD が未設定です。必ず環境変数で設定してください。")


def dashboard_auth_required(f):
    """ダッシュボード用の認証デコレータ（セッションCookie方式）"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not flask_session.get("dashboard_authenticated"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("dashboard_login"))
        return f(*args, **kwargs)
    return decorated

# ブランドカラー
BRAND_COLOR = "#9B59B6"  # パープル系（サロン向け）

JST = ZoneInfo("Asia/Tokyo")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 入力バリデーション・サニタイズ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def sanitize_text(text, max_length=100):
    """テキスト入力のサニタイズ（XSS対策 + 長さ制限）"""
    if not isinstance(text, str):
        return ""
    text = str(html_escape(text.strip()))
    return text[:max_length]


def validate_phone(phone):
    """電話番号バリデーション（日本の電話番号形式）"""
    if not phone:
        return True, ""
    cleaned = re.sub(r"[\s\-‐‑‒–—―ー－]", "", phone)
    if re.match(r"^(0\d{9,10}|\+81\d{9,10})$", cleaned):
        return True, cleaned
    return False, ""


def validate_name(name):
    """名前バリデーション"""
    if not name or not name.strip():
        return False, ""
    name = name.strip()
    if len(name) > 50:
        return False, ""
    sanitized = re.sub(r"<[^>]+>", "", name)
    return True, sanitized


def validate_positive_int(value, max_val=999):
    """正の整数バリデーション"""
    try:
        n = int(value)
        return 1 <= n <= max_val, n
    except (ValueError, TypeError):
        return False, 0


# SDK初期化
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Supabase REST APIヘルパー
SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}


def supabase_get(table, params=None):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    res = http_requests.get(url, headers=SUPABASE_HEADERS, params=params or {})
    res.raise_for_status()
    return res.json()


def supabase_post(table, data):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    res = http_requests.post(url, headers=SUPABASE_HEADERS, json=data)
    res.raise_for_status()
    return res.json()


def supabase_patch(table, data, params):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    res = http_requests.patch(url, headers=SUPABASE_HEADERS, json=data, params=params)
    res.raise_for_status()
    return res.json()


def supabase_delete(table, params):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    res = http_requests.delete(url, headers=SUPABASE_HEADERS, params=params)
    res.raise_for_status()
    return True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# メニュー設定（施術メニュー）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MENU_ITEMS = [
    {"id": "cut", "name": "カット", "price": 4400, "duration": 60, "emoji": "✂️"},
    {"id": "color", "name": "カラー", "price": 6600, "duration": 90, "emoji": "🎨"},
    {"id": "cut_color", "name": "カット+カラー", "price": 9900, "duration": 120, "emoji": "✂️🎨"},
    {"id": "perm", "name": "パーマ", "price": 7700, "duration": 120, "emoji": "🌀"},
    {"id": "cut_perm", "name": "カット+パーマ", "price": 11000, "duration": 150, "emoji": "✂️🌀"},
    {"id": "treatment", "name": "トリートメント", "price": 3300, "duration": 45, "emoji": "💆"},
    {"id": "head_spa", "name": "ヘッドスパ", "price": 4400, "duration": 60, "emoji": "🧖"},
]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 予約セッション管理（Supabase永続化）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SESSION_TTL_MINUTES = 30


def session_get(uid):
    """ユーザーの予約セッションを取得。期限切れなら削除してNone。"""
    try:
        rows = supabase_get("reservation_sessions", {
            "select": "*",
            "line_user_id": f"eq.{uid}",
        })
        if not rows:
            return None
        row = rows[0]
        # 期限切れチェック
        expires = datetime.datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
        if expires < datetime.datetime.now(datetime.timezone.utc):
            session_delete(uid)
            return None
        return row["session_data"]
    except Exception as e:
        logger.error(f"session_get error: {e}")
        return None


def session_set(uid, data):
    """ユーザーの予約セッションを作成または上書き。"""
    try:
        expires = (datetime.datetime.now(datetime.timezone.utc)
                   + datetime.timedelta(minutes=SESSION_TTL_MINUTES)).isoformat()
        payload = {
            "line_user_id": uid,
            "session_data": data,
            "expires_at": expires,
        }
        # UPSERT（on conflict update）
        headers = {**SUPABASE_HEADERS, "Prefer": "return=representation,resolution=merge-duplicates"}
        url = f"{SUPABASE_URL}/rest/v1/reservation_sessions"
        res = http_requests.post(url, headers=headers, json=payload)
        res.raise_for_status()
    except Exception as e:
        logger.error(f"session_set error: {e}")


def session_update(uid, updates):
    """既存セッションの一部フィールドを更新。セッションがなければ何もしない。"""
    current = session_get(uid)
    if current is None:
        return None
    current.update(updates)
    session_set(uid, current)
    return current


def session_delete(uid):
    """ユーザーの予約セッションを削除。"""
    try:
        supabase_delete("reservation_sessions", {"line_user_id": f"eq.{uid}"})
    except Exception:
        pass


def cleanup_expired_sessions():
    """期限切れセッションを一括削除（スケジューラから呼出）。"""
    try:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        supabase_delete("reservation_sessions", {"expires_at": f"lt.{now}"})
    except Exception as e:
        logger.error(f"Session cleanup error: {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# スタッフ取得
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def db_get_active_staff():
    """有効なスタッフ一覧を取得"""
    try:
        rows = supabase_get("staff", {
            "select": "*",
            "is_active": "eq.true",
            "order": "sort_order.asc",
        })
        return rows or []
    except Exception as e:
        logger.error(f"スタッフ取得エラー: {e}")
        return []


def db_is_staff_off(staff_id, date_str):
    """指定スタッフが指定日に休みかチェック"""
    try:
        date_obj = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        dow = date_obj.weekday()
        # 特定日の休み
        rows1 = supabase_get("staff_days_off", {
            "select": "id",
            "staff_id": f"eq.{staff_id}",
            "off_date": f"eq.{date_str}",
        })
        if rows1:
            return True
        # 毎週の休み
        rows2 = supabase_get("staff_days_off", {
            "select": "id",
            "staff_id": f"eq.{staff_id}",
            "day_of_week": f"eq.{dow}",
            "is_recurring": "eq.true",
        })
        return bool(rows2)
    except Exception as e:
        logger.error(f"スタッフ休日チェックエラー: {e}")
        return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Supabase データ操作
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def db_get_or_create_customer(line_user_id, display_name=None):
    try:
        rows = supabase_get("customers", {"line_user_id": f"eq.{line_user_id}", "select": "*"})
        if rows:
            return rows[0]
        new = supabase_post("customers", {
            "line_user_id": line_user_id,
            "display_name": display_name,
        })
        return new[0] if new else None
    except Exception as e:
        logger.error(f"顧客DB操作エラー: {e}")
        return None


def db_save_reservation(reservation_data):
    try:
        res = supabase_post("reservations", reservation_data)
        return res[0] if res else None
    except Exception as e:
        logger.error(f"予約保存エラー: {e}")
        return None


def db_get_user_reservations(line_user_id):
    today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    try:
        rows = supabase_get("reservations", {
            "select": "*",
            "line_user_id": f"eq.{line_user_id}",
            "status": "eq.confirmed",
            "reservation_date": f"gte.{today}",
            "order": "reservation_date.asc,reservation_time.asc",
        })
        return rows or []
    except Exception as e:
        logger.error(f"予約取得エラー: {e}")
        return []


def db_cancel_reservation(reservation_id, line_user_id):
    try:
        res = supabase_patch("reservations", {"status": "cancelled"}, {
            "id": f"eq.{reservation_id}",
            "line_user_id": f"eq.{line_user_id}",
        })
        return bool(res)
    except Exception as e:
        logger.error(f"キャンセルエラー: {e}")
        return False


def db_get_reservations_by_date(date_str):
    try:
        rows = supabase_get("reservations", {
            "select": "*",
            "reservation_date": f"eq.{date_str}",
            "status": "eq.confirmed",
        })
        return rows or []
    except Exception as e:
        logger.error(f"日付別予約取得エラー: {e}")
        return []


def db_get_tomorrow_reminders():
    tomorrow = (datetime.datetime.now(JST) + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        rows = supabase_get("reservations", {
            "select": "*",
            "reservation_date": f"eq.{tomorrow}",
            "status": "eq.confirmed",
            "reminded": "eq.false",
        })
        return rows or []
    except Exception as e:
        logger.error(f"リマインド取得エラー: {e}")
        return []


def db_mark_reminded(reservation_id):
    try:
        supabase_patch("reservations", {"reminded": True}, {"id": f"eq.{reservation_id}"})
    except Exception as e:
        logger.error(f"リマインド更新エラー: {e}")


def db_update_customer_visit(line_user_id, name=None, phone=None):
    try:
        customer = db_get_or_create_customer(line_user_id)
        if customer:
            update_data = {"visit_count": customer.get("visit_count", 0) + 1}
            if name:
                update_data["display_name"] = name
            if phone:
                update_data["phone"] = phone
            supabase_patch("customers", update_data, {"line_user_id": f"eq.{line_user_id}"})
    except Exception as e:
        logger.error(f"顧客更新エラー: {e}")


def db_is_closed_day(date_str):
    try:
        date_obj = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        day_of_week = date_obj.weekday()
        rows1 = supabase_get("closed_days", {"select": "*", "closed_date": f"eq.{date_str}"})
        if rows1:
            return True
        rows2 = supabase_get("closed_days", {
            "select": "*",
            "day_of_week": f"eq.{day_of_week}",
            "is_recurring": "eq.true",
        })
        return bool(rows2)
    except Exception as e:
        logger.error(f"定休日チェックエラー: {e}")
        return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Google Calendar連携
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_calendar_service():
    try:
        creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = Credentials.from_service_account_info(
            creds_dict, scopes=["https://www.googleapis.com/auth/calendar"]
        )
        return build("calendar", "v3", credentials=creds)
    except Exception as e:
        logger.error(f"Google Calendar接続エラー: {e}")
        return None


def create_calendar_event(reservation):
    service = get_calendar_service()
    if not service:
        return None

    staff_text = reservation.get("staff_name") or "指名なし"

    start_dt = datetime.datetime.strptime(
        f"{reservation['reservation_date']} {reservation['reservation_time']}",
        "%Y-%m-%d %H:%M",
    ).replace(tzinfo=JST)
    end_dt = start_dt + datetime.timedelta(minutes=reservation["duration_minutes"])

    event = {
        "summary": f"【予約】{reservation['guest_name']} / {staff_text}",
        "description": (
            f"施術: {reservation['menu_name']}\n"
            f"担当: {staff_text}\n"
            f"お名前: {reservation['guest_name']}\n"
            f"電話番号: {reservation.get('phone', '未設定')}\n"
            f"料金: ¥{reservation.get('total_price', 0):,}"
        ),
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "Asia/Tokyo"},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": "Asia/Tokyo"},
        "colorId": "3",  # ラベンダー
    }

    try:
        created = service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
        return created.get("id")
    except Exception as e:
        logger.error(f"カレンダー登録エラー: {e}")
        return None


def delete_calendar_event(event_id):
    service = get_calendar_service()
    if not service or not event_id:
        return
    try:
        service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id).execute()
    except Exception as e:
        logger.error(f"カレンダー削除エラー: {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 店舗設定（DB）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def db_get_setting(key, default=None):
    try:
        rows = supabase_get("store_settings", {"select": "value", "key": f"eq.{key}"})
        if rows:
            return rows[0]["value"]
        return default
    except Exception as e:
        logger.error(f"設定取得エラー: {e}")
        return default


def db_set_setting(key, value):
    try:
        rows = supabase_get("store_settings", {"select": "key", "key": f"eq.{key}"})
        if rows:
            supabase_patch("store_settings", {"value": str(value)}, {"key": f"eq.{key}"})
        else:
            supabase_post("store_settings", {"key": key, "value": str(value)})
    except Exception as e:
        logger.error(f"設定保存エラー: {e}")


def get_booking_deadline_hours():
    val = db_get_setting("booking_deadline_hours")
    if val is not None:
        try:
            return int(val)
        except ValueError:
            pass
    return BOOKING_DEADLINE_HOURS


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 空き枠計算（スタッフベース）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_available_slots(date_str, staff_id, duration_minutes):
    """指定日・指定スタッフの空き枠を返す。staff_id=0は「指名なし」"""
    now = datetime.datetime.now(JST)
    target_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=JST)

    if db_is_closed_day(date_str):
        return []

    day_reservations = db_get_reservations_by_date(date_str)
    active_staff = db_get_active_staff()

    # 指名なしの場合：全スタッフの中で空いている人がいる枠を返す
    # 指名ありの場合：そのスタッフの枠だけを返す
    if staff_id == 0:
        # その日出勤しているスタッフを取得
        working_staff = [s for s in active_staff if not db_is_staff_off(s["id"], date_str)]
        if not working_staff:
            return []
        target_staff_ids = [s["id"] for s in working_staff]
    else:
        if db_is_staff_off(staff_id, date_str):
            return []
        target_staff_ids = [staff_id]

    slots = []
    hour = STORE_OPEN_HOUR
    minute = 0

    while hour < STORE_CLOSE_HOUR:
        time_str = f"{hour:02d}:{minute:02d}"
        slot_time = target_date.replace(hour=hour, minute=minute)

        deadline_hours = get_booking_deadline_hours()
        deadline = now + datetime.timedelta(hours=deadline_hours)
        if slot_time <= deadline:
            minute += SLOT_INTERVAL_MINUTES
            if minute >= 60:
                hour += 1
                minute = 0
            continue

        end_time = slot_time + datetime.timedelta(minutes=duration_minutes)
        if end_time.hour > STORE_CLOSE_HOUR or (
            end_time.hour == STORE_CLOSE_HOUR and end_time.minute > 0
        ):
            break

        # この時間帯に空いているスタッフがいるか判定
        available_staff_ids = []
        for sid in target_staff_ids:
            is_busy = False
            for r in day_reservations:
                if r.get("staff_id") != sid:
                    continue
                r_start = datetime.datetime.strptime(
                    f"{r['reservation_date']} {r['reservation_time']}", "%Y-%m-%d %H:%M"
                ).replace(tzinfo=JST)
                r_end = r_start + datetime.timedelta(minutes=r.get("duration_minutes", 60))
                # 予約時間帯が重なっているかチェック
                if slot_time < r_end and end_time > r_start:
                    is_busy = True
                    break
            if not is_busy:
                available_staff_ids.append(sid)

        if available_staff_ids:
            slots.append({
                "time": time_str,
                "available_staff_ids": available_staff_ids,
                "available_count": len(available_staff_ids),
            })

        minute += SLOT_INTERVAL_MINUTES
        if minute >= 60:
            hour += 1
            minute = 0

    return slots


def auto_assign_staff(date_str, time_str, duration_minutes, available_staff_ids):
    """指名なしの場合、空いているスタッフの中で予約数が少ない人を自動割り当て"""
    day_reservations = db_get_reservations_by_date(date_str)
    staff_counts = {}
    for sid in available_staff_ids:
        count = sum(1 for r in day_reservations if r.get("staff_id") == sid)
        staff_counts[sid] = count
    # 予約数が少ないスタッフを優先
    best_id = min(available_staff_ids, key=lambda x: staff_counts.get(x, 0))
    return best_id


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Flex Messageテンプレート
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def build_welcome_flex():
    return {
        "type": "bubble",
        "hero": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": STORE_NAME, "weight": "bold", "size": "xl",
                 "color": "#FFFFFF", "align": "center"},
                {"type": "text", "text": "LINE予約システム", "size": "sm",
                 "color": "#FFFFFFCC", "align": "center", "margin": "sm"},
            ],
            "backgroundColor": BRAND_COLOR,
            "paddingAll": "20px",
        },
        "body": {
            "type": "box", "layout": "vertical",
            "contents": [
                {"type": "text", "text": "ご来店ありがとうございます！", "weight": "bold", "size": "md"},
                {"type": "text", "text": "下のボタンからかんたんに予約できます。",
                 "size": "sm", "color": "#666666", "margin": "md", "wrap": True},
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical",
            "contents": [
                {"type": "button", "action": {"type": "postback", "label": "📅 予約する",
                 "data": "action=start_reservation"}, "style": "primary", "color": BRAND_COLOR},
                {"type": "button", "action": {"type": "postback", "label": "📋 予約を確認する",
                 "data": "action=check_reservation"}, "style": "secondary", "margin": "sm"},
                {"type": "button", "action": {"type": "postback", "label": "❌ 予約をキャンセル",
                 "data": "action=list_cancel"}, "style": "secondary", "margin": "sm"},
            ],
        },
    }


def build_menu_flex():
    bubbles = []
    for item in MENU_ITEMS:
        bubble = {
            "type": "bubble", "size": "micro",
            "body": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "text", "text": item["emoji"], "size": "3xl", "align": "center"},
                    {"type": "text", "text": item["name"], "weight": "bold", "size": "sm",
                     "align": "center", "margin": "md", "wrap": True},
                    {"type": "text", "text": f"¥{item['price']:,}", "size": "lg",
                     "color": BRAND_COLOR, "align": "center", "weight": "bold", "margin": "sm"},
                    {"type": "text", "text": f"所要時間: 約{item['duration']}分", "size": "xs",
                     "color": "#999999", "align": "center", "margin": "sm"},
                ],
                "paddingAll": "12px",
            },
            "footer": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "button", "action": {"type": "postback", "label": "選択",
                     "data": f"action=select_menu&menu_id={item['id']}"},
                     "style": "primary", "color": BRAND_COLOR, "height": "sm"}
                ],
            },
        }
        bubbles.append(bubble)
    return {"type": "carousel", "contents": bubbles}


def build_staff_flex():
    """スタッフ選択Flex（指名なしオプション付き）"""
    staff_list = db_get_active_staff()

    bubbles = []
    # 「指名なし」オプション
    bubbles.append({
        "type": "bubble", "size": "micro",
        "body": {
            "type": "box", "layout": "vertical",
            "contents": [
                {"type": "text", "text": "🔄", "size": "3xl", "align": "center"},
                {"type": "text", "text": "指名なし", "weight": "bold", "size": "sm",
                 "align": "center", "margin": "md"},
                {"type": "text", "text": "空いているスタッフに\n自動でお任せ", "size": "xxs",
                 "color": "#999999", "align": "center", "margin": "sm", "wrap": True},
            ],
            "paddingAll": "12px",
        },
        "footer": {
            "type": "box", "layout": "vertical",
            "contents": [
                {"type": "button", "action": {"type": "postback", "label": "選択",
                 "data": "action=select_staff&staff_id=0"},
                 "style": "primary", "color": "#888888", "height": "sm"}
            ],
        },
    })

    for s in staff_list:
        bubble = {
            "type": "bubble", "size": "micro",
            "body": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "text", "text": s.get("emoji", "💇"), "size": "3xl", "align": "center"},
                    {"type": "text", "text": s["name"], "weight": "bold", "size": "sm",
                     "align": "center", "margin": "md", "wrap": True},
                    {"type": "text", "text": s.get("role", ""), "size": "xxs",
                     "color": "#999999", "align": "center", "margin": "sm"},
                ],
                "paddingAll": "12px",
            },
            "footer": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "button", "action": {"type": "postback", "label": "指名する",
                     "data": f"action=select_staff&staff_id={s['id']}"},
                     "style": "primary", "color": BRAND_COLOR, "height": "sm"}
                ],
            },
        }
        bubbles.append(bubble)

    return {"type": "carousel", "contents": bubbles}


def build_date_flex():
    """カレンダー型の日付選択（3ヶ月分カルーセル）"""
    today = datetime.datetime.now(JST)
    today_date = today.date()

    try:
        all_closed = supabase_get("closed_days", {"select": "*"})
    except Exception:
        all_closed = []

    recurring_dows = set()
    specific_dates = set()
    for c in all_closed:
        if c.get("is_recurring") and c.get("day_of_week") is not None:
            recurring_dows.add(c["day_of_week"])
        if c.get("closed_date"):
            specific_dates.add(c["closed_date"])

    def is_closed(d):
        if d.weekday() in recurring_dows:
            return True
        return d.strftime("%Y-%m-%d") in specific_dates

    weekday_labels = ["月", "火", "水", "木", "金", "土", "日"]
    bubbles = []

    for month_offset in range(3):
        year = today.year
        month = today.month + month_offset
        while month > 12:
            year += 1
            month -= 12

        weeks = cal_module.monthcalendar(year, month)

        header_row = {
            "type": "box", "layout": "horizontal", "margin": "md",
            "contents": [
                {"type": "text", "text": wd, "size": "xxs", "align": "center", "flex": 1,
                 "color": BRAND_COLOR if i == 6 else "#1565c0" if i == 5 else "#888888"}
                for i, wd in enumerate(weekday_labels)
            ],
        }

        rows = [
            {"type": "text", "text": f"{year}年{month}月", "weight": "bold",
             "size": "md", "align": "center"},
            {"type": "separator", "margin": "sm"},
            header_row,
        ]

        for week in weeks:
            row_contents = []
            for i, day in enumerate(week):
                if day == 0:
                    row_contents.append({
                        "type": "box", "layout": "vertical", "flex": 1,
                        "contents": [{"type": "text", "text": " ", "size": "sm", "align": "center"}],
                    })
                else:
                    d = datetime.date(year, month, day)
                    date_str = d.strftime("%Y-%m-%d")
                    is_past = d < today_date
                    closed = is_closed(d)

                    if is_past:
                        row_contents.append({
                            "type": "box", "layout": "vertical", "flex": 1,
                            "contents": [{"type": "text", "text": str(day), "size": "sm",
                                          "align": "center", "color": "#DDDDDD"}],
                        })
                    elif closed:
                        row_contents.append({
                            "type": "box", "layout": "vertical", "flex": 1,
                            "action": {"type": "postback", "label": "定休日",
                                       "data": "action=closed_day"},
                            "contents": [{"type": "text", "text": str(day), "size": "sm",
                                          "align": "center", "color": "#CCCCCC",
                                          "decoration": "line-through"}],
                        })
                    else:
                        day_color = BRAND_COLOR if i == 6 else "#1565c0" if i == 5 else "#111111"
                        is_today = d == today_date
                        cell_contents = [
                            {"type": "text", "text": str(day), "size": "sm",
                             "align": "center", "color": day_color,
                             "weight": "bold" if is_today else "regular"}
                        ]
                        if is_today:
                            cell_contents.append(
                                {"type": "text", "text": "●", "size": "xxs",
                                 "align": "center", "color": BRAND_COLOR}
                            )
                        row_contents.append({
                            "type": "box", "layout": "vertical", "flex": 1,
                            "action": {"type": "postback", "label": date_str,
                                       "data": f"action=select_date&date={date_str}"},
                            "contents": cell_contents,
                        })

            rows.append({
                "type": "box", "layout": "horizontal", "margin": "sm",
                "contents": row_contents,
            })

        bubble = {
            "type": "bubble",
            "body": {"type": "box", "layout": "vertical", "contents": rows, "paddingAll": "12px"},
        }
        bubbles.append(bubble)

    return {"type": "carousel", "contents": bubbles}


def build_time_flex(slots):
    buttons = []
    for slot in slots:
        label = f"🕐 {slot['time']}"
        buttons.append({
            "type": "button",
            "action": {"type": "postback", "label": label,
                       "data": f"action=select_time&time={slot['time']}"},
            "style": "secondary", "height": "sm", "margin": "sm",
        })

    rows = []
    for i in range(0, len(buttons), 2):
        rows.append({"type": "box", "layout": "horizontal",
                      "contents": buttons[i:i+2], "spacing": "sm"})

    return {
        "type": "bubble",
        "body": {
            "type": "box", "layout": "vertical",
            "contents": [
                {"type": "text", "text": "🕐 時間を選択", "weight": "bold", "size": "lg"},
                {"type": "text", "text": "ご希望の時間をお選びください",
                 "size": "sm", "color": "#666666", "margin": "md"},
                {"type": "separator", "margin": "lg"},
                *rows,
            ],
        },
    }


def build_confirm_flex(session):
    menu = next((m for m in MENU_ITEMS if m["id"] == session["menu_id"]), None)
    staff_name = session.get("staff_name") or "指名なし（自動割り当て）"

    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical",
            "contents": [
                {"type": "text", "text": "📋 ご予約内容の確認", "weight": "bold",
                 "size": "lg", "color": "#FFFFFF"},
            ],
            "backgroundColor": BRAND_COLOR, "paddingAll": "15px",
        },
        "body": {
            "type": "box", "layout": "vertical",
            "contents": [
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "施術", "size": "sm", "color": "#888888", "flex": 2},
                    {"type": "text", "text": menu["name"] if menu else "", "size": "sm",
                     "weight": "bold", "flex": 3, "wrap": True},
                ]},
                {"type": "box", "layout": "horizontal", "margin": "md", "contents": [
                    {"type": "text", "text": "担当", "size": "sm", "color": "#888888", "flex": 2},
                    {"type": "text", "text": staff_name, "size": "sm",
                     "weight": "bold", "flex": 3, "wrap": True},
                ]},
                {"type": "box", "layout": "horizontal", "margin": "md", "contents": [
                    {"type": "text", "text": "日付", "size": "sm", "color": "#888888", "flex": 2},
                    {"type": "text", "text": session["date"], "size": "sm",
                     "weight": "bold", "flex": 3},
                ]},
                {"type": "box", "layout": "horizontal", "margin": "md", "contents": [
                    {"type": "text", "text": "時間", "size": "sm", "color": "#888888", "flex": 2},
                    {"type": "text", "text": session["time"], "size": "sm",
                     "weight": "bold", "flex": 3},
                ]},
                {"type": "box", "layout": "horizontal", "margin": "md", "contents": [
                    {"type": "text", "text": "料金", "size": "sm", "color": "#888888", "flex": 2},
                    {"type": "text", "text": f"¥{menu['price']:,}" if menu else "",
                     "size": "sm", "weight": "bold", "flex": 3, "color": BRAND_COLOR},
                ]},
                {"type": "separator", "margin": "lg"},
                {"type": "text", "text": "お名前とお電話番号をメッセージで送ってください",
                 "size": "xs", "color": "#999999", "margin": "lg", "wrap": True},
                {"type": "text", "text": "例: 山田太郎 090-1234-5678",
                 "size": "xs", "color": BRAND_COLOR, "margin": "sm"},
            ],
        },
    }


def build_reservation_list_flex(reservations):
    if not reservations:
        return {
            "type": "bubble",
            "body": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "text", "text": "📋 予約一覧", "weight": "bold", "size": "lg"},
                    {"type": "text", "text": "現在、ご予約はありません。",
                     "size": "sm", "color": "#666666", "margin": "md"},
                ],
            },
        }

    bubbles = []
    for r in reservations[:5]:
        staff_text = r.get("staff_name") or "指名なし"
        bubble = {
            "type": "bubble", "size": "kilo",
            "body": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "text", "text": f"📅 {r['reservation_date']}",
                     "weight": "bold", "size": "md"},
                    {"type": "text", "text": f"🕐 {r['reservation_time']}〜",
                     "size": "sm", "margin": "sm"},
                    {"type": "text", "text": f"✂️ {r['menu_name']}",
                     "size": "sm", "margin": "sm", "wrap": True},
                    {"type": "text", "text": f"👤 担当: {staff_text}",
                     "size": "sm", "margin": "sm"},
                ],
            },
        }
        bubbles.append(bubble)
    return {"type": "carousel", "contents": bubbles}


def build_cancel_list_flex(reservations):
    if not reservations:
        return {
            "type": "bubble",
            "body": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "text", "text": "❌ キャンセル", "weight": "bold", "size": "lg"},
                    {"type": "text", "text": "キャンセルできる予約はありません。",
                     "size": "sm", "color": "#666666", "margin": "md"},
                ],
            },
        }

    bubbles = []
    for r in reservations[:5]:
        staff_text = r.get("staff_name") or "指名なし"
        bubble = {
            "type": "bubble", "size": "kilo",
            "body": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "text", "text": f"📅 {r['reservation_date']}",
                     "weight": "bold", "size": "md"},
                    {"type": "text", "text": f"🕐 {r['reservation_time']}〜",
                     "size": "sm", "margin": "sm"},
                    {"type": "text", "text": f"✂️ {r['menu_name']}",
                     "size": "sm", "margin": "sm", "wrap": True},
                    {"type": "text", "text": f"👤 担当: {staff_text}",
                     "size": "sm", "margin": "sm"},
                ],
            },
            "footer": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "button", "action": {"type": "postback",
                     "label": "この予約をキャンセル",
                     "data": f"action=confirm_cancel&reservation_id={r['id']}"},
                     "style": "primary", "color": "#FF4444", "height": "sm"}
                ],
            },
        }
        bubbles.append(bubble)
    return {"type": "carousel", "contents": bubbles}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# プライバシーポリシー・利用規約
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.route("/privacy")
def privacy_policy():
    return render_template("privacy.html", store_name=STORE_NAME, updated_date=datetime.datetime.now(JST).strftime("%Y年%m月%d日"))


@app.route("/terms")
def terms_of_service():
    return render_template("terms.html", store_name=STORE_NAME, updated_date=datetime.datetime.now(JST).strftime("%Y年%m月%d日"))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LINE Webhook
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@app.route("/health", methods=["GET"])
def health():
    """ヘルスチェック（Render監視用）— DB接続確認付き"""
    status = {"app": "ok", "database": "ok"}
    try:
        supabase_get("store_settings", {"select": "key", "limit": "1"})
    except Exception as e:
        status["database"] = f"error: {str(e)[:100]}"
        return jsonify(status), 503
    return jsonify(status), 200


@handler.add(FollowEvent)
def handle_follow(event):
    uid = event.source.user_id
    try:
        profile = line_bot_api.get_profile(uid)
        db_get_or_create_customer(uid, profile.display_name)
    except Exception:
        db_get_or_create_customer(uid)

    line_bot_api.reply_message(
        event.reply_token,
        FlexSendMessage(alt_text="ようこそ！", contents=build_welcome_flex()),
    )


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    uid = event.source.user_id
    text = event.message.text.strip()

    # 名前入力待ち状態
    session = session_get(uid)
    if session and session.get("step") == "awaiting_name":
        parts = text.split()
        if len(parts) >= 2:
            name = parts[0]
            phone = parts[1]
        else:
            name = text
            phone = ""

        # バリデーション
        valid_name, name = validate_name(name)
        if not valid_name:
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage(text="⚠️ お名前を正しく入力してください（50文字以内）"))
            return
        if phone:
            valid_phone, cleaned_phone = validate_phone(phone)
            if not valid_phone:
                line_bot_api.reply_message(event.reply_token,
                    TextSendMessage(text="⚠️ 電話番号の形式が正しくありません。\nお名前と電話番号をスペース区切りで再入力してください。\n例: 山田太郎 090-1234-5678"))
                return
            phone = cleaned_phone

        menu = next((m for m in MENU_ITEMS if m["id"] == session["menu_id"]), None)
        if not menu:
            line_bot_api.reply_message(event.reply_token,
                                       TextSendMessage(text="エラーが発生しました。もう一度「予約」と送ってください。"))
            session_delete(uid)
            return

        # 指名なしの場合、スタッフを自動割り当て
        staff_id = session.get("staff_id", 0)
        staff_name = session.get("staff_name")
        if staff_id == 0:
            slots = get_available_slots(session["date"], 0, menu["duration"])
            target_slot = next((s for s in slots if s["time"] == session["time"]), None)
            if target_slot and target_slot.get("available_staff_ids"):
                assigned_id = auto_assign_staff(
                    session["date"], session["time"], menu["duration"],
                    target_slot["available_staff_ids"]
                )
                staff_id = assigned_id
                assigned_staff = next((s for s in db_get_active_staff() if s["id"] == assigned_id), None)
                staff_name = assigned_staff["name"] if assigned_staff else None

        reservation_data = {
            "line_user_id": uid,
            "guest_name": name,
            "phone": phone,
            "menu_id": session["menu_id"],
            "menu_name": menu["name"],
            "staff_id": staff_id if staff_id != 0 else None,
            "staff_name": staff_name,
            "reservation_date": session["date"],
            "reservation_time": session["time"],
            "duration_minutes": menu["duration"],
            "total_price": menu["price"],
        }

        # 顧客情報更新
        customer = db_get_or_create_customer(uid, name)
        if customer:
            reservation_data["customer_id"] = customer["id"]
        db_update_customer_visit(uid, name, phone)

        saved = db_save_reservation(reservation_data)
        if saved:
            event_id = create_calendar_event(reservation_data)
            if event_id and saved.get("id"):
                supabase_patch("reservations", {"calendar_event_id": event_id},
                               {"id": f"eq.{saved['id']}"})

            staff_display = staff_name or "自動割り当て"
            confirm_text = (
                f"✅ ご予約が確定しました！\n\n"
                f"📅 {session['date']}\n"
                f"🕐 {session['time']}〜\n"
                f"✂️ {menu['name']}\n"
                f"👤 担当: {staff_display}\n"
                f"💰 ¥{menu['price']:,}\n"
                f"👤 {name} 様\n\n"
                f"ご来店お待ちしております！"
            )
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=confirm_text))

            # オーナー通知
            if OWNER_LINE_USER_ID:
                try:
                    owner_text = (
                        f"🔔 新規予約\n"
                        f"{session['date']} {session['time']}\n"
                        f"{menu['name']} / 担当:{staff_display}\n"
                        f"{name} 様 {phone}"
                    )
                    line_bot_api.push_message(OWNER_LINE_USER_ID, TextSendMessage(text=owner_text))
                except Exception:
                    pass
        else:
            line_bot_api.reply_message(event.reply_token,
                                       TextSendMessage(text="予約の保存に失敗しました。もう一度お試しください。"))

        session_delete(uid)
        return

    # キーワードで予約フロー開始
    if any(kw in text for kw in ["予約", "よやく", "メニュー"]):
        session_delete(uid)
        line_bot_api.reply_message(
            event.reply_token,
            FlexSendMessage(alt_text="メニュー選択", contents=build_menu_flex()),
        )
    elif any(kw in text for kw in ["確認", "かくにん"]):
        reservations = db_get_user_reservations(uid)
        line_bot_api.reply_message(
            event.reply_token,
            FlexSendMessage(alt_text="予約一覧", contents=build_reservation_list_flex(reservations)),
        )
    elif any(kw in text for kw in ["キャンセル", "きゃんせる", "取消"]):
        reservations = db_get_user_reservations(uid)
        line_bot_api.reply_message(
            event.reply_token,
            FlexSendMessage(alt_text="キャンセル", contents=build_cancel_list_flex(reservations)),
        )
    else:
        line_bot_api.reply_message(
            event.reply_token,
            FlexSendMessage(alt_text=f"{STORE_NAME}へようこそ", contents=build_welcome_flex()),
        )


@handler.add(PostbackEvent)
def handle_postback(event):
    uid = event.source.user_id
    data = event.postback.data
    params = dict(p.split("=", 1) for p in data.split("&") if "=" in p)
    action = params.get("action", "")

    if action == "start_reservation":
        session_delete(uid)
        line_bot_api.reply_message(
            event.reply_token,
            FlexSendMessage(alt_text="メニュー選択", contents=build_menu_flex()),
        )

    elif action == "select_menu":
        menu_id = params.get("menu_id")
        menu = next((m for m in MENU_ITEMS if m["id"] == menu_id), None)
        if not menu:
            return
        session_set(uid, {"menu_id": menu_id, "step": "staff"})
        line_bot_api.reply_message(
            event.reply_token,
            FlexSendMessage(alt_text="スタッフ選択", contents=build_staff_flex()),
        )

    elif action == "select_staff":
        staff_id = int(params.get("staff_id", 0))
        session = session_get(uid)
        if not session:
            return
        staff_name = None
        if staff_id != 0:
            staff_list = db_get_active_staff()
            staff = next((s for s in staff_list if s["id"] == staff_id), None)
            staff_name = staff["name"] if staff else None
        session_update(uid, {"staff_id": staff_id, "staff_name": staff_name, "step": "date"})

        line_bot_api.reply_message(
            event.reply_token,
            FlexSendMessage(alt_text="日付選択", contents=build_date_flex()),
        )

    elif action == "select_date":
        date_str = params.get("date")
        session = session_get(uid)
        if not session:
            return
        menu = next((m for m in MENU_ITEMS if m["id"] == session["menu_id"]), None)
        if not menu:
            return

        staff_id = session.get("staff_id", 0)
        slots = get_available_slots(date_str, staff_id, menu["duration"])

        if not slots:
            staff_text = session.get("staff_name") or "指名なし"
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"申し訳ございません。{date_str}は{staff_text}の空きがありません。\n別の日付をお選びください。"),
            )
            session_update(uid, {"step": "date"})
            return

        session_update(uid, {"date": date_str, "step": "time"})
        line_bot_api.reply_message(
            event.reply_token,
            FlexSendMessage(alt_text="時間選択", contents=build_time_flex(slots)),
        )

    elif action == "select_time":
        time_str = params.get("time")
        session = session_get(uid)
        if not session:
            return
        session_update(uid, {"time": time_str, "step": "awaiting_name"})
        # 更新後のセッションで確認画面を構築
        session["time"] = time_str
        session["step"] = "awaiting_name"

        line_bot_api.reply_message(
            event.reply_token,
            FlexSendMessage(alt_text="予約確認", contents=build_confirm_flex(session)),
        )

    elif action == "closed_day":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="申し訳ございません、その日は定休日です。\n別の日付をお選びください。"),
        )

    elif action == "check_reservation":
        reservations = db_get_user_reservations(uid)
        line_bot_api.reply_message(
            event.reply_token,
            FlexSendMessage(alt_text="予約一覧", contents=build_reservation_list_flex(reservations)),
        )

    elif action == "list_cancel":
        reservations = db_get_user_reservations(uid)
        line_bot_api.reply_message(
            event.reply_token,
            FlexSendMessage(alt_text="キャンセル", contents=build_cancel_list_flex(reservations)),
        )

    elif action == "confirm_cancel":
        reservation_id = params.get("reservation_id")
        try:
            rows = supabase_get("reservations", {
                "select": "*", "id": f"eq.{reservation_id}",
                "line_user_id": f"eq.{uid}",
            })
            if rows:
                r = rows[0]
                event_id = r.get("calendar_event_id")
                if event_id:
                    delete_calendar_event(event_id)
        except Exception:
            pass

        if db_cancel_reservation(reservation_id, uid):
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="予約をキャンセルしました。\nまたのご予約をお待ちしております。"),
            )
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="キャンセルに失敗しました。もう一度お試しください。"),
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ダッシュボード（ログイン・ログアウト）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LOGIN_PAGE = """<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ログイン</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,sans-serif;background:#f5f5f5;display:flex;align-items:center;justify-content:center;min-height:100vh}
.login{background:#fff;border-radius:16px;padding:40px 32px;width:90%%;max-width:380px;box-shadow:0 2px 12px rgba(0,0,0,.1);text-align:center}
.login h1{font-size:20px;margin-bottom:8px;color:#333}
.login small{color:#888;font-size:13px}
.login input{width:100%%;padding:12px;border:1px solid #ddd;border-radius:8px;font-size:16px;margin-top:20px}
.login input:focus{outline:none;border-color:#9B59B6}
.login button{width:100%%;padding:12px;background:#9B59B6;color:#fff;border:none;border-radius:8px;font-size:16px;font-weight:600;margin-top:12px;cursor:pointer}
.login button:hover{background:#7D3C98}
.error{color:#c62828;font-size:13px;margin-top:12px}
</style></head><body>
<div class="login">
<h1>✂️ %s</h1><small>オーナーダッシュボード</small>
<form method="POST"><input type="password" name="password" placeholder="パスワードを入力" autofocus>
<button type="submit">ログイン</button></form>
%s</div></body></html>"""


@app.route("/dashboard/login", methods=["GET", "POST"])
def dashboard_login():
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw and pw == DASHBOARD_PASSWORD:
            flask_session["dashboard_authenticated"] = True
            return redirect(url_for("dashboard"))
        error_html = '<div class="error">パスワードが正しくありません</div>'
        return LOGIN_PAGE % (STORE_NAME, error_html), 401
    return LOGIN_PAGE % (STORE_NAME, "")


@app.route("/dashboard/logout")
def dashboard_logout():
    flask_session.clear()
    return redirect(url_for("dashboard_login"))


@app.route("/dashboard")
@dashboard_auth_required
def dashboard():
    return render_template("dashboard.html", store_name=STORE_NAME)


# ━━━ ダッシュボードAPI ━━━
@app.route("/api/reservations")
@dashboard_auth_required
def api_reservations():
    period = request.args.get("period", "week")
    today = datetime.datetime.now(JST).date()

    if period == "today":
        start_date = today
        end_date = today
    elif period == "month":
        start_date = today
        end_date = today + datetime.timedelta(days=30)
    else:
        start_date = today
        end_date = today + datetime.timedelta(days=7)

    try:
        rows = supabase_get("reservations", {
            "select": "*",
            "reservation_date": f"gte.{start_date.isoformat()}",
            "order": "reservation_date.asc,reservation_time.asc",
        })
        if period != "month":
            rows = [r for r in rows if r["reservation_date"] <= end_date.isoformat()]
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/reservations/<int:rid>/cancel", methods=["POST"])
@dashboard_auth_required
def api_cancel(rid):
    try:
        rows = supabase_get("reservations", {"select": "*", "id": f"eq.{rid}"})
        if rows:
            r = rows[0]
            event_id = r.get("calendar_event_id")
            if event_id:
                delete_calendar_event(event_id)
            # LINEで通知
            try:
                line_bot_api.push_message(
                    r["line_user_id"],
                    TextSendMessage(text=(
                        f"ご予約のキャンセルをお知らせします。\n\n"
                        f"📅 {r['reservation_date']} {r['reservation_time']}\n"
                        f"✂️ {r['menu_name']}\n\n"
                        f"またのご予約をお待ちしております。"
                    )),
                )
            except Exception:
                pass
        supabase_patch("reservations", {"status": "cancelled"}, {"id": f"eq.{rid}"})
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/customers")
@dashboard_auth_required
def api_customers():
    try:
        rows = supabase_get("customers", {
            "select": "*",
            "order": "visit_count.desc",
        })
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/closed_days")
@dashboard_auth_required
def api_closed_days():
    try:
        rows = supabase_get("closed_days", {"select": "*", "order": "id.asc"})
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/closed_days", methods=["POST"])
@dashboard_auth_required
def api_add_closed_day():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "リクエストボディが空です"}), 400
        safe_data = {}
        if "day_of_week" in data:
            v = data["day_of_week"]
            if not (isinstance(v, int) and 0 <= v <= 6):
                return jsonify({"error": "曜日の値が不正です（0〜6）"}), 400
            safe_data["day_of_week"] = v
        if "closed_date" in data:
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(data["closed_date"])):
                return jsonify({"error": "日付の形式が不正です"}), 400
            safe_data["closed_date"] = data["closed_date"]
        safe_data["reason"] = sanitize_text(data.get("reason", "定休日"), max_length=100)
        safe_data["is_recurring"] = bool(data.get("is_recurring", False))
        res = supabase_post("closed_days", safe_data)
        return jsonify(res[0] if res else {})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/closed_days/<int:cid>", methods=["DELETE"])
@dashboard_auth_required
def api_delete_closed_day(cid):
    try:
        supabase_delete("closed_days", {"id": f"eq.{cid}"})
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ━━━ スタッフ管理API ━━━
@app.route("/api/staff")
@dashboard_auth_required
def api_staff():
    try:
        rows = supabase_get("staff", {"select": "*", "order": "sort_order.asc"})
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/staff", methods=["POST"])
@dashboard_auth_required
def api_add_staff():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "リクエストボディが空です"}), 400
        safe_data = {
            "name": sanitize_text(data.get("name", ""), max_length=50),
            "is_active": bool(data.get("is_active", True)),
            "sort_order": int(data.get("sort_order", 0)),
        }
        if not safe_data["name"]:
            return jsonify({"error": "スタッフ名は必須です"}), 400
        res = supabase_post("staff", safe_data)
        return jsonify(res[0] if res else {})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/staff/<int:sid>", methods=["PATCH"])
@dashboard_auth_required
def api_update_staff(sid):
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "リクエストボディが空です"}), 400
        safe_data = {}
        if "name" in data:
            safe_data["name"] = sanitize_text(data["name"], max_length=50)
        if "is_active" in data:
            safe_data["is_active"] = bool(data["is_active"])
        if "sort_order" in data:
            safe_data["sort_order"] = int(data["sort_order"])
        res = supabase_patch("staff", safe_data, {"id": f"eq.{sid}"})
        return jsonify(res[0] if res else {})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/staff/<int:sid>", methods=["DELETE"])
@dashboard_auth_required
def api_delete_staff(sid):
    try:
        supabase_patch("staff", {"is_active": False}, {"id": f"eq.{sid}"})
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ━━━ スタッフ休日管理API ━━━
@app.route("/api/staff/<int:sid>/days_off")
@dashboard_auth_required
def api_staff_days_off(sid):
    try:
        rows = supabase_get("staff_days_off", {
            "select": "*",
            "staff_id": f"eq.{sid}",
            "order": "id.asc",
        })
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/staff/<int:sid>/days_off", methods=["POST"])
@dashboard_auth_required
def api_add_staff_day_off(sid):
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "��クエストボディが空です"}), 400
        safe_data = {"staff_id": sid}
        if "day_of_week" in data:
            v = data["day_of_week"]
            if not (isinstance(v, int) and 0 <= v <= 6):
                return jsonify({"error": "曜日の値が��正です（0〜6）"}), 400
            safe_data["day_of_week"] = v
        if "off_date" in data:
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(data["off_date"])):
                return jsonify({"error": "日付の形式が不正です"}), 400
            safe_data["off_date"] = data["off_date"]
        safe_data["is_recurring"] = bool(data.get("is_recurring", False))
        res = supabase_post("staff_days_off", safe_data)
        return jsonify(res[0] if res else {})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/staff_days_off/<int:did>", methods=["DELETE"])
@dashboard_auth_required
def api_delete_staff_day_off(did):
    try:
        supabase_delete("staff_days_off", {"id": f"eq.{did}"})
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ━━━ 設定API ━━━
@app.route("/api/settings/deadline")
@dashboard_auth_required
def api_get_deadline():
    hours = get_booking_deadline_hours()
    return jsonify({"booking_deadline_hours": hours})


@app.route("/api/settings/deadline", methods=["POST"])
@dashboard_auth_required
def api_set_deadline():
    data = request.get_json()
    hours = data.get("hours", 1)
    db_set_setting("booking_deadline_hours", str(hours))
    return jsonify({"success": True, "booking_deadline_hours": hours})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# リマインド（前日通知）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def send_reminders():
    reminders = db_get_tomorrow_reminders()
    for r in reminders:
        try:
            staff_text = r.get("staff_name") or "担当未定"
            text = (
                f"📅 明日のご予約リマインド\n\n"
                f"🕐 {r['reservation_time']}〜\n"
                f"✂️ {r['menu_name']}\n"
                f"👤 担当: {staff_text}\n\n"
                f"ご来店お待ちしております！"
            )
            line_bot_api.push_message(r["line_user_id"], TextSendMessage(text=text))
            db_mark_reminded(r["id"])
        except Exception as e:
            logger.error(f"リマインド送信エラー: {e}")


# スケジューラ（毎日18時にリマインド ＋ 10分毎に期限切れセッション削除）
scheduler = BackgroundScheduler(timezone="Asia/Tokyo")
scheduler.add_job(send_reminders, "cron", hour=18, minute=0)
scheduler.add_job(cleanup_expired_sessions, "interval", minutes=10)
scheduler.start()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# トップページ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.route("/")
def index():
    return f"{STORE_NAME} LINE予約Bot is running!"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

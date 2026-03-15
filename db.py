import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone

def get_db_connection():
    return psycopg2.connect(os.getenv("DATABASE_URL"))

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    # 销售记录表
    cursor.execute('''CREATE TABLE IF NOT EXISTS sales (
        id SERIAL PRIMARY KEY, raw_amount NUMERIC, ex_rate NUMERIC, 
        fee NUMERIC, net_amount NUMERIC, details JSONB, 
        date TEXT, added_by BIGINT, chat_id BIGINT)''')
    
    # 权限与时区表
    cursor.execute('''CREATE TABLE IF NOT EXISTS group_permissions (
        chat_id BIGINT, user_id BIGINT, role TEXT, 
        expiry_timestamp TIMESTAMP WITH TIME ZONE,
        tz_offset INTEGER DEFAULT 0, 
        PRIMARY KEY (chat_id, user_id))''')
    conn.commit()
    cursor.close()
    conn.close()

def get_chat_tz(chat_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT tz_offset FROM group_permissions WHERE chat_id = %s LIMIT 1", (chat_id,))
    res = cursor.fetchone()
    conn.close()
    return res[0] if res else 0

def get_user_permission(user_id, chat_id):
    if str(user_id) == os.getenv("MASTER_ID", "0"): return "master", None, 0
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT role, expiry_timestamp, tz_offset FROM group_permissions WHERE chat_id = %s AND user_id = %s", (chat_id, user_id))
    res = cursor.fetchone()
    conn.close()
    return res if res else (None, None, 0)

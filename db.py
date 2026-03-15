import os
import psycopg2
from psycopg2.extras import RealDictCursor

def get_db_connection():
    return psycopg2.connect(os.getenv("DATABASE_URL"))

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    # 销售记录表 (ยอดขาย)
    cursor.execute('''CREATE TABLE IF NOT EXISTS sales (
        id SERIAL PRIMARY KEY, raw_amount NUMERIC, ex_rate NUMERIC, 
        fee NUMERIC, net_amount NUMERIC, details JSONB, 
        date TEXT, added_by BIGINT, chat_id BIGINT)''')
    
    # 权限与群组设置 (สิทธิ์และตั้งค่ารายกลุ่ม)
    cursor.execute('''CREATE TABLE IF NOT EXISTS group_permissions (
        chat_id BIGINT, user_id BIGINT, role TEXT, 
        expiry_timestamp TIMESTAMP WITH TIME ZONE,
        tz_offset INTEGER DEFAULT 0, 
        PRIMARY KEY (chat_id, user_id))''')
    conn.commit()
    cursor.close()
    conn.close()

def get_group_config(chat_id, user_id):
    """ดึงข้อมูลสิทธิ์และตั้งค่า โดยแยกตามกลุ่มและบุคคล"""
    if str(user_id) == os.getenv("MASTER_ID", "0"):
        # สำหรับ Master ให้ดึงค่า TZ ของกลุ่มนั้นๆ มาด้วย
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT tz_offset FROM group_permissions WHERE chat_id = %s LIMIT 1", (chat_id,))
        res = cur.fetchone()
        conn.close()
        return "master", None, (res[0] if res else 0)

    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT role, expiry_timestamp, tz_offset FROM group_permissions WHERE chat_id = %s AND user_id = %s", (chat_id, user_id))
    res = cur.fetchone()
    conn.close()
    return res if res else (None, None, 0)

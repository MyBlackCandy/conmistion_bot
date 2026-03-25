import os
import json
from datetime import datetime, timedelta, timezone
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from psycopg2.extras import RealDictCursor
from db import init_db, get_db_connection, get_group_config

# --- 权限校验 ---
def get_status(user_id, chat_id):
    role, expiry, tz_off = get_group_config(chat_id, user_id)
    if str(user_id) == os.getenv("MASTER_ID", "0"): 
        return True, "管理员 (无限制)", tz_off
    if not expiry: return False, "未获得本项目授权", tz_off
    now_utc = datetime.now(timezone.utc)
    if now_utc > expiry: return False, "本项目已到期", tz_off
    diff = expiry - now_utc
    return True, f"{diff.days}天 {diff.seconds // 3600}小时", tz_off

# --- 辅助: 中文名 ---
def get_line_name(n):
    lines = ["", "一线", "二线", "三线", "四线", "五线", "六线", "七线", "八线", "九线", "十线"]
    return lines[n] if n < len(lines) else f"{n}线"

# --- 指令: 提成报表 (UI 优化版) ---
async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id, chat_id = update.effective_user.id, update.effective_chat.id
    active, _, tz_off = get_status(user_id, chat_id)
    if not active: return

    now_local = datetime.now(timezone(timedelta(hours=tz_off)))
    month_query, month_display = now_local.strftime("%Y-%m"), f"{now_local.year}年{now_local.month}月"
    
    try:
        conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM sales WHERE chat_id=%s AND date LIKE %s ORDER BY id ASC", (chat_id, f"{month_query}%"))
        rows = cur.fetchall(); conn.close()
        
        if not rows: return await update.message.reply_text(f"📊 {month_display} | 暂无记录")

        msg = f"📋 **{month_display} 入金报表**\n"
        msg += f"━━━━━━━━━━━━━━━\n\n"
        
        person_sum = {} # โครงสร้าง: { 'Name': { 'lines': { '一线': {'raw':0, 'net':0, 'comm':0} } } }
        grand_raw, grand_net, grand_comm = 0, 0, 0
        
        for r in rows:
            net_val = float(r['net_amount'])
            raw_val = float(r['raw_amount'])
            grand_raw += raw_val
            grand_net += net_val
            
            msg += f"📅 {r['date']} \n"
            msg += f"▫️入金 : {raw_val:,.0f} 日元 | 入金 : {net_val:,.0f} U \n"
            msg += f"▫️计算 : {raw_val:,.0f} ÷ {r['ex_rate']} - {r['fee']}%\n"
            
            line_entries = []
            for d in r['details']:
                name, l_cn, comm = d['name'], get_line_name(d['line']), float(d['comm'])
                line_entries.append(f"{name}({l_cn}) : {comm:,.0f}")
                
                if name not in person_sum:
                    person_sum[name] = {'lines': {}}
                
                if l_cn not in person_sum[name]['lines']:
                    person_sum[name]['lines'][l_cn] = {'raw': 0, 'net': 0, 'comm': 0}
                
                # สะสมยอดแยกตามสายของแต่ละคน
                person_sum[name]['lines'][l_cn]['raw'] += raw_val
                person_sum[name]['lines'][l_cn]['net'] += net_val
                person_sum[name]['lines'][l_cn]['comm'] += comm
                grand_comm += comm
            
            msg += f"└ 👤 {' | '.join(line_entries)}\n"


         # --- Grand Total Section ---
        msg += f"\n📊 **全月总计**\n"
        msg += f"━━━━━━━━━━━━━━━\n"
        msg += f"💰 **总入金 : ** {grand_raw:,.0f} 日元\n"
        msg += f"📥 **总入金 : ** {grand_net:,.0f} U\n"
        msg += f"🧧 **总提成 : ** {grand_comm:,.0f} U\n"
        msg += f"━━━━━━━━━━━━━━━"


        await update.message.reply_text(msg_total, parse_mode='Markdown')
        
            
         # --- Summary Section (รายคน - แยกตามสาย) ---
        msg_summary = f"👤 **个人提成汇总**\n"
        msg_summary += f"━━━━━━━━━━━━━━━\n"
        
        for name in sorted(person_sum.keys()):
            msg_summary += f"📌 **{name}**\n"
            p_total_comm = 0
            
            for l_name, data in sorted(person_sum[name]['lines'].items()):
                msg_summary += f"    📍 {l_name}:\n"
                msg_summary += f"      ■ 入金 : {data['raw']:,.0f} 日元\n"
                msg_summary += f"      ■ 入金 : {data['net']:,.0f} U\n"
                msg_summary += f"      ■ 提成 : {data['comm']:,.0f} U\n"
                p_total_comm += data['comm']
            
            msg_summary += f"    💰 **总计提成 : {p_total_comm:,.0f} U**\n"
            msg_summary += f"⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"

        # ส่งข้อความที่ 2 แยกต่างหาก
        await update.message.reply_text(msg_summary, parse_mode='Markdown')

    except Exception as e:
        print(f"Report Error: {e}")

# --- 记录处理 (UI 优化版) ---
async def handle_plus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id, chat_id = update.effective_user.id, update.effective_chat.id
    active, _, tz_off = get_status(user_id, chat_id)
    if not active: return
    
    text = update.message.text.strip()
    if not text.startswith('+'): return
    
    try:
        p = text[1:].split()
        # ตรวจสอบว่าข้อมูลพื้นฐานครบ (raw, rate, fee)
        if len(p) < 3: return

        raw, rate, fee_val = float(p[0]), float(p[1]), float(p[2].replace('%',''))
        
        # สูตรการคำนวณ: (ยอดเยน / เรท) - ค่าธรรมเนียมรถ
        net = (raw / rate) * (1 - (fee_val/100))
        
        details, line_summaries = [], []
        
        # วนลูปเก็บข้อมูลรายชื่อและ % คอมมิชชั่น
        for i in range(0, len(p[3:]), 2):
            if i+1 >= len(p[3:]): break # กัน Error กรณีใส่ชื่อแต่ไม่ใส่ %
            
            line_no = (i//2) + 1
            name, comm_p = p[3+i], float(p[4+i].replace('%',''))
            
            # คำนวณยอดคอมมิชชั่นจากยอด Net (U)
            comm_amt = net * (comm_p / 100)
            l_cn = get_line_name(line_no)
            
            details.append({"line": line_no, "name": name, "comm": comm_amt})
            line_summaries.append(f"    ■ {l_cn} | {name} : {comm_amt:,.0f} U")
        
        # บันทึกลง Database
        l_date = datetime.now(timezone(timedelta(hours=tz_off))).strftime("%Y-%m-%d")
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""INSERT INTO sales (raw_amount, ex_rate, fee, net_amount, details, date, added_by, chat_id) 
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", 
                    (raw, rate, fee_val, net, json.dumps(details), l_date, user_id, chat_id))
        conn.commit()
        conn.close()

        # สร้างข้อความยืนยันการลงบันทึก (Receipt)
        res_msg = (
            f"✅ **登记成功** (บันทึกสำเร็จ)\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📊 公式 : {raw:,.0f} 日元 ÷ {rate} - {fee_val}% \n"
            f"💰 净入 : **{net:,.0f}** U\n"
            f"⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
            + "\n".join(line_summaries) + 
            f"\n━━━━━━━━━━━━━━━"
        )
        
        # ส่งข้อความใบเสร็จ
        await update.message.reply_text(res_msg, parse_mode='Markdown')
        
        # เรียกฟังก์ชัน report เพื่อส่งข้อความสรุปแยกอีก 2 ข้อความ
        await report(update, context)
        
    except Exception as e:
        print(f"Handle Plus Error: {e}")
        # กรณี Error อาจจะแจ้งเตือนผู้ใช้เล็กน้อย
        # await update.message.reply_text("❌ รูปแบบข้อมูลไม่ถูกต้อง กรุณาตรวจสอบอีกครั้ง")



# --- 撤销 Undo ---
async def undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id, chat_id = update.effective_user.id, update.effective_chat.id
    role, _, _ = get_group_config(chat_id, user_id)
    if str(user_id) != os.getenv("MASTER_ID", "0") and role != "owner": return

    try:
        conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT id, net_amount FROM sales WHERE chat_id = %s ORDER BY id DESC LIMIT 1", (chat_id,))
        last_rec = cur.fetchone()
        if last_rec:
            cur.execute("DELETE FROM sales WHERE id = %s", (last_rec['id'],))
            conn.commit()
            await update.message.reply_text(f"🗑️ **已撤销成功**\n金额: `{float(last_rec['net_amount']):,.2f}`")
            await report(update, context) 
        conn.close()
    except: pass

# --- อื่นๆ (set_days, set_tz, myid) ---
async def set_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ตรวจสอบสิทธิ์ Master
    if str(update.effective_user.id) != os.getenv("MASTER_ID", "0"): 
        return
    
    try:
        # รับค่าจาก Command: /set_days [target_id] [days]
        chat_id = update.effective_chat.id
        target_id = int(context.args[0])
        days = float(context.args[1])
        
        # ใช้ UTC เป็นแกนกลาง ไม่มีการบวก/ลบ offset ใดๆ ในขั้นตอนนี้
        now_utc = datetime.now(timezone.utc)
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        # ตรวจสอบวันหมดอายุเดิม
        cur.execute("SELECT expiry_timestamp FROM group_permissions WHERE chat_id=%s AND user_id=%s", (chat_id, target_id))
        res = cur.fetchone()
        
        # ถ้ามีวันหมดอายุเดิมและยังไม่หมด ให้ต่อจากของเดิม ถ้าไม่มีให้เริ่มจากตอนนี้
        base = res[0] if res and res[0] and res[0] > now_utc else now_utc
        new_exp = base + timedelta(days=days)
        
        # บันทึกค่าลง Database (บันทึกเป็น Timestamp ตรงๆ)
        cur.execute("""
            INSERT INTO group_permissions (chat_id, user_id, role, expiry_timestamp) 
            VALUES (%s, %s, 'user', %s) 
            ON CONFLICT (chat_id, user_id) 
            DO UPDATE SET expiry_timestamp = %s, role = 'staff'
        """, (chat_id, target_id, new_exp, new_exp))
        
        conn.commit()
        conn.close()
        
        # แสดงผลลัพธ์การบันทึก
        await update.message.reply_text(f"✅ 授权成功 (บันทึกสิทธิ์สำเร็จ)\n到期时间: `{new_exp.strftime('%Y-%m-%d %H:%M:%S')}` (UTC)")
        
    except Exception as e:
        print(f"Set Days Error: {e}")
        await update.message.reply_text("💡 格式: `/set_days [用户ID] [天数]`")

async def set_tz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != os.getenv("MASTER_ID", "0"): return
    try:
        chat_id, offset = update.effective_chat.id, int(context.args[0])
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("UPDATE group_permissions SET tz_offset = %s WHERE chat_id = %s", (offset, chat_id))
        cur.execute("INSERT INTO group_permissions (chat_id, user_id, role, tz_offset) VALUES (%s, %s, 'master', %s) ON CONFLICT (chat_id, user_id) DO UPDATE SET tz_offset=%s", (chat_id, update.effective_user.id, offset, offset))
        conn.commit(); conn.close()
        await update.message.reply_text(f"✅ GMT {offset:+}")
    except: pass

if __name__ == '__main__':
    init_db()
    app = Application.builder().token(os.getenv("BOT_TOKEN")).build()
    app.add_handler(CommandHandler("undo", undo))
    app.add_handler(CommandHandler("add_user", set_days))
    app.add_handler(CommandHandler("set_tz", set_tz))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("myid", lambda u,c: u.message.reply_text(f"ID: `{u.effective_user.id}`\nChat: `{u.effective_chat.id}`")))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_plus))
    app.run_polling()

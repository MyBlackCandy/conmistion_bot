import os
import json
from datetime import datetime, timedelta, timezone
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from db import init_db, get_db_connection, get_group_config

# --- 检查访问权限与时间 (基于群组) ---
def get_status(user_id, chat_id):
    role, expiry, tz_off = get_group_config(chat_id, user_id)
    if role == "master": return True, "管理员 (无限制)", tz_off
    if not expiry: return False, "未获得本项目授权", tz_off
    
    now_utc = datetime.now(timezone.utc)
    if now_utc > expiry: return False, "本项目已到期", tz_off
    
    diff = expiry - now_utc
    days, hours = diff.days, diff.seconds // 3600
    minutes = (diff.seconds % 3600) // 60
    return True, f"{days}天 {hours}小时 {minutes}分", tz_off

# --- 指令: 为特定群组的用户充值 ---
async def set_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != os.getenv("MASTER_ID", "0"): return
    try:
        chat_id = update.effective_chat.id
        target_id, days = int(context.args[0]), float(context.args[1])
        now_utc = datetime.now(timezone.utc)
        
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT expiry_timestamp FROM group_permissions WHERE chat_id=%s AND user_id=%s", (chat_id, target_id))
        res = cur.fetchone()
        
        # 如果还在有效期内，则累加时间
        base = res[0] if res and res[0] and res[0] > now_utc else now_utc
        new_exp = base + timedelta(days=days)
        
        cur.execute("""INSERT INTO group_permissions (chat_id, user_id, role, expiry_timestamp) 
                       VALUES (%s,%s,'user',%s) 
                       ON CONFLICT (chat_id, user_id) DO UPDATE SET expiry_timestamp=%s""", 
                    (chat_id, target_id, new_exp, new_exp))
        conn.commit(); conn.close()
        await update.message.reply_text(f"✅ 充值成功\n群组ID: `{chat_id}`\n用户ID: `{target_id}`\n新到期时间: `{new_exp.strftime('%Y-%m-%d %H:%M')} UTC`")
    except: await update.message.reply_text("💡 格式: `/set_days [用户ID] [天数]`")

# --- 指令: 设置当前群组时区 ---
async def set_tz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != os.getenv("MASTER_ID", "0"): return
    try:
        chat_id, offset = update.effective_chat.id, int(context.args[0])
        conn = get_db_connection(); cur = conn.cursor()
        # 更新该群组所有记录的时区设置
        cur.execute("UPDATE group_permissions SET tz_offset = %s WHERE chat_id = %s", (offset, chat_id))
        # 确保群组在表中存在
        cur.execute("INSERT INTO group_permissions (chat_id, user_id, role, tz_offset) VALUES (%s, %s, 'master', %s) ON CONFLICT (chat_id, user_id) DO UPDATE SET tz_offset=%s", (chat_id, update.effective_user.id, offset, offset))
        conn.commit(); conn.close()
        await update.message.reply_text(f"✅ 本群时区已更新为: GMT {offset:+}")
    except: await update.message.reply_text("💡 格式: `/set_tz [小时]`")

# --- 指令: 仅看本群报表 ---
async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id, chat_id = update.effective_user.id, update.effective_chat.id
    active, _, tz_off = get_status(user_id, chat_id)
    
    # เฉพาะ Master หรือคนที่มีสิทธิ์ในกลุ่มถึงจะดูได้
    if not active and str(user_id) != os.getenv("MASTER_ID", "0"): return

    # กำหนดเดือนปัจจุบันตาม Timezone ของกลุ่ม
    month = datetime.now(timezone(timedelta(hours=tz_off))).strftime("%Y-%m")
    
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM sales WHERE chat_id=%s AND date LIKE %s ORDER BY id ASC", (chat_id, f"{month}%"))
    rows = cur.fetchall()
    conn.close()
    
    if not rows: 
        return await update.message.reply_text(f"📊 {month} | 暂无记录 (暫無記錄)")

    hist_header = f"📋 **{month} 财务记录 (历史)**\n{'━'*15}\n"
    summ_header = f"\n👤 **个人汇总 (总结)**\n{'━'*15}\n"
    
    hist_body = ""
    person_sum = {}

    # --- ส่วนที่ 1: ประวัติการบันทึก (2 บรรทัดต่อรายการ) ---
    for r in rows:
        # บรรทัดที่ 1: วันที่ | สูตรคำนวณ = ยอดสุทธิ
        hist_body += f"🔹 `{r['date']}` | {float(r['raw_amount']):,.0f}/{r['ex_rate']}-{r['fee']}% = **{float(r['net_amount']):,.2f}**\n"
        
        line_entries = []
        for d in r['details']:
            name, l_no, comm = d['name'], d['line'], float(d['comm'])
            line_entries.append(f"{name}(L{l_no}):{comm:,.0f}")
            
            # เก็บข้อมูลไว้สรุปรายคน
            if name not in person_sum: person_sum[name] = {}
            person_sum[name][f"L{l_no}"] = person_sum[name].get(f"L{l_no}", 0) + comm
        
        # บรรทัดที่ 2: รายชื่อสายงานทั้งหมดในดีลนี้
        hist_body += f"└ {', '.join(line_entries)}\n"

    # --- ส่วนที่ 2: สรุปรายคน (2 บรรทัดต่อคน) ---
    summ_body = ""
    for name in sorted(person_sum.keys()):
        total_amt = sum(person_sum[name].values())
        # บรรทัดที่ 1: ชื่อ | ยอดรวม
        summ_body += f"📌 **{name}** | 总计: `{total_amt:,.2f}`\n"
        
        # บรรทัดที่ 2: รายละเอียดแต่ละ Line
        lines_info = [f"{l}:{v:,.2f}" for l, v in sorted(person_sum[name].items())]
        summ_body += f"└ {', '.join(lines_info)}\n"

    await update.message.reply_text(hist_header + hist_body + summ_header + summ_body, parse_mode='Markdown')

# --- 记录处理 ---
async def handle_plus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id, chat_id = update.effective_user.id, update.effective_chat.id
    active, time_left, tz_off = get_status(user_id, chat_id)
    
    if not active:
        return # 无权限时不回复

    text = update.message.text.strip()
    if not text.startswith('+'): return
    
    try:
        p = text[1:].split()
        # [0]=ยอดดิบ, [1]=เรท, [2]=ค่าธรรมเนียม
        raw, rate, fee_val = float(p[0]), float(p[1]), float(p[2].replace('%',''))
        net = (raw / rate) * (1 - (fee_val/100))
        
        # คำนวณรายละเอียดสายงานแบบ Dynamic
        details = []
        line_summaries = []
        # เริ่มวนลูปจากตำแหน่งที่ 3 (ชื่อคนแรก)
        for i in range(0, len(p[3:]), 2):
            line_no = (i//2) + 1
            name = p[3+i]
            comm_p = float(p[4+i].replace('%',''))
            comm_amt = net * (comm_p / 100)
            
            details.append({"line": line_no, "name": name, "comm": comm_amt})
            line_summaries.append(f"👤 {name} (L{line_no}): `{comm_amt:,.2f}`")
        
        # บันทึกลงฐานข้อมูล
        l_date = datetime.now(timezone(timedelta(hours=tz_off))).strftime("%Y-%m-%d")
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("INSERT INTO sales (raw_amount, ex_rate, fee, net_amount, details, date, added_by, chat_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (raw, rate, fee_val, net, json.dumps(details), l_date, user_id, chat_id))
        conn.commit(); conn.close()

        # --- ส่วนแสดงผลตอบกลับหลังบันทึก ---
        response = (
            f"✅ **录入成功 (记录已保存)**\n"
            f"💰 净值: `{net:,.2f}`\n"
            f"📊 计算: `{raw:,.0f} / {rate} - {fee_val}%` \n"
            f"━━━━━━━━━━━━━━━\n"
            + "\n".join(line_summaries) + "\n"
            f"━━━━━━━━━━━━━━━\n"
            f"⏳ 剩余有效期: {time_left}"
        )
        
        await update.message.reply_text(response, parse_mode='Markdown')

    except Exception as e:
        # หากเกิดข้อผิดพลาดในการคำนวณหรือรูปแบบไม่ถูกต้อง บอทจะเงียบไว้เพื่อไม่ให้กวนการสนทนา
        pass

if __name__ == '__main__':
    init_db()
    app = Application.builder().token(os.getenv("BOT_TOKEN")).build()
    app.add_handler(CommandHandler("set_days", set_days))
    app.add_handler(CommandHandler("set_tz", set_tz))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("myid", lambda u,c: u.message.reply_text(f"Your ID: `{u.effective_user.id}`\nChat ID: `{u.effective_chat.id}`")))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_plus))
    app.run_polling()

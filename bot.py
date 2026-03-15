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

        # --- Header ---
        msg = f"📋 **{month_display} 入金报表**\n"
        msg += f"━━━━━━━━━━━━━━━\n\n"
        
        person_sum = {}
        for r in rows:
            # รายการบันทึก
            msg += f"{r['date']}\n"
            msg += f"     入金U: {float(r['net_amount']):,.2f} | ({float(r['raw_amount']):,.0f}/{r['ex_rate']}-{r['fee']}%)\n"
            
            line_entries = []
            for d in r['details']:
                name, l_cn, comm = d['name'], get_line_name(d['line']), float(d['comm'])
                line_entries.append(f"{name}({l_cn}) : {comm:,.2f}")
                if name not in person_sum: person_sum[name] = {}
                person_sum[name][l_cn] = person_sum[name].get(l_cn, 0) + comm
            
            msg += f"     {' | '.join(line_entries)}\n"
           

        # --- Summary Section ---
        msg += f"\n👤**个人提成**\n"
        msg += f"━━━━━━━━━━━━━━━\n"
        
        for name in sorted(person_sum.keys()):
            total = sum(person_sum[name].values())
            msg += f"📌 **{name}**"
            msg += f" --->  总提成: {total:,.2f}\n"
            lines_info = [f"{l} : {v:,.2f}" for l, v in sorted(person_sum[name].items())]
            msg += f"{' | '.join(lines_info)}\n"
        
        msg += f"━━━━━━━━━━━━━━━"
        await update.message.reply_text(msg, parse_mode='Markdown')

    except Exception as e:
        print(f"Report Error: {e}")

# --- 记录处理 (UI 优化版) ---
async def handle_plus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id, chat_id = update.effective_user.id, update.effective_chat.id
    active, time_left, tz_off = get_status(user_id, chat_id)
    if not active: return
    text = update.message.text.strip()
    if not text.startswith('+'): return
    
    try:
        p = text[1:].split()
        raw, rate, fee_val = float(p[0]), float(p[1]), float(p[2].replace('%',''))
        net = (raw / rate) * (1 - (fee_val/100))
        details, line_summaries = [], []
        
        for i in range(0, len(p[3:]), 2):
            line_no = (i//2) + 1
            name, comm_p = p[3+i], float(p[4+i].replace('%',''))
            comm_amt, l_cn = net * (comm_p / 100), get_line_name(line_no)
            details.append({"line": line_no, "name": name, "comm": comm_amt})
            line_summaries.append(f"   {l_cn} | {name}: `{comm_amt:,.2f}`")
        
        l_date = datetime.now(timezone(timedelta(hours=tz_off))).strftime("%Y-%m-%d")
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("""INSERT INTO sales (raw_amount, ex_rate, fee, net_amount, details, date, added_by, chat_id) 
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", (raw, rate, fee_val, net, json.dumps(details), l_date, user_id, chat_id))
        conn.commit(); conn.close()

        res_msg = (
            f"✅ **录入成功**\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📊 公式: `{raw:,.0f} ÷ {rate} - {fee_val}%` \n"
            f"💰 净入: `{net:,.2f}`\n"
            f"⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
            + "\n".join(line_summaries) + 
            f"\n━━━━━━━━━━━━━━━\n"
            f"⏳ 授权剩余: {time_left}"
        )
        await update.message.reply_text(res_msg, parse_mode='Markdown')
        await report(update, context)
        
    except Exception as e:
        print(f"Handle Plus Error: {e}")

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
    if str(update.effective_user.id) != os.getenv("MASTER_ID", "0"): return
    try:
        chat_id, target_id, days = update.effective_chat.id, int(context.args[0]), float(context.args[1])
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT expiry_timestamp FROM group_permissions WHERE chat_id=%s AND user_id=%s", (chat_id, target_id))
        res = cur.fetchone()
        base = res[0] if res and res[0] and res[0] > datetime.now(timezone.utc) else datetime.now(timezone.utc)
        new_exp = base + timedelta(days=days)
        cur.execute("INSERT INTO group_permissions (chat_id, user_id, role, expiry_timestamp) VALUES (%s,%s,'user',%s) ON CONFLICT (chat_id, user_id) DO UPDATE SET expiry_timestamp=%s", (chat_id, target_id, new_exp, new_exp))
        conn.commit(); conn.close()
        await update.message.reply_text(f"✅ 成功 | {new_exp.strftime('%Y-%m-%d')}")
    except: pass

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
    app.add_handler(CommandHandler("set_days", set_days))
    app.add_handler(CommandHandler("set_tz", set_tz))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("myid", lambda u,c: u.message.reply_text(f"ID: `{u.effective_user.id}`\nChat: `{u.effective_chat.id}`")))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_plus))
    app.run_polling()

import os
import json
from datetime import datetime, timedelta, timezone
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from db import init_db, get_db_connection, get_chat_tz, get_user_permission

# --- 辅助函数: 检查剩余时间 ---
def get_time_left(user_id, chat_id):
    role, expiry, tz_off = get_user_permission(user_id, chat_id)
    if role == "master": return True, "管理员 (无限制)", tz_off
    if not expiry: return False, "未授权", tz_off
    
    now_utc = datetime.now(timezone.utc)
    if now_utc > expiry: return False, "已过期", tz_off
    
    diff = expiry - now_utc
    days = diff.days
    hours, rem = divmod(diff.seconds, 3600)
    minutes, _ = divmod(rem, 60)
    return True, f"{days}天 {hours}小时 {minutes}分", tz_off

# --- 指令: 设置有效期 ---
async def set_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != os.getenv("MASTER_ID", "0"): return
    try:
        target_id, days = int(context.args[0]), float(context.args[1])
        now_utc = datetime.now(timezone.utc)
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT expiry_timestamp FROM group_permissions WHERE chat_id=%s AND user_id=%s", (update.effective_chat.id, target_id))
        res = cur.fetchone()
        base = res[0] if res and res[0] and res[0] > now_utc else now_utc
        new_exp = base + timedelta(days=days)
        cur.execute("INSERT INTO group_permissions (chat_id, user_id, role, expiry_timestamp) VALUES (%s,%s,'user',%s) ON CONFLICT (chat_id, user_id) DO UPDATE SET expiry_timestamp=%s", (update.effective_chat.id, target_id, new_exp, new_exp))
        conn.commit(); conn.close()
        await update.message.reply_text(f"✅ 用户 {target_id} 已充值 {days} 天")
    except: await update.message.reply_text("💡 格式: /set_days [ID] [天数]")

# --- 指令: 设置时区 ---
async def set_tz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != os.getenv("MASTER_ID", "0"): return
    try:
        offset = int(context.args[0])
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("UPDATE group_permissions SET tz_offset = %s WHERE chat_id = %s", (offset, update.effective_chat.id))
        conn.commit(); conn.close()
        await update.message.reply_text(f"✅ 时区已设为 GMT {offset:+}")
    except: await update.message.reply_text("💡 格式: /set_tz [偏移量]")

# --- 指令: 月度报表 ---
async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id, chat_id = update.effective_user.id, update.effective_chat.id
    role, _, tz_off = get_user_permission(user_id, chat_id)
    if role not in ["master", "user"]: return

    month = datetime.now(timezone(timedelta(hours=tz_off))).strftime("%Y-%m")
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM sales WHERE chat_id=%s AND date LIKE %s ORDER BY id ASC", (chat_id, f"{month}%"))
    rows = cur.fetchall(); conn.close()
    if not rows: return await update.message.reply_text("📭 本月无数据")

    hist = f"📋 **{month} 财务记录**\n{'━'*15}\n"
    summary = f"\n👤 **个人总结 (Individual Summary)**\n{'━'*15}\n"
    person_sum = {}

    for r in rows:
        hist += f"🔹 `{r['date']}` | {float(r['raw_amount']):,.0f}/{r['ex_rate']}-{r['fee']}% = **{float(r['net_amount']):,.2f}**\n"
        lines = []
        for d in r['details']:
            lines.append(f"{d['name']}(L{d['line']}):{float(d['comm']):,.0f}")
            if d['name'] not in person_sum: person_sum[d['name']] = {}
            p_dict = person_sum[d['name']]
            p_dict[f"L{d['line']}"] = p_dict.get(f"L{d['line']}", 0) + float(d['comm'])
        hist += f"└ {', '.join(lines)}\n───\n"

    for name, lines in sorted(person_sum.items()):
        total = sum(lines.values())
        summary += f"📌 **{name}** | 总计: `{total:,.2f}`\n"
        summary += f"└ {', '.join([f'{k}: {v:,.2f}' for k, v in sorted(lines.items())])}\n───\n"
    
    await update.message.reply_text(hist + summary, parse_mode='Markdown')

# --- 核心逻辑: 记录记录 ---
async def handle_plus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id, chat_id = update.effective_user.id, update.effective_chat.id
    active, time_left, tz_off = get_time_left(user_id, chat_id)
    if not active: return await update.message.reply_text(f"❌ 访问受限: {time_left}")

    if not update.message.text.startswith('+'): return
    try:
        p = update.message.text[1:].split()
        raw, rate, fee = float(p[0]), float(p[1]), float(p[2].replace('%',''))
        net = (raw / rate) * (1 - (fee/100))
        details = [{"line": i//2+1, "name": p[3+i], "comm": net*(float(p[4+i].replace('%',''))/100)} for i in range(0, len(p[3:]), 2)]
        
        l_date = datetime.now(timezone(timedelta(hours=tz_off))).strftime("%Y-%m-%d")
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("INSERT INTO sales (raw_amount, ex_rate, fee, net_amount, details, date, added_by, chat_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (raw, rate, fee, net, json.dumps(details), l_date, user_id, chat_id))
        conn.commit(); conn.close()
        await update.message.reply_text(f"✅ 记录成功: **{net:,.2f}**\n⏳ 剩余时间: {time_left}", parse_mode='Markdown')
    except: await update.message.reply_text("❌ 格式错误")

if __name__ == '__main__':
    init_db()
    app = Application.builder().token(os.getenv("BOT_TOKEN")).build()
    app.add_handler(CommandHandler("set_days", set_days))
    app.add_handler(CommandHandler("set_tz", set_tz))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("myid", lambda u,c: u.message.reply_text(f"您的 ID: `{u.effective_user.id}`")))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_plus))
    app.run_polling()

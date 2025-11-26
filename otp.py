import os
import time
import uuid
import threading
import requests
import telebot
from telebot import types
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --- 1. KONFIGURASI ---
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_KEY = os.getenv("SMSHUB_API_KEY")
ALLOWED_USERS = [int(x) for x in os.getenv("ALLOWED_USERS", "").split(",") if x]

TARGET_COUNTRY = "6"      # Indonesia
TARGET_SERVICE = "asy"    # Fore Coffee
TARGET_PRICE = "0.0181"   # Max Harga

# Header Template
BASE_HEADERS_TEMPLATE = {
    'Host': 'api.fore.coffee', 'language': 'id',
    'User-Agent': 'Fore Coffee/4.11.0 (coffee.fore.fore; build:1577; iOS 18.5.0) Alamofire/5.10.2',
    'sentry-trace': '82f1ea8728ec4d9f98e6c380e9ee3e74-0b525eb3ee734332-0',
    'country-id': '1', 'platform': 'ios', 'Connection': 'keep-alive',
    'appsflyer-id': '1759206300240-5775732', 'Accept-Language': 'en-ID;q=1.0, id-ID;q=0.9',
    'timezone': '+07:00', 'jailbroken': '0', 'device-model': 'iPhone 12',
    'Accept': '*/*', 'app-version': '4.11.0', 'os-version': '18.5',
    'Content-Type': 'application/json',
}

CREDENTIALS = [
    {'secret-key': os.getenv('SECRET_KEY_1'), 'push-token': os.getenv('PUSH_TOKEN_1')},
    {'secret-key': os.getenv('SECRET_KEY_2'), 'push-token': os.getenv('PUSH_TOKEN_2')}
]

bot = telebot.TeleBot(BOT_TOKEN)
manual_stops = {}
# Dictionary untuk menyimpan sesi aktif agar bisa di-resend
active_sessions = {} 

# --- 2. HTTP SESSION & SAFE TELEGRAM ---

def get_session():
    session = requests.Session()
    retry = Retry(connect=3, backoff_factor=0.5)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session
session = get_session()

def safe_send_message(chat_id, text, reply_markup=None):
    for i in range(3):
        try:
            return bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=reply_markup)
        except Exception as e:
            print(f"⚠️ Gagal kirim pesan: {e}")
            time.sleep(2)
    return None

def safe_edit_message(text, chat_id, message_id, reply_markup=None):
    for i in range(3):
        try:
            return bot.edit_message_text(text, chat_id, message_id, parse_mode="Markdown", reply_markup=reply_markup)
        except Exception as e:
            if "message is not modified" in str(e): return
            print(f"⚠️ Gagal edit pesan: {e}")
            time.sleep(2)
    return None

# --- 3. FUNGSI API ---

def normalize_phone(phone):
    phone = str(phone).strip().replace('-', '').replace(' ', '').replace('+', '')
    if phone.startswith('62'): return phone[2:] 
    if phone.startswith('08'): return phone[1:]
    return phone

def get_fresh_token(headers):
    try:
        r = session.get('https://api.fore.coffee/auth/get-token', headers=headers, timeout=10).json()
        if r.get('statusCode') == 200: return r.get('payload', {}).get('access_token')
    except: pass
    return None

def check_is_registered(phone, headers):
    try:
        data = {"phone": f"+62{phone}"}
        r = session.post('https://api.fore.coffee/auth/check-phone', headers=headers, json=data, timeout=10).json()
        if r.get('status') == 'success': return r.get('payload', {}).get('is_registered') == 0
    except: pass
    return None

def request_otp_fore(phone, headers):
    try:
        data = {"method": "", "phone": f"+62{phone}"}
        r = session.post('https://api.fore.coffee/auth/req-login-code', headers=headers, json=data, timeout=10).json()
        return r.get('status') == 'success'
    except: return False

def order_smshub():
    try:
        url = f"https://smshub.org/stubs/handler_api.php?api_key={API_KEY}&action=getNumber&service={TARGET_SERVICE}&country={TARGET_COUNTRY}&maxPrice={TARGET_PRICE}"
        resp = session.get(url, timeout=15).text
        if "ACCESS_NUMBER" in resp:
            parts = resp.split(":")
            return {"status": "success", "id": parts[1], "number": parts[2]}
        return {"status": "error", "msg": resp}
    except Exception as e: return {"status": "error", "msg": str(e)}

def set_status(id, status):
    # Status: 1=Ready, 3=Retry (Minta SMS Lagi), 6=Selesai, 8=Cancel
    try: session.get(f"https://smshub.org/stubs/handler_api.php?api_key={API_KEY}&action=setStatus&id={id}&status={status}", timeout=10)
    except: pass

def get_status(id):
    try: return session.get(f"https://smshub.org/stubs/handler_api.php?api_key={API_KEY}&action=getStatus&id={id}", timeout=10).text
    except: return "ERROR"

def get_balance():
    try:
        resp = session.get(f"https://smshub.org/stubs/handler_api.php?api_key={API_KEY}&action=getBalance", timeout=10).text
        return resp.split(":")[1] + " RUB" if "ACCESS_BALANCE" in resp else resp
    except: return "Error"

# --- 4. WORKER: MONITORING RESEND ---

def monitor_resend(chat_id, msg_id, act_id, phone, headers, clean_phone):
    """
    Fungsi khusus untuk memonitor OTP KEDUA/KETIGA setelah tombol Resend diklik.
    """
    safe_edit_message(f"🔄 **Menunggu OTP Baru...**\n📱 `{phone}`", chat_id, msg_id)
    
    wait_start = time.time()
    
    while time.time() - wait_start < 300: # 5 Menit Timeout
        status_sms = get_status(act_id)

        if "STATUS_OK" in status_sms:
            # OTP BARU MASUK
            otp_code = status_sms.split(":")[1]
            
            success_text = (
                f"✅ **OTP BARU DITERIMA!**\n"
                f"📱 `{phone}`\n"
                f"🔑 `{otp_code}`\n"
            )
            # Tombol Tetap Ada (Bisa Resend Lagi atau Done)
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("✅ Selesai (Done)", callback_data=f"done_{act_id}"))
            markup.add(types.InlineKeyboardButton("🔄 Resend OTP Lagi", callback_data=f"resend_{act_id}"))
            
            safe_send_message(chat_id, f"🔔 OTP Baru: `{otp_code}`")
            safe_edit_message(success_text, chat_id, msg_id, reply_markup=markup)
            return # Selesai monitor, tunggu input user selanjutnya

        elif "STATUS_CANCEL" in status_sms:
            safe_edit_message(f"🚫 {phone} Dibatalkan Server.", chat_id, msg_id)
            return

        time.sleep(3)
    
    # Jika timeout
    safe_edit_message(f"❌ {phone} Timeout Resend (Tidak ada SMS).", chat_id, msg_id)


# --- 5. WORKER UTAMA (THE HUNTER) ---

def worker_hunt_otp(chat_id, cred_index, worker_num):
    while True:
        if manual_stops.get(f"worker_{worker_num}", False): break

        # 1. BELI
        res = order_smshub()
        if res['status'] != 'success':
            if "NO_NUMBERS" in str(res['msg']):
                time.sleep(5)
                continue
            elif "NO_BALANCE" in str(res['msg']):
                safe_send_message(chat_id, f"⚠️ Worker {worker_num} Stop: Saldo Habis.")
                break
            time.sleep(3)
            continue

        act_id = res['id']
        full_phone = res['number']
        clean_phone = normalize_phone(full_phone)

        # 2. SETUP FORE
        cred = CREDENTIALS[cred_index % len(CREDENTIALS)]
        headers = BASE_HEADERS_TEMPLATE.copy()
        headers['secret-key'] = cred['secret-key']
        headers['push-token'] = cred['push-token']
        headers['device-id'] = str(uuid.uuid4()).upper()

        token = get_fresh_token(headers)
        if not token: 
            time.sleep(1)
            continue
        headers['access-token'] = token

        # 3. CEK FRESH
        is_fresh = check_is_registered(clean_phone, headers)

        if is_fresh is True:
            if request_otp_fore(clean_phone, headers):
                # Simpan sesi untuk fitur Resend nanti
                active_sessions[act_id] = {
                    'headers': headers,
                    'phone': full_phone,
                    'clean_phone': clean_phone,
                    'chat_id': chat_id
                }

                msg_text = (
                    f"⚡ **Worker {worker_num}: Menunggu OTP**\n"
                    f"📱 `{full_phone}`\n"
                    f"⏳ _Exp 5 Menit..._"
                )
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("❌ Cancel", callback_data=f"stop_{worker_num}_{act_id}"))
                
                sent_msg = safe_send_message(chat_id, msg_text, reply_markup=markup)
                if not sent_msg: continue 

                # UPDATE SESI dengan message_id
                active_sessions[act_id]['msg_id'] = sent_msg.message_id

                # MONITORING AWAL
                wait_start = time.time()
                got_otp = False
                
                while time.time() - wait_start < 300:
                    if manual_stops.get(f"worker_{worker_num}", False):
                        set_status(act_id, 8)
                        safe_edit_message(f"🚫 Worker {worker_num} Stopped.", chat_id, sent_msg.message_id)
                        return 

                    status_sms = get_status(act_id)

                    if "STATUS_OK" in status_sms:
                        otp_code = status_sms.split(":")[1]
                        
                        success_text = (
                            f"✅ **OTP DITERIMA (Worker {worker_num})**\n"
                            f"📱 `{full_phone}`\n"
                            f"🔑 `{otp_code}`\n"
                        )
                        # DISINI KITA TAMBAHKAN TOMBOL RESEND
                        markup_done = types.InlineKeyboardMarkup()
                        markup_done.add(types.InlineKeyboardButton("✅ Selesai (Done)", callback_data=f"done_{act_id}"))
                        markup_done.add(types.InlineKeyboardButton("🔄 Resend OTP", callback_data=f"resend_{act_id}"))
                        
                        safe_send_message(chat_id, f"🔔 OTP: `{otp_code}`")
                        safe_edit_message(success_text, chat_id, sent_msg.message_id, reply_markup=markup_done)
                        
                        got_otp = True
                        break 
                    
                    elif "STATUS_CANCEL" in status_sms:
                        break 
                    time.sleep(3)
                
                if got_otp:
                    break # Worker selesai, menunggu aksi user (Done/Resend)
                else:
                    print(f"[Worker {worker_num}] {full_phone} Timeout. Replace.")
                    set_status(act_id, 8) 
                    safe_edit_message(f"♻️ `{full_phone}` Timeout. Ganti Baru...", chat_id, sent_msg.message_id)
                    # Loop lagi (beli baru)
            else:
                print(f"[Worker {worker_num}] Gagal Req OTP.")
        else:
            print(f"[Worker {worker_num}] {full_phone} Terdaftar. Skip.")
            time.sleep(2)

# --- 6. HANDLER TELEGRAM & CALLBACK ---

def is_allowed(uid): return uid in ALLOWED_USERS

@bot.message_handler(commands=['start'])
def start(m):
    if not is_allowed(m.from_user.id): return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🚀 Beli Massal", "💰 Cek Saldo")
    safe_send_message(m.chat.id, "🤖 **Fore Hunter V5 (Resend Feature)**", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text == "💰 Cek Saldo")
def cek_saldo(m):
    if not is_allowed(m.from_user.id): return
    safe_send_message(m.chat.id, f"Saldo: `{get_balance()}`")

@bot.message_handler(func=lambda m: m.text == "🚀 Beli Massal")
def ask_qty(m):
    if not is_allowed(m.from_user.id): return
    msg = safe_send_message(m.chat.id, "🔢 **Jumlah Worker?**\n(Contoh: 5)")
    if msg: bot.register_next_step_handler(msg, process_buy)

def process_buy(m):
    if not is_allowed(m.from_user.id): return
    try:
        qty = int(m.text)
        if qty > 20: return safe_send_message(m.chat.id, "⚠️ Maks 20 Worker.")
    except: return
    
    safe_send_message(m.chat.id, f"⚡ {qty} Worker Berjalan...")
    for i in range(1, qty+1):
        manual_stops[f"worker_{i}"] = False
        t = threading.Thread(target=worker_hunt_otp, args=(m.chat.id, i, i))
        t.start()
        time.sleep(1.5)

@bot.callback_query_handler(func=lambda call: True)
def cb(call):
    action = call.data.split("_")[0]
    act_id = call.data.split("_")[1]
    
    if action == "stop":
        worker_num = call.data.split("_")[1] # Format stop_WORKER_ID
        real_act_id = call.data.split("_")[2]
        
        manual_stops[f"worker_{worker_num}"] = True
        set_status(real_act_id, 8) 
        bot.answer_callback_query(call.id, "Stopped")
        safe_edit_message("🚫 Stopped Manual.", call.message.chat.id, call.message.message_id)
        
    elif action == "done":
        set_status(act_id, 6) # Finish
        if act_id in active_sessions: del active_sessions[act_id]
        bot.answer_callback_query(call.id, "Saved")
        safe_edit_message("✅ Order Selesai.", call.message.chat.id, call.message.message_id)
    
    elif action == "resend":
        # 1. Ambil data sesi
        session_data = active_sessions.get(act_id)
        if not session_data:
            bot.answer_callback_query(call.id, "Sesi kadaluarsa/hilang.")
            return

        bot.answer_callback_query(call.id, "Meminta SMS Ulang...")
        
        # 2. Set Status SMSHub ke 3 (RETRY)
        set_status(act_id, 3)
        
        # 3. Request Fore Lagi
        req = request_otp_fore(session_data['clean_phone'], session_data['headers'])
        
        if req:
            # 4. Jalankan Thread Monitoring Khusus Resend
            t = threading.Thread(target=monitor_resend, args=(
                session_data['chat_id'], 
                call.message.message_id, 
                act_id, 
                session_data['phone'], 
                session_data['headers'], 
                session_data['clean_phone']
            ))
            t.start()
        else:
            bot.send_message(call.message.chat.id, "❌ Gagal Request OTP ke Fore.")

# --- 7. MAIN LOOP ---
print("Bot Fore V5 (Resend) Jalan...")

if __name__ == "__main__":
    try:
        while True:
            try:
                bot.infinity_polling(timeout=90, long_polling_timeout=5)
            except Exception as e:
                if "Break infinity polling" in str(e): break
                print(f"⚠️ Reconnect: {e}")
                time.sleep(3)
    except (KeyboardInterrupt, SystemExit):
        print("\n🛑 Stop Manual.")
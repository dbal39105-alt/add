import requests
import json
import base64
import uuid
import re
import sqlite3
from datetime import datetime
import os
import sys
import time
import logging
import signal
from io import BytesIO
import PyPDF2
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# Windows UTF-8 console output support
try:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

# ============== LOGGING SETUP ==============
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============== BOT TOKEN ==============
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8777443945:AAGM_f-WtydhbzRDZ1bgMt1X6ATu_oushHs")

# ============== ANTI-CAPTCHA CONFIG ==============
ANTI_CAPTCHA_KEY = os.environ.get("ANTI_CAPTCHA_KEY", "b05b20aabb26049c7c730cdbdd682153")

# ============== PROXY CONFIG (for UIDAI only) ==============
# Live tested working Indian proxies for UIDAI access from Vercel
INDIAN_PROXIES = [
    "117.236.124.166:3128",
    "14.139.235.82:3128",
    "103.22.173.77:1111",
    "182.71.123.38:80",
    "14.143.83.222:1111",
    "103.135.70.9:8080",
]

_proxy_index = 0

def _get_next_indian_proxy():
    global _proxy_index
    if not INDIAN_PROXIES:
        return None
    proxy = INDIAN_PROXIES[_proxy_index % len(INDIAN_PROXIES)]
    _proxy_index += 1
    return proxy

def _build_proxy_dict():
    """Build requests proxy dict using verified Indian proxy pool."""
    proxy = _get_next_indian_proxy()
    if proxy:
        proxy_url = f"http://{proxy}"
        return {"http": proxy_url, "https": proxy_url}
    return None

UIDAI_PROXIES = _build_proxy_dict()
if UIDAI_PROXIES:
    logger.info(f"UIDAI proxy configured: {UIDAI_PROXIES}")
else:
    logger.warning("No UIDAI proxy configured — UIDAI may block datacenter IPs")

# ============== SESSION FACTORY ==============
def create_session(max_retries=0):
    session = requests.Session()
    session.mount('https://', requests.adapters.HTTPAdapter(
        pool_connections=100, pool_maxsize=300, max_retries=max_retries, pool_block=False
    ))
    session.mount('http://', requests.adapters.HTTPAdapter(
        pool_connections=100, pool_maxsize=300, max_retries=max_retries, pool_block=False
    ))
    return session

telegram_session = None
def get_telegram_session():
    global telegram_session
    if telegram_session is None:
        telegram_session = create_session()
    return telegram_session

def get_uidai_session():
    """UIDAI session with Indian proxy to bypass datacenter IP block."""
    session = create_session()
    proxies = _build_proxy_dict()
    if proxies:
        session.proxies.update(proxies)
    return session


# ============== PDF PASSWORD CRACKER ==============
class PDFPasswordCracker:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.found_password = None
        self.stop_flag = False
        self.progress = 0
        self.total_years = 0

    def try_password(self, pdf_path, password):
        try:
            with open(pdf_path, 'rb') as file:
                pdf_reader = PyPDF2.PdfReader(file)
                if pdf_reader.decrypt(password):
                    return True, password
                return False, None
        except Exception as e:
            logger.debug(f"Error with password {password}: {e}")
            return False, None

    def decrypt_pdf(self, pdf_path, password, output_path=None):
        try:
            temp_dir = "/tmp" if os.environ.get("VERCEL") or os.path.exists("/tmp") else "."
            if output_path is None:
                base_name = os.path.basename(pdf_path).replace('.pdf', '_decrypted.pdf')
                output_path = os.path.join(temp_dir, base_name)
            with open(pdf_path, 'rb') as file:
                pdf_reader = PyPDF2.PdfReader(file)
                pdf_reader.decrypt(password)
                pdf_writer = PyPDF2.PdfWriter()
                for page in pdf_reader.pages:
                    pdf_writer.add_page(page)
                with open(output_path, 'wb') as output_file:
                    pdf_writer.write(output_file)
            logger.info(f"Decrypted PDF saved: {output_path}")
            return output_path
        except Exception as e:
            logger.error(f"Error decrypting PDF: {e}")
            return None

    def crack_pdf(self, pdf_path, name, progress_callback=None):
        self.found_password = None
        self.stop_flag = False
        self.progress = 0
        name_upper = name.upper()
        patterns = []
        name_prefix = name_upper[:4] if len(name_upper) >= 4 else name_upper
        patterns.append(('first4', name_prefix))
        if len(name_upper) >= 6:
            patterns.append(('first6', name_upper[:6]))
        name_full = name_upper[:10] if len(name_upper) > 10 else name_upper
        patterns.append(('full', name_full))
        patterns.append(('lower_first4', name_prefix.lower()))
        if len(name_upper) >= 6:
            patterns.append(('lower_first6', name_upper[:6].lower()))
        patterns.append(('title_first4', name_prefix.title()))
        patterns.append(('first4_short', name_prefix[:4]))
        patterns.append(('with_at', f"{name_prefix}@"))
        patterns.append(('with_hash', f"{name_prefix}#"))
        patterns.append(('with_exclaim', f"{name_prefix}!"))
        patterns.append(('year_first', "@"))
        patterns.append(('only_name', name_prefix))
        current_year = datetime.now().year
        common_years = list(range(1940, 2010)) + list(range(1930, 1940)) + list(range(2010, current_year + 1))
        prioritized_passwords = []
        for year in common_years:
            for pattern_name, prefix in patterns:
                if pattern_name == 'year_first':
                    password = f"{year}{prefix}"
                elif pattern_name == 'only_name':
                    password = prefix
                elif pattern_name == 'first4_short':
                    password = f"{prefix[:4]}{year}"
                elif pattern_name == 'with_at':
                    password = f"{prefix}@{year}"
                elif pattern_name == 'with_hash':
                    password = f"{prefix}#{year}"
                elif pattern_name == 'with_exclaim':
                    password = f"{prefix}!{year}"
                else:
                    password = f"{prefix}{year}"
                prioritized_passwords.append(password)
        seen = set()
        unique_passwords = []
        for pwd in prioritized_passwords:
            if pwd not in seen:
                seen.add(pwd)
                unique_passwords.append(pwd)
        checked = 0
        batch_size = 20
        for i in range(0, len(unique_passwords), batch_size):
            if self.stop_flag:
                break
            batch = unique_passwords[i:i+batch_size]
            futures = [(self.executor.submit(self.try_password, pdf_path, p), p) for p in batch]
            for future, password in futures:
                if self.stop_flag:
                    break
                try:
                    success, found_pwd = future.result(timeout=2)
                    checked += 1
                    if success:
                        self.found_password = found_pwd
                        self.stop_flag = True
                        decrypted_path = self.decrypt_pdf(pdf_path, found_pwd)
                        return True, found_pwd, decrypted_path if decrypted_path else None
                except Exception as e:
                    logger.debug(f"Error checking password {password}: {e}")
                    continue
        no_year_passwords = [prefix for pattern_name, prefix in patterns if pattern_name not in ['only_name']]
        for password in no_year_passwords:
            if self.stop_flag:
                break
            success, found_pwd = self.try_password(pdf_path, password)
            if success:
                self.found_password = found_pwd
                self.stop_flag = True
                decrypted_path = self.decrypt_pdf(pdf_path, found_pwd)
                return True, found_pwd, decrypted_path if decrypted_path else None
        return False, None, None

# ============== AADHAAR BOT CLASS ==============
class AadhaarBot:
    def __init__(self):
        self.session = get_uidai_session()
        self.base_headers = {
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en_IN',
            'Connection': 'keep-alive',
            'Content-Type': 'application/json',
            'Origin': 'https://myaadhaar.uidai.gov.in',
            'Referer': 'https://myaadhaar.uidai.gov.in/',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-site',
            'User-Agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36',
            'appid': 'MYAADHAAR',
            'sec-ch-ua': '"Not-A.Brand";v="99", "Chromium";v="124"',
            'sec-ch-ua-mobile': '?1',
            'sec-ch-ua-platform': '"Android"',
        }
        self.session.headers.update(self.base_headers)
        logger.info("AadhaarBot initialized")
        self.cracker = PDFPasswordCracker()

    def generate_transaction_id(self):
        return str(uuid.uuid4())

    def is_base64(self, s):
        if not isinstance(s, str) or len(s) < 100:
            return False
        if s.startswith('data:'):
            s = s.split(',')[1] if ',' in s else s
        if len(s) % 4 != 0:
            return False
        try:
            base64.b64decode(s)
            return True
        except:
            return False

    def detect_file_type(self, file_bytes):
        if file_bytes[:4] == b'%PDF':
            return 'pdf'
        elif file_bytes[:8] == b'\x89PNG\r\n\x1a\n':
            return 'png'
        elif file_bytes[:2] == b'\xff\xd8':
            return 'jpg'
        return 'unknown'

    def detect_and_decode_base64(self, data, field_name="unknown", save=False):
        decoded_items = []
        if isinstance(data, dict):
            for key, value in list(data.items()):
                if isinstance(value, str) and len(value) > 100 and self.is_base64(value):
                    try:
                        clean_base64 = value.split(',')[1] if value.startswith('data:') and ',' in value else value
                        decoded_bytes = base64.b64decode(clean_base64)
                        file_type = self.detect_file_type(decoded_bytes)
                        if save and file_type in ['pdf', 'png', 'jpg']:
                            temp_dir = "/tmp" if os.environ.get("VERCEL") or os.path.exists("/tmp") else "."
                            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                            ext = {'pdf': 'pdf', 'png': 'png', 'jpg': 'jpg'}.get(file_type, 'bin')
                            filename = os.path.join(temp_dir, f"decoded_{field_name}_{key}_{timestamp}.{ext}")
                            with open(filename, 'wb') as f:
                                f.write(decoded_bytes)
                            decoded_items.append({'field': key, 'filename': filename, 'type': file_type, 'size': len(decoded_bytes), 'data': decoded_bytes})
                            logger.info(f"Saved: {filename}")
                        elif not save:
                            decoded_items.append({'field': key, 'type': file_type, 'size': len(decoded_bytes), 'data': decoded_bytes})
                    except Exception as e:
                        logger.error(f"Base64 decode error: {e}")
                if isinstance(value, (dict, list)):
                    decoded_items.extend(self.detect_and_decode_base64(value, f"{field_name}.{key}", save))
        elif isinstance(data, list):
            for idx, item in enumerate(data):
                if isinstance(item, (dict, list)):
                    decoded_items.extend(self.detect_and_decode_base64(item, f"{field_name}[{idx}]", save))
        return decoded_items

    def _uidai_post(self, url, json_data, timeout=(5, 12)):
        """Post to UIDAI with automatic proxy failover across candidate Indian proxies."""
        # Try current session first
        try:
            r = self.session.post(url, json=json_data, timeout=timeout)
            if r.status_code == 200:
                return r
        except Exception as e:
            logger.warning(f"Primary proxy post to {url} failed: {e}")

        # Failover through all tested Indian proxies
        for proxy in INDIAN_PROXIES:
            try:
                temp_session = create_session()
                temp_session.headers.update(self.session.headers)
                temp_session.proxies.update({"http": f"http://{proxy}", "https": f"http://{proxy}"})
                r = temp_session.post(url, json=json_data, timeout=(4, 10))
                if r.status_code == 200:
                    self.session = temp_session
                    return r
            except Exception:
                continue

        # Direct connection fallback
        try:
            temp_session = create_session()
            temp_session.headers.update(self.session.headers)
            return temp_session.post(url, json=json_data, timeout=timeout)
        except Exception as e:
            logger.error(f"All proxies failed for {url}: {e}")
            raise

    def get_captcha(self, user_id):
        touch_session(user_id)
        transaction_id = self.generate_transaction_id()
        self.session.headers.update({'x-request-id': transaction_id, 'transactionId': transaction_id})
        captcha_data = {'captchaLength': '6', 'captchaType': '2', 'audioCaptchaRequired': True}
        try:
            response = self._uidai_post(
                'https://tathya.uidai.gov.in/audioCaptchaService/api/captcha/v3/generation',
                json_data=captcha_data, timeout=(5, 12)
            )
            if not response or response.status_code != 200:
                return None, None, None
            resp_json = response.json()
            captcha_txn_id = resp_json.get('transactionId')
            captcha_base64 = resp_json.get('imageBase64')
            if not captcha_base64:
                for key, value in resp_json.items():
                    if isinstance(value, str) and len(value) > 100 and self.is_base64(value):
                        captcha_base64 = value
                        break
            if not captcha_base64:
                return None, None, None
            if captcha_base64.startswith('data:image'):
                captcha_base64 = captcha_base64.split(',')[1]
            image_bytes = base64.b64decode(captcha_base64)
            return image_bytes, captcha_txn_id, transaction_id
        except Exception as e:
            logger.error(f"Error getting captcha: {str(e)}")
            return None, None, None

    def send_aadhaar_otp(self, user_id, eid_number, captcha_value, captcha_txn_id, transaction_id):
        touch_session(user_id)
        self.session.headers.update({'x-request-id': transaction_id, 'transactionId': transaction_id})
        otp_request_data = {
            'eidNumber': eid_number, 'idType': 'eid',
            'captchaTxnId': captcha_txn_id, 'captchaValue': captcha_value,
            'transactionId': transaction_id, 'resendOTP': False
        }
        try:
            response = self._uidai_post(
                'https://tathya.uidai.gov.in/unifiedAppAuthService/api/v2/generate/aadhaar/otp',
                json_data=otp_request_data, timeout=(6, 15)
            )
            if response and response.status_code == 200:
                resp_json = response.json()
                otp_txn_id = resp_json.get('txnId')
                status = resp_json.get('status')
                message = resp_json.get('message')
                if otp_txn_id and status == "Success":
                    return True, otp_txn_id, message
                else:
                    return False, None, message
            else:
                return False, None, f"HTTP {response.status_code if response else 'error'}"
        except Exception as e:
            return False, None, str(e)

    def download_aadhaar_pdf(self, user_id, eid_number, otp, otp_txn_id, transaction_id, mask=False):
        touch_session(user_id)
        self.session.headers.update({'x-request-id': transaction_id, 'transactionId': transaction_id})
        download_data = {'eid': eid_number, 'mask': mask, 'otp': otp, 'otpTxnId': otp_txn_id}
        try:
            response = self._uidai_post(
                'https://tathya.uidai.gov.in/downloadAadhaarService/api/aadhaar/download',
                json_data=download_data, timeout=(6, 20)
            )
            if response and response.status_code == 200:
                resp_json = response.json()
                decoded_files = self.detect_and_decode_base64(resp_json, "aadhaar_download", save=True)
                if decoded_files:
                    return True, decoded_files[0]['filename']
                else:
                    if resp_json.get('status') == 'Error' or resp_json.get('errorCode'):
                        error_msg = resp_json.get('message', resp_json.get('errorMessage', 'Unknown error'))
                        return False, error_msg
                    else:
                        return False, "No PDF data found"
            else:
                return False, f"HTTP {response.status_code if response else 'error'}"
        except Exception as e:
            return False, str(e)

    def send_eid_otp(self, user_id, mobile, name, captcha_code, captcha_txn_id, transaction_id):
        touch_session(user_id)
        self.session.headers.update({'x-request-id': transaction_id, 'transactionId': transaction_id})
        request_data = {
            'mobileNumber': mobile, 'dob': None, 'email': None,
            'name': name.upper(), 'option': 'EID', 'otp': None,
            'otpTxnId': None, 'captchaTxnId': captcha_txn_id,
            'captcha': captcha_code, 'resendOtp': False
        }
        try:
            response = self._uidai_post(
                'https://tathya.uidai.gov.in/retrieveEidUid/ext/v1/generic/retrieveuideid',
                json_data=request_data, timeout=(6, 15)
            )
            if response and response.status_code == 200:
                resp_json = response.json()
                if 'responseData' in resp_json:
                    response_data = resp_json['responseData']
                    otp_txn_id = response_data.get('otpTxnId')
                    status = response_data.get('status')
                    if otp_txn_id and status == "Success":
                        return True, otp_txn_id
                    else:
                        return False, response_data.get('message', 'Unknown error')
                else:
                    return False, 'Invalid response'
            else:
                return False, f"HTTP {response.status_code if response else 'error'}"
        except Exception as e:
            return False, str(e)

    def verify_eid_otp(self, user_id, mobile, name, otp_code, otp_txn_id, captcha_txn_id, captcha_code):
        touch_session(user_id)
        self.session.headers.update({'x-request-id': self.generate_transaction_id()})
        verify_data = {
            'mobileNumber': mobile, 'dob': None, 'name': name.upper(),
            'email': None, 'option': 'EID', 'otp': otp_code,
            'otpTxnId': otp_txn_id, 'captchaTxnId': captcha_txn_id,
            'captcha': captcha_code, 'resendOtp': False
        }
        try:
            response = self._uidai_post(
                'https://tathya.uidai.gov.in/retrieveEidUid/ext/v1/generic/retrieveuideid',
                json_data=verify_data, timeout=(6, 15)
            )
            if response and response.status_code == 200:
                resp_json = response.json()
                if resp_json.get('status') == 200 or resp_json.get('status') == "Success":
                    if 'responseData' in resp_json:
                        response_data = resp_json['responseData']
                        eid_number = response_data.get('eidNumber')
                        name_from_response = response_data.get('name', name)
                        if eid_number:
                            return True, eid_number, name_from_response
                        else:
                            return False, None, "No EID found"
                    else:
                        return False, None, "Invalid response"
                else:
                    error_msg = resp_json.get('errorDetails', {}).get('messageEnglish', 'Verification failed')
                    return False, None, error_msg
            else:
                return False, None, f"HTTP {response.status_code if response else 'error'}"
        except Exception as e:
            return False, None, str(e)

    def crack_pdf_with_name(self, pdf_path, name, progress_callback=None):
        success, password, decrypted_path = self.cracker.crack_pdf(pdf_path, name, progress_callback)
        tips = None
        return success, password, decrypted_path, tips

# ============================================================
# Auto CAPTCHA solver
# ============================================================
try:
    import ddddocr as _ddddocr
    _ocr = _ddddocr.DdddOcr(show_ad=False)
    logger.info("ddddocr loaded — local CAPTCHA solving active")
except ImportError:
    _ocr = None
    logger.warning("ddddocr not installed — run: pip install ddddocr")

def _solve_local(image_bytes):
    if _ocr is None:
        return None
    try:
        result = _ocr.classification(image_bytes)
        result = re.sub(r'[^a-zA-Z0-9]', '', result)
        logger.info(f"ddddocr solved: {result}")
        return result if result else None
    except Exception as e:
        logger.error(f"ddddocr error: {e}")
        return None

def _solve_anticaptcha(image_bytes):
    """Solve captcha using anti-captcha.com API with optimized polling."""
    try:
        img_b64 = base64.b64encode(image_bytes).decode()
        resp = requests.post(
            "https://api.anti-captcha.com/createTask",
            json={"clientKey": ANTI_CAPTCHA_KEY, "task": {
                "type": "ImageToTextTask",
                "body": img_b64,
                "phrase": False,
                "case": False,
                "numeric": 0,
                "math": False,
                "minLength": 4,
                "maxLength": 8
            }},
            timeout=10
        ).json()
        if resp.get("errorId") != 0:
            logger.warning(f"Anti-Captcha createTask error: {resp.get('errorDescription')}")
            return None
        task_id = resp.get("taskId")
        if not task_id:
            logger.warning("Anti-Captcha: no taskId returned")
            return None
        # Poll every 1s for up to 20 seconds
        for attempt in range(20):
            time.sleep(1)
            try:
                result = requests.post(
                    "https://api.anti-captcha.com/getTaskResult",
                    json={"clientKey": ANTI_CAPTCHA_KEY, "taskId": task_id},
                    timeout=8
                ).json()
            except Exception as poll_err:
                logger.warning(f"Anti-Captcha poll error: {poll_err}")
                continue
            if result.get("status") == "ready":
                sol = result.get("solution", {}).get("text", "").strip()
                # Clean non-alphanumeric characters
                sol = re.sub(r'[^a-zA-Z0-9]', '', sol)
                if sol:
                    logger.info(f"Anti-Captcha solved in {attempt+1}s: {sol}")
                    return sol
                else:
                    logger.warning("Anti-Captcha returned empty solution")
                    return None
            elif result.get("status") == "processing":
                continue
            else:
                logger.warning(f"Anti-Captcha unexpected status: {result.get('status')}")
                break
        logger.warning("Anti-Captcha timed out after 20s")
        return None
    except Exception as e:
        logger.warning(f"Anti-Captcha error: {e}")
        return None

def solve_captcha_auto(image_bytes):
    """Try anti-captcha first, fallback to local OCR."""
    # Try anti-captcha if key is configured
    if ANTI_CAPTCHA_KEY:
        result = _solve_anticaptcha(image_bytes)
        if result:
            return result
        logger.warning("Anti-Captcha failed, trying local OCR")
    # Fallback to local ddddocr
    local = _solve_local(image_bytes)
    if local:
        return local
    logger.warning("All captcha solvers failed")
    return None

def _new_bot():
    return AadhaarBot()

def _get_captcha(chat_id, retries=2):
    for attempt in range(retries):
        image_bytes, captcha_txn_id, transaction_id = _new_bot().get_captcha(chat_id)
        if image_bytes:
            return image_bytes, captcha_txn_id, transaction_id
        logger.warning(f"Captcha attempt {attempt + 1} failed for {chat_id}")
        time.sleep(0.5)
    return None, None, None

def _captcha_failed(chat_id, message_id=None):
    clear_session(chat_id)
    txt = (f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
           f"⚠️  <b>〔 UIDAI Unavailable 〕</b>\n\n"
           f"◌  Could not reach UIDAI server.\n"
           f"◌  Please try again in a moment.\n\n"
           f"{DIVIDER}\n"
           f"<i>◌  Select a method below to start again.</i>")
    if message_id:
        edit_message(chat_id, message_id, txt)
    else:
        send_message(chat_id, txt, reply_markup=get_main_keyboard())

# ============== CONFIG ==============
DIVIDER         = "━━━━━━━━━━━━━━━━━━━━━━━"
BOT_NAME        = "𝙍𝘼𝙃𝙐𝙇 𝘼𝘼𝘿𝙃𝘼𝙍 𝘽𝙊𝙏"
OWNER_ID        = 7477511589
OWNER_USERNAME  = "@SaitamaX_404"
SESSION_TIMEOUT = 300

PLANS = {
    '100':  {'credits': 10,  'price': '₹2499',  'lifetime': False},
    '250':  {'credits': 20,  'price': '₹4999',  'lifetime': False},
    '500':  {'credits': 50,  'price': '₹9999',  'lifetime': False},
    '1000': {'credits': 0,   'price': '18999', 'lifetime': True},
}
CHANNEL_USERNAME = "-1002970304603"
CHANNEL_NAME     = "Rahul APK Developer Information"
CHANNEL_LINK     = "https://t.me/Aadhar_info"

# ============== DATABASE ==============
TEMP_DIR     = "/tmp" if os.environ.get("VERCEL") or os.path.exists("/tmp") else "."
DB_FILE      = os.path.join(TEMP_DIR, "bot.db")
_db_conn     = None
_db_lock     = threading.Lock()
_db_init_lk  = threading.Lock()

def _get_db():
    global _db_conn
    if _db_conn is None:
        with _db_init_lk:
            if _db_conn is None:
                conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=30)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA cache_size=10000")
                _db_conn = conn
    return _db_conn

def _init_db():
    conn = _get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id        TEXT PRIMARY KEY,
            credits        INTEGER NOT NULL DEFAULT 0,
            lifetime       INTEGER NOT NULL DEFAULT 0,
            referred_by    TEXT,
            referral_count INTEGER NOT NULL DEFAULT 0,
            joined         TEXT NOT NULL,
            banned         INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            chat_id        TEXT PRIMARY KEY,
            step           TEXT NOT NULL,
            data           TEXT NOT NULL,
            last_activity  REAL NOT NULL
        );
        INSERT OR IGNORE INTO settings (key, value) VALUES ('free_credits', '1');
        INSERT OR IGNORE INTO settings (key, value) VALUES ('free_mode', '0');
    """)
    conn.commit()
    _migrate_from_json()
    _init_settings_cache()

def _migrate_from_json():
    json_path = os.path.join(TEMP_DIR, 'users.json') if os.path.exists(os.path.join(TEMP_DIR, 'users.json')) else 'users.json'
    if not os.path.exists(json_path):
        return
    conn = _get_db()
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
        return
    try:
        with open(json_path, 'r') as f:
            data = json.load(f)
        for uid, u in data.items():
            conn.execute("""
                INSERT OR IGNORE INTO users
                (user_id, credits, lifetime, referred_by, referral_count, joined, banned)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                uid,
                int(u.get('credits', 0)) if not u.get('lifetime') else 0,
                1 if u.get('lifetime') else 0,
                u.get('referred_by'),
                int(u.get('referral_count', 0)),
                u.get('joined', datetime.now().isoformat()),
                1 if u.get('banned') else 0,
            ))
        conn.commit()
        logger.info(f"Migrated {len(data)} users from users.json to bot.db")
        os.rename(json_path, json_path + '.migrated')
    except Exception as e:
        logger.error(f"Migration error: {e}")

def ensure_user(user_id, referrer_id=None):
    uid = str(user_id)
    with _db_lock:
        conn = _get_db()
        conn.execute("""
            INSERT OR IGNORE INTO users
            (user_id, credits, lifetime, referred_by, referral_count, joined, banned)
            VALUES (?, ?, 0, ?, 0, ?, 0)
        """, (uid, get_free_credits(), str(referrer_id) if referrer_id else None, datetime.now().isoformat()))
        if conn.execute("SELECT changes()").fetchone()[0] > 0:
            if referrer_id:
                rid = str(referrer_id)
                conn.execute("""
                    UPDATE users SET credits = credits + 1, referral_count = referral_count + 1
                    WHERE user_id = ? AND user_id != ?
                """, (rid, uid))
            conn.commit()
            return True
        return False

def get_user(user_id):
    row = _get_db().execute("SELECT * FROM users WHERE user_id = ?", (str(user_id),)).fetchone()
    return dict(row) if row else None

def get_credits(user_id):
    row = _get_db().execute("SELECT credits, lifetime FROM users WHERE user_id = ?", (str(user_id),)).fetchone()
    if row is None:
        return 0
    return float('inf') if row['lifetime'] else row['credits']

def is_lifetime(user_id):
    row = _get_db().execute("SELECT lifetime FROM users WHERE user_id = ?", (str(user_id),)).fetchone()
    return bool(row['lifetime']) if row else False

def has_credits(user_id):
    if is_free_mode():
        return True
    return get_credits(user_id) > 0

def add_credits(user_id, amount, make_lifetime=False):
    uid = str(user_id)
    with _db_lock:
        conn = _get_db()
        if make_lifetime:
            conn.execute("UPDATE users SET lifetime = 1 WHERE user_id = ?", (uid,))
        else:
            conn.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (amount, uid))
        conn.commit()

def set_ban(user_id, banned: bool):
    with _db_lock:
        conn = _get_db()
        conn.execute("UPDATE users SET banned = ? WHERE user_id = ?", (1 if banned else 0, str(user_id)))
        conn.commit()

def is_banned(user_id):
    row = _get_db().execute("SELECT banned FROM users WHERE user_id = ?", (str(user_id),)).fetchone()
    return bool(row['banned']) if row else False

def all_users():
    rows = _get_db().execute("SELECT * FROM users").fetchall()
    return {r['user_id']: dict(r) for r in rows}

def deduct_credit(user_id):
    if is_free_mode():
        return
    with _db_lock:
        conn = _get_db()
        conn.execute("UPDATE users SET credits = MAX(0, credits - 1) WHERE user_id = ? AND lifetime = 0", (str(user_id),))
        conn.commit()

def remove_credits_from(user_id, amount):
    with _db_lock:
        conn = _get_db()
        conn.execute("UPDATE users SET credits = MAX(0, credits - ?) WHERE user_id = ? AND lifetime = 0", (amount, str(user_id)))
        conn.commit()

# ============== BOT SETTINGS ==============
_settings_lock  = threading.Lock()
_settings_cache = {'free_credits': 1, 'free_mode': False}

def _init_settings_cache():
    global _settings_cache
    try:
        rows = _get_db().execute("SELECT key, value FROM settings").fetchall()
        for row in rows:
            if row['key'] == 'free_credits':
                _settings_cache['free_credits'] = int(row['value'])
            elif row['key'] == 'free_mode':
                _settings_cache['free_mode'] = (row['value'] == '1')
    except Exception as e:
        logger.error(f"Settings load error: {e}")

def get_free_credits():
    with _settings_lock:
        return _settings_cache.get('free_credits', 1)

def set_free_credits(amount):
    with _settings_lock:
        _settings_cache['free_credits'] = amount
    with _db_lock:
        conn = _get_db()
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('free_credits', ?)", (str(amount),))
        conn.commit()

def is_free_mode():
    with _settings_lock:
        return _settings_cache.get('free_mode', False)

def toggle_free_mode():
    with _settings_lock:
        new_val = not _settings_cache.get('free_mode', False)
        _settings_cache['free_mode'] = new_val
    with _db_lock:
        conn = _get_db()
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('free_mode', ?)", ('1' if new_val else '0',))
        conn.commit()
    return new_val

# ============== SESSION MANAGEMENT ==============
user_sessions   = {}
_sessions_lock  = threading.Lock()

def get_session(chat_id):
    cid = str(chat_id)
    with _sessions_lock:
        if chat_id in user_sessions:
            return user_sessions[chat_id]
        try:
            row = _get_db().execute("SELECT step, data, last_activity FROM sessions WHERE chat_id = ?", (cid,)).fetchone()
            if row:
                s = {
                    'step': row['step'],
                    'data': json.loads(row['data']) if row['data'] else {},
                    'last_activity': row['last_activity']
                }
                user_sessions[chat_id] = s
                return s
        except Exception:
            pass
        return user_sessions.get(chat_id, {'step': 'main', 'data': {}, 'last_activity': time.time()})

def set_session(chat_id, step, data=None):
    cid = str(chat_id)
    with _sessions_lock:
        existing = user_sessions.get(chat_id, {})
        d = data if data is not None else existing.get('data', {})
        s = {'step': step, 'data': d, 'last_activity': time.time()}
        user_sessions[chat_id] = s
        try:
            with _db_lock:
                _get_db().execute("INSERT OR REPLACE INTO sessions (chat_id, step, data, last_activity) VALUES (?, ?, ?, ?)",
                                  (cid, step, json.dumps(d), time.time()))
                _get_db().commit()
        except Exception:
            pass

def update_session_data(chat_id, key, value):
    cid = str(chat_id)
    with _sessions_lock:
        if chat_id not in user_sessions:
            user_sessions[chat_id] = {'step': 'main', 'data': {}, 'last_activity': time.time()}
        user_sessions[chat_id]['data'][key] = value
        user_sessions[chat_id]['last_activity'] = time.time()
        try:
            with _db_lock:
                _get_db().execute("INSERT OR REPLACE INTO sessions (chat_id, step, data, last_activity) VALUES (?, ?, ?, ?)",
                                  (cid, user_sessions[chat_id]['step'], json.dumps(user_sessions[chat_id]['data']), time.time()))
                _get_db().commit()
        except Exception:
            pass

def clear_session(chat_id):
    cid = str(chat_id)
    with _sessions_lock:
        user_sessions[chat_id] = {'step': 'main', 'data': {}, 'last_activity': time.time()}
        try:
            with _db_lock:
                _get_db().execute("DELETE FROM sessions WHERE chat_id = ?", (cid,))
                _get_db().commit()
        except Exception:
            pass

def touch_session(chat_id):
    cid = str(chat_id)
    with _sessions_lock:
        if chat_id in user_sessions:
            user_sessions[chat_id]['last_activity'] = time.time()
            try:
                with _db_lock:
                    _get_db().execute("UPDATE sessions SET last_activity = ? WHERE chat_id = ?", (time.time(), cid))
                    _get_db().commit()
            except Exception:
                pass

def _cleanup_sessions():
    while True:
        time.sleep(20)
        try:
            expired = []
            with _sessions_lock:
                for cid, s in list(user_sessions.items()):
                    if s.get('step', 'main') != 'main':
                        idle = time.time() - s.get('last_activity', time.time())
                        if idle > SESSION_TIMEOUT:
                            user_sessions[cid] = {'step': 'main', 'data': {}, 'last_activity': time.time()}
                            expired.append(cid)
            for cid in expired:
                try:
                    send_message(cid,
                        f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                        f"⏰  <b>〔 Session Expired 〕</b>\n\n"
                        f"⚠️  Reason   ·  Idle for 60 seconds\n"
                        f"💳  Credits  ·  Not deducted\n\n"
                        f"{DIVIDER}\n"
                        f"<i>🔄  Select a method below to start again.</i>",
                        reply_markup=get_main_keyboard()
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Session cleanup error: {e}")

# ============== CHANNEL MEMBERSHIP ==============
def is_channel_member(user_id):
    """
    Check if user is a member of the required Telegram channel.
    """
    try:
        if str(user_id) == str(OWNER_ID):
            return True
            
        target_chat = int(CHANNEL_USERNAME) if str(CHANNEL_USERNAME).startswith("-100") or str(CHANNEL_USERNAME).isdigit() else CHANNEL_USERNAME
        
        response = get_telegram_session().get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getChatMember",
            params={'chat_id': target_chat, 'user_id': int(user_id)},
            timeout=6
        )
        
        if response.status_code == 200:
            r = response.json()
            if r.get('ok'):
                status = r['result']['status']
                return status in ('member', 'administrator', 'creator')
        logger.warning(f"Verification Check returned non-200 or failure layout: {response.text}")
    except Exception as e:
        logger.error(f"Channel check error: {e}")
    return False

# ============== BOT USERNAME ==============
_bot_username = None
def get_bot_username():
    global _bot_username
    if _bot_username:
        return _bot_username
    try:
        r = get_telegram_session().get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe", timeout=5
        ).json()
        if r.get('ok'):
            _bot_username = r['result']['username']
    except Exception:
        pass
    return _bot_username or "UIDAIGrambot"

# ============== TELEGRAM HELPERS ==============
def send_message(chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'}
    if reply_markup:
        data['reply_markup'] = json.dumps(reply_markup)
    try:
        response = get_telegram_session().post(url, json=data, timeout=10)
        result = response.json()
        if not result.get('ok'):
            logger.error(f"Telegram send error: {result}")
        return result
    except Exception as e:
        logger.error(f"Error sending message: {e}")
        return None

def answer_callback_query(callback_query_id, text=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    data = {'callback_query_id': callback_query_id}
    if text:
        data['text'] = text
    try:
        get_telegram_session().post(url, json=data, timeout=5)
    except Exception as e:
        logger.error(f"Error answering callback: {e}")

def send_photo(chat_id, photo_bytes, caption=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {'photo': ('captcha.png', photo_bytes, 'image/png')}
    data = {'chat_id': chat_id, 'parse_mode': 'HTML'}
    if caption:
        data['caption'] = caption
    try:
        response = get_telegram_session().post(url, data=data, files=files, timeout=20)
        return response.json()
    except Exception as e:
        logger.error(f"Error sending photo: {e}")
        return None

def send_photo_url(chat_id, url, caption=None, reply_markup=None):
    tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    data   = {'chat_id': chat_id, 'photo': url, 'parse_mode': 'HTML'}
    if caption:
        data['caption'] = caption
    if reply_markup:
        data['reply_markup'] = json.dumps(reply_markup)
    try:
        get_telegram_session().post(tg_url, json=data, timeout=15)
    except Exception as e:
        logger.error(f"Error sending photo URL: {e}")

def edit_message(chat_id, message_id, text):
    url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    data = {'chat_id': chat_id, 'message_id': message_id, 'text': text, 'parse_mode': 'HTML'}
    try:
        get_telegram_session().post(url, json=data, timeout=10)
    except Exception as e:
        logger.error(f"Error editing message: {e}")

def send_document(chat_id, file_path, caption=None, filename="Aadhaar.pdf"):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    try:
        with open(file_path, 'rb') as f:
            files = {'document': (filename, f, 'application/pdf')}
            data = {'chat_id': chat_id, 'parse_mode': 'HTML'}
            if caption:
                data['caption'] = caption
            response = get_telegram_session().post(url, data=data, files=files, timeout=30).json()
        try:
            os.remove(file_path)
        except Exception:
            pass
        return response
    except Exception as e:
        logger.error(f"Error sending document: {e}")
        return None

# ============== KEYBOARDS ==============
def get_main_keyboard():
    return {
        'keyboard': [
            ['📱  Mobile Number', '🆔  Aadhaar Number'],
            ['📋  EID'],
            ['💳  Credits', '💰  Buy Credits', '🎁  Referral'],
        ],
        'resize_keyboard': True,
        'one_time_keyboard': False
    }

def get_cancel_keyboard():
    return {'inline_keyboard': [[{'text': '❌  Cancel', 'callback_data': 'cancel'}]]}

def get_buy_keyboard():
    return {
        'inline_keyboard': [
            [{'text': '💳  10 Credits  —  $10',  'callback_data': 'buy_10'}],
            [{'text': '💳  20 Credits  —  $20',  'callback_data': 'buy_20'}],
            [{'text': '💳  50 Credits  —  $50',  'callback_data': 'buy_50'}],
            [{'text': '👑  Lifetime     —  $100', 'callback_data': 'buy_100'}],
        ]
    }

def get_join_keyboard():
    return {
        'inline_keyboard': [
            [{'text': '🌐  Join Channel',    'url': CHANNEL_LINK}],
            [{'text': '✅  I have joined',   'callback_data': 'check_join'}],
        ]
    }

# ============== ADMIN KEYBOARDS ==============
def get_admin_home_keyboard():
    fm   = is_free_mode()
    fc   = get_free_credits()
    fm_label = f"🔓  Free Mode: {'ON ✅' if fm else 'OFF ❌'}"
    fc_label = f"🎁  Free Credits: {fc}"
    return {
        'inline_keyboard': [
            [{'text': '📊  Statistics',      'callback_data': 'adm_stats'},
             {'text': '👥  User List',        'callback_data': 'adm_users_0'}],
            [{'text': '🔍  Lookup User',      'callback_data': 'adm_lookup'},
             {'text': '📢  Broadcast',        'callback_data': 'adm_broadcast'}],
            [{'text': fm_label,               'callback_data': 'adm_freemode'}],
            [{'text': fc_label,               'callback_data': 'adm_freecredits'}],
            [{'text': '💳  Anti-Captcha Bal', 'callback_data': 'adm_acbal'}],
        ]
    }

def get_admin_back_keyboard():
    return {'inline_keyboard': [[{'text': '◀  Back to Panel', 'callback_data': 'adm_home'}]]}

def get_user_profile_keyboard(uid, banned, lifetime):
    rows = [
        [{'text': '➕  Add Credits',    'callback_data': f'adm_addcr_{uid}'},
         {'text': '➖  Remove Credits', 'callback_data': f'adm_remcr_{uid}'}],
        [{'text': '♾  Grant Lifetime',  'callback_data': f'adm_lifetime_{uid}'}],
        [{'text': '🚫  Ban' if not banned else '✅  Unban',
          'callback_data': f'adm_ban_{uid}' if not banned else f'adm_unban_{uid}'}],
        [{'text': '◀  Back',            'callback_data': 'adm_home'}],
    ]
    return {'inline_keyboard': rows}

def get_users_page_keyboard(page, total_pages):
    nav = []
    if page > 0:
        nav.append({'text': '◀  Prev', 'callback_data': f'adm_users_{page-1}'})
    if page < total_pages - 1:
        nav.append({'text': 'Next  ▶', 'callback_data': f'adm_users_{page+1}'})
    rows = []
    if nav:
        rows.append(nav)
    rows.append([{'text': '◀  Back to Panel', 'callback_data': 'adm_home'}])
    return {'inline_keyboard': rows}

# ============== SHARED DISPLAY HELPERS ==============
def show_credits_info(chat_id):
    u  = get_user(chat_id)
    cr = get_credits(chat_id)
    free_mode = is_free_mode()
    if free_mode:
        cr_display = "🔓  Unlimited (Free Mode)"
    elif cr == float('inf'):
        cr_display = "♾  Lifetime"
    else:
        cr_display = f"<b>{int(cr)}</b>"
    ref_count  = u.get('referral_count', 0) if u else 0
    joined     = u.get('joined', '')[:10] if u else '—'
    note = ("🔓  Free Mode is active — unlimited downloads\n"
            "🔹  Earn free credits via your referral link") if free_mode else (
            "🔹  1 credit = 1 Aadhaar download\n"
            "🔹  Earn free credits via your referral link")
    send_message(
        chat_id,
        f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
        f"💳  <b>〔 My Credits 〕</b>\n\n"
        f"⚡  Balance      ·  {cr_display}\n"
        f"👥  Referrals    ·  {ref_count}\n"
        f"📅  Member since ·  {joined}\n\n"
        f"{DIVIDER}\n"
        f"<i>{note}</i>"
    )

def show_buy_menu(chat_id):
    send_message(
        chat_id,
        f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
        f"💰  <b>〔 Buy Credits 〕</b>\n\n"
        f"💳  10 credits    ·  <b>$10</b>\n"
        f"💳  20 credits    ·  <b>$20</b>\n"
        f"💳  50 credits    ·  <b>$50</b>\n"
        f"👑  Lifetime      ·  <b>$100</b>\n\n"
        f"{DIVIDER}\n"
        f"<i>👇  Tap a plan below to see payment details</i>",
        reply_markup=get_buy_keyboard()
    )

def show_referral_info(chat_id):
    username  = get_bot_username()
    link      = f"https://t.me/{username}?start=ref_{chat_id}"
    u         = get_user(chat_id)
    ref_count = u.get('referral_count', 0) if u else 0
    earned    = ref_count
    send_message(
        chat_id,
        f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
        f"🎁  <b>〔 Referral Program 〕</b>\n\n"
        f"🔗  Your Link:\n<code>{link}</code>\n\n"
        f"👥  Friends joined  ·  <b>{ref_count}</b>\n"
        f"⚡  Credits earned  ·  <b>{earned}</b>\n\n"
        f"{DIVIDER}\n"
        f"<i>💡  Share your link — earn +1 credit per friend who joins!</i>"
    )

# ============== ADMIN DISPLAY ==============
def show_admin_home(chat_id):
    data = all_users()
    total          = len(data)
    lifetime_count = sum(1 for u in data.values() if u.get('lifetime'))
    total_credits  = sum(u.get('credits', 0) for u in data.values() if not u.get('lifetime'))
    banned_count   = sum(1 for u in data.values() if u.get('banned'))
    today          = datetime.now().date().isoformat()
    new_today      = sum(1 for u in data.values() if u.get('joined', '')[:10] == today)
    fm             = is_free_mode()
    fc             = get_free_credits()
    send_message(
        chat_id,
        f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
        f"<b>〔 Admin Panel 〕</b>\n\n"
        f"◈  Total Users   ·  <b>{total}</b>\n"
        f"◈  New Today     ·  <b>{new_today}</b>\n"
        f"◈  Lifetime      ·  <b>{lifetime_count}</b>\n"
        f"◈  Credits Pool  ·  <b>{total_credits}</b>\n"
        f"◈  Banned        ·  <b>{banned_count}</b>\n\n"
        f"{DIVIDER}\n"
        f"◈  Free Mode     ·  <b>{'ON ✅' if fm else 'OFF ❌'}</b>\n"
        f"◈  Free Credits  ·  <b>{fc}</b> per new user\n\n"
        f"{DIVIDER}\n"
        f"<i>◌  Select an action below</i>",
        reply_markup=get_admin_home_keyboard()
    )

def show_admin_stats(chat_id):
    data = all_users()
    total          = len(data)
    lifetime_count = sum(1 for u in data.values() if u.get('lifetime'))
    total_credits  = sum(u.get('credits', 0) for u in data.values() if not u.get('lifetime'))
    banned_count   = sum(1 for u in data.values() if u.get('banned'))
    total_refs     = sum(u.get('referral_count', 0) for u in data.values())
    today          = datetime.now().date().isoformat()
    new_today      = sum(1 for u in data.values() if u.get('joined', '')[:10] == today)
    rich_users     = [(uid, u.get('credits', 0)) for uid, u in data.items() if not u.get('lifetime') and u.get('credits', 0) > 0]
    rich_users.sort(key=lambda x: x[1], reverse=True)
    top_str = "\n".join(f"   <code>{uid}</code>  ·  {cr}" for uid, cr in rich_users[:5]) or "   —"
    send_message(
        chat_id,
        f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
        f"<b>〔 Statistics 〕</b>\n\n"
        f"◈  Total Users     ·  <b>{total}</b>\n"
        f"◈  New Today        ·  <b>{new_today}</b>\n"
        f"◈  Lifetime Users  ·  <b>{lifetime_count}</b>\n"
        f"◈  Banned Users     ·  <b>{banned_count}</b>\n"
        f"◈  Credits in Pool ·  <b>{total_credits}</b>\n"
        f"◈  Total Referrals ·  <b>{total_refs}</b>\n\n"
        f"<b>Top 5 by credits:</b>\n{top_str}\n\n"
        f"{DIVIDER}",
        reply_markup=get_admin_back_keyboard()
    )

def show_admin_user_profile(chat_id, target_uid):
    u = get_user(target_uid)
    if not u:
        send_message(chat_id,
            f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
            f"✗  User <code>{target_uid}</code> not found.\n\n{DIVIDER}",
            reply_markup=get_admin_back_keyboard()
        )
        return
    cr         = get_credits(target_uid)
    cr_display = "♾  Lifetime" if cr == float('inf') else str(int(cr))
    banned    = u.get('banned', False)
    joined    = u.get('joined', '—')[:10]
    refs      = u.get('referral_count', 0)
    referred  = u.get('referred_by', '—') or '—'
    status    = '🚫  Banned' if banned else ('♾  Lifetime' if u.get('lifetime') else '✅  Active')
    send_message(
        chat_id,
        f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
        f"<b>〔 User Profile 〕</b>\n\n"
        f"◈  ID           ·  <code>{target_uid}</code>\n"
        f"◈  Status       ·  {status}\n"
        f"◈  Credits      ·  <b>{cr_display}</b>\n"
        f"◈  Referrals   ·  {refs}\n"
        f"◈  Referred By ·  {referred}\n"
        f"◈  Joined       ·  {joined}\n\n"
        f"{DIVIDER}",
        reply_markup=get_user_profile_keyboard(target_uid, banned, cr == float('inf'))
    )

def show_admin_users_page(chat_id, page):
    data   = all_users()
    items  = sorted(data.items(), key=lambda x: x[1].get('joined', ''), reverse=True)
    per_page = 10
    total_pages = max(1, (len(items) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    chunk = items[page * per_page:(page + 1) * per_page]
    lines = []
    for uid, u in chunk:
        cr  = '♾' if u.get('lifetime') else str(u.get('credits', 0))
        ban = '🚫' if u.get('banned') else '✅'
        lines.append(f"{ban}  <code>{uid}</code>  ·  {cr} cr  ·  {u.get('joined','')[:10]}")
    body = "\n".join(lines) or "No users."
    send_message(
        chat_id,
        f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
        f"<b>〔 Users  —  Page {page+1}/{total_pages} 〕</b>\n\n"
        f"{body}\n\n"
        f"{DIVIDER}\n"
        f"<i>◌  Use /lookup &lt;id&gt; to view a profile</i>",
        reply_markup=get_users_page_keyboard(page, total_pages)
    )

def get_anticaptcha_balance():
    if not ANTI_CAPTCHA_KEY:
        return None
    try:
        r = requests.post(
            "https://api.anti-captcha.com/getBalance",
            json={"clientKey": ANTI_CAPTCHA_KEY}, timeout=8
        ).json()
        if r.get("errorId") == 0:
            return r.get("balance", 0)
    except Exception:
        pass
    return None

def broadcast_message(sender_id, text):
    data   = all_users()
    sent   = 0
    failed = 0
    for uid in data:
        if uid == str(sender_id):
            continue
        try:
            result = send_message(int(uid), text)
            if result and result.get('ok'):
                sent += 1
            else:
                failed += 1
        except Exception:
            failed += 1
        time.sleep(0.05)
    return sent, failed

# ============== GATES ==============
def channel_gate(chat_id):
    if is_channel_member(chat_id):
        return True
    send_message(
        chat_id,
        f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
        f"🌐  <b>〔 Channel Required 〕</b>\n\n"
        f"⚠️  Join <b>{CHANNEL_NAME}</b> to use this bot.\n\n"
        f"{DIVIDER}\n"
        f"<i>👇  Tap Join below, then confirm with the button.</i>",
        reply_markup=get_join_keyboard()
    )
    return False

def credit_gate(chat_id):
    if has_credits(chat_id):
        return True
    send_message(
        chat_id,
        f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
        f"💳  <b>〔 No Credits 〕</b>\n\n"
        f"❌  Balance  ·  <b>0</b>\n\n"
        f"💰  Tap <b>Buy Credits</b> to purchase a plan.\n"
        f"🎁  Tap <b>Referral</b> to earn credits free.\n\n"
        f"{DIVIDER}"
    )
    return False

# ============== PDF DELIVERY ==============
def deliver_pdf(chat_id, pdf_path, verified_name):
    name_display = verified_name if verified_name and verified_name.strip() else "Mr."
    send_message(
        chat_id,
        f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
        f"⚙️  <b>〔 Processing 〕</b>\n\n"
        f"<i>🔓  Decrypting your document…</i>"
    )
    try:
        crack_success, password, decrypted_path, _ = _new_bot().crack_pdf_with_name(pdf_path, name_display, None)
        if crack_success and decrypted_path:
            caption = (
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"📄  <b>〔 Document Ready ✅ 〕</b>\n\n"
                f"👤  Name    ·  {name_display}\n"
                f"📋  Format  ·  e-Aadhaar PDF\n"
                f"🔓  Status  ·  <b>Unlocked</b>\n"
                f"{DIVIDER}"
            )
            send_document(chat_id, decrypted_path, caption=caption, filename="Aadhaar.pdf")
            try:
                if os.path.exists(pdf_path):
                    os.remove(pdf_path)
            except Exception:
                pass
        else:
            caption = (
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"📄  <b>〔 Document Ready 〕</b>\n\n"
                f"👤  Name    ·  {name_display}\n"
                f"📋  Format  ·  e-Aadhaar PDF\n"
                f"🔒  Status  ·  Password Protected\n"
                f"{DIVIDER}\n\n"
                f"<i>💡  Password: first 4 letters of name + birth year\n"
                f"   Example: <code>RAJE1995</code></i>"
            )
            send_document(chat_id, pdf_path, caption=caption, filename="Aadhaar.pdf")
    except Exception as e:
        logger.error(f"PDF delivery error: {e}")
        send_document(
            chat_id, pdf_path,
            caption=f"<b>{BOT_NAME}</b>\n{DIVIDER}\n📄  <b>〔 Document Ready 〕</b>",
            filename="Aadhaar.pdf"
        )

    deduct_credit(chat_id)
    cr = get_credits(chat_id)
    cr_display = "♾  Lifetime" if cr == float('inf') else str(int(cr))
    clear_session(chat_id)
    send_message(
        chat_id,
        f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
        f"✅  <b>〔 Download Complete 〕</b>\n\n"
        f"💳  Credits remaining  ·  {cr_display}\n\n"
        f"{DIVIDER}\n"
        f"<i>🔄  Select a method below for another download.</i>",
        reply_markup=get_main_keyboard()
    )

# ============== CALLBACK HANDLER ==============
def handle_callback(chat_id, callback_query_id, data):
    answer_callback_query(callback_query_id)
    ensure_user(chat_id)

    if data == 'check_join':
        if is_channel_member(chat_id):
            ensure_user(chat_id)
            cr = get_credits(chat_id)
            cr_display = "♾  Lifetime" if cr == float('inf') else str(int(cr))
            send_message(
                chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"✅  <b>〔 Access Granted 〕</b>\n\n"
                f"🟢  Status   ·  Verified\n"
                f"💳  Credits  ·  {cr_display}\n\n"
                f"{DIVIDER}\n"
                f"<i>👇  Select a method below to begin.</i>",
                reply_markup=get_main_keyboard()
            )
        else:
            send_message(
                chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"⚠️  <b>〔 Not Joined Yet 〕</b>\n\n"
                f"❌  Channel membership not detected.\n\n"
                f"<i>👇  Join the channel, then tap the button again.</i>",
                reply_markup=get_join_keyboard()
            )
        return

    if data == 'cancel':
        clear_session(chat_id)
        send_message(
            chat_id,
            f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
            f"<i>❌  Session cancelled.</i>"
        )
        return

    if data == 'auto_name':
        s = get_session(chat_id)
        if s.get('step') == 'awaiting_name':
            handle_message(chat_id, 'MR')
        return

    if data == 'credits':
        show_credits_info(chat_id)
        return

    if data == 'buy':
        show_buy_menu(chat_id)
        return

    if data == 'referral':
        show_referral_info(chat_id)
        return

    if data.startswith('buy_'):
        plan_key = data.split('_')[1]
        plan = PLANS.get(plan_key)
        if not plan:
            return
        label = "Lifetime" if plan['lifetime'] else f"{plan['credits']} credits"
        send_message(
            chat_id,
            f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
            f"💰  <b>〔 Payment — {plan['price']} 〕</b>\n\n"
            f"📦  Plan    ·  {label}\n"
            f"💵  Amount  ·  <b>{plan['price']}</b>\n\n"
            f"{DIVIDER}\n"
            f"📩  Message <b>{OWNER_USERNAME}</b> on Telegram to pay\n\n"
            f"🪪  Your ID  ·  <code>{chat_id}</code>\n\n"
            f"{DIVIDER}\n"
            f"<i>⏳  Credits will be added after payment is verified.</i>"
        )
        return

    # ── ADMIN CALLBACKS ──────────────────────────────────────
    if chat_id == OWNER_ID and data.startswith('adm_'):
        if data == 'adm_home':
            show_admin_home(chat_id)
            return

        if data == 'adm_stats':
            show_admin_stats(chat_id)
            return

        if data == 'adm_freemode':
            new_state = toggle_free_mode()
            state_str = 'ON ✅  — Users can download without credits' if new_state else 'OFF ❌  — Credits required as normal'
            send_message(chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"<b>〔 Free Mode 〕</b>\n\n"
                f"◈  Status  ·  <b>{state_str}</b>\n\n{DIVIDER}",
                reply_markup=get_admin_home_keyboard()
            )
            return

        if data == 'adm_freecredits':
            set_session(chat_id, 'admin_awaiting_freecredits', {})
            send_message(chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"<b>〔 Free Credits 〕</b>\n\n"
                f"◈  Current  ·  <b>{get_free_credits()}</b> credits per new user\n\n"
                f"▸  Enter the new amount\n\n{DIVIDER}",
                reply_markup=get_admin_back_keyboard()
            )
            return

        if data == 'adm_acbal':
            bal = get_anticaptcha_balance()
            if bal is None:
                msg = "✗  Could not fetch balance (key missing or API error)."
            else:
                msg = f"◈  Anti-Captcha Balance  ·  <b>${bal:.4f}</b>"
            send_message(chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n<b>〔 Anti-Captcha 〕</b>\n\n{msg}\n\n{DIVIDER}",
                reply_markup=get_admin_back_keyboard()
            )
            return

        if data == 'adm_lookup':
            set_session(chat_id, 'admin_awaiting_lookup', {})
            send_message(chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"<b>〔 Lookup User 〕</b>\n\n"
                f"▸  Send the Telegram User ID\n\n{DIVIDER}",
                reply_markup=get_admin_back_keyboard()
            )
            return

        if data == 'adm_broadcast':
            set_session(chat_id, 'admin_awaiting_broadcast', {})
            send_message(chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"<b>〔 Broadcast 〕</b>\n\n"
                f"▸  Send the message text to broadcast to all users\n\n"
                f"<i>◌  HTML formatting supported</i>\n{DIVIDER}",
                reply_markup=get_admin_back_keyboard()
            )
            return

        if data.startswith('adm_users_'):
            try:
                page = int(data.split('_')[-1])
            except ValueError:
                page = 0
            show_admin_users_page(chat_id, page)
            return

        if data.startswith('adm_addcr_'):
            uid = data[len('adm_addcr_'):]
            set_session(chat_id, 'admin_awaiting_addcr', {'target_uid': uid})
            send_message(chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"<b>〔 Add Credits 〕</b>\n\n"
                f"◈  Target  ·  <code>{uid}</code>\n\n"
                f"▸  Enter amount to add (or <b>-1</b> for Lifetime)\n\n{DIVIDER}",
                reply_markup=get_admin_back_keyboard()
            )
            return

        if data.startswith('adm_remcr_'):
            uid = data[len('adm_remcr_'):]
            set_session(chat_id, 'admin_awaiting_remcr', {'target_uid': uid})
            send_message(chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"<b>〔 Remove Credits 〕</b>\n\n"
                f"◈  Target  ·  <code>{uid}</code>\n\n"
                f"▸  Enter amount to remove\n\n{DIVIDER}",
                reply_markup=get_admin_back_keyboard()
            )
            return

        if data.startswith('adm_lifetime_'):
            uid = data[len('adm_lifetime_'):]
            add_credits(uid, 0, make_lifetime=True)
            send_message(int(uid),
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"<b>〔 Credits Received 〕</b>\n\n"
                f"◈  Plan    ·  Lifetime\n"
                f"◈  Status  ·  Active\n\n{DIVIDER}",
                reply_markup=get_main_keyboard()
            )
            send_message(chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"✅  Granted <b>Lifetime</b> to <code>{uid}</code>\n\n{DIVIDER}",
                reply_markup=get_admin_back_keyboard()
            )
            return

        if data.startswith('adm_ban_'):
            uid = data[len('adm_ban_'):]
            set_ban(uid, True)
            send_message(chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"🚫  User <code>{uid}</code> has been <b>banned</b>.\n\n{DIVIDER}",
                reply_markup=get_admin_back_keyboard()
            )
            return

        if data.startswith('adm_unban_'):
            uid = data[len('adm_unban_'):]
            set_ban(uid, False)
            send_message(chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"✅  User <code>{uid}</code> has been <b>unbanned</b>.\n\n{DIVIDER}",
                reply_markup=get_admin_back_keyboard()
            )
            return

    # ── USER CALLBACKS ────────────────────────────────────────
    if data in ('search_mobile', 'search_aadhaar', 'search_eid'):
        if not channel_gate(chat_id):
            return
        if not credit_gate(chat_id):
            return

    if data == 'search_mobile':
        set_session(chat_id, 'awaiting_mobile', {'mode': 'mobile'})
        send_message(
            chat_id,
            f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
            f"<b>〔 Mobile Search 〕</b>\n\n"
            f"▸  Enter your 10-digit mobile number\n\n"
            f"<i>◌  OTP will be sent to this number</i>",
            reply_markup=get_cancel_keyboard()
        )
    elif data == 'search_aadhaar':
        set_session(chat_id, 'awaiting_aadhaar', {'mode': 'aadhaar'})
        send_message(
            chat_id,
            f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
            f"<b>〔 Aadhaar Search 〕</b>\n\n"
            f"▸  Enter your 12-digit Aadhaar number\n\n"
            f"<i>◌  Spaces are removed automatically</i>",
            reply_markup=get_cancel_keyboard()
        )
    elif data == 'search_eid':
        set_session(chat_id, 'awaiting_eid_input', {'mode': 'eid'})
        send_message(
            chat_id,
            f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
            f"<b>〔 EID Search 〕</b>\n\n"
            f"▸  Enter your Enrollment ID (EID)\n\n"
            f"<i>◌  Format: 1234/56789/12345</i>",
            reply_markup=get_cancel_keyboard()
        )

# ============== ADMIN COMMAND HANDLER ==============
def handle_admin_command(chat_id, text):
    parts = text.strip().split()
    cmd = parts[0].lower()

    if cmd == '/admin':
        show_admin_home(chat_id)
        return True

    if cmd == '/lookup' and len(parts) == 2:
        show_admin_user_profile(chat_id, parts[1])
        return True

    return False

# ============== MESSAGE HANDLER ==============
_KB_ACTIONS = {
    '📱  mobile number':  'search_mobile',
    '🆔  aadhaar number': 'search_aadhaar',
    '📋  eid':            'search_eid',
    '💳  credits':        'credits',
    '💰  buy credits':    'buy',
    '🎁  referral':       'referral',
}

def handle_message(chat_id, message_text):
    logger.info(f"Msg [{chat_id}]: {message_text[:60]}")
    ensure_user(chat_id)

    if is_banned(chat_id):
        send_message(chat_id,
            f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
            f"🚫  <b>You have been banned.</b>\n\n"
            f"<i>◌  Contact support if you believe this is a mistake.</i>"
        )
        return

    if chat_id != OWNER_ID and not is_channel_member(chat_id):
        send_message(chat_id,
            f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
            f"<b>〔 Channel Required 〕</b>\n\n"
            f"▸  Join <b>{CHANNEL_NAME}</b> to use this bot.\n\n"
            f"{DIVIDER}\n"
            f"<i>◌  Tap Join below, then confirm with the button.</i>",
            reply_markup=get_join_keyboard()
        )
        return

    if chat_id == OWNER_ID and message_text.startswith('/'):
        if handle_admin_command(chat_id, message_text):
            return

    # ── ADMIN SESSION INPUT ───────────────────────────────────
    if chat_id == OWNER_ID:
        s    = get_session(chat_id)
        step = s.get('step', 'main')
        sd   = s.get('data', {})

        if step == 'admin_awaiting_lookup':
            clear_session(chat_id)
            show_admin_user_profile(chat_id, message_text.strip())
            return

        if step == 'admin_awaiting_addcr':
            clear_session(chat_id)
            uid = sd.get('target_uid', '')
            try:
                amount = int(message_text.strip())
                if amount == -1:
                    add_credits(uid, 0, make_lifetime=True)
                    send_message(int(uid),
                        f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                        f"<b>〔 Credits Received 〕</b>\n\n"
                        f"◈  Plan    ·  Lifetime\n◈  Status  ·  Active\n\n{DIVIDER}",
                        reply_markup=get_main_keyboard()
                    )
                    send_message(chat_id,
                        f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                        f"✅  Granted <b>Lifetime</b> to <code>{uid}</code>\n\n{DIVIDER}",
                        reply_markup=get_admin_back_keyboard()
                    )
                else:
                    add_credits(uid, amount)
                    send_message(int(uid),
                        f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                        f"<b>〔 Credits Received 〕</b>\n\n"
                        f"◈  Credits  ·  +{amount}\n"
                        f"◈  Balance  ·  {int(get_credits(uid))}\n\n{DIVIDER}",
                        reply_markup=get_main_keyboard()
                    )
                    send_message(chat_id,
                        f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                        f"✅  Added <b>{amount}</b> credits to <code>{uid}</code>\n\n{DIVIDER}",
                        reply_markup=get_admin_back_keyboard()
                    )
            except ValueError:
                send_message(chat_id, f"<b>{BOT_NAME}</b>\n{DIVIDER}\n✗  Invalid amount.\n\n{DIVIDER}", reply_markup=get_admin_back_keyboard())
            return

        if step == 'admin_awaiting_remcr':
            clear_session(chat_id)
            uid = sd.get('target_uid', '')
            try:
                amount = int(message_text.strip())
                remove_credits_from(uid, amount)
                send_message(chat_id,
                    f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                    f"✅  Removed <b>{amount}</b> credits from <code>{uid}</code>\n"
                    f"◈  New Balance  ·  {int(get_credits(uid))}\n\n{DIVIDER}",
                    reply_markup=get_admin_back_keyboard()
                )
            except ValueError:
                send_message(chat_id, f"<b>{BOT_NAME}</b>\n{DIVIDER}\n✗  Invalid amount.\n\n{DIVIDER}", reply_markup=get_admin_back_keyboard())
            return

        if step == 'admin_awaiting_freecredits':
            clear_session(chat_id)
            try:
                amount = int(message_text.strip())
                if amount < 0:
                    raise ValueError
                set_free_credits(amount)
                send_message(chat_id,
                    f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                    f"✅  Free credits set to <b>{amount}</b> per new user.\n\n{DIVIDER}",
                    reply_markup=get_admin_home_keyboard()
                )
            except ValueError:
                send_message(chat_id,
                    f"<b>{BOT_NAME}</b>\n{DIVIDER}\n✗  Invalid amount. Enter a number ≥ 0.\n\n{DIVIDER}",
                    reply_markup=get_admin_back_keyboard()
                )
            return

        if step == 'admin_awaiting_broadcast':
            clear_session(chat_id)
            send_message(chat_id, f"<b>{BOT_NAME}</b>\n{DIVIDER}\n<b>〔 Broadcasting… 〕</b>\n\n<i>◌  Please wait…</i>")
            sent, failed = broadcast_message(chat_id, message_text)
            send_message(chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"<b>〔 Broadcast Complete 〕</b>\n\n"
                f"◈  Sent    ·  <b>{sent}</b>\n"
                f"◈  Failed  ·  <b>{failed}</b>\n\n{DIVIDER}",
                reply_markup=get_admin_back_keyboard()
            )
            return

    action = _KB_ACTIONS.get(message_text.strip().lower())
    if action:
        if action in ('search_mobile', 'search_aadhaar', 'search_eid'):
            if not channel_gate(chat_id):
                return
            if not credit_gate(chat_id):
                return
            clear_session(chat_id)
            if action == 'search_mobile':
                set_session(chat_id, 'awaiting_mobile', {'mode': 'mobile'})
                send_message(
                    chat_id,
                    f"<b>{BOT_NAME}</b>\n{DIVIDER}\n📱  <b>〔 Mobile Search 〕</b>\n\n▸  Enter your 10-digit mobile number\n\n<i>📩  OTP will be sent to this number</i>",
                    reply_markup=get_cancel_keyboard()
                )
            elif action == 'search_aadhaar':
                set_session(chat_id, 'awaiting_aadhaar', {'mode': 'aadhaar'})
                send_message(
                    chat_id,
                    f"<b>{BOT_NAME}</b>\n{DIVIDER}\n🆔  <b>〔 Aadhaar Search 〕</b>\n\n▸  Enter your 12-digit Aadhaar number\n\n<i>🔹  Spaces are removed automatically</i>",
                    reply_markup=get_cancel_keyboard()
                )
            elif action == 'search_eid':
                set_session(chat_id, 'awaiting_eid_input', {'mode': 'eid'})
                send_message(
                    chat_id,
                    f"<b>{BOT_NAME}</b>\n{DIVIDER}\n📋  <b>〔 EID Search 〕</b>\n\n▸  Enter your Enrollment ID (EID)\n\n<i>🔹  Format: 1234/56789/12345</i>",
                    reply_markup=get_cancel_keyboard()
                )
        elif action == 'credits':
            show_credits_info(chat_id)
        elif action == 'buy':
            show_buy_menu(chat_id)
        elif action == 'referral':
            show_referral_info(chat_id)
        return

    s = get_session(chat_id)
    current_step = s.get('step', 'main')
    d = s.get('data', {})

    if current_step != 'main':
        idle = time.time() - s.get('last_activity', time.time())
        if idle > SESSION_TIMEOUT:
            clear_session(chat_id)
            send_message(
                chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"⏰  <b>〔 Session Expired 〕</b>\n\n"
                f"⚠️  Reason   ·  Idle for 60 seconds\n"
                f"💳  Credits  ·  Not deducted\n\n"
                f"{DIVIDER}\n"
                f"<i>🔄  Select a method below to start again.</i>"
            )
            return

    touch_session(chat_id)

    if message_text.lower() in ['/cancel', 'cancel']:
        clear_session(chat_id)
        send_message(
            chat_id,
            f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
            f"<i>❌  Session cancelled.</i>"
        )
        return

    if current_step == 'main':
        clean_digits = re.sub(r'\D', '', message_text)
        if len(clean_digits) == 12 and clean_digits.startswith('91'):
            clean_digits = clean_digits[2:]
        elif len(clean_digits) == 11 and clean_digits.startswith('0'):
            clean_digits = clean_digits[1:]

        # Auto-detect 10-digit mobile number sent in main step
        if len(clean_digits) == 10:
            if not channel_gate(chat_id) or not credit_gate(chat_id):
                return
            set_session(chat_id, 'awaiting_name', {'mode': 'mobile', 'mobile': clean_digits})
            send_message(
                chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"📱  <b>Mobile:</b> <code>{clean_digits}</code>\n\n"
                f"👤  <b>〔 Step 2 of 4 — Name 〕</b>\n\n"
                f"▸  Enter your full name as on Aadhaar\n\n"
                f"<i>💡  Don't know the name? Press the button below.</i>",
                reply_markup={
                    'inline_keyboard': [
                        [{'text': '🔍  Find Auto', 'callback_data': 'auto_name'}],
                        [{'text': '❌  Cancel',    'callback_data': 'cancel'}],
                    ]
                }
            )
            return

        # Auto-detect 12-digit Aadhaar number sent in main step
        if len(clean_digits) == 12:
            if not channel_gate(chat_id) or not credit_gate(chat_id):
                return
            set_session(chat_id, 'awaiting_captcha_direct', {'eid': clean_digits, 'verified_name': 'Mr.'})
            _r3 = send_message(
                chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"🆔  <b>Aadhaar:</b> <code>{clean_digits}</code>\n\n"
                f"<b>〔 Captcha 〕</b>\n\n"
                f"◌  Generating…"
            )
            _mid3 = _r3.get('result', {}).get('message_id') if _r3 else None
            image_bytes, captcha_txn_id, transaction_id = _get_captcha(chat_id)
            if not image_bytes:
                _captcha_failed(chat_id, _mid3)
                return
            sd = get_session(chat_id)['data']
            solved = solve_captcha_auto(image_bytes)
            if solved:
                _txt3 = (f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                         f"<b>〔 Captcha 〕</b>\n\n"
                         f"◌  Generating…  ✓\n"
                         f"◌  Auto-solved   ✓\n"
                         f"◌  Sending OTP…")
                if _mid3: edit_message(chat_id, _mid3, _txt3)
                else: send_message(chat_id, _txt3)
                sd2 = {**sd, 'captcha2_txn_id': captcha_txn_id, 'transaction_id2': transaction_id, 'captcha2_code': solved}
                set_session(chat_id, 'sending_pdf_otp_direct', sd2)
                success, otp_txn_id, msg = _new_bot().send_aadhaar_otp(chat_id, sd2['eid'], solved, captcha_txn_id, transaction_id)
                if success:
                    set_session(chat_id, 'awaiting_pdf_otp_direct', {**sd2, 'pdf_otp_txn_id': otp_txn_id})
                    send_message(chat_id, f"<b>{BOT_NAME}</b>\n{DIVIDER}\n<b>〔 OTP Sent  ✓ 〕</b>\n\n▸  Enter the 6-digit OTP to download your PDF\n\n<i>◌  Valid for 10 minutes</i>", reply_markup=get_cancel_keyboard())
                else:
                    clear_session(chat_id)
                    send_message(chat_id, f"<b>{BOT_NAME}</b>\n{DIVIDER}\n❌  OTP failed — {msg}\n\n<i>◌  Select a method below to retry.</i>")
            else:
                set_session(chat_id, 'awaiting_captcha_direct', {**sd, 'captcha2_txn_id': captcha_txn_id, 'transaction_id2': transaction_id})
                send_photo(chat_id, image_bytes, caption="<i>▸  Type the characters shown above</i>")
            return

        return

    # MOBILE FLOW
    if current_step == 'awaiting_mobile':
        clean_mobile = re.sub(r'\D', '', message_text)
        if len(clean_mobile) == 12 and clean_mobile.startswith('91'):
            clean_mobile = clean_mobile[2:]
        elif len(clean_mobile) == 11 and clean_mobile.startswith('0'):
            clean_mobile = clean_mobile[1:]

        if len(clean_mobile) == 10:
            set_session(chat_id, 'awaiting_name', {**d, 'mobile': clean_mobile})
            send_message(
                chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"📱  <b>Mobile:</b> <code>{clean_mobile}</code>\n\n"
                f"👤  <b>〔 Step 2 of 4 — Name 〕</b>\n\n"
                f"▸  Enter your full name as on Aadhaar\n\n"
                f"<i>💡  Don't know the name? Press the button below.</i>",
                reply_markup={
                    'inline_keyboard': [
                        [{'text': '🔍  Find Auto', 'callback_data': 'auto_name'}],
                        [{'text': '❌  Cancel',    'callback_data': 'cancel'}],
                    ]
                }
            )
        else:
            send_message(
                chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"❌  Invalid number.\n\n"
                f"<i>🔹  Enter a 10-digit mobile number.</i>",
                reply_markup=get_cancel_keyboard()
            )

    elif current_step == 'awaiting_name':
        name = message_text.strip().upper() if len(message_text.strip()) >= 2 else "MR"
        _r = send_message(
            chat_id,
            f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
            f"<b>〔 Captcha 〕</b>\n\n"
            f"<b>〔 Captcha 〕</b>\n\n"
            f"◌  Generating…"
        )
        _mid = _r.get('result', {}).get('message_id') if _r else None
        image_bytes, captcha_txn_id, transaction_id = _get_captcha(chat_id)
        if image_bytes:
            solved = solve_captcha_auto(image_bytes)
            if solved:
                _txt = (f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                        f"<b>〔 Captcha 〕</b>\n\n"
                        f"◌  Generating…  ✓\n"
                        f"◌  Auto-solved   ✓\n"
                        f"◌  Sending OTP…")
                if _mid: edit_message(chat_id, _mid, _txt)
                else: send_message(chat_id, _txt)
                session_data = {**d, 'name': name, 'captcha1_txn_id': captcha_txn_id, 'transaction_id': transaction_id, 'captcha_code': solved}
                set_session(chat_id, 'sending_otp', session_data)
                success, result = _new_bot().send_eid_otp(chat_id, session_data['mobile'], session_data['name'], solved, captcha_txn_id, transaction_id)
                if success:
                    set_session(chat_id, 'awaiting_otp', {**session_data, 'eid_otp_txn_id': result})
                    send_message(chat_id, f"<b>{BOT_NAME}</b>\n{DIVIDER}\n📩  <b>〔 OTP Sent ✅ 〕</b>\n\n▸  Enter the 6-digit OTP sent to your mobile\n\n<i>⏳  Valid for 10 minutes</i>", reply_markup=get_cancel_keyboard())
                else:
                    clear_session(chat_id)
                    send_message(chat_id, f"<b>{BOT_NAME}</b>\n{DIVIDER}\n❌  OTP failed — {result}\n\n<i>🔄  Select a method below to retry.</i>")
            else:
                set_session(chat_id, 'awaiting_captcha1', {**d, 'name': name,
                                            'captcha1_txn_id': captcha_txn_id, 'transaction_id': transaction_id})
                send_photo(chat_id, image_bytes, caption="<i>👆  Type the characters shown above</i>")
        else:
            _captcha_failed(chat_id, _mid)

    elif current_step == 'awaiting_captcha1':
        set_session(chat_id, 'sending_otp', {**d, 'captcha_code': message_text.strip()})
        send_message(
            chat_id,
            f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
            f"📡  <b>〔 Sending OTP 〕</b>\n\n"
            f"<i>⏳  Please wait…</i>"
        )
        sd = get_session(chat_id)['data']
        success, result = _new_bot().send_eid_otp(
            chat_id, sd['mobile'], sd['name'],
            sd['captcha_code'], sd['captcha1_txn_id'], sd['transaction_id']
        )
        if success:
            set_session(chat_id, 'awaiting_otp', {**sd, 'eid_otp_txn_id': result})
            send_message(
                chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"📩  <b>〔 OTP Sent ✅ 〕</b>\n\n"
                f"▸  Enter the 6-digit OTP sent to your mobile\n\n"
                f"<i>⏳  Valid for 10 minutes</i>",
                reply_markup=get_cancel_keyboard()
            )
        else:
            clear_session(chat_id)
            send_message(
                chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"❌  OTP failed — {result}\n\n"
                f"<i>🔄  Select a method below to retry.</i>"
            )

    elif current_step == 'awaiting_otp':
        if re.match(r'^\d{6}$', message_text):
            send_message(
                chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"<b>〔 Verifying 〕</b>\n\n"
                f"<i>⏳  Checking OTP…</i>"
            )
            success, eid, name = _new_bot().verify_eid_otp(
                chat_id, d['mobile'], d['name'], message_text,
                d['eid_otp_txn_id'], d['captcha1_txn_id'], d['captcha_code']
            )
            if success:
                verified_name = name if name and name.strip() else "Mr."
                set_session(chat_id, 'awaiting_captcha2', {**d, 'eid': eid, 'verified_name': verified_name})
                _r2 = send_message(
                    chat_id,
                    f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                    f"✅  <b>〔 Identity Verified 〕</b>\n\n"
                    f"👤  Name  ·  {verified_name}\n"
                    f"🪪  EID   ·  <code>{eid}</code>\n\n"
                    f"{DIVIDER}\n"
                    f"🤖  <b>〔 Captcha for PDF 〕</b>\n\n"
                    f"◌  Generating…"
                )
                _mid2 = _r2.get('result', {}).get('message_id') if _r2 else None
                image_bytes, captcha_txn_id, transaction_id = _get_captcha(chat_id)
                if not image_bytes:
                    _captcha_failed(chat_id, _mid2)
                    return
                if image_bytes:
                    sd = get_session(chat_id)['data']
                    solved2 = solve_captcha_auto(image_bytes)
                    if solved2:
                        _txt2 = (f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                                 f"✅  <b>〔 Identity Verified 〕</b>\n\n"
                                 f"👤  Name  ·  {verified_name}\n"
                                 f"🪪  EID   ·  <code>{eid}</code>\n\n"
                                 f"{DIVIDER}\n"
                                 f"🤖  <b>〔 Captcha for PDF 〕</b>\n\n"
                                 f"◌  Generating…  ✓\n"
                                 f"◌  Auto-solved   ✓\n"
                                 f"◌  Sending OTP…")
                        if _mid2: edit_message(chat_id, _mid2, _txt2)
                        else: send_message(chat_id, _txt2)
                        sd2 = {**sd, 'captcha2_txn_id': captcha_txn_id, 'transaction_id2': transaction_id, 'captcha2_code': solved2}
                        set_session(chat_id, 'sending_pdf_otp', sd2)
                        success2, otp_txn_id, msg2 = _new_bot().send_aadhaar_otp(chat_id, sd2['eid'], solved2, captcha_txn_id, transaction_id)
                        if success2:
                            set_session(chat_id, 'awaiting_pdf_otp', {**sd2, 'pdf_otp_txn_id': otp_txn_id})
                            send_message(chat_id, f"<b>{BOT_NAME}</b>\n{DIVIDER}\n📩  <b>〔 OTP Sent ✅ 〕</b>\n\n▸  Enter the 6-digit OTP to download your PDF\n\n<i>⏳  Valid for 10 minutes</i>", reply_markup=get_cancel_keyboard())
                        else:
                            clear_session(chat_id)
                            send_message(chat_id, f"<b>{BOT_NAME}</b>\n{DIVIDER}\n❌  OTP failed — {msg2}\n\n<i>🔄  Select a method below to retry.</i>")
                    else:
                        set_session(chat_id, 'awaiting_captcha2', {**sd, 'captcha2_txn_id': captcha_txn_id, 'transaction_id2': transaction_id})
                        send_photo(chat_id, image_bytes, caption="<i>👆  Type the characters shown above</i>")
                else:
                    clear_session(chat_id)
                    send_message(
                        chat_id,
                        f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                        f"❌  Captcha unavailable.\n\n"
                        f"<i>🔄  Please try again.</i>"
                    )
            else:
                clear_session(chat_id)
                send_message(
                    chat_id,
                    f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                    f"❌  Verification failed — {eid}\n\n"
                    f"<i>🔄  Select a method below to retry.</i>"
                )
        else:
            send_message(
                chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"❌  Invalid OTP.\n\n"
                f"<i>🔹  Enter the 6-digit number (digits only).</i>"
            )

    elif current_step == 'awaiting_captcha2':
        sd = {**d, 'captcha2_code': message_text.strip()}
        set_session(chat_id, 'sending_pdf_otp', sd)
        send_message(
            chat_id,
            f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
            f"📡  <b>〔 Sending OTP for PDF 〕</b>\n\n"
            f"<i>⏳  Please wait…</i>"
        )
        success, otp_txn_id, msg = _new_bot().send_aadhaar_otp(
            chat_id, sd['eid'], sd['captcha2_code'], sd['captcha2_txn_id'], sd['transaction_id2']
        )
        if success:
            set_session(chat_id, 'awaiting_pdf_otp', {**sd, 'pdf_otp_txn_id': otp_txn_id})
            send_message(
                chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"📩  <b>〔 OTP Sent ✅ 〕</b>\n\n"
                f"▸  Enter the 6-digit OTP to download your PDF\n\n"
                f"<i>⏳  Valid for 10 minutes</i>",
                reply_markup=get_cancel_keyboard()
            )
        else:
            clear_session(chat_id)
            send_message(
                chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"❌  OTP failed — {msg}\n\n"
                f"<i>🔄  Select a method below to retry.</i>"
            )

    elif current_step == 'awaiting_pdf_otp':
        if re.match(r'^\d{6}$', message_text):
            send_message(
                chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"📥  <b>〔 Downloading 〕</b>\n\n"
                f"<i>⚡  Fetching your Aadhaar PDF…</i>"
            )
            success, pdf_path = _new_bot().download_aadhaar_pdf(
                chat_id, d['eid'], message_text, d['pdf_otp_txn_id'], d['transaction_id2'], False
            )
            if success and pdf_path and '.pdf' in pdf_path:
                deliver_pdf(chat_id, pdf_path, d.get('verified_name', 'Mr.'))
            else:
                clear_session(chat_id)
                send_message(
                    chat_id,
                    f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                    f"❌  Download failed — {pdf_path}\n\n"
                    f"<i>🔄  Select a method below to retry.</i>"
                )
        else:
            send_message(
                chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"❌  Invalid OTP.\n\n"
                f"<i>🔹  Enter the 6-digit number (digits only).</i>"
            )

    # AADHAAR / EID DIRECT FLOW
    elif current_step == 'awaiting_aadhaar':
        uid = message_text.strip().replace(' ', '')
        if re.match(r'^\d{12}$', uid):
            set_session(chat_id, 'awaiting_captcha_direct', {**d, 'eid': uid, 'verified_name': 'Mr.'})
            _r3 = send_message(
                chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"<b>〔 Captcha 〕</b>\n\n"
                f"<b>〔 Captcha 〕</b>\n\n"
                f"◌  Generating…"
            )
            _mid3 = _r3.get('result', {}).get('message_id') if _r3 else None
            image_bytes, captcha_txn_id, transaction_id = _get_captcha(chat_id)
            if not image_bytes:
                _captcha_failed(chat_id, _mid3)
                return
            if image_bytes:
                sd = get_session(chat_id)['data']
                solved = solve_captcha_auto(image_bytes)
                if solved:
                    _txt3 = (f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                             f"<b>〔 Captcha 〕</b>\n\n"
                             f"◌  Generating…  ✓\n"
                             f"◌  Auto-solved   ✓\n"
                             f"◌  Sending OTP…")
                    if _mid3: edit_message(chat_id, _mid3, _txt3)
                    else: send_message(chat_id, _txt3)
                    sd2 = {**sd, 'captcha2_txn_id': captcha_txn_id, 'transaction_id2': transaction_id, 'captcha2_code': solved}
                    set_session(chat_id, 'sending_pdf_otp_direct', sd2)
                    success, otp_txn_id, msg = _new_bot().send_aadhaar_otp(chat_id, sd2['eid'], solved, captcha_txn_id, transaction_id)
                    if success:
                        set_session(chat_id, 'awaiting_pdf_otp_direct', {**sd2, 'pdf_otp_txn_id': otp_txn_id})
                        send_message(chat_id, f"<b>{BOT_NAME}</b>\n{DIVIDER}\n<b>〔 OTP Sent  ✓ 〕</b>\n\n▸  Enter the 6-digit OTP to download your PDF\n\n<i>◌  Valid for 10 minutes</i>", reply_markup=get_cancel_keyboard())
                    else:
                        clear_session(chat_id)
                        send_message(chat_id, f"<b>{BOT_NAME}</b>\n{DIVIDER}\n推  OTP failed — {msg}\n\n<i>◌  Select a method below to retry.</i>")
                else:
                    set_session(chat_id, 'awaiting_captcha_direct', {**sd, 'captcha2_txn_id': captcha_txn_id, 'transaction_id2': transaction_id})
                    send_photo(chat_id, image_bytes, caption="<i>▸  Type the characters shown above</i>")
            else:
                clear_session(chat_id)
                send_message(
                    chat_id,
                    f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                    f"❌  Captcha unavailable.\n\n"
                    f"<i>🔄  Please try again.</i>"
                )
        else:
            send_message(
                chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"✗  Invalid Aadhaar.\n\n"
                f"<i>◌  Enter the 12-digit Aadhaar number (digits only).</i>"
            )

    elif current_step == 'awaiting_eid_input':
        eid = message_text.strip()
        if len(eid) >= 10:
            set_session(chat_id, 'awaiting_captcha_direct', {**d, 'eid': eid, 'verified_name': 'Mr.'})
            _r4 = send_message(
                chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"<b>〔 Captcha 〕</b>\n\n"
                f"◌  Generating…"
            )
            _mid4 = _r4.get('result', {}).get('message_id') if _r4 else None
            image_bytes, captcha_txn_id, transaction_id = _get_captcha(chat_id)
            if not image_bytes:
                _captcha_failed(chat_id, _mid4)
                return
            if image_bytes:
                sd = get_session(chat_id)['data']
                solved = solve_captcha_auto(image_bytes)
                if solved:
                    _txt4 = (f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                             f"<b>〔 Captcha 〕</b>\n\n"
                             f"◌  Generating…  ✓\n"
                             f"◌  Auto-solved   ✓\n"
                             f"◌  Sending OTP…")
                    if _mid4: edit_message(chat_id, _mid4, _txt4)
                    else: send_message(chat_id, _txt4)
                    sd2 = {**sd, 'captcha2_txn_id': captcha_txn_id, 'transaction_id2': transaction_id, 'captcha2_code': solved}
                    set_session(chat_id, 'sending_pdf_otp_direct', sd2)
                    success, otp_txn_id, msg = _new_bot().send_aadhaar_otp(chat_id, sd2['eid'], solved, captcha_txn_id, transaction_id)
                    if success:
                        set_session(chat_id, 'awaiting_pdf_otp_direct', {**sd2, 'pdf_otp_txn_id': otp_txn_id})
                        send_message(chat_id, f"<b>{BOT_NAME}</b>\n{DIVIDER}\n<b>〔 OTP Sent  ✓ 〕</b>\n\n▸  Enter the 6-digit OTP to download your PDF\n\n<i>◌  Valid for 10 minutes</i>", reply_markup=get_cancel_keyboard())
                    else:
                        clear_session(chat_id)
                        send_message(chat_id, f"<b>{BOT_NAME}</b>\n{DIVIDER}\n推  OTP failed — {msg}\n\n<i>◌  Select a method below to retry.</i>")
                else:
                    set_session(chat_id, 'awaiting_captcha_direct', {**sd, 'captcha2_txn_id': captcha_txn_id, 'transaction_id2': transaction_id})
                    send_photo(chat_id, image_bytes, caption="<i>▸  Type the characters shown above</i>")
            else:
                clear_session(chat_id)
                send_message(
                    chat_id,
                    f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                    f"❌  Captcha unavailable.\n\n"
                    f"<i>🔄  Please try again.</i>"
                )
        else:
            send_message(
                chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"✗  Invalid EID.\n\n"
                f"<i>#  Please check and re-enter your Enrollment ID.</i>"
            )

    elif current_step == 'awaiting_captcha_direct':
        sd = {**d, 'captcha2_code': message_text.strip()}
        set_session(chat_id, 'sending_pdf_otp_direct', sd)
        send_message(
            chat_id,
            f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
            f"📡  <b>〔 Sending OTP 〕</b>\n\n"
            f"<i>⏳  Please wait…</i>"
        )
        success, otp_txn_id, msg = _new_bot().send_aadhaar_otp(
            chat_id, sd['eid'], sd['captcha2_code'], sd['captcha2_txn_id'], sd['transaction_id2']
        )
        if success:
            set_session(chat_id, 'awaiting_pdf_otp_direct', {**sd, 'pdf_otp_txn_id': otp_txn_id})
            send_message(
                chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"📩  <b>〔 OTP Sent ✅ 〕</b>\n\n"
                f"▸  Enter the 6-digit OTP to download your PDF\n\n"
                f"<i>⏳  Valid for 10 minutes</i>",
                reply_markup=get_cancel_keyboard()
            )
        else:
            clear_session(chat_id)
            send_message(
                chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"❌  OTP failed — {msg}\n\n"
                f"<i>🔄  Select a method below to retry.</i>"
            )

    elif current_step == 'awaiting_pdf_otp_direct':
        if re.match(r'^\d{6}$', message_text):
            send_message(
                chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"📥  <b>〔 Downloading 〕</b>\n\n"
                f"<i>⚡  Fetching your Aadhaar PDF…</i>"
            )
            success, pdf_path = _new_bot().download_aadhaar_pdf(
                chat_id, d['eid'], message_text, d['pdf_otp_txn_id'], d['transaction_id2'], False
            )
            if success and pdf_path and '.pdf' in pdf_path:
                deliver_pdf(chat_id, pdf_path, d.get('verified_name', 'Mr.'))
            else:
                clear_session(chat_id)
                send_message(
                    chat_id,
                    f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                    f"❌  Download failed — {pdf_path}\n\n"
                    f"<i>🔄  Select a method below to retry.</i>"
                )
        else:
            send_message(
                chat_id,
                f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                f"❌  Invalid OTP.\n\n"
                f"<i>🔹  Enter the 6-digit number (digits only).</i>"
            )

# ============== CONCURRENT UPDATE DISPATCHER ==============
_msg_executor = ThreadPoolExecutor(max_workers=300)

def _process_update(update):
    try:
        if 'callback_query' in update:
            cq   = update['callback_query']
            cid  = cq['message']['chat']['id']
            cqid = cq['id']
            data = cq.get('data', '')
            handle_callback(cid, cqid, data)

        elif 'message' in update:
            msg  = update['message']
            cid  = msg['chat']['id']
            text = msg.get('text', '').strip()
            if not text:
                return

            if text.startswith('/start'):
                parts = text.split()
                referrer_id = None
                if len(parts) > 1 and parts[1].startswith('ref_'):
                    try:
                        referrer_id = int(parts[1][4:])
                    except ValueError:
                        pass

                if not is_channel_member(cid):
                    send_message(
                        cid,
                        f"<b>{BOT_NAME}</b>\n{DIVIDER}\n"
                        f"<b>〔 Channel Required 〕</b>\n\n"
                        f"▸  Join <b>{CHANNEL_NAME}</b> to use this bot.\n\n"
                        f"{DIVIDER}\n"
                        f"<i>◌  Tap the button below after joining.</i>",
                        reply_markup=get_join_keyboard()
                    )
                    return

                ensure_user(cid, referrer_id)
                clear_session(cid)
                cr = get_credits(cid)
                cr_display = "♾  Lifetime" if cr == float('inf') else str(int(cr))
                send_photo_url(
                    cid,
                    "https://cdn.phototourl.com/free/2026-08-06-213b94ef-6427-4561-81ff-678b15070f01.png",
                    caption=(
                        f"<b>{BOT_NAME}</b>\n{DIVIDER}\n\n"
                        f"<b>e-Aadhaar PDF  —  straight to Telegram</b>\n\n"
                        f"◈  Source     ·  Official UIDAI portal\n"
                        f"◈  Delivery  ·  Auto-unlocked, no password\n"
                        f"◈  Methods   ·  Mobile  ·  Aadhaar  ·  EID\n\n"
                        f"{DIVIDER}\n"
                        f"◈  Credits   ·  {cr_display}\n\n"
                        f"<i>◌  Select a method below to begin.</i>"
                    ),
                    reply_markup=get_main_keyboard()
                )
            elif text.startswith('/admin') and cid == OWNER_ID:
                handle_admin_command(cid, text)
            else:
                handle_message(cid, text)

    except Exception as e:
        logger.error(f"Update processing error: {e}")

# ============== GET UPDATES ==============
def get_updates(offset=None):
    url    = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {'timeout': 10, 'allowed_updates': ['message', 'callback_query']}
    if offset:
        params['offset'] = offset
    try:
        response = get_telegram_session().get(url, params=params, timeout=13)
        result   = response.json()
        if result.get('ok'):
            return result.get('result', [])
        else:
            logger.error(f"Telegram API error: {result}")
            return []
    except Exception as e:
        logger.error(f"Error getting updates: {e}")
        return []

# ============== MAIN ==============
def main():
    _init_db()

    print("━" * 50)
    print(f"  {BOT_NAME}  —  starting up")
    print("━" * 50)

    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("[ error ] TELEGRAM_BOT_TOKEN not set.")
        return

    try:
        r = get_telegram_session().get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe", timeout=10
        )
        bot_info = r.json()
        if bot_info.get('ok'):
            _bot_username_val = bot_info['result']['username']
            global _bot_username
            _bot_username = _bot_username_val
            print(f"[ online ]  @{_bot_username_val}")
            print(f"[ network]  direct connection")
            print(f"[ cracker]  PyPDF2 / 4 threads")
            print(f"[ credits]  system active")
            print(f"[ owner  ]  {OWNER_ID}")
        else:
            print(f"[ error ] Bot auth failed: {bot_info}")
            return
    except Exception as e:
        print(f"[ error ] {e}")
        return

    def _sigint(sig, frame):
        print("\n[ stopped ]")
        os._exit(0)
    signal.signal(signal.SIGINT, _sigint)

    def _start_health_server():
        port = int(os.environ.get("PORT", 0))
        if not port:
            return
        from http.server import HTTPServer, BaseHTTPRequestHandler
        class HealthHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"status": "online", "service": "telegram-bot"}')
            def log_message(self, format, *args):
                pass
        try:
            server = HTTPServer(('0.0.0.0', port), HealthHandler)
            hs_thread = threading.Thread(target=server.serve_forever, daemon=True)
            hs_thread.start()
            print(f"[ server ] Health server listening on 0.0.0.0:{port}")
        except Exception as ex:
            logger.warning(f"Health server failed on port {port}: {ex}")

    _start_health_server()

    t = threading.Thread(target=_cleanup_sessions, daemon=True)
    t.start()

    print("━" * 50)
    print("  running  —  Ctrl+C to stop")
    print("━" * 50)

    last_update_id = 0

    while True:
        try:
            updates = get_updates(last_update_id + 1)
            for update in updates:
                last_update_id = update.get('update_id')
                _msg_executor.submit(_process_update, update)

        except KeyboardInterrupt:
            print("\n[ stopped ]")
            os._exit(0)
        except Exception as e:
            logger.error(f"Main loop error: {e}")
            time.sleep(5)

def process_webhook_update(update):
    """Entry point for Vercel serverless webhook handler."""
    _process_update(update)

def init_bot():
    """Entry point to initialize database in serverless environment."""
    _init_db()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[ stopped ]")
        os._exit(0)
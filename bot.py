import asyncio
import telegram
import pandas as pd
import requests
import json
import os
import time
import html
import websockets
import threading
from collections import deque
from datetime import datetime
from telegram.request import HTTPXRequest

# ==========================================
# API 키 및 채널 정보 설정
# ==========================================
# 조회 전용 API 키라 여기 직접 입력하는 방식으로 되돌렸습니다.
# 아래 4~5개 값만 본인 것으로 채워 넣으면 됩니다. (텔레그램 토큰은 유출되면
# 봇을 남이 조종할 수 있으니, 재발급한 새 토큰으로 넣어주세요)
access_key = "WVYsdYxwX8mSLmNbfAAvpUfxmkDzmAUcDUwpxjix"
secret_key = "LAA8nPP3bHexq1RbNx55mt0pRm9FlNdNtWjIxBkD"
tel_token = "8736494256:AAHVC0AgdruU9uJAW6BP8tchOW0-Yhft23A"
chat_id = '-1003728148201'
briefing_chat_id = '-1003706095649'

if tel_token.startswith("여기에") or chat_id.startswith("여기에"):
    raise SystemExit(
        "❌ 코드 상단의 tel_token / chat_id 값을 실제 값으로 바꿔주세요."
    )

request = HTTPXRequest(connection_pool_size=50, read_timeout=10, write_timeout=10)
bot = telegram.Bot(token=tel_token, request=request)

# ==========================================
# 상태 저장소
# ==========================================
# 매수/매도를 절대 섞지 않도록 (symbol, side) 튜플을 키로 사용
accumulated_data = {}      # {(symbol, side): [{'time':..,'amount':..}, ...]}
last_alert_time = {}       # {(symbol, side): datetime}
exclude_tickers = ['KRW-BTC', 'KRW-ETH', 'KRW-XRP', 'KRW-SOL', 'KRW-USDT']

RAW_TRADES_BUFFER = []
raw_buffer_lock = threading.Lock()
KOREAN_NAMES_MAP = {}

# 5분/30분/1시간/4시간 등락률 계산용 시계열 저장소
# {symbol: deque[(timestamp, price), ...]}  (시간순 정렬 유지)
PRICE_WINDOWS_SEC = {'r5m': 300, 'r30m': 1800, 'r1h': 3600, 'r4h': 14400}
PRICE_HISTORY_MAX_SEC = 14400 + 600  # 4시간 10분치만 유지
price_snapshots = {}
price_snapshots_lock = threading.Lock()
_history_seeded = False

# 거래건수 기준 감지 (금액이 적어도 체결이 몰리면 감지)
TRADE_COUNT_WINDOW_SEC = 30
TRADE_COUNT_THRESHOLD = 20          # 30초 내 20건 이상 체결되면 "거래건수 급증"
TRADE_COUNT_MIN_AMOUNT = 300000     # 너무 작은 잡체결(더스트)까지 세지 않도록 최소 금액 필터


def format_price(price):
    if price >= 1000:
        return f"{price:,.0f}"
    elif price >= 100:
        return f"{price:,.1f}"
    elif price >= 1:
        return f"{price:,.2f}"
    else:
        return f"{price:,.4f}"


def format_won(value):
    if value >= 100000000: return f"{value / 100000000:,.2f}억"
    if value >= 10000: return f"{value / 10000:,.0f}만"
    return f"{value:,.0f}"


def save_whale_log_to_json(symbol, name, price, amount, total_window, side, type_icon, trigger_reason):
    log_file = "whale_alerts.json"
    new_log = {
        'time': datetime.now().strftime('%H:%M:%S'), 'symbol': symbol, 'name': name,
        'price': f"{format_price(price)}원", 'amount': format_won(amount),
        'total_30s': format_won(total_window),
        'side': side,                      # 'BID' or 'ASK'
        'side_label': '매수' if side == 'BID' else '매도',
        'type': type_icon,
        'reason': trigger_reason,          # '금액' or '건수'
    }
    logs = []
    if os.path.exists(log_file):
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                logs = json.load(f)
        except Exception:
            logs = []
    logs.insert(0, new_log)
    logs = logs[:100]
    tmp_file = log_file + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(logs, f, ensure_ascii=False, indent=4)
    os.replace(tmp_file, log_file)


def collect_raw_trade_to_buffer(coin, amount, ask_bid):
    if amount < 100000:
        return  # 10만원 미만 잡체결은 노이즈라 제외 (실시간 알림 감지 로직과는 별개)
    with raw_buffer_lock:
        RAW_TRADES_BUFFER.append({
            'timestamp': time.time(),
            'coin': coin, 'amount': amount, 'ask_bid': ask_bid
        })


# [집계 알고리즘]: 매수/매도 각각 30위까지 백그라운드 초고속 연산
def calculate_snapshot_loop():
    global RAW_TRADES_BUFFER, KOREAN_NAMES_MAP
    while True:
        try:
            time.sleep(2)
            with raw_buffer_lock:
                if not RAW_TRADES_BUFFER:
                    continue
                now_ts = time.time()
                cutoff = now_ts - 3600
                RAW_TRADES_BUFFER[:] = [t for t in RAW_TRADES_BUFFER if t['timestamp'] >= cutoff]
                # 임계값을 10만원으로 낮추면서 초당 체결이 몰리는 코인이 있을 경우
                # 메모리가 과도하게 커지지 않도록 최근 5만건으로 상한을 둠
                if len(RAW_TRADES_BUFFER) > 50000:
                    RAW_TRADES_BUFFER[:] = RAW_TRADES_BUFFER[-50000:]
                buffer_snapshot = list(RAW_TRADES_BUFFER)

            df_mem = pd.DataFrame(buffer_snapshot)
            t_5m, t_10m, t_30m, t_1h = now_ts - 300, now_ts - 600, now_ts - 1800, now_ts - 3600

            def make_summary_json(target_type):
                df_side = df_mem[df_mem['ask_bid'] == target_type]
                if df_side.empty: return []

                side_summary = []
                for coin in df_side['coin'].unique():
                    df_c = df_side[df_side['coin'] == coin]
                    v_5m = df_c[df_c['timestamp'] >= t_5m]['amount'].sum()
                    v_10m = df_c[df_c['timestamp'] >= t_10m]['amount'].sum()
                    v_30m = df_c[df_c['timestamp'] >= t_30m]['amount'].sum()
                    v_1h = df_c[df_c['timestamp'] >= t_1h]['amount'].sum()
                    df_c_1h = df_c[df_c['timestamp'] >= t_1h]
                    total_cnt = len(df_c_1h)
                    heavy_cnt = len(df_c_1h[df_c_1h['amount'] >= 1000000])

                    if max(v_5m, v_10m) > 0:
                        coin_upper = coin.upper()
                        side_summary.append({
                            '코인': KOREAN_NAMES_MAP.get(f"KRW-{coin_upper}", coin_upper),
                            '체결건수(1h)': f"{total_cnt:,}건",
                            '100만↑(1h)': f"{heavy_cnt:,}건",
                            '5m': v_5m, '10m': v_10m, '30m': v_30m, '1h': v_1h,
                            'sort_key': max(v_5m, v_10m)
                        })

                if not side_summary: return []

                def to_won_fmt(val):
                    if val >= 100000000:
                        return f"{val / 100000000:,.1f}억"
                    elif val >= 10000:
                        return f"{val / 10000:,.0f}만"
                    return "0"

                df_summary = pd.DataFrame(side_summary).sort_values(by='sort_key', ascending=False).head(
                    30).reset_index(drop=True)
                df_summary.index = df_summary.index + 1
                df_summary['5분 누적'] = df_summary['5m'].apply(to_won_fmt)
                df_summary['10분 누적'] = df_summary['10m'].apply(to_won_fmt)
                df_summary['30분 누적'] = df_summary['30m'].apply(to_won_fmt)
                df_summary['1시간 누적'] = df_summary['1h'].apply(to_won_fmt)

                final_cols = ['코인', '체결건수(1h)', '100만↑(1h)', '5분 누적', '10분 누적', '30분 누적', '1시간 누적']
                return df_summary[final_cols].to_dict(orient='records')

            bid_json = make_summary_json('BID')
            with open("calculated_snapshot.json.tmp", "w", encoding="utf-8") as f:
                json.dump(bid_json, f, ensure_ascii=False, indent=4)
            os.replace("calculated_snapshot.json.tmp", "calculated_snapshot.json")

            ask_json = make_summary_json('ASK')
            with open("calculated_snapshot_ask.json.tmp", "w", encoding="utf-8") as f:
                json.dump(ask_json, f, ensure_ascii=False, indent=4)
            os.replace("calculated_snapshot_ask.json.tmp", "calculated_snapshot_ask.json")

        except Exception as e:
            print(f"⚠️ calculate_snapshot_loop 에러: {e}")


threading.Thread(target=calculate_snapshot_loop, daemon=True).start()


def get_all_indicators(symbol):
    try:
        import pyupbit
        df = pyupbit.get_ohlcv(symbol, interval="minute1", count=70)
        if df is None or len(df) < 65: return None
        curr = df['close'].iloc[-1]
        delta = df['close'].diff()
        ups, downs = delta.copy(), delta.copy()
        ups[ups < 0] = 0
        downs[downs > 0] = 0
        avg_ups = ups.rolling(window=14).mean()
        avg_downs = downs.abs().rolling(window=14).mean()
        rsi = 100 - (100 / (1 + (avg_ups / avg_downs)))
        calc_diff = lambda idx: round(((curr - df['close'].iloc[idx]) / df['close'].iloc[idx]) * 100, 2)
        return {'rsi': round(rsi.iloc[-1], 2), 'chg_1m': calc_diff(-2), 'chg_3m': calc_diff(-4),
                'chg_1h': calc_diff(-61)}
    except Exception:
        return None


def get_window_stats(symbol, side, trade_amount):
    """최근 TRADE_COUNT_WINDOW_SEC(초) 동안의 누적 금액과 건수를 함께 반환"""
    key = (symbol, side)
    now = datetime.now()
    if key not in accumulated_data:
        accumulated_data[key] = []
    accumulated_data[key].append({'time': now, 'amount': trade_amount})
    accumulated_data[key] = [
        i for i in accumulated_data[key]
        if (now - i['time']).total_seconds() < TRADE_COUNT_WINDOW_SEC
    ]
    window = accumulated_data[key]
    total_amount = sum(i['amount'] for i in window)
    count = len(window)
    return total_amount, count


async def fetch_all_current_prices(tickers):
    import aiohttp
    chunk_size = 90
    ticker_chunks = [tickers[i:i + chunk_size] for i in range(0, len(tickers), chunk_size)]
    combined_data = {}
    async with aiohttp.ClientSession() as session:
        for chunk in ticker_chunks:
            url = f"https://api.upbit.com/v1/ticker?markets={','.join(chunk)}"
            try:
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        for t in data: combined_data[t['market']] = t['trade_price']
            except Exception:
                pass
            await asyncio.sleep(0.05)
    return combined_data


def _record_snapshot(symbol, price, ts):
    dq = price_snapshots.setdefault(symbol, deque())
    dq.append((ts, price))
    cutoff = ts - PRICE_HISTORY_MAX_SEC
    while dq and dq[0][0] < cutoff:
        dq.popleft()


def _price_at_or_before(symbol, target_ts):
    dq = price_snapshots.get(symbol)
    if not dq:
        return None
    best = None
    for ts, p in dq:
        if ts <= target_ts:
            best = p
        else:
            break
    if best is None:
        best = dq[0][1]  # 데이터가 아직 그 시점까지 안 쌓였으면 가장 오래된 값으로 대체
    return best


def seed_price_history(tickers):
    """부팅 시 1회, 과거 캔들로 5m/30m/1h/4h 지점 값을 미리 채워 넣어
    처음부터 등락률이 어느정도 정확하게 나오도록 함"""
    print("⏳ 과거 스냅샷 데이터 빌드 중... (업비트 API 요청 제한 준수 모드)")
    total = len(tickers)
    now = time.time()

    with price_snapshots_lock:
        for idx, t in enumerate(tickers, start=1):
            try:
                time.sleep(0.12)  # 초당 ~8회로 안전하게 제한
                if idx % 20 == 0 or idx == total:
                    print(f" └─ 진행률: [{idx}/{total}] 코인 과거 데이터 빌드 중...")

                res = requests.get(
                    f"https://api.upbit.com/v1/candles/minutes/15?market={t}&count=35", timeout=5
                ).json()
                if res and isinstance(res, list):
                    def close_at(i):
                        return res[i]['trade_price'] if len(res) > i else res[-1]['trade_price']

                    dq = deque()
                    # res[0] = 가장 최근 15분봉, res[i] = i*15분 전
                    dq.append((now - 4 * 3600, close_at(16)))  # 4시간 전
                    dq.append((now - 3600, close_at(4)))       # 1시간 전
                    dq.append((now - 1800, close_at(2)))       # 30분 전
                    dq.append((now - 300, close_at(0)))        # 5분 전(근사치)
                    price_snapshots[t] = dq
            except Exception:
                price_snapshots.setdefault(t, deque())
    print("✅ 과거 스냅샷 데이터 빌드 성공!")


async def send_periodic_report(korean_names):
    try:
        tickers = [t for t in korean_names.keys() if t not in exclude_tickers]
        current_prices = await fetch_all_current_prices(tickers)
        if not current_prices: return

        now_ts = time.time()
        with price_snapshots_lock:
            for sym, curr_p in current_prices.items():
                _record_snapshot(sym, curr_p, now_ts)

            results = []
            for sym, curr_p in current_prices.items():
                rates = {}
                for label, window_sec in PRICE_WINDOWS_SEC.items():
                    past_price = _price_at_or_before(sym, now_ts - window_sec)
                    rates[label] = round((curr_p - past_price) / past_price * 100, 2) if past_price else 0.0

                results.append({
                    'symbol': sym, 'name': korean_names.get(sym, sym), 'price': curr_p,
                    'r5m': rates['r5m'], 'r30m': rates['r30m'], 'r1h': rates['r1h'], 'r4h': rates['r4h'],
                })

        report_file = "dashboard_data.json"
        temp_report = report_file + ".tmp"
        full_df = pd.DataFrame(results)
        with open(temp_report, "w", encoding="utf-8") as f:
            f.write(full_df.to_json(orient='records', force_ascii=False))

        for _ in range(5):
            try:
                os.replace(temp_report, report_file)
                break
            except PermissionError:
                await asyncio.sleep(0.05)
    except Exception as e:
        print(f"⚠️ send_periodic_report 에러: {e}")


async def briefing_scheduler(korean_names):
    # 대시보드가 켜지자마자 데이터 공백으로 뻗지 않도록 시작하자마자 1회 즉시 실행
    await send_periodic_report(korean_names)
    while True:
        try:
            await asyncio.sleep(60)
            await send_periodic_report(korean_names)
        except Exception as e:
            print(f"⚠️ briefing_scheduler 에러: {e}")


def start_bot_loop():
    while True:
        try:
            asyncio.run(run_monitoring())
        except Exception as e:
            print(f"🔄 세션 단절 캐치: 1초 후 자동 재시작... ({e})")
            time.sleep(1)


def _escape_html(text):
    return html.escape(str(text))


async def safe_send(c_id, text):
    try:
        await bot.send_message(
            chat_id=c_id, text=text, parse_mode='HTML',
            connect_timeout=15, read_timeout=15
        )
    except Exception as e:
        print(f"⚠️ 텔레그램 전송 실패: {e}")


async def handle_trade(symbol, coin_name, price, amount, side):
    """side: 'BID' 또는 'ASK'. 매수/매도를 완전히 대칭으로 처리."""
    total_window, count = get_window_stats(symbol, side, amount)

    trigger_reason = None
    if amount >= 10000000:
        trigger_reason = "금액(단일)"
    elif total_window >= 30000000:
        trigger_reason = "금액(누적)"
    elif count >= TRADE_COUNT_THRESHOLD and total_window >= TRADE_COUNT_MIN_AMOUNT:
        trigger_reason = "건수"

    if trigger_reason is None:
        return

    key = (symbol, side)
    if key in last_alert_time and (datetime.now() - last_alert_time[key]).total_seconds() < 5:
        return
    last_alert_time[key] = datetime.now()

    stats = await asyncio.to_thread(get_all_indicators, symbol)
    if not stats:
        return

    is_bid = side == 'BID'
    side_label = "매수" if is_bid else "매도"
    alert_icon = "🐳단일" if amount >= 10000000 else ("🌊연속" if trigger_reason == "금액(누적)" else "⚡건수급증")
    side_icon = "🔴" if is_bid else "🔵"

    save_whale_log_to_json(symbol, coin_name, price, amount, total_window, side, alert_icon, trigger_reason)

    p_str = format_price(price)
    safe_name = _escape_html(coin_name)
    msg = (
        f"{side_icon} {alert_icon} <b>[{side_label}]</b>\n\n"
        f"▶ 종목: <b>{safe_name}</b>\n"
        f"▶ 현재가: {p_str}원\n"
        f"▶ 감지사유: {trigger_reason}\n"
        f"━━━━━━━━━━━━\n"
        f"💰 {side_label}: {format_won(amount)} / {TRADE_COUNT_WINDOW_SEC}초 누적: {format_won(total_window)} ({count}건)\n"
        f"📈 RSI: {stats['rsi']}\n"
        f"⏰ {datetime.now().strftime('%H:%M:%S')}"
    )
    asyncio.create_task(safe_send(chat_id, msg))


async def run_monitoring():
    global KOREAN_NAMES_MAP, _history_seeded
    try:
        res = requests.get("https://api.upbit.com/v1/market/all", timeout=5).json()
        KOREAN_NAMES_MAP = {t['market']: t['korean_name'] for t in res if t['market'].startswith("KRW-")}
    except Exception:
        if not KOREAN_NAMES_MAP:
            KOREAN_NAMES_MAP = {}

    tickers = [t for t in KOREAN_NAMES_MAP.keys() if t not in exclude_tickers]

    # 재연결 시마다 무거운 과거 데이터 재빌드를 반복하지 않도록 최초 1회만 실행
    if not _history_seeded:
        seed_price_history(tickers)
        _history_seeded = True

    asyncio.create_task(briefing_scheduler(KOREAN_NAMES_MAP))

    uri = "wss://api.upbit.com/websocket/v1"
    print("🚀 [매수/매도 30종목 인프라] 가동 시작...")

    async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as websocket:
        subscribe_fmt = [{"ticket": "UNIQUE_TICKET"}, {"type": "trade", "codes": tickers, "isOnlyRealtime": True}]
        await websocket.send(json.dumps(subscribe_fmt))

        while True:
            recv_data = await websocket.recv()
            data = json.loads(recv_data)

            if not data or 'code' not in data: continue
            symbol = data['code']
            if symbol in exclude_tickers: continue

            price = data['trade_price']
            amount = price * data['trade_volume']
            ask_bid = str(data.get('ask_bid', '')).upper()
            if ask_bid not in ('BID', 'ASK'):
                continue

            coin_ticker = symbol.replace('KRW-', '')
            collect_raw_trade_to_buffer(coin_ticker, amount, ask_bid)

            if amount < 300000:
                continue  # 너무 작은 체결은 알림 감지 대상에서 제외 (건수 카운트에도 노이즈 방지)

            coin_name = KOREAN_NAMES_MAP.get(symbol, symbol)
            asyncio.create_task(handle_trade(symbol, coin_name, price, amount, ask_bid))


if __name__ == "__main__":
    start_bot_loop()

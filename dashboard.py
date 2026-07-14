import warnings
import logging
import os
import json
import time

# 스레드 경고 로그 완전 제거 최상단 배치
warnings.filterwarnings("ignore", message="missing ScriptRunContext")
logging.getLogger("streamlit.runtime.scriptrunner").setLevel(logging.ERROR)

import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
from streamlit_autorefresh import st_autorefresh

st.set_page_config(page_title="🐋 WHALE MONITORING SYSTEM", layout="wide")
st_autorefresh(interval=1000, key="datarefresh")

title_col, toggle_col = st.columns([5, 1])
with title_col:
    st.title("🖥️ 가상자산 통합 제어판 & 고래 트래커")
    st.caption(f"⚡ [매수/매도 듀얼 관제 인프라] 실시간 30위 대용량 가동 중 | {pd.Timestamp.now().strftime('%H:%M:%S')}")
with toggle_col:
    st.markdown("<div style='height:1.8rem;'></div>", unsafe_allow_html=True)
    st.toggle("🔊 알림음", value=True, key="sound_on")


def load_data(filename):
    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content: return pd.DataFrame()
                return pd.DataFrame(json.loads(content))
        except Exception:
            return pd.DataFrame()
    return pd.DataFrame()


df_prices = load_data("dashboard_data.json")
df_whales = load_data("whale_alerts.json")

coin_price_map = {row.symbol.replace("KRW-", ""): row.price for row in df_prices.itertuples()} if not df_prices.empty else {}

# ==========================================
# 🚨 신규 고래 진입 시 번쩍임(펄스) 효과
# ==========================================
# whale_alerts.json 맨 앞 항목이 이전 새로고침 때와 달라지면 "새 고래"로 보고
# 몇 초간 배너 + 테두리가 반짝이다가 자동으로 꺼지도록 처리합니다.
if 'last_whale_key' not in st.session_state:
    st.session_state.last_whale_key = None
if 'flash_until' not in st.session_state:
    st.session_state.flash_until = 0.0
if 'flash_side' not in st.session_state:
    st.session_state.flash_side = 'BID'

FLASH_DURATION_SEC = 3.0  # 번쩍이는 지속 시간
play_sound_now = False

if not df_whales.empty:
    top_row = df_whales.iloc[0]
    current_key = f"{top_row.get('time', '')}_{top_row.get('symbol', '')}_{top_row.get('amount', '')}"
    if current_key != st.session_state.last_whale_key:
        st.session_state.last_whale_key = current_key
        st.session_state.flash_until = time.time() + FLASH_DURATION_SEC
        st.session_state.flash_side = top_row.get('side', 'BID')
        play_sound_now = True  # 새 고래가 감지된 이 순간에만 1회 재생

flash_active = time.time() < st.session_state.flash_until
flash_color = "#ff4d4d" if st.session_state.flash_side == 'BID' else "#4da6ff"
flash_side_label = "매수" if st.session_state.flash_side == 'BID' else "매도"

# 🔊 알림음: 매수는 높은 음(880Hz), 매도는 낮은 음(560Hz)로 구분해서 삐 소리 재생
# (브라우저 자동재생 정책상, 페이지 로드 후 사용자가 한 번이라도 클릭/조작해야
#  소리가 정상적으로 재생됩니다 — 이는 크롬 등 브라우저 자체 정책이라 우회 불가)
if play_sound_now and st.session_state.get("sound_on", True):
    beep_freq = 880 if st.session_state.flash_side == 'BID' else 560
    components.html(f"""
    <script>
    try {{
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = 'sine';
        osc.frequency.value = {beep_freq};
        gain.gain.setValueAtTime(0.0001, ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.35, ctx.currentTime + 0.01);
        gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.4);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start();
        osc.stop(ctx.currentTime + 0.45);
    }} catch (e) {{}}
    </script>
    """, height=0)

st.markdown(f"""
<style>
@keyframes whale-pulse {{
    0%   {{ box-shadow: 0 0 4px 0 {flash_color}66; background-color: {flash_color}00; }}
    50%  {{ box-shadow: 0 0 26px 8px {flash_color}; background-color: {flash_color}33; }}
    100% {{ box-shadow: 0 0 4px 0 {flash_color}66; background-color: {flash_color}00; }}
}}
.whale-flash-banner {{
    animation: whale-pulse 0.6s ease-in-out infinite;
    border: 2px solid {flash_color};
    border-radius: 10px;
    padding: 8px 14px;
    font-weight: 700;
    text-align: center;
    margin-bottom: 8px;
}}
.whale-flash-metric {{
    animation: whale-pulse 0.6s ease-in-out infinite;
    border: 2px solid {flash_color};
    border-radius: 10px;
    padding: 10px;
    text-align: center;
}}
</style>
""", unsafe_allow_html=True)

if df_prices.empty:
    st.warning("⏳ 관제 엔진(bot.py)이 과거 정산 데이터를 기록 중입니다. 잠시만 대기해 주세요 (약 10~15초 소요)")
    st.info("💡 Tip: bot.py 콘솔창의 데이터 빌드가 성공 사인(✅)으로 완료되면 실시간 대시보드가 자동으로 로드됩니다.")
else:
    # 표시할 등락률 컬럼 (5분 / 30분 / 1시간 / 4시간)
    rate_cols = [c for c in ['r5m', 'r30m', 'r1h', 'r4h'] if c in df_prices.columns]
    sort_col = 'r30m' if 'r30m' in df_prices.columns else rate_cols[0]

    df_top = df_prices.sort_values(by=sort_col, ascending=False)

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric(label="🚀 30m 최고 상승", value=df_top.iloc[0]['name'], delta=f"{df_top.iloc[0][sort_col]:+.2f}%")
    with col2:
        st.metric(label="📉 30m 최대 하락", value=df_top.iloc[-1]['name'], delta=f"{df_top.iloc[-1][sort_col]:+.2f}%")
    with col3:
        st.metric(label="🔍 모니터링 종목", value=f"{len(df_prices)} 개")
    with col4:
        last_whale = df_whales.iloc[0]['name'] if not df_whales.empty else "없음"
        if flash_active:
            st.markdown(f"""
            <div class="whale-flash-metric">
                <div style="font-size:0.8rem;opacity:0.85;">🐳 실시간 고래 포착</div>
                <div style="font-size:1.4rem;font-weight:700;">{last_whale}</div>
                <div style="font-size:0.85rem;">LIVE · {flash_side_label}</div>
            </div>
            """, unsafe_allow_html=True)
        else:
            st.metric(label="🐳 실시간 고래 포착", value=last_whale, delta="LIVE", delta_color="inverse")

    st.markdown("---")

    left_main_col, right_whale_col = st.columns([0.5, 1.5])

    with left_main_col:
        # 시간대 선택 탭 (5분 / 30분 / 1시간 / 4시간)
        tf_labels = {'r5m': '5분', 'r30m': '30분', 'r1h': '1시간', 'r4h': '4시간'}
        tf_choice = st.radio(
            "기준 시간대", [tf_labels[c] for c in rate_cols], horizontal=True, label_visibility="collapsed"
        )
        tf_col = {v: k for k, v in tf_labels.items()}[tf_choice]
        df_tf = df_prices.sort_values(by=tf_col, ascending=False)

        st.markdown(f"### 🔥 **{tf_choice} 급상승 TOP 5**")
        for i, row in enumerate(df_tf.head(5).itertuples(), 1):
            val = getattr(row, tf_col)
            st.markdown(f"**{i}위. {row.name}** | **{row.price:,.1f}원** (🔴 **{val:+.2f}%**)")

        st.markdown(f"\n### ❄️ **{tf_choice} 급하락 TOP 5**")
        for i, row in enumerate(df_tf.tail(5).iloc[::-1].itertuples(), 1):
            val = getattr(row, tf_col)
            st.markdown(f"**{i}위. {row.name}** | **{row.price:,.1f}원** (🔵 **{val:+.2f}%**)")

        st.markdown("---")
        st.subheader("🔍 종목 검색기")
        q = st.text_input("코인 검색", placeholder="코인명 또는 심볼 입력...", label_visibility="collapsed")
        if q:
            df_f = df_prices[
                df_prices['name'].str.contains(q, case=False, regex=False, na=False)
                | df_prices['symbol'].str.contains(q, case=False, regex=False, na=False)
            ]
        else:
            df_f = df_prices

        show_cols = ['name', 'symbol', 'price'] + rate_cols
        col_name_map = {
            'name': '종목', 'symbol': '심볼', 'price': '현재가',
            'r5m': '5m 대비', 'r30m': '30m 대비', 'r1h': '1h 대비', 'r4h': '4h 대비',
        }
        fmt_map = {'현재가': '{:,.1f}원'}
        for c in rate_cols:
            fmt_map[col_name_map[c]] = '{:+.2f}%'

        st.dataframe(
            df_f[show_cols].rename(columns=col_name_map).style.format(fmt_map),
            width='stretch', hide_index=True, height=250
        )

    with right_whale_col:
        sub_col1, sub_col2, sub_col3 = st.columns([0.8, 1.1, 1.1])

        # 1. 실시간 고래 피드 (매수 / 매도 분리)
        with sub_col1:
            if flash_active:
                st.markdown(
                    f'<div class="whale-flash-banner">🚨 새 고래 {flash_side_label} 포착!</div>',
                    unsafe_allow_html=True
                )
            st.markdown("##### 🚨 고래 진입 피드")
            if df_whales.empty:
                st.info("🌊 신호 대기 중...")
            else:
                black_list = ['BTC', 'XRP', 'USDT', 'SOL', 'ETH']
                df_whales_filtered = df_whales[
                    ~df_whales['symbol'].str.replace('KRW-', '').str.upper().isin(black_list)
                ].copy()

                if df_whales_filtered.empty:
                    st.caption("진입 내역 없음")
                else:
                    def get_live_price(sym):
                        return f"{coin_price_map.get(sym.replace('KRW-', ''), 0):,.1f}원"

                    df_whales_filtered['현재가'] = df_whales_filtered['symbol'].apply(get_live_price)

                    has_side = 'side' in df_whales_filtered.columns
                    bid_tab, ask_tab = st.tabs(["🔴 매수", "🔵 매도"])

                    display_cols = ['time', 'name', 'amount', '현재가']
                    rename_cols = {'time': '시간', 'name': '종목', 'amount': '체결액'}

                    with bid_tab:
                        df_bid = df_whales_filtered[df_whales_filtered['side'] == 'BID'] if has_side else df_whales_filtered
                        if df_bid.empty:
                            st.caption("매수 진입 내역 없음")
                        else:
                            st.dataframe(
                                df_bid[display_cols].rename(columns=rename_cols),
                                width='stretch', hide_index=True, height=600
                            )

                    with ask_tab:
                        df_ask = df_whales_filtered[df_whales_filtered['side'] == 'ASK'] if has_side else pd.DataFrame()
                        if df_ask.empty:
                            st.caption("매도 진입 내역 없음")
                        else:
                            st.dataframe(
                                df_ask[display_cols].rename(columns=rename_cols),
                                width='stretch', hide_index=True, height=600
                            )

        # 2. 실시간 매수 자금 유입 스냅샷 (TOP 30)
        with sub_col2:
            st.markdown("##### 🔴 실시간 매수 유입 (TOP 30)")
            df_snapshot_bid = load_data("calculated_snapshot.json")
            if df_snapshot_bid.empty:
                st.caption("집계 대기 중...")
            else:
                st.dataframe(df_snapshot_bid, width='stretch', height=650)

        # 3. 실시간 매도 자금 유출 스냅샷 (TOP 30)
        with sub_col3:
            st.markdown("##### 🔵 실시간 매도 유출 (TOP 30)")
            df_snapshot_ask = load_data("calculated_snapshot_ask.json")
            if df_snapshot_ask.empty:
                st.caption("집계 대기 중...")
            else:
                st.dataframe(df_snapshot_ask, width='stretch', height=650)

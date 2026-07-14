import warnings
import logging
import os
import json
import time
import subprocess
import sys

# 스레드 경고 로그 완전 제거 최상단 배치
warnings.filterwarnings("ignore", message="missing ScriptRunContext")
logging.getLogger("streamlit.runtime.scriptrunner").setLevel(logging.ERROR)

import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
from streamlit_autorefresh import st_autorefresh

# --- [해결책 A] bot.py 백그라운드 구동 및 초기화 로직 --
current_dir = os.path.dirname(os.path.abspath(__file__))
bot_path = os.path.join(current_dir, "bot.py")

# 세션 상태를 이용해 이 브라우저 세션에서 봇 구동 명령을 딱 1번만 실행하도록 제어
if "bot_started" not in st.session_state:
    try:
        # 혹시 기존에 돌고 있을지 모를 좀비 bot.py 프로세스가 있다면 먼저 강제 종료하여 충돌 방지
        if sys.platform != "win32":
            subprocess.run(["pkill", "-f", "bot.py"], capture_output=True)
        
        # 확실하게 dashboard.py와 같은 경로(cwd)에서 bot.py를 백그라운드로 실행시킵니다.
        subprocess.Popen([sys.executable, bot_path], cwd=current_dir)
        st.session_state["bot_started"] = True
    except Exception as e:
        st.error(f"관제 엔진(bot.py) 기동 실패: {e}")

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
    # Streamlit Cloud 가상 환경에 맞춰 파일 절대 경로를 명확하게 잡아서 읽어옵니다.
    full_path = os.path.join(current_dir, filename)
    if os.path.exists(full_path):
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content: 
                    return pd.DataFrame()
                return pd.DataFrame(json.loads(content))
        except Exception:
            return pd.DataFrame()
    return pd.DataFrame()


# 데이터 로드
df_whales = load_data("whale_alerts.json")

# 만약 아직 bot.py가 연산을 끝내지 못해 파일이 생성되지 않았다면 로딩 대기 안내 출력
if df_whales.empty:
    st.warning("⏳ 관제 엔진(bot.py)이 가동 준비 중이거나 과거 정산 데이터를 기록 중입니다. 잠시만 대기해 주세요 (약 10~15초 소요)")
    st.info("💡 Tip: 만약 1분 이상 대기 화면이 계속된다면, 업비트 API 키의 IP 제한(내 PC 전용 IP) 설정 때문일 수 있습니다. 업비트 개발자 센터에서 IP 제한을 해제해 주세요.")
    
    # 서버 디버깅용: 현재 폴더 안에서 데이터 파일들이 실시간으로 만들어지고 있는지 보여줍니다.
    st.write("📁 현재 서버 폴더 파일 목록:", os.listdir(current_dir))
else:
    # 텔레그램 알림음 재생 컴포넌트 삽입 (이전 수집 건 대비 새 알림이 있을 때만 사운드 재생)
    if "last_whale_count" not in st.session_state:
        st.session_state["last_whale_count"] = len(df_whales)

    current_count = len(df_whales)
    if current_count > st.session_state["last_whale_count"]:
        if st.session_state.get("sound_on", True):
            # 오디오 엘리먼트를 숨겨서 자동 재생 실행
            st.components.v1.html(
                """
                <audio autoplay style="display:none;">
                    <source src="https://assets.mixkit.co/active_storage/sfx/2869/2869-120.wav" type="audio/wav">
                </audio>
                """,
                height=0
            )
        st.session_state["last_whale_count"] = current_count

    # UI 레이아웃 분할
    sub_col1, sub_col2, sub_col3 = st.columns([1.5, 1, 1])

    # 1. 고래 실시간 진입 목록 (좌측 넓은 화면)
    with sub_col1:
        st.markdown("##### 🐳 고래 실시간 대량 체결 목록 (1천만원 이상)")
        
        # 필터링 사이드바 대용 상단 제어바
        search_col, side_col = st.columns([1, 1])
        with search_col:
            search_ticker = st.text_input("🔍 코인 심볼 검색 (예: BTC, SOL)", "").strip().upper()
        with side_col:
            filter_side = st.selectbox("↕️ 체결 방향 필터", ["전체", "매수 (BID)", "매도 (ASK)"])

        # 데이터 필터링 적용
        df_whales_filtered = df_whales.copy()
        if search_ticker:
            # KRW-BTC 나 BTC 검색 둘 다 지원하도록 처리
            df_whales_filtered = df_whales_filtered[
                df_whales_filtered['ticker'].str.contains(search_ticker)
            ]
        
        has_side = 'side' in df_whales_filtered.columns
        if has_side and filter_side != "전체":
            target_side = "BID" if "매수" in filter_side else "ASK"
            df_whales_filtered = df_whales_filtered[df_whales_filtered['side'] == target_side]

        if df_whales_filtered.empty:
            st.info("검색 또는 필터 조건에 부합하는 체결 내역이 없습니다.")
        else:
            # 뷰포트용 데이터 정리
            display_cols = ['time', 'ticker', 'price', 'amount', 'ratio']
            if has_side:
                display_cols.insert(2, 'side')
            
            rename_cols = {
                'time': '시각',
                'ticker': '종목',
                'side': '구분',
                'price': '체결가',
                'amount': '체결금액',
                'ratio': '체결비중(%)'
            }

            # 탭을 통해 매수/매도 전체를 직관적으로 관찰
            all_tab, bid_tab, ask_tab = st.tabs(["📊 전체 현황", "🔴 매수 집중", "🔵 매도 집중"])
            
            with all_tab:
                st.dataframe(
                    df_whales_filtered[display_cols].rename(columns=rename_cols),
                    width='stretch', hide_index=True, height=600
                )
            
            with bid_tab:
                df_bid = df_whales_filtered[df_whales_filtered['side'] == 'BID'] if has_side else pd.DataFrame()
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

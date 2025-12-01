import flet as ft
import requests
import pyotp
import threading
import time
import uuid
import datetime
import traceback
import json
import os
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

CONFIG_FILE = "config.json"
SCRIPMASTER_FILE = "scripmaster.json"

class AppState:
    def __init__(self):
        self.api_key, self.client_id, self.jwt_token, self.feed_token, self.refresh_token, self.smart_api, self.sws = "", "", None, None, None, None, None
        self.current_view, self.scrips, self.live_feed_status = "watchlist", [], "DISCONNECTED"
        self.watchlist = [{"symbol": "RELIANCE-EQ", "token": "2885", "ltp": 0.0, "wc": 0.0, "loading": True}, {"symbol": "SBIN-EQ", "token": "3045", "ltp": 0.0, "wc": 0.0, "loading": True}]
        self.alerts, self.logs, self.is_paused, self.connected = [], [], False, False

state = AppState()

# --- CONFIG & API HELPERS ---
def load_config():
    if not os.path.exists(CONFIG_FILE): return {"api_key": "", "client_id": ""}
    with open(CONFIG_FILE, 'r') as f: return json.load(f)

def save_config(api_key, client_id):
    with open(CONFIG_FILE, 'w') as f: json.dump({"api_key": api_key, "client_id": client_id}, f)

def load_scrips():
    # Load scrips in a separate thread to not block UI
    def _load():
        if not os.path.exists(SCRIPMASTER_FILE) or (time.time() - os.path.getmtime(SCRIPMASTER_FILE) > 86400):
            try:
                print("Downloading Scrip Master (50MB+)... this may take time.")
                r = requests.get("https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json")
                data = r.json()
                # Filter for NSE Equity only to keep it fast
                state.scrips = [s for s in data if s.get('exch_seg') == 'NSE']
                with open(SCRIPMASTER_FILE, 'w') as f: json.dump(state.scrips, f)
                print(f"Scrip Master Downloaded. Loaded {len(state.scrips)} symbols.")
            except Exception as e: print(f"Scrip Master Download Failed: {e}")
        else:
            print("Loading Scrips from Cache...")
            with open(SCRIPMASTER_FILE, 'r') as f: state.scrips = json.load(f)
            print(f"Loaded {len(state.scrips)} NSE scrips.")
    
    threading.Thread(target=_load, daemon=True).start()

def angel_login(api_key, client_id, password, totp_secret):
    try:
        smartApi = SmartConnect(api_key=api_key, timeout=20)
        totp_val = pyotp.TOTP(totp_secret).now()
        data = smartApi.generateSession(client_id, password, totp_val)
        if data.get('status'):
            state.smart_api, state.jwt_token, state.feed_token, state.refresh_token = smartApi, data['data']['jwtToken'], data['data']['feedToken'], data['data']['refreshToken']
            return True, "Login Successful"
        else: return False, data.get('message', 'Login Error')
    except Exception as e: return False, str(e)

# --- AUTOMATIC SESSION REFRESHER ---
def session_refresher():
    while state.connected:
        time.sleep(1800) 
        try:
            if state.smart_api and state.refresh_token:
                token_data = state.smart_api.generateToken(state.refresh_token)
                if token_data and token_data.get('status'):
                    state.jwt_token = token_data['data']['jwtToken']
                    print("Session token refreshed successfully.")
        except Exception as e:
            print(f"Error refreshing token: {e}")

def fetch_stock_data(stock_item, page):
    if not state.smart_api: return
    try:
        # Fetch LTP
        ltp_data = state.smart_api.ltpData("NSE", stock_item['symbol'], stock_item['token'])
        if ltp_data and ltp_data.get('status') and 'ltp' in ltp_data['data']: 
            stock_item['ltp'] = ltp_data['data']['ltp']
        
        # Fetch Weekly Close - Use 365 days to ensure we have enough history
        now = datetime.datetime.now()
        to_date_str = now.strftime('%Y-%m-%d %H:%M')
        from_date_str = (now - datetime.timedelta(days=365)).strftime('%Y-%m-%d 09:15')
        
        historic_req = {"exchange": "NSE", "symboltoken": stock_item['token'], "interval": "ONE_WEEK", "fromdate": from_date_str, "todate": to_date_str}
        
        try:
            hist_data = state.smart_api.getCandleData(historic_req)
            if hist_data and hist_data.get('status') and hist_data.get('data'):
                candles = hist_data['data']
                if len(candles) > 0:
                    last_candle = candles[-1]
                    last_date_str = last_candle[0].split('T')[0]
                    last_date = datetime.datetime.strptime(last_date_str, "%Y-%m-%d").date()
                    
                    today = datetime.datetime.now().date()
                    # Start of current week (Monday)
                    start_of_current_week = today - datetime.timedelta(days=today.weekday())
                    
                    # If last candle is from current week (on or after Monday), use previous week
                    if last_date >= start_of_current_week:
                        if len(candles) > 1:
                            stock_item['wc'] = candles[-2][4]
                            print(f"WC for {stock_item['symbol']}: {stock_item['wc']} (using prev week, current incomplete)")
                        else:
                            raise Exception("Need daily fallback")
                    else:
                        stock_item['wc'] = last_candle[4]
                        print(f"WC for {stock_item['symbol']}: {stock_item['wc']} (date: {last_date})")
                else:
                    raise Exception("Empty weekly data")
            else:
                raise Exception("No weekly data")
        except Exception as e:
            print(f"Weekly fetch failed for {stock_item['symbol']}: {e}. Trying daily...")
            # Fallback to ONE_DAY
            historic_req['interval'] = "ONE_DAY"
            hist_data = state.smart_api.getCandleData(historic_req)
            if hist_data and hist_data.get('status') and hist_data.get('data'):
                daily_candles = hist_data['data']
                today = datetime.datetime.now().date()
                start_of_current_week = today - datetime.timedelta(days=today.weekday())
                
                # Find last trading day before current week
                for i in range(len(daily_candles) - 1, -1, -1):
                    c_date_str = daily_candles[i][0].split('T')[0]
                    c_date = datetime.datetime.strptime(c_date_str, "%Y-%m-%d").date()
                    if c_date < start_of_current_week:
                        stock_item['wc'] = daily_candles[i][4]
                        print(f"WC for {stock_item['symbol']}: {stock_item['wc']} from daily (date: {c_date})")
                        break
    except Exception as e:
        print(f"Data Fetch Error for {stock_item['symbol']}: {e}")
    finally:
        stock_item['loading'] = False
        if page and page.route == "/app":
             try: page.update()
             except: pass

def refresh_all_stocks_data(page):
    for stock in state.watchlist: stock['loading'] = True
    if page: page.update()
    for stock in state.watchlist:
        threading.Thread(target=fetch_stock_data, args=(stock, page), daemon=True).start()

def start_websocket(page):
    if not all([state.jwt_token, state.api_key, state.client_id, state.feed_token]): return
    state.sws = SmartWebSocketV2(state.jwt_token, state.api_key, state.client_id, state.feed_token)
    def on_data(wsapp, message):
        if isinstance(message, list):
            for tick in message:
                try:
                    token, ltp = tick.get('tk'), tick.get('lp')
                    if token and ltp:
                        price = float(ltp) / 100.0
                        for stock in state.watchlist:
                            if str(stock['token']) == str(token):
                                stock['ltp'] = price
                                check_alerts(stock, page)
                                break
                except (ValueError, KeyError): continue
        if page and page.route == "/app":
             try: page.update()
             except: pass

    def on_open(wsapp):
        state.live_feed_status = "CONNECTED"
        token_list = [item['token'] for item in state.watchlist]
        state.sws.subscribe("watchlist_sub", 1, [{"exchangeType": 1, "tokens": token_list}])
        if page: page.update()
    
    def on_close(wsapp, code, reason):
        state.live_feed_status = "DISCONNECTED"
        if page: page.update()

    state.sws.on_data, state.sws.on_open, state.sws.on_close = on_data, on_open, on_close
    threading.Thread(target=state.sws.connect, daemon=True).start()

def check_alerts(stock, page):
    if state.is_paused: return
    alert_triggered = False
    for alert in list(state.alerts):
        if str(alert["token"]) == str(stock["token"]):
            triggered = False
            if alert["condition"] == "ABOVE" and stock["ltp"] >= alert["price"]: triggered = True
            elif alert["condition"] == "BELOW" and stock["ltp"] <= alert["price"]: triggered = True
            if triggered:
                msg = f"{stock['symbol']} hit {alert['price']} ({alert['condition']})"
                state.logs.insert(0, {"time": datetime.datetime.now().strftime("%H:%M:%S"), "symbol": stock['symbol'], "msg": msg})
                if alert in state.alerts: state.alerts.remove(alert)
                alert_triggered = True
    if alert_triggered and page and state.current_view in ['alerts', 'logs']:
        try: page.update()
        except: pass

def generate_369_levels(ltp, weekly_close):
    levels, pattern = [], [30, 60, 90] if weekly_close > 3333 else [3, 6, 9]
    curr_res, curr_sup = weekly_close, weekly_close
    for i in range(15):
        step = pattern[i % 3]
        curr_res += step
        if curr_res > ltp: levels.append({"price": round(curr_res, 2), "type": "ABOVE"})
        curr_sup -= step
        if curr_sup < ltp: levels.append({"price": round(curr_sup, 2), "type": "BELOW"})
    return levels

def main(page: ft.Page):
    page.title, page.theme_mode, page.padding, page.window_width, page.window_height = "Trade Yantra", ft.ThemeMode.DARK, 0, 400, 800
    config = load_config()
    # Load scrips in background
    load_scrips()

    api_input = ft.TextField(label="SmartAPI Key", password=True, value=config.get("api_key"))
    client_input = ft.TextField(label="Client ID", value=config.get("client_id"))
    pass_input, totp_input = ft.TextField(label="Password", password=True), ft.TextField(label="TOTP Secret", password=True)
    login_status, login_progress = ft.Text("", color="red", size=12), ft.ProgressRing(visible=False, width=16, height=16)

    def handle_login(e):
        login_progress.visible = True
        page.update()
        success, msg = angel_login(api_input.value, client_input.value, pass_input.value, totp_input.value)
        if success:
            state.api_key, state.client_id = api_input.value, client_input.value
            save_config(state.api_key, state.client_id)
            state.connected = True
            refresh_all_stocks_data(page)
            start_websocket(page)
            threading.Thread(target=session_refresher, daemon=True).start()
            page.go("/app")
        else: login_status.value = f"Error: {msg}"
        login_progress.visible = False
        page.update()

    login_view = ft.Column([ft.Text("Trade Yantra", size=30, weight="bold", color="blue"), ft.Text("Secure Login", size=16, color="grey"), ft.Divider(), api_input, client_input, pass_input, totp_input, ft.ElevatedButton("Login", on_click=handle_login, icon="login"), ft.Row([login_progress, login_status])], alignment="center", horizontal_alignment="center", spacing=15, expand=True)
    
    body_container = ft.Container(padding=10, expand=True)

    def update_view():
        view_map = {"watchlist": get_watchlist_view, "alerts": get_alerts_view, "logs": get_logs_view}
        body_container.content = view_map[state.current_view]()
        try:
            if page.route == "/app": page.update()
        except Exception: pass

    def get_watchlist_view():
        live_status = ft.Text(f"Live Feed: {state.live_feed_status}", size=10, weight="bold", color="green" if state.live_feed_status == "CONNECTED" else "red")
        watchlist_items = []
        for stock in state.watchlist:
            price_or_loader = ft.ProgressRing(width=16, height=16) if stock.get('loading') else ft.Text(f"₹{stock['ltp']:.2f}", size=16, weight="bold", color="green")
            wc_text = f"WC: {stock['wc']:.2f}" if stock.get('wc', 0) > 0 else "WC: ..."
            watchlist_items.append(ft.Container(content=ft.Row([ft.Column([ft.Text(stock["symbol"], weight="bold"), ft.Text(stock["token"], size=10, color="grey")]), ft.Row([ft.Column([price_or_loader, ft.Text(wc_text, size=10, color="grey")], alignment="end"), ft.IconButton("delete", icon_color="red", on_click=lambda e, t=stock['token']: remove_stock_from_watchlist(t))])], alignment="spaceBetween"), padding=5, border=ft.border.only(bottom=ft.BorderSide(1, "#2A2A2A"))))
        return ft.Column([ft.Row([live_status, ft.ElevatedButton("Add Symbol", icon="search", on_click=open_search_bs), ft.IconButton("refresh", on_click=lambda e: refresh_all_stocks_data(page))]), ft.Divider(), ft.Column(watchlist_items, scroll=ft.ScrollMode.AUTO, expand=True)], expand=True)

    def remove_stock_from_watchlist(token):
        state.watchlist = [s for s in state.watchlist if s['token'] != token]
        update_view()

    def get_alerts_view():
        pause_btn = ft.ElevatedButton("Pause", icon="pause", on_click=toggle_pause, bgcolor="orange", color="black")
        if state.is_paused: pause_btn.text, pause_btn.bgcolor, pause_btn.icon = "Resume", "green", "play_arrow"
        
        alerts_items = []
        if not state.alerts: alerts_items.append(ft.Text("No active alerts.", color="grey"))
        for alert in state.alerts:
            color, icon = ("green", "trending_up") if alert["condition"] == "ABOVE" else ("red", "trending_down")
            alerts_items.append(ft.Container(content=ft.Row([ft.Row([ft.Icon(icon, color=color), ft.Column([ft.Text(alert["symbol"], weight="bold"), ft.Text(f"Target: {alert['price']}")])]), ft.IconButton("delete", icon_color="red", on_click=lambda e, aid=alert["id"]: delete_alert(aid)) ], alignment="spaceBetween"), padding=10, bgcolor="#2A2A2A", border_radius=5))
        return ft.Column([ft.Container(content=ft.Column([ft.Text("Strategy: 3-6-9...", size=10, color="grey"), ft.ElevatedButton("Auto-Generate Levels", icon="autorenew", on_click=generate_alerts), pause_btn]), padding=10, bgcolor="#2A2A2A", border_radius=10), ft.Divider(), ft.Text("Active Monitoring", weight="bold"), ft.Column(alerts_items, scroll=ft.ScrollMode.AUTO, expand=True)], expand=True)

    def get_logs_view():
        log_items = [ft.Container(content=ft.Row([ft.Text(log["time"], size=10, color="grey"), ft.Text(log["symbol"], weight="bold", width=80), ft.Text(log["msg"], expand=True)]), padding=5, border=ft.border.only(bottom=ft.BorderSide(1, "#2A2A2A"))) for log in state.logs]
        return ft.Column([ft.Column(log_items, scroll=ft.ScrollMode.AUTO, expand=True)], expand=True)

    def toggle_pause(e):
        state.is_paused = not state.is_paused
        update_view()
        
    def generate_alerts(e):
        count = 0
        for stock in state.watchlist:
            if stock['ltp'] > 0 and stock['wc'] > 0:
                levels = generate_369_levels(stock["ltp"], stock["wc"])
                for lvl in levels:
                    if not any(a["token"] == stock["token"] and a["price"] == lvl["price"] for a in state.alerts):
                        state.alerts.append({"id": str(uuid.uuid4()),"symbol": stock["symbol"],"token": stock["token"],"price": lvl["price"],"condition": lvl["type"]})
                        count += 1
        state.logs.insert(0, {"time": datetime.datetime.now().strftime("%H:%M:%S"), "symbol": "SYSTEM", "msg": f"Auto-added {count} alerts"})
        update_view()

    def delete_alert(aid):
        state.alerts = [a for a in state.alerts if a["id"] != aid]
        update_view()

    def show_view(view_name):
        state.current_view = view_name
        update_view()

    def open_search_bs(e):
        search_input = ft.TextField(label="Search Symbol (e.g. RELI)", autofocus=True, on_change=lambda e: update_search_results(e.data))
        search_results_col = ft.Column(scroll=ft.ScrollMode.AUTO, expand=True)
        loading_text = ft.Text("Loading Master Data...", visible=False, color="yellow")
        
        def update_search_results(query):
            if not state.scrips:
                loading_text.visible = True
                loading_text.update()
                return

            if len(query) < 2: 
                search_results_col.controls = []
            else:
                q = query.upper()
                # Optimized search: Name starts with query OR Symbol contains query
                matches = [s for s in state.scrips if s['symbol'].startswith(q) or s.get('name','').startswith(q)][:20]
                
                search_results_col.controls = [ft.ListTile(title=ft.Text(s['symbol']), subtitle=ft.Text(s.get('name','')), on_click=lambda e, scrip=s: add_searched_stock(scrip)) for s in matches]
            bs.update()

        def add_searched_stock(scrip):
            if not any(s['token'] == scrip['token'] for s in state.watchlist):
                new_stock = {"symbol": scrip['symbol'], "token": scrip['token'], "ltp": 0.0, "wc": 0.0, "loading": True}
                state.watchlist.append(new_stock)
                threading.Thread(target=fetch_stock_data, args=(new_stock, page), daemon=True).start()
                if state.sws: state.sws.subscribe("add", 1, [{"exchangeType": 1, "tokens": [new_stock['token']]}])
                page.close(bs)
                update_view()
        
        bs = ft.BottomSheet(ft.Container(ft.Column([loading_text, search_input, search_results_col], expand=True), padding=20, height=page.height*0.8), open=True)
        page.overlay.append(bs)
        page.update()

    nav_buttons = ft.Row([ft.ElevatedButton("Watchlist", on_click=lambda e: show_view("watchlist"), expand=True), ft.ElevatedButton("Alerts", on_click=lambda e: show_view("alerts"), expand=True), ft.ElevatedButton("History", on_click=lambda e: show_view("logs"), expand=True)], alignment="center")
    app_bar = ft.AppBar(title=ft.Text("Trade Yantra"), bgcolor="#212121")

    def route_change(e):
        page.views.clear()
        page.views.append(ft.View("/", [ft.Container(content=login_view, expand=True, padding=20)]))
        if page.route == "/app":
            page.views.append(ft.View("/app", [app_bar, body_container, nav_buttons]))
            show_view("watchlist")
        page.update()

    page.on_route_change = route_change
    page.go(page.route)

if __name__ == "__main__":
    ft.app(target=main)

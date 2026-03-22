"""
OKX 模拟盘网格交易面板 - Railway 部署版
"""

import hmac, hashlib, base64, time, json, threading, os
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template_string
from flask_cors import CORS
import requests

# ===== API Key 从环境变量读取（Railway 上设置，安全！）=====
API_KEY    = os.environ.get("OKX_API_KEY", "")
SECRET_KEY = os.environ.get("OKX_SECRET_KEY", "")
PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")

BASE_URL   = "https://www.okx.com"
SYMBOL     = "BTC-USDT"
GRID_LOW   = 82000
GRID_HIGH  = 90000
GRID_COUNT = 10
INIT_FUND  = 200

app = Flask(__name__)
CORS(app)

state = {
    "balance": INIT_FUND,
    "pnl": 0.0,
    "trades": [],
    "price": 0,
    "wins": 0,
    "total_trades": 0,
    "max_balance": INIT_FUND,
    "max_drawdown": 0.0,
    "pnl_history": [INIT_FUND],
    "grids": [],
    "log": [],
    "running": True,
    "api_connected": False
}

def sign(timestamp, method, path, body=""):
    msg = timestamp + method + path + (body or "")
    mac = hmac.new(SECRET_KEY.encode(), msg.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def get_headers(method, path, body=""):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    return {
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": sign(ts, method, path, body),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type": "application/json",
        "x-simulated-trading": "1"
    }

def okx_get(path):
    try:
        r = requests.get(BASE_URL + path, headers=get_headers("GET", path), timeout=8)
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def okx_post(path, body):
    b = json.dumps(body)
    try:
        r = requests.post(BASE_URL + path, headers=get_headers("POST", path, b), data=b, timeout=8)
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def get_price():
    data = okx_get(f"/api/v5/market/ticker?instId={SYMBOL}")
    try:
        price = float(data["data"][0]["last"])
        state["api_connected"] = True
        return price
    except:
        state["api_connected"] = False
        return state["price"] or 86000

def get_balance():
    data = okx_get("/api/v5/account/balance?ccy=USDT")
    try:
        return float(data["data"][0]["details"][0]["availBal"])
    except:
        return state["balance"]

def build_grids():
    step = (GRID_HIGH - GRID_LOW) / GRID_COUNT
    return [{"price": round(GRID_LOW + i * step), "filled": False} for i in range(GRID_COUNT + 1)]

def run_grid(price):
    for g in state["grids"]:
        if not g["filled"] and price <= g["price"]:
            qty = round(INIT_FUND / GRID_COUNT / price, 6)
            result = okx_post("/api/v5/trade/order", {
                "instId": SYMBOL, "tdMode": "cash",
                "side": "buy", "ordType": "market", "sz": str(qty)
            })
            g["filled"] = True
            pnl_est = round(g["price"] * 0.008 * qty, 4)
            add_trade("买入", price, qty, pnl_est, result)

        elif g["filled"] and price >= g["price"] * 1.008:
            qty = round(INIT_FUND / GRID_COUNT / price, 6)
            result = okx_post("/api/v5/trade/order", {
                "instId": SYMBOL, "tdMode": "cash",
                "side": "sell", "ordType": "market", "sz": str(qty)
            })
            g["filled"] = False
            pnl_est = round(g["price"] * 0.008 * qty, 4)
            add_trade("卖出", price, qty, pnl_est, result)

def add_trade(side, price, qty, pnl_val, api_result):
    state["trades"].insert(0, {
        "time": datetime.now().strftime("%H:%M:%S"),
        "side": side,
        "price": round(price),
        "qty": qty,
        "pnl": round(pnl_val, 2)
    })
    if len(state["trades"]) > 20:
        state["trades"].pop()
    state["pnl"] = round(state["pnl"] + pnl_val, 2)
    state["total_trades"] += 1
    if pnl_val > 0:
        state["wins"] += 1
    ok = "成功" if "data" in api_result else "失败"
    state["log"].insert(0, f"[{datetime.now().strftime('%H:%M:%S')}] {side} {qty} BTC @ ${round(price):,} → {ok}")
    if len(state["log"]) > 50:
        state["log"].pop()

def trading_loop():
    state["grids"] = build_grids()
    state["log"].insert(0, f"[{datetime.now().strftime('%H:%M:%S')}] 网格策略启动，正在连接 OKX...")
    while True:
        if state["running"]:
            try:
                price = get_price()
                state["price"] = round(price)
                bal = get_balance()
                state["balance"] = round(bal, 2)
                state["pnl"] = round(bal - INIT_FUND, 2)
                if bal > state["max_balance"]:
                    state["max_balance"] = bal
                dd = (state["max_balance"] - bal) / state["max_balance"] * 100
                state["max_drawdown"] = round(max(dd, 0), 2)
                state["pnl_history"].append(round(bal, 2))
                if len(state["pnl_history"]) > 60:
                    state["pnl_history"].pop(0)
                if API_KEY:
                    run_grid(price)
            except Exception as e:
                state["log"].insert(0, f"[错误] {str(e)}")
        time.sleep(15)

HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>OKX 模拟盘</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0;font-family:-apple-system,sans-serif}
body{background:#f5f5f0;color:#1a1a1a;padding:16px}
h2{font-size:17px;font-weight:500;margin-bottom:14px;display:flex;align-items:center;gap:10px}
.metrics{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-bottom:14px}
@media(min-width:600px){.metrics{grid-template-columns:repeat(4,1fr)}}
.metric{background:#fff;border-radius:10px;padding:12px 14px;border:0.5px solid #e0e0d8}
.metric-label{font-size:11px;color:#888;margin-bottom:4px}
.metric-value{font-size:20px;font-weight:500}
.up{color:#1D9E75}.down{color:#D85A30}
.panel{background:#fff;border-radius:10px;padding:14px;border:0.5px solid #e0e0d8;margin-bottom:14px}
.panel h3{font-size:12px;color:#888;font-weight:400;margin-bottom:10px}
table{width:100%;font-size:12px;border-collapse:collapse}
th{text-align:left;color:#888;font-weight:400;padding:3px 6px 7px;border-bottom:0.5px solid #eee}
td{padding:5px 6px;border-bottom:0.5px solid #eee}
.badge{font-size:11px;padding:3px 10px;border-radius:6px;font-weight:500}
.live{background:#E1F5EE;color:#0F6E56}.offline{background:#f5f5f0;color:#888}
.log{font-size:11px;color:#666;font-family:monospace;max-height:100px;overflow-y:auto;line-height:1.9}
.price{font-size:13px;color:#888;font-weight:400}
</style></head>
<body>
<h2>OKX 模拟盘 · 网格策略
  <span id="badge" class="badge offline">连接中...</span>
  <span class="price">BTC <span id="btcprice">—</span></span>
</h2>
<div class="metrics">
  <div class="metric"><div class="metric-label">账户余额 (USDT)</div><div class="metric-value" id="balance">—</div></div>
  <div class="metric"><div class="metric-label">累计盈亏</div><div class="metric-value" id="pnl">—</div></div>
  <div class="metric"><div class="metric-label">胜率</div><div class="metric-value" id="winrate">—</div></div>
  <div class="metric"><div class="metric-label">最大回撤</div><div class="metric-value down" id="drawdown">—</div></div>
</div>
<div class="panel">
  <h3>净值曲线</h3>
  <div style="position:relative;height:140px"><canvas id="chart"></canvas></div>
</div>
<div class="panel">
  <h3>成交记录</h3>
  <table><thead><tr><th>时间</th><th>方向</th><th>价格</th><th>盈亏</th></tr></thead>
  <tbody id="trades"></tbody></table>
</div>
<div class="panel">
  <h3>运行日志</h3>
  <div class="log" id="log">连接中...</div>
</div>
<script>
let chart;
async function refresh(){
  try{
    const d=await fetch('/api/state').then(r=>r.json());
    document.getElementById('btcprice').textContent='$'+(d.price||0).toLocaleString();
    document.getElementById('balance').textContent='$'+d.balance.toFixed(2);
    const p=d.pnl;
    document.getElementById('pnl').className='metric-value '+(p>=0?'up':'down');
    document.getElementById('pnl').textContent=(p>=0?'+':'')+p.toFixed(2);
    const wr=d.total_trades>0?Math.round(d.wins/d.total_trades*100):0;
    document.getElementById('winrate').textContent=wr+'%';
    document.getElementById('drawdown').textContent=d.max_drawdown.toFixed(1)+'%';
    const badge=document.getElementById('badge');
    badge.textContent=d.api_connected?'实时运行':'模拟模式';
    badge.className='badge '+(d.api_connected?'live':'offline');
    document.getElementById('trades').innerHTML=d.trades.map(t=>
      `<tr><td>${t.time}</td><td style="color:${t.side==='买入'?'#1D9E75':'#D85A30'}">${t.side}</td><td>$${t.price.toLocaleString()}</td><td style="color:${t.pnl>=0?'#1D9E75':'#D85A30'}">${t.pnl>=0?'+':''}${t.pnl}</td></tr>`
    ).join('');
    document.getElementById('log').innerHTML=d.log.join('<br>')||'等待信号...';
    if(!chart){
      chart=new Chart(document.getElementById('chart'),{
        type:'line',
        data:{labels:d.pnl_history.map((_,i)=>i),datasets:[{data:d.pnl_history,borderColor:'#1D9E75',backgroundColor:'rgba(29,158,117,0.08)',borderWidth:1.5,pointRadius:0,fill:true,tension:0.3}]},
        options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{display:false},y:{ticks:{callback:v=>'$'+v.toFixed(0)}}}}
      });
    }else{
      chart.data.labels=d.pnl_history.map((_,i)=>i);
      chart.data.datasets[0].data=d.pnl_history;
      chart.update('none');
    }
  }catch(e){}
}
refresh();
setInterval(refresh,10000);
</script></body></html>"""

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/state")
def api_state():
    return jsonify(state)

@app.route("/api/toggle")
def toggle():
    state["running"] = not state["running"]
    return jsonify({"running": state["running"]})

if __name__ == "__main__":
    threading.Thread(target=trading_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    print(f"✅ 启动成功！访问: http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
